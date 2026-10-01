"""Turns 1-second bars (+ periodic depth snapshots) into a regular per-second grid with causal features and, for
research only, forward outcomes at 1/5/10/30/60/120/300 seconds. Pure (DataFrames in, DataFrame out) - no I/O.

Regularisation: `micro_bars_1s` (and raw ticks aggregated the same way) has one row per second that had activity,
never a dense grid. We reindex to every second between the first and last observed second and forward-fill price/
spread (the last known quote is still the quote), and zero-fill volumes/counts (no trade that second is 0 volume,
not missing). This is what lets "return over the next N seconds" be a fixed row offset.
"""
import numpy as np
import pandas as pd

HORIZONS_S = [1, 5, 10, 30, 60, 120, 300]
FEATURES = ["ret_1s", "ret_5s", "ret_10s", "ret_30s", "ret_60s", "spread_pct", "spread_chg_10s", "spread_z_30s",
           "imbalance_5bp", "imbalance_25bp", "flow_imb_5s", "flow_imb_30s", "vol_accel", "vwap_dist_30s",
           "run_len_s", "z_ret_1s_30s", "trades_per_s_30s"]


def regular_grid(bars: pd.DataFrame) -> pd.DataFrame:
    """One symbol's 1s-bar table (columns from micro_bars_1s) -> a dense per-second frame, ffilled/zero-filled."""
    if bars.empty:
        return bars
    b = bars.sort_values("ts").set_index("ts")
    full = pd.date_range(b.index.min(), b.index.max(), freq="1s", tz="UTC")
    g = b.reindex(full)
    g["mid_close"] = g["mid_close"].ffill()
    for k in ("mid_open", "mid_high", "mid_low"):
        g[k] = g[k].fillna(g["mid_close"])
    g["spread_avg"] = g["spread_avg"].ffill()
    g["spread_max"] = g["spread_max"].fillna(g["spread_avg"])
    for k in ("n_quotes", "n_trades", "buy_volume", "sell_volume"):
        g[k] = g[k].fillna(0.0)
    g["vwap"] = g["vwap"].fillna(g["mid_close"])
    g.index.name = "ts"
    return g.reset_index()


def add_depth(df: pd.DataFrame, depth: pd.DataFrame) -> pd.DataFrame:
    """As-of merge the periodic (every ~2s) depth/imbalance snapshots onto the per-second grid (last snapshot known)."""
    out = df.copy()
    if depth.empty:
        out["imbalance_5bp"] = out["imbalance_25bp"] = np.nan
        return out
    d = depth.sort_values("ts")[["ts", "imbalance_5bp", "imbalance_25bp", "bid_depth_5bp", "ask_depth_5bp"]]
    return pd.merge_asof(out.sort_values("ts"), d, on="ts", direction="backward")


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Causal per-second features. `df` = one symbol's regular grid with depth merged."""
    f = df.copy()
    c = f["mid_close"]
    for n in (1, 5, 10, 30, 60):
        f[f"ret_{n}s"] = (c / c.shift(n) - 1) * 100
    f["spread_pct"] = f["spread_avg"]
    f["spread_chg_10s"] = f["spread_pct"] - f["spread_pct"].shift(10)
    roll_spread = f["spread_pct"].rolling(30)
    f["spread_z_30s"] = (f["spread_pct"] - roll_spread.mean()) / roll_spread.std().replace(0, np.nan)
    buy5, sell5 = f["buy_volume"].rolling(5).sum(), f["sell_volume"].rolling(5).sum()
    buy30, sell30 = f["buy_volume"].rolling(30).sum(), f["sell_volume"].rolling(30).sum()
    f["flow_imb_5s"] = (buy5 - sell5) / (buy5 + sell5).replace(0, np.nan)
    f["flow_imb_30s"] = (buy30 - sell30) / (buy30 + sell30).replace(0, np.nan)
    tr5, tr30 = f["n_trades"].rolling(5).sum(), f["n_trades"].rolling(30).sum()
    f["vol_accel"] = (tr5 / 5) / (tr30 / 30).replace(0, np.nan)
    f["trades_per_s_30s"] = tr30 / 30
    vwap30 = (f["vwap"] * (f["buy_volume"] + f["sell_volume"])).rolling(30).sum() / (f["buy_volume"] + f["sell_volume"]).rolling(30).sum().replace(0, np.nan)
    f["vwap_dist_30s"] = (c / vwap30 - 1) * 100
    up = np.sign(f["ret_1s"].fillna(0))
    grp = (up != up.shift()).cumsum()
    f["run_len_s"] = up * (up.groupby(grp).cumcount() + 1)
    sig30 = f["ret_1s"].rolling(30).std().replace(0, np.nan)
    f["z_ret_1s_30s"] = f["ret_1s"] / sig30
    return f


def add_outcomes(f: pd.DataFrame, horizons: list[int] = HORIZONS_S) -> pd.DataFrame:
    """Forward, NON-causal columns for evaluation only: gross % of a long/short entered at this second's mid, MFE/MAE
    over the path, and the spread change over the horizon. `f` must already be a regular per-second grid."""
    from numpy.lib.stride_tricks import sliding_window_view
    n = len(f)
    hmax = max(horizons)
    m = n - hmax - 1
    if m <= 0:
        return f.iloc[:0]
    out = f.iloc[:m].copy()
    c = f["mid_close"].to_numpy(float)
    sp = f["spread_pct"].to_numpy(float)
    entry = c[:m]
    W = sliding_window_view(c, hmax + 1)[:m]        # row t -> c[t..t+hmax]
    for h in horizons:
        fut = W[:, h]
        out[f"gross_long_{h}s"] = (fut / entry - 1) * 100
        path = W[:, :h + 1]
        out[f"mfe_long_{h}s"] = (path.max(1) / entry - 1) * 100
        out[f"mae_long_{h}s"] = (path.min(1) / entry - 1) * 100
        out[f"spread_chg_fwd_{h}s"] = sp[h:h + m] - sp[:m]
    return out


def bounce_stats(f: pd.DataFrame) -> dict:
    """Dataset-level bid-ask-bounce diagnostics: lag-1 autocorrelation of 1-second returns (negative = bounce/
    reversal dominated) and the share of consecutive nonzero returns that flip sign."""
    r = f["ret_1s"].dropna()
    r = r[r != 0]
    if len(r) < 10:
        return {"n": len(r), "autocorr_lag1": None, "flip_share": None}
    ac = r.autocorr(1)
    flips = (np.sign(r) != np.sign(r.shift(1))).iloc[1:].mean()
    return {"n": len(r), "autocorr_lag1": float(ac), "flip_share": float(flips)}


def build(bars_by_symbol: dict[str, pd.DataFrame], depth_by_symbol: dict[str, pd.DataFrame], with_outcomes: bool = True) -> pd.DataFrame:
    parts = []
    for sym, bars in bars_by_symbol.items():
        g = regular_grid(bars)
        if g.empty:
            continue
        g = add_depth(g, depth_by_symbol.get(sym, pd.DataFrame(columns=["ts", "imbalance_5bp", "imbalance_25bp"])))
        g = compute_features(g)
        if with_outcomes:
            g = add_outcomes(g)
        g["symbol"] = sym
        parts.append(g.dropna(subset=["ret_30s", "z_ret_1s_30s"]))
    return pd.concat(parts).set_index("ts").sort_index(kind="stable") if parts else pd.DataFrame()
