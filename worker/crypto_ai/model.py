"""LightGBM models: one per coin, one booster per horizon (24h / 48h).

Training is walk-forward with an embargo gap so no training label overlaps the test window.
The out-of-sample results are stored as BACKTEST metrics on the model version and are never
mixed with live paper-trading results.
"""
import math
from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from .config import HORIZONS
from .features import MODEL_FEATURES, compute_features, describe, make_labels

PARAMS = dict(
    objective="binary", n_estimators=200, learning_rate=0.03, num_leaves=8, min_child_samples=100,
    subsample=0.7, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0, verbose=-1, n_jobs=2,
    random_state=7,
)


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _sigmoid(z):
    return 1 / (1 + np.exp(-z))


def apply_calibration(p_raw: float, cal: dict) -> float:
    return float(_sigmoid(cal["a"] * (_logit(p_raw) - cal["center"])))


def _dataset(feats: pd.DataFrame, horizon_h: int, cols: list[str]) -> pd.DataFrame:
    lab = make_labels(feats, horizon_h)
    d = feats[cols].join(lab)
    d["close"] = feats["close"]
    d = d[d.index.minute == 0]              # hourly samples: fewer near-duplicate rows
    d = d.dropna(subset=["y", "ret_24h", "corr_btc_24h", "volatility_24h", "macd_hist_1h"])
    return d


def _fit(X, y):
    return lgb.LGBMClassifier(**PARAMS).fit(X, y)


def walk_forward(d: pd.DataFrame, horizon_h: int, cols: list[str], folds: int = 5) -> pd.DataFrame:
    n = len(d)
    start = n // 2
    block = (n - start) // folds
    out = []
    for k in range(folds):
        t0 = start + k * block
        t1 = n if k == folds - 1 else t0 + block
        train = d.iloc[: max(t0 - horizon_h, 0)]      # embargo: drop rows whose label reaches into the test block
        test = d.iloc[t0:t1]
        if len(train) < 500 or train["y"].nunique() < 2 or test.empty:
            continue
        m = _fit(train[cols], train["y"])
        out.append(pd.DataFrame({"p_raw": m.predict_proba(test[cols])[:, 1], "y": test["y"],
                                 "fwd_ret": test["fwd_ret"], "fold": k}, index=test.index))
    return pd.concat(out) if out else pd.DataFrame(columns=["p_raw", "y", "fwd_ret", "fold"])


def fold_auc(oos: pd.DataFrame) -> list[float]:
    """AUC inside each walk-forward fold. Pooling folds can fake skill when the base rate drifts between them."""
    return [float(roc_auc_score(g["y"], g["p_raw"])) for _, g in oos.groupby("fold") if g["y"].nunique() == 2]


def fit_calibration(oos: pd.DataFrame) -> dict:
    """p = sigmoid(a * (logit(p_raw) - center)).  `center` is the model's typical out-of-sample opinion, so a
    normal day reads 50/50 and BUY/SELL needs a real deviation from it (a bull-market base rate alone is not
    skill). a >= 0: calibration may shrink or stretch the model but never flip it. No skill => a = 0 => 50/50 => HOLD."""
    z = _logit(oos["p_raw"].values)
    center = float(z.mean())
    lr = LogisticRegression(C=1e6, fit_intercept=False).fit((z - center).reshape(-1, 1), oos["y"])
    a = float(lr.coef_[0][0])
    if a <= 0:
        return {"a": 0.0, "center": 0.0, "no_skill": True}
    return {"a": a, "center": center, "no_skill": False}


def simulate(oos: pd.DataFrame, horizon_h: int, cfg: dict) -> dict:
    """Backtest of the long-only paper strategy on out-of-sample calibrated probabilities."""
    thr = cfg["signal_threshold_pct"] / 100
    cost = 2 * (cfg["trading_fee_pct"] + cfg["slippage_pct"]) / 100
    size = cfg["normal_position_pct"] / 100
    equity, busy_until, rets = 1.0, None, []
    for ts, r in oos.iterrows():
        if busy_until is not None and ts < busy_until:
            continue
        if r["p"] >= thr:
            net = r["fwd_ret"] - cost
            rets.append(net)
            equity *= 1 + size * net
            busy_until = ts + pd.Timedelta(hours=horizon_h)
    return {
        "trades": len(rets),
        "win_rate": float(np.mean([x > 0 for x in rets])) if rets else None,
        "avg_net_return_pct": float(np.mean(rets) * 100) if rets else None,
        "compounded_return_pct": float((equity - 1) * 100),
    }


