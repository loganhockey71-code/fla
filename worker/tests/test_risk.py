"""Trade-level risk management (pure logic; no database)."""
import numpy as np
import pandas as pd
import pytest

from crypto_ai import risk


def ohlc(closes, spread_pct=1.0, n=None):
    """Synthetic 15m candles: high/low straddle the close by `spread_pct`% so true range is easy to reason about."""
    closes = np.asarray(closes, dtype=float)
    if n is not None:
        closes = np.full(n, closes[0]) if closes.size == 1 else closes
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    half = closes * spread_pct / 100 / 2
    return pd.DataFrame({"open": closes, "high": closes + half, "low": closes - half, "close": closes, "volume": 10.0}, index=idx)


# ---------------------------------------------------------------- ATR
def test_atr_pct_is_none_with_too_little_history():
    assert risk.atr_pct(ohlc([100.0] * 5), 14) is None


def test_atr_pct_tracks_a_known_constant_true_range():
    df = ohlc([100.0] * 20, spread_pct=2.0)      # every bar's high-low range is 2% of price, flat closes
    a = risk.atr_pct(df, 14)
    assert a == pytest.approx(2.0, rel=0.05)


def test_atr_pct_is_none_when_flat_and_zero_range():
    assert risk.atr_pct(ohlc([100.0] * 20, spread_pct=0.0), 14) is None


# ---------------------------------------------------------------- entry planning
def test_plan_exits_falls_back_to_none_without_volatility(cfg):
    assert risk.plan_exits(100.0, None, cfg) == {"stop_price": None, "take_profit_price": None, "entry_atr_pct": None, "expected_rr": None}


def test_plan_exits_scales_with_atr(cfg):
    tight = risk.plan_exits(100.0, 1.0, cfg)
    wide = risk.plan_exits(100.0, 3.0, cfg)
    # more volatility -> both legs sit farther from entry
    assert wide["stop_price"] < tight["stop_price"] < 100.0 < tight["take_profit_price"] < wide["take_profit_price"]
    assert tight["expected_rr"] == pytest.approx(wide["expected_rr"])                      # same policy ratio either way
    assert tight["expected_rr"] == pytest.approx(cfg["take_profit_atr_mult"] / cfg["stop_loss_atr_mult"])


def test_has_edge_with_unknown_volatility_defers(cfg):
    assert risk.has_edge(100.0, None, None, cfg) == (True, None)


def test_has_edge_rejects_a_reward_risk_ratio_below_the_minimum(cfg):
    ok, rr = risk.has_edge(100.0, stop_price=99.0, take_profit_price=100.8, cfg=cfg)       # rr = 0.8, well under 1.0%+ costs
    assert not ok and rr == pytest.approx(0.8)


def test_has_edge_accepts_a_trade_that_clears_costs_and_the_minimum_ratio(cfg):
    exits = risk.plan_exits(100.0, 2.0, cfg)                                               # default mults: rr ~1.67
    ok, rr = risk.has_edge(100.0, exits["stop_price"], exits["take_profit_price"], cfg)
    assert ok and rr >= cfg["min_reward_risk_ratio"]


def test_risk_based_budget_falls_back_without_a_stop(cfg):
    assert risk.risk_based_budget(1000.0, 1000.0, None, 100.0, 200.0, cfg) == 200.0


def test_risk_based_budget_caps_a_wide_stop_but_never_loosens_a_tight_one(cfg):
    # a stop 10% away means risking $10 for every $100 invested: for a 1% account-risk cap on $1000 that's $100 max size
    small = risk.risk_based_budget(1000.0, 1000.0, stop_price=90.0, entry_price=100.0, existing_budget=500.0, cfg=cfg)
    assert small == pytest.approx(100.0)
    # a very tight stop wouldn't need the cap at all - existing (confidence-based) sizing still wins
    tight = risk.risk_based_budget(1000.0, 1000.0, stop_price=99.9, entry_price=100.0, existing_budget=50.0, cfg=cfg)
    assert tight == pytest.approx(50.0)


def test_risk_based_budget_never_exceeds_cash():
    cfg = {"max_risk_pct_per_trade": 100.0}
    assert risk.risk_based_budget(1000.0, 30.0, stop_price=50.0, entry_price=100.0, existing_budget=500.0, cfg=cfg) == pytest.approx(30.0)


# ---------------------------------------------------------------- while a trade is open
def test_update_excursion_tracks_best_and_worst_since_entry():
    mfe, mae = risk.update_excursion(100.0, 105.0, None, None)
    assert mfe == pytest.approx(5.0) and mae == pytest.approx(0.0)
    mfe, mae = risk.update_excursion(100.0, 97.0, mfe, mae)
    assert mfe == pytest.approx(5.0) and mae == pytest.approx(-3.0)          # best-so-far is kept, worst-so-far updates
    mfe, mae = risk.update_excursion(100.0, 108.0, mfe, mae)
    assert mfe == pytest.approx(8.0) and mae == pytest.approx(-3.0)


def test_trailing_stop_never_activates_before_the_activation_threshold(cfg):
    stop, hw, active = risk.update_trailing_stop(100.0, 95.0, 95.0, high_water=101.0, current_price=101.0, atr_pct_now=1.0, cfg=cfg)
    assert not active and stop == 95.0                                       # only +1 of the +5 (trailing_activation_r=1.0 needs +5) risk covered


