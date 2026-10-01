"""Trade-outcome labels: for every 1m bar and each direction, what would a stop/target/time-limited trade entered at the
NEXT candle's open have netted after fees, slippage and spread? This (not 'is price higher in 1h') is what the model learns.

Barriers are sized from the volatility known at the decision bar (unit = ATR% x sqrt(hold bars)), exactly like live entries.
Intrabar rule (shared with the engine): if one candle touches both stop and target the STOP is assumed to have hit first.
A gap through the stop fills at the candle open. MFE/MAE are measured over the life of the trade.
"""
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import costs

MIN_UNIT_PCT = 0.02   # never plan a stop tighter than this: below it the barrier is inside normal tick noise
EXIT_CODES = {1: "stop_loss", 2: "take_profit", 3: "time_stop"}


def unit_pct(atr_pct, hold_bars: int):
    """Expected typical move over the hold window, %: what the stop/target multiples scale."""
    return np.maximum(np.asarray(atr_pct, dtype=float) * np.sqrt(hold_bars), MIN_UNIT_PCT)


def label_trades(feats: pd.DataFrame, cfg: dict, stop_mult: float | None = None, tp_mult: float | None = None,
                 spread_pct: float = 0.0) -> pd.DataFrame:
    """One row per decision bar with, for long and short: net %, gross %, exit code, bars held, MFE %, MAE %.
    The last hold_bars+1 rows are dropped (their trade would run past the data)."""
    H = int(cfg["scalp_hold_bars"])
    sm = cfg["scalp_stop_mult"] if stop_mult is None else stop_mult
    tm = cfg["scalp_tp_mult"] if tp_mult is None else tp_mult
    o, h, l, c = (feats[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(feats)
    m = n - H - 1
    if m <= 0:
        return pd.DataFrame(index=feats.index[:0])
    unit = unit_pct(feats["atr_1m_pct"].to_numpy(float), H)[:m]
    W_h = sliding_window_view(h, H)[1:1 + m]      # row t -> bars t+1 .. t+H
    W_l = sliding_window_view(l, H)[1:1 + m]
    W_o = sliding_window_view(o, H)[1:1 + m]
    exit_close = c[H:H + m]                        # close of bar t+H
    entry_mid = o[1:1 + m]
    rows = np.arange(m)
    bars = np.arange(H)[None, :]
    out = {}
    for name, d in (("long", 1), ("short", -1)):
        sl_px = entry_mid * (1 - d * sm * unit / 100)
        tp_px = entry_mid * (1 + d * tm * unit / 100)
        if d > 0:
            hit_sl, hit_tp = W_l <= sl_px[:, None], W_h >= tp_px[:, None]
        else:
            hit_sl, hit_tp = W_h >= sl_px[:, None], W_l <= tp_px[:, None]
        i_sl = np.where(hit_sl.any(1), hit_sl.argmax(1), H)
        i_tp = np.where(hit_tp.any(1), hit_tp.argmax(1), H)
        stop_first = (i_sl <= i_tp) & (i_sl < H)
        tp_hit = i_tp < i_sl
        idx = np.where(stop_first, i_sl, np.where(tp_hit, i_tp, H - 1))
        open_at = W_o[rows, idx]
        gap_stop = np.minimum(sl_px, open_at) if d > 0 else np.maximum(sl_px, open_at)
        exit_mid = np.where(stop_first, gap_stop, np.where(tp_hit, tp_px, exit_close))
        code = np.where(stop_first, 1, np.where(tp_hit, 2, 3))
        entry_fill = costs.fill(entry_mid, d, True, spread_pct, cfg["slippage_pct"])
        exit_fill = costs.fill(exit_mid, d, False, spread_pct, cfg["slippage_pct"])
        life = bars <= idx[:, None]
        if d > 0:
            mfe = (np.where(life, W_h, -np.inf).max(1) / entry_mid - 1) * 100
            mae = (np.where(life, W_l, np.inf).min(1) / entry_mid - 1) * 100
        else:
            mfe = (1 - np.where(life, W_l, np.inf).min(1) / entry_mid) * 100
            mae = (1 - np.where(life, W_h, -np.inf).max(1) / entry_mid) * 100
        out[f"net_{name}"] = costs.net_pct(entry_fill, exit_fill, d, cfg["trading_fee_pct"])
        out[f"gross_{name}"] = costs.gross_pct(entry_fill, exit_fill, d)
        out[f"exit_{name}"] = code
        out[f"bars_{name}"] = idx + 1
        out[f"mfe_{name}"] = np.maximum(mfe, 0.0)
        out[f"mae_{name}"] = np.minimum(mae, 0.0)
    out["unit_pct"] = unit
    return pd.DataFrame(out, index=feats.index[:m])
