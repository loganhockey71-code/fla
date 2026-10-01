"""The scalper's model: trained on TRADE OUTCOMES, not on direction.

Target = the net % (after fees, slippage, spread) that a stop/target/time-limited trade entered at the next open would
have booked (see labels.py), separately for a long and a short. Two LightGBM regressors (pooled over BTC/ETH/XRP with
coin_id as an input) predict that expected net edge; the objective is squared error on net P&L, so it is optimised for what
we are paid, not for log-loss on up/down. Trading is then gated by an edge THRESHOLD picked on out-of-sample validation
net P&L; if no threshold earns a positive net result the model gets threshold=inf, i.e. it does not trade.

Overlap note: consecutive 1m rows share most of their future, so a naive average over all rows overstates how much evidence
there is. Every P&L estimate here replays signals through `simulate_signals`, which takes one trade at a time per coin.
"""
import numpy as np
import pandas as pd

from . import costs
from .features import FEATURES, compute_features
from .labels import label_trades

PARAMS = dict(objective="regression", n_estimators=250, learning_rate=0.03, num_leaves=15, min_child_samples=300,
              subsample=0.7, subsample_freq=1, colsample_bytree=0.7, reg_lambda=10.0, verbose=-1, n_jobs=2, random_state=7)
LABEL_CLIP = 3.0           # % - one freak print must not dominate a squared-error fit
THRESHOLD_GRID = [0.02, 0.04, 0.06, 0.08, 0.10, 0.13, 0.16, 0.20, 0.25, 0.30, 0.40, 0.50, 0.70, 1.00]


# ---------------------------------------------------------------- dataset
def build_dataset(frames: dict[str, pd.DataFrame], cfg: dict) -> pd.DataFrame:
    """Pooled feature+label rows for every coin, time-sorted. Only rows with known volatility (ATR, vol_ratio)."""
    parts, base = [], {}
    for s, df in frames.items():
        feats = compute_features(frames, s, base)
        lab = label_trades(feats, cfg, spread_pct=costs.ASSUMED_SPREAD_PCT.get(s, 0.0))
        d = feats.loc[lab.index].join(lab)
        d["symbol"] = s
        parts.append(d.dropna(subset=["atr_1m_pct", "vol_ratio", "efficiency_30m", "sigma_30m"]))
    return pd.concat(parts).sort_index(kind="stable") if parts else pd.DataFrame()


def feature_matrix(d: pd.DataFrame) -> pd.DataFrame:
    return d[FEATURES]


# ---------------------------------------------------------------- fit / predict / serialise
def fit_bundle(train: pd.DataFrame) -> dict:
    """{'long': lightgbm text, 'short': lightgbm text}."""
    import lightgbm as lgb
    X = feature_matrix(train)
    out = {}
    for name in ("long", "short"):
        y = train[f"net_{name}"].clip(-LABEL_CLIP, LABEL_CLIP)
        m = lgb.LGBMRegressor(**PARAMS).fit(X, y)
        out[name] = m.booster_.model_to_string()
    return out


_BOOSTERS: dict[int, object] = {}


def _booster(text: str):
    import lightgbm as lgb
    key = hash(text)
    if key not in _BOOSTERS:
        if len(_BOOSTERS) > 8:
            _BOOSTERS.clear()
        _BOOSTERS[key] = lgb.Booster(model_str=text)
    return _BOOSTERS[key]


