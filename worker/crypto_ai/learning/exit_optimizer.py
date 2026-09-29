"""Walk-forward search for stop-loss / take-profit ATR multiples that actually produced the best NET results on
real historical candles - never a random train/test split (same time-ordered-fold idea as model.py's
walk_forward, applied here to exit parameters instead of model weights).

Entries are real past BUY signals already logged in `predictions` (the short-horizon model, not replayed live),
so this measures "if we'd used these stop/target distances on the calls we actually made" rather than an
idealised set of entries. Each one is walked forward bar-by-bar through the real candle path using the exact
same pure exit logic as live trading (risk.check_exit) - not a separate approximation of it - until a stop,
target, trailing-stop, or a max hold time is hit.

Scored on net P&L / win rate / profit factor / drawdown, never directional accuracy. A combo is ranked by its
WORST fold, not its average: one that only wins in a single time period and loses in the others is exactly the
overfitting this is meant to catch, and folds are cut by TIME, not shuffled, to make sure of that.

Nothing here is applied automatically - `cli.py optimize-exits` only prints a report for a human to act on,
the same way `learned_patterns` are suggestions and never auto-applied.
"""
import numpy as np
import pandas as pd

from .. import risk
from ..config import SHORT_HORIZON_H

GRID_STOP_MULT = [1.0, 1.5, 2.0, 2.5]
GRID_TP_MULT = [1.5, 2.0, 2.5, 3.0, 4.0]
MAX_HOLD_BARS_DEFAULT = 16   # 4 hours of 15m bars: a ceiling if neither the stop nor the target is ever hit


def simulate_one(df: pd.DataFrame, entry_i: int, stop_mult: float, tp_mult: float, cfg: dict,
                  max_hold_bars: int = MAX_HOLD_BARS_DEFAULT) -> dict | None:
    """Walk forward from candle index `entry_i`, sizing the stop/target off the ATR known AT entry (never a
    later bar), then applying the live trailing-stop/momentum-reversal rules bar by bar. None if there isn't
    enough history before `entry_i` to size a stop, or the series ends before the trade could resolve."""
    if entry_i < cfg["atr_period_bars"] + 1 or entry_i >= len(df):
        return None
    atr = risk.atr_pct(df.iloc[:entry_i + 1], cfg["atr_period_bars"])
    if not atr:
        return None
    entry_price = float(df["close"].iloc[entry_i])
    stop = entry_price * (1 - stop_mult * atr / 100)
    take_profit = entry_price * (1 + tp_mult * atr / 100)
    trade = {"exec_price": entry_price, "initial_stop_price": stop, "stop_price": stop,
             "take_profit_price": take_profit, "high_water_price": entry_price}
    fold_cfg = {**cfg, "stop_loss_atr_mult": stop_mult, "take_profit_atr_mult": tp_mult}
    cost_pct = 2 * (cfg["trading_fee_pct"] + cfg["slippage_pct"])
    end = min(entry_i + max_hold_bars, len(df) - 1)
    if end <= entry_i:
        return None
    for i in range(entry_i + 1, end + 1):
        price = float(df["close"].iloc[i])
        recent = df["close"].iloc[max(0, i - cfg["momentum_reversal_lookback_bars"]): i + 1]
        hit = risk.check_exit(trade, price, recent, atr, fold_cfg)
        trade["stop_price"], trade["high_water_price"] = hit["stop_price"], hit["high_water_price"]
        if hit["exit_reason"]:
            return {"entry_time": df.index[entry_i], "exit_time": df.index[i], "exit_reason": hit["exit_reason"],
                    "bars_held": i - entry_i, "net_pct": (price / entry_price - 1) * 100 - cost_pct}
    price = float(df["close"].iloc[end])
    return {"entry_time": df.index[entry_i], "exit_time": df.index[end], "exit_reason": "max_hold",
            "bars_held": end - entry_i, "net_pct": (price / entry_price - 1) * 100 - cost_pct}


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "net_pct_total": None, "win_rate": None, "profit_factor": None, "avg_pct": None, "max_drawdown_pct": None}
    pct = [t["net_pct"] for t in trades]
    wins, losses = [x for x in pct if x > 0], [x for x in pct if x <= 0]
    equity = np.cumsum(pct)
    dd = float(np.max(np.maximum.accumulate(equity) - equity))
    return {
        "n": len(trades), "net_pct_total": float(sum(pct)), "win_rate": len(wins) / len(pct),
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else None,
        "avg_pct": float(np.mean(pct)), "max_drawdown_pct": dd,
    }


