"""Trade-level risk management: volatility-sized stops/targets, trailing stops, momentum-reversal exits,
risk-based position sizing, and a fee/slippage-aware edge check. Long-only (spot, no leverage), same as the
rest of the project.

Every decision function here is pure (no I/O) so it can be unit tested directly; `recent_candles` is the one
thin database-facing helper, reading bars already saved by `collect()` every tick - no extra Coinbase calls.
"""
import numpy as np
import pandas as pd

from .config import BAR


# ---------------------------------------------------------------- volatility (ATR%)
def true_range_pct(df: pd.DataFrame) -> pd.Series:
    """True range (high/low/prior-close) as a percentage of the prior close, per bar."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr / prev_close * 100


def atr_pct(df: pd.DataFrame, n: int) -> float | None:
    """Average true range over the last `n` bars, as a % of price. None if there isn't enough history yet -
    callers must fall back to the pre-existing (non-volatility-aware) behaviour when this is None."""
    if len(df) < n + 1:
        return None
    v = true_range_pct(df).iloc[-n:].mean()
    return float(v) if np.isfinite(v) and v > 0 else None


# ---------------------------------------------------------------- entry planning
def plan_exits(entry_price: float, atr_pct_now: float | None, cfg: dict) -> dict:
    """Stop-loss and take-profit distances sized off current volatility (ATR%), long-only.
    Both legs scale with `atr_pct_now`, so a calm entry gets a tight stop/target and a volatile one gets a
    wide one; the ratio between them is a fixed policy (`take_profit_atr_mult` / `stop_loss_atr_mult`) -
    `has_edge` is what actually decides whether a trade clears the minimum reward:risk after costs."""
    if not atr_pct_now:
        return {"stop_price": None, "take_profit_price": None, "entry_atr_pct": None, "expected_rr": None}
    stop_dist_pct = cfg["stop_loss_atr_mult"] * atr_pct_now
    tp_dist_pct = cfg["take_profit_atr_mult"] * atr_pct_now
    return {
        "stop_price": entry_price * (1 - stop_dist_pct / 100),
        "take_profit_price": entry_price * (1 + tp_dist_pct / 100),
        "entry_atr_pct": atr_pct_now,
        "expected_rr": tp_dist_pct / stop_dist_pct,
    }


def has_edge(entry_price: float, stop_price: float | None, take_profit_price: float | None, cfg: dict) -> tuple[bool, float | None]:
    """Whether a planned trade clears round-trip costs (fee + slippage, both ways - same convention as
    model.py's backtest) by enough margin to also meet `min_reward_risk_ratio`. (True, None) when there is no
    stop/target yet (volatility unknown) - callers should treat that as "no opinion", not "no edge"."""
    if stop_price is None or take_profit_price is None:
        return True, None
    cost_pct = 2 * (cfg["trading_fee_pct"] + cfg["slippage_pct"])
    reward_pct = (take_profit_price / entry_price - 1) * 100
    risk_pct = (1 - stop_price / entry_price) * 100
    if risk_pct <= 0:
        return False, None
    rr = reward_pct / risk_pct
    return (reward_pct - cost_pct > 0 and rr >= cfg["min_reward_risk_ratio"]), rr


def risk_based_budget(total_value: float, cash: float, stop_price: float | None, entry_price: float,
                       existing_budget: float, cfg: dict) -> float:
    """Caps the confidence-based `existing_budget` so a stop-out loses at most `max_risk_pct_per_trade` of the
    account. Only ever tightens the existing sizing rules, never loosens them, and falls back to
    `existing_budget` unchanged when the stop distance isn't known yet."""
    if not stop_price or entry_price <= 0 or stop_price >= entry_price:
        return existing_budget
    stop_distance_pct = (1 - stop_price / entry_price) * 100
    max_risk_usd = total_value * cfg["max_risk_pct_per_trade"] / 100
    risk_capped = max_risk_usd / (stop_distance_pct / 100)
    return max(0.0, min(existing_budget, risk_capped, cash))


# ---------------------------------------------------------------- trade management (while open)
def update_excursion(entry_price: float, current_price: float, mfe_pct: float | None, mae_pct: float | None) -> tuple[float, float]:
    """Rolling maximum favourable / adverse excursion since entry, in % (long-only: up is favourable)."""
    move = (current_price / entry_price - 1) * 100
    return max(mfe_pct or 0.0, move, 0.0), min(mae_pct or 0.0, move, 0.0)


def update_trailing_stop(entry_price: float, initial_stop: float | None, current_stop: float | None,
                          high_water: float, current_price: float, atr_pct_now: float | None, cfg: dict) -> tuple[float | None, float, bool]:
    """(new_stop, new_high_water, trail_active). The stop only ever moves UP for a long position (never back
    down toward more risk - "never increase risk after entering"), and only once price has moved
    `trailing_activation_r` times the ORIGINAL risk in our favour. Once active it trails
    `trailing_atr_mult` x ATR behind the highest price seen since entry."""
    hw = max(high_water, current_price)
    if initial_stop is None or current_stop is None:
        return current_stop, hw, False
    initial_risk = entry_price - initial_stop
    if initial_risk <= 0:
        return current_stop, hw, False
    activated = (hw - entry_price) >= cfg["trailing_activation_r"] * initial_risk
    if not activated or not atr_pct_now:
        return current_stop, hw, False
    trail_candidate = hw * (1 - cfg["trailing_atr_mult"] * atr_pct_now / 100)
    return max(current_stop, trail_candidate), hw, True


def momentum_reversed(recent_closes: pd.Series, atr_pct_now: float | None, cfg: dict) -> bool:
    """Short-term momentum has turned against a long position: the return over the last
    `momentum_reversal_lookback_bars` bars is a negative move of at least `momentum_reversal_atr_mult` x ATR -
    an early-exit trigger so a clear trend reversal doesn't have to wait for the (much wider) stop-loss."""
    n = cfg["momentum_reversal_lookback_bars"]
    if len(recent_closes) < n + 1 or not atr_pct_now:
        return False
    ret_pct = (recent_closes.iloc[-1] / recent_closes.iloc[-1 - n] - 1) * 100
    return bool(ret_pct <= -cfg["momentum_reversal_atr_mult"] * atr_pct_now)


def check_exit(trade: dict, current_price: float, recent_closes: pd.Series, atr_pct_now: float | None, cfg: dict) -> dict:
    """One position, one tick. Always returns the up-to-date trailing-stop state (so the caller can persist it
    even when nothing fires); "exit_reason" is None to keep holding, else one of
    take_profit / stop_loss / trailing_stop / momentum_reversal."""
    entry = trade["exec_price"]
    stop = trade.get("stop_price")
    take_profit = trade.get("take_profit_price")
    new_stop, high_water, trail_active = update_trailing_stop(
        entry, trade.get("initial_stop_price"), stop, trade.get("high_water_price") or entry, current_price, atr_pct_now, cfg)
    state = {"stop_price": new_stop, "high_water_price": high_water, "trail_active": trail_active}
    if take_profit is not None and current_price >= take_profit:
        return {"exit_reason": "take_profit", **state}
    if new_stop is not None and current_price <= new_stop:
        return {"exit_reason": "trailing_stop" if trail_active else "stop_loss", **state}
    if momentum_reversed(recent_closes, atr_pct_now, cfg):
        return {"exit_reason": "momentum_reversal", **state}
    return {"exit_reason": None, **state}


# ---------------------------------------------------------------- database wrapper
def recent_candles(db, symbol: str, bars: int) -> pd.DataFrame:
    """Last `bars` closed 15m candles for `symbol`, oldest first. Reads what `collect()` already saved -
    no extra Coinbase call."""
    rows = db.all("select ts, open, high, low, close, volume from candles where symbol=%s and granularity=%s "
                  "order by ts desc limit %s", [symbol, BAR, bars])
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows[::-1]).set_index("ts")
    return df
