"""Maker-fill simulator. Pure functions, no I/O - `tests/test_micro.py` proves the fill/no-fill logic on synthetic
trade sequences.

We do NOT have a free real-time order-book delta feed (level2/full now require Coinbase auth - see micro_collect.py),
so exact queue position is unobservable. The approximation used here, standard in microstructure research given only
trade prints + a periodic depth snapshot, is:

  A resting BUY at price L, with `queue_ahead` displayed size already resting at L when we joined the queue, fills the
  instant the CUMULATIVE size of aggressor-SELL trades printing at or through L (i.e. price <= L) reaches queue_ahead.
  (Any trade printing below L must have consumed everything resting at L first, ours included, so it always counts.)
  A resting SELL is the mirror: consumed by aggressor-BUY trades at price >= L.

This assumes our own order is small next to typical trade size (a fill is a point event, not partial), and it OVERSTATES
the fill rate a little: other traders' new limit orders can join the queue ahead of us after we place it, which we
cannot see without L3 data - a real deployment would need to treat these as optimistic upper bounds, not guarantees.
An order that never accumulates enough opposite flow within `horizon_s` did NOT fill: that is a real, common outcome
here (never a loss, and never assumed away).
"""
from dataclasses import dataclass

import pandas as pd


@dataclass
class FillResult:
    filled: bool
    fill_time: pd.Timestamp | None = None
    fill_price: float | None = None
    time_to_fill_s: float | None = None
    reason: str = "timeout"     # timeout | filled


def simulate_maker_fill(side: str, limit_price: float, queue_ahead: float, trades: pd.DataFrame, t0: pd.Timestamp, horizon_s: float) -> FillResult:
    """`trades`: columns [ts, price, size, aggressor], ts > t0, sorted ascending, filtered to ts <= t0+horizon (or filtered
    here). `side` = the maker order's side ('buy' or 'sell'); only the opposing aggressor can fill it."""
    need_aggressor = "sell" if side == "buy" else "buy"
    crosses = (lambda p: p <= limit_price) if side == "buy" else (lambda p: p >= limit_price)
    cutoff = t0 + pd.Timedelta(seconds=horizon_s)
    window = trades[(trades["ts"] > t0) & (trades["ts"] <= cutoff) & (trades["aggressor"] == need_aggressor) & trades["price"].apply(crosses)]
    remaining = max(queue_ahead, 0.0)
    for row in window.itertuples():
        if remaining <= 0:
            return FillResult(True, row.ts, limit_price, (row.ts - t0).total_seconds(), "filled")
        remaining -= row.size
        if remaining <= 0:
            return FillResult(True, row.ts, limit_price, (row.ts - t0).total_seconds(), "filled")
    return FillResult(False)


def mid_at(mid_series: pd.Series, t: pd.Timestamp) -> float | None:
    """Last known mid at or before `t` (as-of), from a Series indexed by timestamp."""
    idx = mid_series.index.searchsorted(t, side="right") - 1
    return float(mid_series.iloc[idx]) if idx >= 0 else None


def adverse_selection(side: str, fill_price: float, fill_time: pd.Timestamp, mid_series: pd.Series, horizons_s: list[float]) -> dict:
    """Price drift AFTER a maker fill, in the direction that matters for the position: negative = the fill was
    adverse (price kept moving the wrong way right after you were hit), positive = favourable."""
    d = 1 if side == "buy" else -1
    out = {}
    for h in horizons_s:
        m = mid_at(mid_series, fill_time + pd.Timedelta(seconds=h))
        out[f"drift_{h}s_pct"] = d * (m / fill_price - 1) * 100 if m is not None else None
    return out


def compare_execution(side: str, t0: pd.Timestamp, mid0: float, spread_pct0: float, limit_price: float, queue_ahead: float,
                      trades: pd.DataFrame, mid_series: pd.Series, cfg: dict, fill_horizon_s: float, hold_after_fill_s: float) -> dict:
    """market / maker / no_trade net % for one hypothetical entry, all exiting after the SAME hold time (maker's clock
    starts at its (possibly later) fill; a taker's starts at t0), both exiting with a taker (market) order - the
    maker is assumed to manage risk actively once in, not sit in a second resting order. `spread_pct0` should be the
    REAL measured spread at t0, not an assumed one."""
    d = 1 if side == "buy" else -1
    fee_t, slip = cfg["trading_fee_pct"] / 100, cfg["slippage_pct"] / 100
    fee_m = cfg.get("maker_fee_pct", cfg["trading_fee_pct"]) / 100

    def taker_leg(mid, is_entry):
        buying = (d > 0) == is_entry
        return mid * (1 + spread_pct0 / 200 + slip) if buying else mid * (1 - spread_pct0 / 200 - slip)

    # ---- market order: in now, out after hold_after_fill_s from t0
    exit_mid_t = mid_at(mid_series, t0 + pd.Timedelta(seconds=hold_after_fill_s))
    market = None
    if exit_mid_t is not None:
        ef, xf = taker_leg(mid0, True), taker_leg(exit_mid_t, False)
        market = (d * (xf / ef - 1) - fee_t - fee_t) * 100 if d > 0 else ((1 - xf / ef) - fee_t - fee_t) * 100

    # ---- maker order: wait up to fill_horizon_s for a fill, then hold hold_after_fill_s more before a taker exit
    fr = simulate_maker_fill(side, limit_price, queue_ahead, trades, t0, fill_horizon_s)
    maker = None
    if fr.filled:
        exit_mid_m = mid_at(mid_series, fr.fill_time + pd.Timedelta(seconds=hold_after_fill_s))
        if exit_mid_m is not None:
            xf = taker_leg(exit_mid_m, False)
            gross = d * (xf / limit_price - 1) * 100 if d > 0 else (1 - xf / limit_price) * 100
            maker = gross - (fee_m + fee_t) * 100
    return {"market_net_pct": market, "maker_net_pct": maker, "maker_filled": fr.filled, "maker_fill_time": fr.fill_time,
            "maker_time_to_fill_s": fr.time_to_fill_s, "no_trade_net_pct": 0.0}
