"""Named candlestick / structure patterns at a decision bar, for STUDYING what works (never a model input, never a trade rule).

Every pattern is read from candles that had closed at the decision, so it is causal. Each tag is stored with the signal and the
trade, and the learning loop (learn.condition_tags / pattern_findings) reports which tags actually earned money after costs.
Direction-aware: a "bullish_engulfing" read on a short is a tag AGAINST the trade, which is exactly what we want to learn about.
"""
import numpy as np
import pandas as pd

DOJI_BODY = 0.1          # body / range at or below this = indecision
LONG_WICK = 0.6          # wick / range at or above this = rejection
BREAK_BARS = 20


def _f(x):
    return float(x) if x is not None and np.isfinite(x) else None


def candle_patterns(df: pd.DataFrame, bars: int = BREAK_BARS) -> list[str]:
    """Pattern tags for the LAST candle of `df` (closed 1m candles, ascending, columns open/high/low/close/volume)."""
    if df is None or len(df) < 4:
        return []
    w = df.iloc[-(bars + 1):]
    o, h, l, c, v = (w[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    rng = np.where((h - l) > 0, h - l, np.nan)
    body = np.abs(c - o) / rng
    upper = (h - np.maximum(c, o)) / rng
    lower = (np.minimum(c, o) - l) / rng
    up, dn = c > o, c < o
    tags: list[str] = []
    if body[-1] <= DOJI_BODY:
        tags.append("doji")
    if lower[-1] >= LONG_WICK and body[-1] <= 0.35:
        tags.append("hammer")                          # long lower wick: buyers rejected lower prices
    if upper[-1] >= LONG_WICK and body[-1] <= 0.35:
        tags.append("shooting_star")                   # long upper wick: sellers rejected higher prices
    if dn[-2] and up[-1] and c[-1] >= o[-2] and o[-1] <= c[-2]:
        tags.append("bullish_engulfing")
    if up[-2] and dn[-1] and c[-1] <= o[-2] and o[-1] >= c[-2]:
        tags.append("bearish_engulfing")
    if h[-1] <= h[-2] and l[-1] >= l[-2]:
        tags.append("inside_bar")                      # compression, often before an expansion
    if up[-3:].all() and c[-1] > c[-2] > c[-3]:
        tags.append("three_up")
    if dn[-3:].all() and c[-1] < c[-2] < c[-3]:
        tags.append("three_down")
    if len(w) >= 6:
        prior_hi, prior_lo = h[:-1].max(), l[:-1].min()
        if c[-1] > prior_hi:
            tags.append("breakout_high")               # closed above everything in the lookback: breaking resistance
        elif c[-1] < prior_lo:
            tags.append("breakdown_low")               # closed below everything in the lookback: breaking support
        else:
            span = prior_hi - prior_lo
            if span > 0:
                pos = (c[-1] - prior_lo) / span
                if pos <= 0.1:
                    tags.append("at_support")
                elif pos >= 0.9:
                    tags.append("at_resistance")
    base_v = np.nanmean(v[:-1]) if len(v) > 1 else np.nan
    if np.isfinite(base_v) and base_v > 0 and v[-1] >= 2.0 * base_v:
        tags.append("volume_spike")
    return tags
