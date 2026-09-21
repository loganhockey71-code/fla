"""Post-mortems for WRONG predictions: what happened before the move, which signals were missed or mis-weighted,
and what similar past situations looked like.

Nothing in this module changes a model. It only writes rows to `post_mortems` (append-only). One mistake is
just one example; patterns are judged over many examples in patterns.py, and models are only ever replaced by
retrain.py after a held-out comparison.
"""
import json
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from ..config import BAR
from ..features import MODEL_FEATURES, compute_features, make_labels

H = pd.Timedelta(hours=1)
BAND_SIGNAL = {"BUY": 1, "SELL": -1, "HOLD": 0}
IMPACT_COL = {"BTC": "btc_impact_score", "ETH": "eth_impact_score", "XRP": "xrp_impact_score"}
KEY_MACRO = ("CPIAUCSL", "CPILFESL", "UNRATE", "PAYEMS")

# tag -> (priority for the primary explanation, description)
TAGS = {
    "event_shock_missed": (0, "an event/announcement after the call pointed the way the price then moved"),
    "macro_release_in_window": (1, "a major macro release (CPI, jobs, unemployment) landed inside the window"),
    "market_led_move": (2, "the wider crypto market moved together with this coin"),
    "volume_shock": (3, "a volume spike coincided with the biggest move"),
    "volatility_regime_shift": (4, "realised volatility jumped to at least twice its level at the time of the call"),
    "event_misread": (5, "a positive/negative event existed before the call, yet price went the other way"),
    "analogs_disagreed": (6, "the most similar past situations mostly went the other way from the model's call"),
    "misleading_drivers": (7, "the top model drivers pushed toward the wrong side"),
    "prediction_market_shift": (8, "a prediction market repriced sharply in the direction of the outcome"),
    "hold_missed_move": (9, "the model stayed HOLD but price moved beyond the hold band"),
    "unexplained": (10, "no measurable cause in the data we collect"),
}
EXTRA_TAGS = {"low_conviction": "the call was close to a coin flip, so a miss like this is expected about half the time",
              "leading_signal_available": "something visible before the move started (BTC, volume or news) pointed the right way"}


