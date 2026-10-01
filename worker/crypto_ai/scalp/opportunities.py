"""The trade-opportunity dataset: for EVERY 1-minute bar and BOTH directions, what happened over the next 1-15 minutes if a trade
had been entered at the next candle's open?  Not "was price higher later" but the full path: MFE and MAE at 1/2/3/5/10/15 minutes,
gross and net (after fees + slippage + spread) return of a fixed-horizon exit, whether a take-profit was reached before a stop,
the best possible target and the worst adverse move, plus every feature and the regime at the decision bar.

Causality: features come from candles closed at the decision bar (see features.py); outcomes only use bars AFTER it.
"""
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import costs
from .features import FEATURES, compute_features, regime_of

HORIZONS = [1, 2, 3, 5, 10, 15]
HMAX = max(HORIZONS)
BARRIERS = [(0.2, 0.4), (0.3, 0.6), (0.5, 1.0), (1.0, 1.5)]      # (stop %, target %) pairs recorded as "target before stop" flags
BARRIER_H = 5

EXTRA = ["ema9_21", "ema21_50", "dist_ema9", "run_len", "z1", "z3", "z5", "vol_accel", "brk_hi20", "brk_lo20", "abs_ret_1m"]


def extra_features(df: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    """Research-only features (not used by the live model): EMA structure, consecutive candles, move size in ATR units,
    volume acceleration, 20-bar breaks. All causal."""
    c, o, h, l, v = df["close"], df["open"], df["high"], df["low"], df["volume"]
    e9, e21, e50 = (c.ewm(span=n, adjust=False).mean() for n in (9, 21, 50))
    x = pd.DataFrame(index=df.index)
    x["ema9_21"] = (e9 / e21 - 1) * 100
    x["ema21_50"] = (e21 / e50 - 1) * 100
    x["dist_ema9"] = (c / e9 - 1) * 100
    up = (c > o).astype(int) - (c < o).astype(int)
    grp = (up != up.shift()).cumsum()
    x["run_len"] = up * (up.groupby(grp).cumcount() + 1)
    atr = f["atr_1m_pct"]
    x["z1"] = f["ret_1m"] / atr
    x["z3"] = f["ret_3m"] / (atr * np.sqrt(3))
    x["z5"] = f["ret_5m"] / (atr * np.sqrt(5))
    x["vol_accel"] = v.rolling(3).mean() / v.rolling(15).mean().replace(0, np.nan)
    x["brk_hi20"] = (c >= h.rolling(20).max().shift(1)).astype(float)
    x["brk_lo20"] = (c <= l.rolling(20).min().shift(1)).astype(float)
    x["abs_ret_1m"] = f["ret_1m"].abs()
    return x


def _dir_block(d: int, o1, W_h, W_l, W_c, cfg, spread) -> dict:
    """Outcome columns for one direction. o1 = entry mid (next open); W_* = (m, HMAX) windows of the bars after the decision."""
    out = {}
    ef = costs.fill(o1, d, True, spread, cfg["slippage_pct"])
    fav = np.maximum.accumulate(W_h if d > 0 else -W_l, axis=1)           # running best price in our favour
    adv = np.minimum.accumulate(W_l if d > 0 else -W_h, axis=1)           # running worst
    for h in HORIZONS:
        i = h - 1
        if d > 0:
            mfe = (fav[:, i] / o1 - 1) * 100
            mae = (adv[:, i] / o1 - 1) * 100
        else:
            mfe = (1 - (-fav[:, i]) / o1) * 100
            mae = (1 - (-adv[:, i]) / o1) * 100
        xf = costs.fill(W_c[:, i], d, False, spread, cfg["slippage_pct"])
        out[f"mfe_{h}"], out[f"mae_{h}"] = np.maximum(mfe, 0), np.minimum(mae, 0)
        out[f"gross_{h}"] = d * (W_c[:, i] / o1 - 1) * 100
        out[f"net_{h}"] = costs.net_pct(ef, xf, d, cfg["trading_fee_pct"])
    Hb = BARRIER_H
    for sl, tp in BARRIERS:
        if d > 0:
            hit_sl = W_l[:, :Hb] <= (o1 * (1 - sl / 100))[:, None]
            hit_tp = W_h[:, :Hb] >= (o1 * (1 + tp / 100))[:, None]
        else:
            hit_sl = W_h[:, :Hb] >= (o1 * (1 + sl / 100))[:, None]
            hit_tp = W_l[:, :Hb] <= (o1 * (1 - tp / 100))[:, None]
        i_sl = np.where(hit_sl.any(1), hit_sl.argmax(1), 99)
        i_tp = np.where(hit_tp.any(1), hit_tp.argmax(1), 99)
        out[f"tp_first_sl{sl}_tp{tp}"] = ((i_tp < i_sl)).astype(np.int8)     # a same-candle touch counts as the stop (conservative)
    out["best_tp"] = out["mfe_15"]
    out["worst_adverse"] = out["mae_15"]
    out["outcome_net"] = out["net_15"]
    return out


def build_opportunities(frames: dict[str, pd.DataFrame], cfg: dict) -> pd.DataFrame:
    """One row per (bar, coin) with long_* and short_* outcome columns, all features and the regime. Sorted by time."""
    parts, base = [], {}
    for sym, df in frames.items():
        f = compute_features(frames, sym, base)
        x = extra_features(df, f)
        n = len(f)
        m = n - HMAX - 1
        if m <= 0:
            continue
        o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        o1 = o[1:1 + m]
        W_h, W_l, W_c = (sliding_window_view(a, HMAX)[1:1 + m] for a in (h, l, c))
        spread = costs.ASSUMED_SPREAD_PCT.get(sym, 0.0)
        cols = {}
        for name, d in (("long", 1), ("short", -1)):
            for k, v in _dir_block(d, o1, W_h, W_l, W_c, cfg, spread).items():
                cols[f"{name}_{k}"] = v
        out = pd.concat([f.iloc[:m][FEATURES + ["open", "close", "volume"]].copy(), x.iloc[:m]], axis=1)
        out["entry_price"] = o1
        out["symbol"] = sym
        out["regime"] = [regime_of(a, b) for a, b in zip(f["vol_ratio"].iloc[:m], f["efficiency_30m"].iloc[:m])]
        out = pd.concat([out, pd.DataFrame(cols, index=out.index)], axis=1)
        parts.append(out.dropna(subset=["atr_1m_pct", "vol_ratio", "efficiency_30m"]))
    return pd.concat(parts).sort_index(kind="stable")