def test_trailing_stop_activates_and_only_ever_moves_up(cfg):
    stop, hw, active = risk.update_trailing_stop(100.0, 95.0, 95.0, high_water=100.0, current_price=106.0, atr_pct_now=1.0, cfg=cfg)
    assert active and stop > 95.0 and stop == pytest.approx(106.0 * (1 - cfg["trailing_atr_mult"] * 1.0 / 100))
    # price pulls back: the stop must NOT retreat (never increase risk after entering)
    stop2, hw2, active2 = risk.update_trailing_stop(100.0, 95.0, stop, high_water=hw, current_price=103.0, atr_pct_now=1.0, cfg=cfg)
    assert stop2 == pytest.approx(stop) and hw2 == pytest.approx(106.0)


def test_trailing_stop_is_a_noop_without_an_initial_stop(cfg):
    stop, hw, active = risk.update_trailing_stop(100.0, None, None, high_water=100.0, current_price=110.0, atr_pct_now=1.0, cfg=cfg)
    assert stop is None and not active


def test_momentum_reversed_needs_enough_history_and_volatility(cfg):
    assert risk.momentum_reversed(pd.Series([100.0, 99.0]), 1.0, cfg) is False            # too few bars
    assert risk.momentum_reversed(pd.Series([100.0] * 5), None, cfg) is False             # no ATR yet


def test_momentum_reversed_fires_on_a_sharp_adverse_move(cfg):
    closes = pd.Series([100.0, 100.0, 100.0, 98.0])                                       # -2% over 3 bars, ATR 1% -> -2 ATRs
    assert risk.momentum_reversed(closes, atr_pct_now=1.0, cfg=cfg) is True


def test_momentum_reversed_ignores_ordinary_noise(cfg):
    closes = pd.Series([100.0, 100.0, 100.0, 99.9])
    assert risk.momentum_reversed(closes, atr_pct_now=1.0, cfg=cfg) is False


# ---------------------------------------------------------------- check_exit (integration of the above)
def _trade(entry=100.0, stop=95.0, tp=110.0, hw=None):
    return {"exec_price": entry, "initial_stop_price": stop, "stop_price": stop, "take_profit_price": tp, "high_water_price": hw or entry}


def test_check_exit_holds_when_nothing_has_fired(cfg):
    hit = risk.check_exit(_trade(), 101.0, pd.Series([100.0, 100.5, 101.0, 101.0]), atr_pct_now=1.0, cfg=cfg)
    assert hit["exit_reason"] is None


def test_check_exit_fires_take_profit(cfg):
    hit = risk.check_exit(_trade(), 111.0, pd.Series([100.0, 105.0, 110.0, 111.0]), atr_pct_now=1.0, cfg=cfg)
    assert hit["exit_reason"] == "take_profit"


def test_check_exit_fires_stop_loss_before_trailing_activates(cfg):
    hit = risk.check_exit(_trade(), 94.0, pd.Series([100.0, 98.0, 96.0, 94.0]), atr_pct_now=1.0, cfg=cfg)
    assert hit["exit_reason"] == "stop_loss"


def test_check_exit_fires_trailing_stop_once_armed_and_price_pulls_back(cfg):
    t = _trade(hw=106.0)
    t["stop_price"] = 106.0 * (1 - cfg["trailing_atr_mult"] * 1.0 / 100)     # already trailing from a prior tick
    t["trail_active"] = True
    hit = risk.check_exit(t, t["stop_price"] - 0.01, pd.Series([106.0, 105.0, 104.5, t["stop_price"] - 0.01]), atr_pct_now=1.0, cfg=cfg)
    assert hit["exit_reason"] == "trailing_stop"


def test_check_exit_fires_momentum_reversal_when_nothing_else_has(cfg):
    hit = risk.check_exit(_trade(stop=50.0, tp=200.0), 98.0, pd.Series([100.0, 100.0, 100.0, 98.0]), atr_pct_now=1.0, cfg=cfg)
    assert hit["exit_reason"] == "momentum_reversal"


def test_check_exit_always_returns_current_trailing_state_even_when_holding(cfg):
    hit = risk.check_exit(_trade(), 106.0, pd.Series([100.0, 102.0, 104.0, 106.0]), atr_pct_now=1.0, cfg=cfg)
    assert hit["exit_reason"] is None and hit["trail_active"] and hit["stop_price"] > 95.0


# ---------------------------------------------------------------- database wrapper
class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def all(self, sql, params=None):
        return self.rows


def test_recent_candles_returns_oldest_first():
    rows = [{"ts": pd.Timestamp("2026-01-01T00:30", tz="UTC"), "open": 3, "high": 3, "low": 3, "close": 3, "volume": 1},
            {"ts": pd.Timestamp("2026-01-01T00:15", tz="UTC"), "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
            {"ts": pd.Timestamp("2026-01-01T00:00", tz="UTC"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
    df = risk.recent_candles(_FakeDB(rows), "BTC", 3)
    assert list(df["close"]) == [1, 2, 3]


def test_recent_candles_empty_is_a_well_shaped_empty_frame():
    df = risk.recent_candles(_FakeDB([]), "BTC", 3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"] and len(df) == 0
