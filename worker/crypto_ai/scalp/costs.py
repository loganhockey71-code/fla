"""One definition of what a trade costs and nets. Used identically by the label builder, the backtest and live trading,
so the model is trained on, and the backtest measured with, exactly the P&L the live account books.

A fill crosses half the spread and slips against us; a fee is charged on each leg's notional. Longs are spot-style;
shorts are a paper simulation (sell first, buy back later) with the same fee schedule and no funding (holds are minutes).
P&L is expressed as a % of the entry notional (qty * entry_fill), so it is comparable across coins and sizes.
"""
import math

# Spread assumed for HISTORICAL replays where the real bid/ask was not recorded (live trading uses the real ticker spread).
ASSUMED_SPREAD_PCT = {"BTC": 0.01, "ETH": 0.02, "XRP": 0.05}


def fill(mid, direction: int, is_entry: bool, spread_pct: float, slip_pct: float, use_spread: bool = True):
    """Simulated execution price. Buying (long entry or short exit) pays up; selling (long exit or short entry) gives up.
    Works on scalars and numpy arrays."""
    buying = (direction > 0) == is_entry
    adverse = ((spread_pct if use_spread else 0.0) / 2 + slip_pct) / 100
    return mid * (1 + adverse) if buying else mid * (1 - adverse)


def net_pct(entry_fill, exit_fill, direction: int, fee_pct: float):
    """Net P&L as a % of entry notional after a fee on each leg. Works on scalars and numpy arrays."""
    f = fee_pct / 100
    if direction > 0:
        return ((exit_fill / entry_fill) * (1 - f) - (1 + f)) * 100
    return (1 - exit_fill / entry_fill - f * (1 + exit_fill / entry_fill)) * 100


def gross_pct(entry_fill, exit_fill, direction: int):
    return direction * (exit_fill / entry_fill - 1) * 100


def round_trip_cost_pct(cfg: dict, spread_pct: float = 0.0) -> float:
    """Fees + slippage + spread, both legs, in % of notional: the hurdle a trade's move must clear."""
    spread = spread_pct if cfg.get("use_real_spread", True) else 0.0
    return 2 * (cfg["trading_fee_pct"] + cfg["slippage_pct"]) + spread


def impact_pct(notional: float, dollar_vol_15m, unit_pct: float, cfg: dict) -> float:
    """EXTRA slippage per side (%) caused by the order's own size: coef x unit x sqrt(share of the last 15 minutes' dollar volume
    the order would be). Zero when the liquidity is unknown or the order is empty. A flat slippage percentage is only honest for
    small orders; this is what makes a large position cost more than a small one."""
    try:
        liq, n, u = float(dollar_vol_15m), float(notional), float(unit_pct)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(liq) and liq > 0 and math.isfinite(n) and n > 0 and math.isfinite(u) and u > 0):
        return 0.0
    return float(cfg.get("scalp_impact_coef", 1.0)) * u * math.sqrt(n / liq)
