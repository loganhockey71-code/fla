"""Walk-forward stop-loss/take-profit search (pure logic; no database)."""
import numpy as np
import pandas as pd
import pytest

from crypto_ai.learning import exit_optimizer as eo


def series(closes, spread_pct=1.0):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    half = closes * spread_pct / 100 / 2
    return pd.DataFrame({"open": closes, "high": closes + half, "low": closes - half, "close": closes, "volume": 10.0}, index=idx)


def warmup(n=20, seed=3, vol=0.15):
    """A small, smooth random walk (not an artificial alternation) so ATR is well-defined and non-zero without
    leaving a spurious spike/dip right at the entry boundary."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, vol, n)
    return list(100 * np.cumprod(1 + steps / 100))


WARMUP = warmup()             # ~1% ATR at spread_pct=1.0, ends close to 100
ENTRY_I = len(WARMUP) - 1


# ---------------------------------------------------------------- simulate_one
def test_simulate_one_none_without_enough_history_before_entry(cfg):
    df = series(WARMUP[:5] + [100.0] * 5)
    assert eo.simulate_one(df, entry_i=6, stop_mult=1.5, tp_mult=2.5, cfg=cfg) is None


def test_simulate_one_none_at_the_end_of_the_series(cfg):
    df = series(WARMUP)
    assert eo.simulate_one(df, entry_i=len(df) - 1, stop_mult=1.5, tp_mult=2.5, cfg=cfg) is None


def test_simulate_one_hits_take_profit_on_a_rally(cfg):
    entry = WARMUP[-1]
    df = series(WARMUP + [entry * 1.03, entry * 1.05, entry * 1.08, entry * 1.12])
    t = eo.simulate_one(df, entry_i=ENTRY_I, stop_mult=1.5, tp_mult=2.5, cfg=cfg)
    assert t is not None and t["exit_reason"] == "take_profit" and t["net_pct"] > 0


def test_simulate_one_hits_stop_loss_on_a_selloff(cfg):
    entry = WARMUP[-1]
    df = series(WARMUP + [entry * 0.98, entry * 0.96, entry * 0.93, entry * 0.90])
    t = eo.simulate_one(df, entry_i=ENTRY_I, stop_mult=1.5, tp_mult=2.5, cfg=cfg)
    assert t is not None and t["exit_reason"] == "stop_loss" and t["net_pct"] < 0


def test_simulate_one_falls_back_to_max_hold_when_neither_fires(cfg):
    entry = WARMUP[-1]
    df = series(WARMUP + [entry * 1.002, entry * 0.999, entry * 1.003, entry * 1.001])   # small jitter, nothing fires
    t = eo.simulate_one(df, entry_i=ENTRY_I, stop_mult=1.5, tp_mult=2.5, cfg=cfg, max_hold_bars=4)
    assert t is not None and t["exit_reason"] == "max_hold" and t["bars_held"] == 4


def test_simulate_one_deducts_round_trip_costs(cfg):
    # flat AT the entry price the whole way, forced out at max_hold: net P&L is exactly -costs, not zero
    entry = WARMUP[-1]
    df = series(WARMUP + [entry] * 6, spread_pct=0.0)
    t = eo.simulate_one(df, entry_i=ENTRY_I, stop_mult=5.0, tp_mult=10.0, cfg=cfg, max_hold_bars=4)
    cost_pct = 2 * (cfg["trading_fee_pct"] + cfg["slippage_pct"])
    assert t["exit_reason"] == "max_hold" and t["net_pct"] == pytest.approx(-cost_pct, abs=1e-9)


# ---------------------------------------------------------------- _stats
def test_stats_of_no_trades_is_all_none():
    s = eo._stats([])
    assert s["n"] == 0 and s["net_pct_total"] is None and s["profit_factor"] is None


def test_stats_matches_hand_computed_numbers():
    trades = [{"net_pct": 2.0}, {"net_pct": -1.0}, {"net_pct": 3.0}, {"net_pct": -0.5}]
    s = eo._stats(trades)
    assert s["n"] == 4 and s["win_rate"] == 0.5
    assert s["net_pct_total"] == pytest.approx(3.5)
    assert s["profit_factor"] == pytest.approx(5.0 / 1.5)


# ---------------------------------------------------------------- walk_forward_grid
def test_walk_forward_grid_errors_with_too_few_entries(cfg):
    df = series(WARMUP + [100.0] * 20)
    res = eo.walk_forward_grid(df, entry_idx=[16, 17], cfg=cfg, folds=4)
    assert res["error"] and res["ranked"] == [] and res["best"] is None


def test_walk_forward_grid_skips_combos_below_the_reward_risk_floor(cfg):
    df = series(WARMUP + [WARMUP[-1]] * 40, spread_pct=0.0)
    entries = list(range(ENTRY_I, ENTRY_I + 20))
    res = eo.walk_forward_grid(df, entry_idx=entries, cfg=cfg, folds=4, stop_grid=[2.0], tp_grid=[1.0, 3.0])
    assert res["tried"] == 1                                      # 1.0/2.0=0.5 < min_reward_risk_ratio, only 3.0/2.0=1.5 survives
    if res["ranked"]:
        assert res["ranked"][0]["take_profit_atr_mult"] == 3.0


def test_walk_forward_grid_prefers_the_consistently_better_combo_on_a_clean_uptrend(cfg):
    # a steady, low-noise rally: a wider target should clearly out-earn one so tight it barely captures the move
    entry = WARMUP[-1]
    trend = list(entry + np.arange(1, 81) * 0.35)
    df = series(WARMUP + trend, spread_pct=0.2)
    entries = list(range(ENTRY_I, ENTRY_I + 70, 3))
    res = eo.walk_forward_grid(df, entry_idx=entries, cfg=cfg, folds=4, stop_grid=[1.5], tp_grid=[1.5, 4.0], max_hold_bars=20)
    assert res["best"] is not None
    assert res["best"]["take_profit_atr_mult"] == 4.0             # the wider target should win on a clean trend