def _f(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


# ---------------------------------------------------------------- pure measurements
def path_stats(df: pd.DataFrame, price0: float, t0, t1) -> dict:
    seg = df[(df.index >= t0) & (df.index < t1)]
    if seg.empty:
        return {}
    ctx = df[(df.index >= t0 - H) & (df.index < t1)]["close"]
    r4 = ctx.pct_change(4).dropna()
    big = {}
    if len(r4):
        i = r4.abs().idxmax()
        start = i - H                                           # pct_change(4) ends at bar i; the hour started 3 bars earlier
        big = {"return": _f(r4.loc[i]), "start": start.isoformat(), "offset_h": _f((start - t0) / H)}
    return {"final_return": _f(seg["close"].iloc[-1] / price0 - 1), "runup": _f(seg["high"].max() / price0 - 1),
            "drawdown": _f(seg["low"].min() / price0 - 1), "biggest_hour": big}


def volume_stats(df: pd.DataFrame, t0, t1) -> dict:
    base = df[(df.index >= t0 - 24 * H) & (df.index < t0)]["volume"].mean()
    win = df[(df.index >= t0) & (df.index < t1)]["volume"]
    if not len(win) or not base or base <= 0:
        return {}
    r4 = win.rolling(4).sum().dropna() / (4 * base)
    if r4.empty:
        return {}
    i = r4.idxmax()
    return {"max_spike": _f(r4.max()), "at": (i - 3 * pd.Timedelta(seconds=BAR)).isoformat()}


def vol_ratio(df: pd.DataFrame, t0, t1) -> float | None:
    lr = np.log(df["close"]).diff()
    before = lr[(lr.index >= t0 - 24 * H) & (lr.index < t0)].std()
    during = lr[(lr.index >= t0) & (lr.index < t1)].std()
    return _f(during / before) if before and before > 0 else None


def market_context(frames: dict, symbol: str, t0, t1, own_return: float) -> dict:
    others = {}
    for s, df in frames.items():
        if s == symbol or df.empty:
            continue
        seg = df[(df.index >= t0) & (df.index < t1)]["close"]
        prev = df[df.index < t0]["close"]
        if len(seg) and len(prev):
            others[s] = _f(seg.iloc[-1] / prev.iloc[-1] - 1)
    same = [r for r in others.values() if r is not None and r * own_return > 0 and abs(r) >= 0.5 * abs(own_return)]
    led = len(others) > 0 and (len(same) == len(others) if symbol == "BTC" else "BTC" in others and others["BTC"] is not None and others["BTC"] * own_return > 0 and abs(others["BTC"]) >= 0.6 * abs(own_return))
    return {"others_return": others, "market_led": bool(led)}


def leading_signals(frames: dict, symbol: str, move_start, direction: int, ev: pd.DataFrame, pm: pd.DataFrame) -> list[dict]:
    """What was visible in the 6 hours BEFORE the big move began (possibly after the prediction was made)."""
    out = []
    a, b = move_start - 6 * H, move_start
    for s, df in frames.items():
        if s == symbol or df.empty:
            continue
        seg = df[(df.index >= a) & (df.index < b)]["close"]
        prev = df[df.index < a]["close"]
        if len(seg) and len(prev):
            r = seg.iloc[-1] / prev.iloc[-1] - 1
            if r * direction > 0 and abs(r) >= 0.005:
                out.append({"kind": "other_coin_already_moving", "coin": s, "return": _f(r)})
    df = frames.get(symbol)
    if df is not None and not df.empty:
        base = df[(df.index >= a - 24 * H) & (df.index < a)]["volume"].mean()
        pre = df[(df.index >= b - H) & (df.index < b)]["volume"].sum() / 4
        if base and pre / base >= 2:
            out.append({"kind": "volume_building", "ratio": _f(pre / base)})
    if len(ev):
        col = IMPACT_COL[symbol]
        for _, r in ev[(ev["detected_at"] >= a) & (ev["detected_at"] < b)].iterrows():
            if symbol in r["affected_coins"] and r[col] * direction > 0 and abs(r[col]) >= 30:
                out.append({"kind": "news_preceded", "title": r["title"][:100], "impact": int(r[col])})
    if len(pm):
        w = pm[(pm["captured_at"] >= a) & (pm["captured_at"] < b) & pm["coins"].apply(lambda c: symbol in c)]
        for _, g in w.groupby(["platform", "market_id"]):
            if len(g) >= 2:
                mv = g["yes_prob"].iloc[-1] - g["yes_prob"].iloc[0]
                if abs(mv) >= 0.08 and mv * g["direction"].iloc[-1] * direction > 0:
                    out.append({"kind": "prediction_market_moved", "title": g["title"].iloc[-1][:100], "move": _f(mv)})
    return out


def events_review(ev: pd.DataFrame, symbol: str, created, target, outcome_dir: int, pred_dir: int) -> dict:
    if not len(ev):
        return {"missed": [], "misread": [], "all": 0}
    col = IMPACT_COL[symbol]
    w = ev[(ev["detected_at"] >= created - 6 * H) & (ev["detected_at"] <= target)]
    w = w[w["affected_coins"].apply(lambda c: symbol in c)]
    missed, misread = [], []
    for _, r in w.iterrows():
        imp = int(r[col])
        item = {"title": r["title"][:110], "impact": imp, "importance": int(r["importance_score"]), "detected_at": r["detected_at"].isoformat(), "tier": int(r["origin_tier"] or 4)}
        after = r["detected_at"] > created
        if after and imp * outcome_dir > 0 and abs(imp) >= 30:
            missed.append(item)
        elif not after and pred_dir != 0 and imp * pred_dir > 0 and abs(imp) >= 30:
            misread.append(item)                                   # event agreed with our call, price still went the other way
    return {"missed": missed[:5], "misread": misread[:5], "all": int(len(w))}


def macro_in_window(macro: pd.DataFrame, created, target) -> list[dict]:
    if not len(macro):
        return []
    w = macro[(macro["available_at"] >= created) & (macro["available_at"] <= target) & macro["series_id"].isin(KEY_MACRO)]
    return [{"series": r["series_id"], "released": r["available_at"].isoformat(), "value": _f(r["value"])} for _, r in w.iterrows()]


def pm_shifts(pm: pd.DataFrame, symbol: str, created, target, outcome_dir: int) -> list[dict]:
    if not len(pm):
        return []
    w = pm[(pm["captured_at"] >= created - 6 * H) & (pm["captured_at"] <= target) & pm["coins"].apply(lambda c: symbol in c)]
    out = []
    for _, g in w.groupby(["platform", "market_id"]):
        if len(g) >= 2:
            mv = float(g["yes_prob"].iloc[-1] - g["yes_prob"].iloc[0])
            if abs(mv) >= 0.08 and mv * float(g["direction"].iloc[-1]) * outcome_dir > 0:
                out.append({"platform": g["platform"].iloc[-1], "title": g["title"].iloc[-1][:100], "move": _f(mv)})
    return out


def driver_review(pred: dict) -> list[dict]:
    """Drivers that pushed TOWARD the side that turned out wrong."""
    want = {"BUY": "bullish", "SELL": "bearish"}.get(pred["signal"])
    drivers = ((pred.get("features") or {}).get("drivers")) or []
    return [{"feature": d["feature"], "text": d["text"], "pushes": d["pushes"]} for d in drivers if want and d.get("pushes") == want]


# ---------------------------------------------------------------- similar past situations (k-nearest neighbours)
class Pool:
    """Standardised feature history for one coin and horizon, with known outcomes."""

    def __init__(self, frames: dict, symbol: str, horizon_h: int):
        f = compute_features(frames, symbol)
        lab = make_labels(f, horizon_h)
        d = f[MODEL_FEATURES].join(lab)
        d = d[d.index.minute == 0].dropna(subset=["fwd_ret"])
        self.h = horizon_h
        self.med = d[MODEL_FEATURES].median()
        self.iqr = (d[MODEL_FEATURES].quantile(0.75) - d[MODEL_FEATURES].quantile(0.25)).replace(0, np.nan)
        self.z = ((d[MODEL_FEATURES] - self.med) / self.iqr).clip(-5, 5)
        self.ret = d["fwd_ret"]

    def similar(self, x: dict, created, k: int = 25, min_pool: int = 150) -> dict | None:
        """Nearest past situations whose outcome was already known at `created` (no look-ahead)."""
        ok = (self.z.index + pd.Timedelta(hours=self.h)) <= created
        z, ret = self.z[ok], self.ret[ok]
        if len(z) < min_pool:
            return None
        vec = ((pd.Series({c: x.get(c) for c in MODEL_FEATURES}, dtype=float) - self.med) / self.iqr).clip(-5, 5)
        diff = (z - vec) ** 2
        dist = np.sqrt(diff.mean(axis=1, skipna=True))
        near = dist.nsmallest(k).index
        r = ret.loc[near]
        return {"k": int(len(r)), "up_rate": _f((r > 0).mean()), "mean_return": _f(r.mean()), "median_return": _f(r.median()),
                "mean_abs_return": _f(r.abs().mean()), "closest": [{"when": t.isoformat(), "distance": _f(dist.loc[t]), "return": _f(ret.loc[t])} for t in near[:5]]}


# ---------------------------------------------------------------- putting it together
def classify(pred: dict, f: dict, band_pct: float) -> tuple[str, list[str]]:
    tags, sig = [], pred["signal"]
    if f["events"]["missed"]:
        tags.append("event_shock_missed")
    if f["macro"]:
        tags.append("macro_release_in_window")
    if f["market"].get("market_led"):
        tags.append("market_led_move")
    v = f.get("volume") or {}
    big = (f["path"].get("biggest_hour") or {})
    if v.get("max_spike") and v["max_spike"] >= 3 and abs(big.get("return") or 0) >= 0.01:
        tags.append("volume_shock")
    if (f.get("vol_ratio") or 0) >= 2:
        tags.append("volatility_regime_shift")
    if f["events"]["misread"]:
        tags.append("event_misread")
    nb = f.get("similar")
    if nb and sig in ("BUY", "SELL"):
        went_our_way = nb["up_rate"] if sig == "BUY" else 1 - nb["up_rate"]
        if went_our_way < 0.45:
            tags.append("analogs_disagreed")
    if f["drivers_wrong"]:
        tags.append("misleading_drivers")
    if f["pm"]:
        tags.append("prediction_market_shift")
    if sig == "HOLD" and abs(f["actual_return_pct"]) > band_pct:
        tags.append("hold_missed_move")
    if not [t for t in tags if t != "hold_missed_move"] and sig != "HOLD":
        tags.append("unexplained")
    if not tags:
        tags.append("unexplained")
    primary = min(tags, key=lambda t: TAGS[t][0])
    extra = []
    if abs(pred["bullish_prob"] - 0.5) < 0.05:
        extra.append("low_conviction")
    if f["leading"]:
        extra.append("leading_signal_available")
    return primary, tags + extra


def analyze(pred: dict, frames: dict, ev: pd.DataFrame, pm: pd.DataFrame, macro: pd.DataFrame, pool: Pool | None, band_pct: float) -> dict:
    sym, created, target = pred["symbol"], pd.Timestamp(pred["created_at"]), pd.Timestamp(pred["target_time"])
    df = frames[sym]
    ret = pred["actual_return_pct"] / 100
    outcome_dir = 1 if ret > 0 else -1
    pred_dir = BAND_SIGNAL[pred["signal"]]
    path = path_stats(df, pred["price_at_prediction"], created, target)
    big = path.get("biggest_hour") or {}
    move_start = pd.Timestamp(big["start"]) if big.get("start") else created
    f = {
        "actual_return_pct": pred["actual_return_pct"], "path": path, "volume": volume_stats(df, created, target),
        "vol_ratio": vol_ratio(df, created, target), "market": market_context(frames, sym, created, target, ret),
        "leading": leading_signals(frames, sym, move_start, 1 if (big.get("return") or ret) > 0 else -1, ev, pm),
        "events": events_review(ev, sym, created, target, outcome_dir, pred_dir), "macro": macro_in_window(macro, created, target),
        "pm": pm_shifts(pm, sym, created, target, outcome_dir), "drivers_wrong": driver_review(pred),
        "similar": pool.similar(((pred.get("features") or {}).get("model_inputs")) or {}, created) if pool else None,
    }
    primary, tags = classify(pred, f, band_pct)
    f["summary_facts"] = {"model_said": f"{pred['signal']} at {pred['bullish_prob']:.0%} up", "outcome": f"{pred['actual_return_pct']:+.2f}%"}
    return {"error_class": primary, "tags": tags, "findings": f, "summary": summarize(pred, f, primary, tags)}


def summarize(pred: dict, f: dict, primary: str, tags: list[str]) -> str:
    big = (f["path"].get("biggest_hour") or {})
    parts = [f"{pred['symbol']} {pred['horizon_h']}h {pred['signal']} ({pred['bullish_prob']:.0%} up) was wrong: price moved {pred['actual_return_pct']:+.2f}%."]
    if big.get("return") is not None:
        parts.append(f"The biggest hour was {big['return'] * 100:+.2f}%, {big['offset_h']:.0f} h after the call.")
    parts.append(f"Main explanation: {TAGS[primary][1]}.")
    if f["events"]["missed"]:
        parts.append(f"Missed event: \"{f['events']['missed'][0]['title']}\" (impact {f['events']['missed'][0]['impact']:+d}).")
    if f["leading"]:
        parts.append("Visible before the move: " + "; ".join(x["kind"].replace("_", " ") for x in f["leading"][:3]) + ".")
    if f.get("similar"):
        parts.append(f"In the {f['similar']['k']} most similar past situations price rose {f['similar']['up_rate']:.0%} of the time.")
    if "low_conviction" in tags:
        parts.append("The call was near 50/50, so an error like this is expected about half the time.")
    return " ".join(parts)


# ---------------------------------------------------------------- database job
def load_context(db):
    ev = pd.DataFrame(db.all("""select detected_at, affected_coins, btc_impact_score, eth_impact_score, xrp_impact_score, importance_score, origin_tier, title
                                from research_events order by detected_at"""))
    pm = pd.DataFrame(db.all("select platform, market_id, title, coins, direction, yes_prob, captured_at from prediction_market_snapshots order by captured_at"))
    macro = pd.DataFrame(db.all("select series_id, value, available_at from macro_data where series_id = any(%s)", [list(KEY_MACRO)]))
    for d, c in ((ev, "detected_at"), (pm, "captured_at"), (macro, "available_at")):
        if len(d):
            d[c] = pd.to_datetime(d[c], utc=True)
    return ev, pm, macro


def run_postmortems(db, cfg: dict, frames: dict | None = None, limit: int = 300) -> int:
    """Write a post-mortem for every scored-wrong prediction that doesn't have one. Never touches models."""
    rows = db.all("""select p.*, r.actual_price, r.actual_return_pct from predictions p join prediction_results r on r.prediction_id = p.id
                     left join post_mortems m on m.prediction_id = p.id where r.signal_correct = false and m.id is null order by p.created_at limit %s""", [limit])
    if not rows:
        return 0
    if frames is None:
        from .data import load_frames
        frames = load_frames(db, days=400)
    ev, pm, macro = load_context(db)
    pools: dict = {}
    n = 0
    for p in rows:
        key = (p["symbol"], p["horizon_h"])
        if key not in pools:
            try:
                pools[key] = Pool(frames, p["symbol"], p["horizon_h"])
            except Exception:
                pools[key] = None
        try:
            res = analyze(p, frames, ev, pm, macro, pools[key], cfg[f"hold_band_pct_{p['horizon_h']}h"])
        except Exception as e:                                       # one odd row must not block the rest
            print(f"[learn] post-mortem {p['id']} skipped: {type(e).__name__}")
            continue
        db.run("""insert into post_mortems (prediction_id, symbol, horizon_h, variant, signal, actual_return_pct, error_class, tags, findings, summary)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (prediction_id) do nothing""",
               [p["id"], p["symbol"], p["horizon_h"], p["variant"], p["signal"], p["actual_return_pct"], res["error_class"],
                json.dumps(res["tags"]), json.dumps(res["findings"], default=str), res["summary"]])
        n += 1
    return n