def predict_edges(bundle: dict, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Predicted NET edge %, (long, short), for each row of X (columns = FEATURES)."""
    cols = X[FEATURES]
    return _booster(bundle["long"]).predict(cols), _booster(bundle["short"]).predict(cols)


# ---------------------------------------------------------------- net-P&L evaluation of a signal rule
def simulate_signals(sym_df: pd.DataFrame, thr: float, allow_shorts: bool = True) -> pd.DataFrame:
    """One coin, time-ordered rows with columns edge_long/edge_short and the outcome labels. Take the better direction when
    its edge >= thr, hold it for its label's duration (no overlapping trades), then look for the next. Returns one row per
    simulated trade: net %, direction, symbol, ts."""
    el, es = sym_df["edge_long"].to_numpy(), sym_df["edge_short"].to_numpy()
    if not allow_shorts:
        es = np.full_like(es, -np.inf)
    best = np.maximum(el, es)
    cand = np.flatnonzero(best >= thr)
    rows, i, k = [], 0, 0
    nl, ns = sym_df["net_long"].to_numpy(), sym_df["net_short"].to_numpy()
    bl, bs = sym_df["bars_long"].to_numpy(), sym_df["bars_short"].to_numpy()
    while k < len(cand):
        i = cand[k]
        long = el[i] >= es[i]
        rows.append((sym_df.index[i], 1 if long else -1, nl[i] if long else ns[i]))
        nxt = i + int(bl[i] if long else bs[i]) + 1
        k = np.searchsorted(cand, nxt)
    return pd.DataFrame(rows, columns=["ts", "direction", "net_pct"])


def evaluate(pred: pd.DataFrame, thr: float, allow_shorts: bool = True) -> dict:
    """Net-P&L summary of the rule 'trade when predicted edge >= thr' over pooled per-coin frames (col `symbol`)."""
    trades = [simulate_signals(g, thr, allow_shorts).assign(symbol=s) for s, g in pred.groupby("symbol")]
    trades = [t for t in trades if len(t)]
    t = pd.concat(trades) if trades else pd.DataFrame(columns=["net_pct"])
    n = len(t)
    if n == 0:
        return {"n": 0, "mean_net_pct": 0.0, "total_net_pct": 0.0, "win_rate": None, "stderr": None, "trades": t}
    x = t["net_pct"].to_numpy()
    return {"n": n, "mean_net_pct": float(x.mean()), "total_net_pct": float(x.sum()), "win_rate": float((x > 0).mean()),
            "stderr": float(x.std(ddof=1) / np.sqrt(n)) if n > 1 else None, "trades": t}


def pick_threshold(pred: pd.DataFrame, cfg: dict, allow_shorts: bool = True) -> dict:
    """Threshold maximising total net % on a validation window, subject to enough trades AND a positive mean whose one-stderr
    lower bound is still > 0 (a lucky handful of trades does not qualify). None found => never trade (thr=inf)."""
    best, table = None, []
    for thr in THRESHOLD_GRID:
        r = evaluate(pred, thr, allow_shorts)
        table.append({"thr": thr, "n": r["n"], "mean_net_pct": r["mean_net_pct"], "total_net_pct": r["total_net_pct"]})
        if r["n"] < cfg["scalp_min_oos_trades"] or r["stderr"] is None:
            continue
        if r["mean_net_pct"] - r["stderr"] <= 0:
            continue
        if best is None or r["total_net_pct"] > best["total_net_pct"]:
            best = {"thr": thr, "n": r["n"], "mean_net_pct": r["mean_net_pct"], "total_net_pct": r["total_net_pct"]}
    return {"thr": best["thr"] if best else float("inf"), "chosen": best, "table": table}


def predict_frame(bundle: dict, d: pd.DataFrame) -> pd.DataFrame:
    el, es = predict_edges(bundle, d)
    p = d.copy()
    p["edge_long"], p["edge_short"] = el, es
    return p


# ---------------------------------------------------------------- champion / challenger
def daily_net(trades: pd.DataFrame) -> pd.Series:
    if trades is None or not len(trades):
        return pd.Series(dtype=float)
    return trades.set_index(pd.DatetimeIndex(trades["ts"]))["net_pct"].groupby(lambda t: t.date()).sum()


def paired_p_value(a: pd.Series, b: pd.Series, n_boot: int = 4000, seed: int = 11) -> float:
    """One-sided p that challenger `a` does NOT beat champion `b` on daily net %, by resampling days (not trades)."""
    days = sorted(set(a.index) | set(b.index))
    if len(days) < 3:
        return 1.0
    diff = np.array([a.get(d, 0.0) - b.get(d, 0.0) for d in days])
    rng = np.random.default_rng(seed)
    boots = rng.choice(diff, size=(n_boot, len(diff)), replace=True).mean(1)
    return float((boots <= 0).mean())


def compare_on_holdout(holdout: pd.DataFrame, champ: dict | None, challenger: dict, cfg: dict) -> dict:
    """Both models score the SAME unseen window, each with its own validated threshold. The challenger is only better if
    it nets more, has enough trades, a positive mean net, and wins a paired day-level bootstrap."""
    allow = cfg["scalp_allow_shorts"]
    ch = evaluate(predict_frame(challenger["bundle"], holdout), challenger["thr"], allow) if np.isfinite(challenger["thr"]) else {"n": 0, "total_net_pct": 0.0, "mean_net_pct": 0.0, "trades": pd.DataFrame()}
    if champ and np.isfinite(champ["thr"]):
        cp = evaluate(predict_frame(champ["bundle"], holdout), champ["thr"], allow)
    else:
        cp = {"n": 0, "total_net_pct": 0.0, "mean_net_pct": 0.0, "trades": pd.DataFrame()}
    p = paired_p_value(daily_net(ch["trades"]), daily_net(cp["trades"]))
    promote = (ch["n"] >= cfg["scalp_min_oos_trades"] and ch["mean_net_pct"] > 0 and
               ch["total_net_pct"] > cp["total_net_pct"] + cfg["scalp_promote_min_gain_pct"] and p <= cfg["scalp_promote_max_p"])
    why = ("challenger nets more out-of-sample" if promote else
           f"not promoted: challenger {ch['n']} trades, net {ch['total_net_pct']:+.2f}% (mean {ch['mean_net_pct']:+.3f}%), "
           f"champion net {cp['total_net_pct']:+.2f}%, p={p:.2f}")
    return {"promote": bool(promote), "p": p, "reason": why,
            "challenger": {k: ch[k] for k in ("n", "total_net_pct", "mean_net_pct")},
            "champion": {k: cp[k] for k in ("n", "total_net_pct", "mean_net_pct")}}


def train_with_threshold(d: pd.DataFrame, cfg: dict, val_days: float) -> dict:
    """Train on everything before the last `val_days`, pick the threshold on that validation tail (unseen by the fit)."""
    cut = d.index.max() - pd.Timedelta(days=val_days)
    embargo = pd.Timedelta(minutes=int(cfg["scalp_hold_bars"]) + 2)          # no training label may reach into validation
    train, val = d[d.index < cut - embargo], d[d.index >= cut]
    bundle = fit_bundle(train)
    picked = pick_threshold(predict_frame(bundle, val), cfg, cfg["scalp_allow_shorts"])
    return {"bundle": bundle, "thr": picked["thr"], "picked": picked, "train_rows": len(train), "val_rows": len(val),
            "train_end": str(train.index.max()), "val_start": str(val.index.min())}