def walk_forward_grid(df: pd.DataFrame, entry_idx: list[int], cfg: dict, folds: int = 4,
                       stop_grid: list[float] = GRID_STOP_MULT, tp_grid: list[float] = GRID_TP_MULT,
                       max_hold_bars: int = MAX_HOLD_BARS_DEFAULT) -> dict:
    """Splits entries into `folds` TIME-ORDERED chunks (never shuffled). Every (stop_mult, tp_mult) combo below
    `min_reward_risk_ratio` is skipped outright - not a candidate regardless of backtest performance, matching
    the live has_edge() gate. Surviving combos are ranked by their WORST fold's profit factor."""
    entry_idx = sorted(set(int(i) for i in entry_idx))
    if len(entry_idx) < folds:
        return {"error": f"only {len(entry_idx)} usable entries - need at least {folds}", "tried": 0, "ranked": [], "best": None}
    chunks = np.array_split(entry_idx, folds)
    results = []
    for stop_mult in stop_grid:
        for tp_mult in tp_grid:
            if tp_mult / stop_mult < cfg["min_reward_risk_ratio"]:
                continue
            fold_stats, all_trades = [], []
            for chunk in chunks:
                trades = [t for t in (simulate_one(df, i, stop_mult, tp_mult, cfg, max_hold_bars) for i in chunk) if t is not None]
                fold_stats.append(_stats(trades))
                all_trades += trades
            # a fold with trades but zero losers has an undefined (not bad!) profit factor - treat it as the
            # best possible outcome for that fold, not as unusable. Only a fold with NO trades at all (can't be
            # judged) disqualifies the combo.
            pfs = [] if any(f["n"] == 0 for f in fold_stats) else \
                [f["profit_factor"] if f["profit_factor"] is not None else float("inf") for f in fold_stats]
            results.append({"stop_loss_atr_mult": stop_mult, "take_profit_atr_mult": tp_mult,
                            "overall": _stats(all_trades), "folds": fold_stats,
                            "worst_fold_profit_factor": min(pfs) if pfs else None})
    ranked = sorted((r for r in results if r["worst_fold_profit_factor"] is not None),
                    key=lambda r: r["worst_fold_profit_factor"], reverse=True)
    return {"tried": len(results), "ranked": ranked, "best": ranked[0] if ranked else None}


# ---------------------------------------------------------------- database wrapper
def historical_entries(db, symbol: str) -> list:
    """Real past BUY signals from the short-horizon model's own logged predictions - not replayed live."""
    return db.all("select created_at, price_at_prediction from predictions where symbol=%s and horizon_h=%s "
                  "and variant='market' and signal='BUY' order by created_at", [symbol, SHORT_HORIZON_H])


def run_for_symbol(db, symbol: str, cfg: dict, folds: int = 4) -> dict:
    """Loads this coin's saved candles and past BUY calls, maps each call to the candle bar right after it (so
    the backtest can never look ahead), and runs the walk-forward grid search."""
    from ..config import BAR
    rows = db.all("select ts, open, high, low, close, volume from candles where symbol=%s and granularity=%s order by ts", [symbol, BAR])
    if not rows:
        return {"error": "no saved candles for this symbol yet - run `tick` a few times or `train` first", "tried": 0, "ranked": [], "best": None}
    df = pd.DataFrame(rows).set_index("ts")
    entries = historical_entries(db, symbol)
    idx = df.index
    entry_idx = []
    for e in entries:
        pos = idx.searchsorted(pd.Timestamp(e["created_at"]))
        if pos < len(idx):
            entry_idx.append(pos)
    return walk_forward_grid(df, entry_idx, cfg, folds=folds)