def train_symbol(symbol: str, frames: dict[str, pd.DataFrame], cfg: dict, variant: str = "market", research: dict | None = None) -> dict:
    """variant 'market' = candle features only. 'research' = the same plus point-in-time macro + event features."""
    feats = compute_features(frames, symbol)
    cols = list(MODEL_FEATURES)
    if variant == "research":
        from .research.features import RESEARCH_FEATURES, research_frame
        feats = feats.join(research_frame(research, symbol, feats.index))
        cols += RESEARCH_FEATURES
    blobs, cals, metrics = {}, {}, {}
    n_samples = 0
    for h in HORIZONS:
        d = _dataset(feats, h, cols)
        if len(d) < 1500:
            raise RuntimeError(f"{symbol} {h}h: only {len(d)} usable samples - fetch more history")
        oos = walk_forward(d, h, cols)
        cal = fit_calibration(oos)
        oos["p"] = _sigmoid(cal["a"] * (_logit(oos["p_raw"]) - cal["center"]))
        pred_up = oos["p"] > 0.5
        base_up = float(oos["y"].mean())
        m = {
            "oos_samples": int(len(oos)),
            "oos_start": str(oos.index[0]), "oos_end": str(oos.index[-1]),
            "directional_accuracy": float((pred_up == (oos["y"] == 1)).mean()),
            "always_up_accuracy": max(base_up, 1 - base_up),
            "auc": float(np.mean(fold_auc(oos))),
            "auc_by_fold": fold_auc(oos),
            "no_skill": cal["no_skill"],
            "brier": float(brier_score_loss(oos["y"], oos["p"])),
            "share_p_above_threshold": float((oos["p"] >= cfg["signal_threshold_pct"] / 100).mean()),
            "buy_and_hold_return_pct": float(
                (feats["close"].loc[oos.index[-1]] / feats["close"].loc[oos.index[0]] - 1) * 100),
            "strategy": simulate(oos, h, cfg),
        }
        metrics[str(h)] = m
        cals[str(h)] = cal
        final = _fit(d[cols], d["y"])
        blobs[str(h)] = final.booster_.model_to_string()
        n_samples = max(n_samples, len(d))
    now = datetime.now(timezone.utc)
    metrics["note"] = ("BACKTEST: walk-forward out-of-sample on historical candles. Not live results. "
                       "Costs use the fee/slippage settings at training time; spread not modelled.")
    return {
        "symbol": symbol, "variant": variant,
        "version": f"lgbm-{symbol}-{now:%Y%m%dT%H%MZ}" if variant == "market" else f"lgbm-{symbol}-{variant}-{now:%Y%m%dT%H%MZ}", "trained_at": now,
        "train_start": feats.index[0].to_pydatetime(), "train_end": feats.index[-1].to_pydatetime(),
        "n_samples": n_samples, "feature_names": cols, "model_blob": blobs,
        "calibration": cals, "backtest_metrics": metrics,
    }


def predict_probability(model_row: dict, horizon_h: int, feature_row: pd.Series) -> tuple[float, list[dict]]:
    """Returns (calibrated P(up), top-3 driver dicts). feature_row must contain only data known now."""
    names = model_row["feature_names"]
    booster = lgb.Booster(model_str=model_row["model_blob"][str(horizon_h)])
    x = feature_row[names].astype(float).values.reshape(1, -1)
    p_raw = float(booster.predict(x)[0])
    p = apply_calibration(p_raw, model_row["calibration"][str(horizon_h)])
    contrib = booster.predict(x, pred_contrib=True)[0][:-1]
    top = np.argsort(-np.abs(contrib))[:3]
    drivers = [{"feature": names[i], "value": float(x[0][i]) if np.isfinite(x[0][i]) else None,
                "pushes": "bullish" if contrib[i] > 0 else "bearish", "text": describe(names[i], float(x[0][i]))}
               for i in top]
    return p, drivers
