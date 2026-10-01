"""Simple, non-ML microstructure strategies to test BEFORE any model - each is a pure (long_mask, short_mask) rule on
the per-second feature grid from micro_features.py. Mirrors crypto_ai.scalp.research.setups() (candle-scale)."""
import numpy as np


def _sig(long_, short_):
    return np.asarray(long_, bool), np.asarray(short_, bool)


def setups() -> dict:
    S = {}
    for bp, k in (("5bp", 0.3), ("5bp", 0.6), ("25bp", 0.3), ("25bp", 0.6)):
        col = f"imbalance_{bp}"
        S[f"orderbook_imbalance {bp}>={k}"] = lambda d, col=col, k=k: _sig(d[col] >= k, d[col] <= -k)
    for w, k in (("5s", 0.5), ("5s", 0.8), ("30s", 0.5), ("30s", 0.8)):
        col = f"flow_imb_{w}"
        S[f"trade_flow_imbalance {w}>={k}"] = lambda d, col=col, k=k: _sig(d[col] >= k, d[col] <= -k)
    S["spread_compression_follow (tight spread + momentum)"] = lambda d: _sig((d.spread_z_30s <= -1.0) & (d.ret_5s > 0), (d.spread_z_30s <= -1.0) & (d.ret_5s < 0))
    S["spread_expansion_fade (wide spread -> reversion)"] = lambda d: _sig((d.spread_z_30s >= 1.5) & (d.ret_5s < 0), (d.spread_z_30s >= 1.5) & (d.ret_5s > 0))
    for k in (2.0, 3.0):
        S[f"liquidity_sweep_fade |z1s|>={k} + vol_accel"] = lambda d, k=k: _sig((d.z_ret_1s_30s <= -k) & (d.vol_accel >= 1.5), (d.z_ret_1s_30s >= k) & (d.vol_accel >= 1.5))
        S[f"liquidity_sweep_follow |z1s|>={k} + vol_accel"] = lambda d, k=k: _sig((d.z_ret_1s_30s >= k) & (d.vol_accel >= 1.5), (d.z_ret_1s_30s <= -k) & (d.vol_accel >= 1.5))
    for w in ("ret_10s", "ret_30s"):
        S[f"reversal {w}<=-0.05%"] = lambda d, w=w: _sig(d[w] <= -0.05, d[w] >= 0.05)
        S[f"momentum {w}>=0.05%"] = lambda d, w=w: _sig(d[w] >= 0.05, d[w] <= -0.05)
    S["vwap_reversion (30s)"] = lambda d: _sig(d.vwap_dist_30s <= -0.03, d.vwap_dist_30s >= 0.03)
    S["vwap_trend (30s)"] = lambda d: _sig(d.vwap_dist_30s >= 0.03, d.vwap_dist_30s <= -0.03)
    S["volume_accel_follow"] = lambda d: _sig((d.vol_accel >= 2.0) & (d.ret_5s > 0), (d.vol_accel >= 2.0) & (d.ret_5s < 0))
    S["volume_accel_fade"] = lambda d: _sig((d.vol_accel >= 2.0) & (d.ret_5s < 0), (d.vol_accel >= 2.0) & (d.ret_5s > 0))
    return S
