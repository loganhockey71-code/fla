"""The behaviours the project is built around, as pure logic (no database, no network):

  * a prediction only trades if the NEXT candle confirms it; if confirmation fails there is no trade
  * position size honours balance, volatility, liquidity, fees, spread, slippage (incl. the order's own impact) and maximum loss
  * the account is never risked blindly (exposure cap, drawdown halt, daily loss limit, settings clamped to safe ranges)
  * coins are never chosen because their price is low
  * news: independent agreement raises confidence, conflicting / opposing news blocks or shrinks the trade, nothing from the future is used
  * every prediction is graded using only candles AFTER the decision, and the lessons say why it was right or wrong
  * the project is paper-trading only
"""

import numpy as np
import pandas as pd
import pytest

from crypto_ai import config
from crypto_ai.scalp import backtest, costs, engine, features, learn, news, patterns
from crypto_ai.scalp.exit_policy import select_entries

TS = pd.Timestamp("2026-03-02 12:00", tz="UTC")


@pytest.fixture
def cfg():
    return dict(config.DEFAULT_SETTINGS)


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


# ---------------------------------------------------------------- next-candle confirmation
class TestConfirmation:
    REF, ATR = 100.0, 0.10        # decision candle closed at 100; one 1m ATR = 0.10% = 0.10

    def check(self, d, b, edge=0.2, thr=0.1, cfg=None):
        return engine.confirm_signal(d, self.REF, self.ATR, b, edge, thr, cfg or dict(config.DEFAULT_SETTINGS))

    def test_a_candle_that_agrees_confirms_a_long(self):
        r = self.check(1, bar(100.0, 100.08, 99.99, 100.06))
        assert r["ok"] and r["reason"] == "confirmed" and r["move_atr"] == pytest.approx(0.6)

    def test_a_candle_that_agrees_confirms_a_short(self):
        r = self.check(-1, bar(100.0, 100.01, 99.92, 99.94))
        assert r["ok"] and r["move_atr"] == pytest.approx(0.6)

    def test_a_candle_against_the_prediction_means_no_trade(self):
        assert self.check(1, bar(100.05, 100.06, 99.95, 99.97))["reason"] == "candle_against"
        assert self.check(-1, bar(99.95, 100.05, 99.94, 100.03))["reason"] == "candle_against"

    def test_a_green_candle_that_barely_moves_is_not_follow_through(self):
        r = self.check(1, bar(100.0, 100.01, 99.99, 100.01))                     # +0.1 ATR < 0.25
        assert not r["ok"] and r["reason"] == "no_follow_through"

    def test_a_candle_that_dumps_first_and_recovers_is_a_reversal_not_a_confirmation(self):
        r = self.check(1, bar(100.0, 100.05, 99.85, 100.04))                    # traded 1.5 ATR against the call before closing up
        assert not r["ok"] and r["reason"] == "reversal"

    def test_chasing_a_move_that_already_happened_is_refused(self):
        r = self.check(1, bar(100.0, 100.3, 99.99, 100.25))                     # already ran 2.5 ATR
        assert not r["ok"] and r["reason"] == "chased"

    def test_confirmation_needs_the_model_to_still_see_the_edge(self):
        r = self.check(1, bar(100.0, 100.08, 99.99, 100.06), edge=0.05, thr=0.1)
        assert not r["ok"] and r["reason"] == "edge_gone"
        assert not self.check(1, bar(100.0, 100.08, 99.99, 100.06), edge=float("nan"))["ok"]

    def test_unknown_volatility_never_confirms(self):
        assert engine.confirm_signal(1, 100.0, None, bar(100, 101, 99, 100.5), 1.0, 0.1, dict(config.DEFAULT_SETTINGS))["reason"] == "no_volatility_estimate"
        assert engine.confirm_signal(1, 100.0, float("nan"), bar(100, 101, 99, 100.5), 1.0, 0.1, dict(config.DEFAULT_SETTINGS))["ok"] is False

    def test_confirmation_is_on_by_default_and_cannot_be_switched_off_by_a_database_row(self):
        assert config.DEFAULT_SETTINGS["scalp_confirm_required"] is True
        assert config.merge_settings({"scalp_confirm_required": False})["scalp_confirm_required"] is True


# ---------------------------------------------------------------- the backtest uses the same confirmation
def synthetic_pred(n=400, drift=0.0, seed=1, vol=0.0004):
    """One coin's feature frame whose model shouts a strong LONG (edge 1.0) for 2 minutes out of every 8."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-03-02", periods=n, freq="1min", tz="UTC", name="ts")
    c = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o = np.r_[c[0], c[:-1]]
    f = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0001, "low": np.minimum(o, c) * 0.9999, "close": c, "volume": 500.0}, index=idx)
    f["atr_1m_pct"], f["vol_ratio"], f["efficiency_30m"] = 0.04, 1.0, 0.2
    f["dollar_vol_15m"] = (f["close"] * f["volume"]).rolling(15, min_periods=1).sum()
    f["edge_long"] = np.where(np.arange(n) % 8 < 2, 1.0, 0.0)               # the signal comes and goes, so the arming latch re-arms and many signals form
    f["edge_short"] = -1.0
    return f


def replay_cfg(**kw):
    return {**config.DEFAULT_SETTINGS, "trading_fee_pct": 0.02, "slippage_pct": 0.005, "scalp_min_edge_pct": 0.01, "scalp_max_exposure_pct": 100.0, **kw}


def test_backtest_without_confirmation_trades_the_signal_and_with_confirmation_it_needs_agreement():
    pred = {"BTC": synthetic_pred(), "ETH": synthetic_pred(seed=2), "XRP": synthetic_pred(seed=3)}
    off = backtest.replay({}, pred, 0.01, replay_cfg(scalp_confirm_required=False))
    on = backtest.replay({}, pred, 0.01, replay_cfg(scalp_confirm_required=True))
    assert len(off["trades"]) > 3
    assert on["rejects"].get("signals", 0) > 0 and on["rejects"].get("confirmed", 0) > 0
    # every signal either confirmed or was refused for a NAMED reason: nothing silently disappears
    refused = sum(v for k, v in on["rejects"].items() if k.startswith("confirm_"))
    assert on["rejects"]["confirmed"] + refused <= on["rejects"]["signals"]
    assert refused > 0, "on random walk data some confirmations must fail"


def test_a_market_that_always_moves_against_the_prediction_never_trades_when_confirmation_is_required():
    n = 300
    idx = pd.date_range("2026-03-02", periods=n, freq="1min", tz="UTC", name="ts")
    c = 100 * np.exp(-0.0005 * np.arange(n))                                 # steady decline, every candle red
    o = np.r_[c[0], c[:-1]]
    frame = pd.DataFrame({"open": o, "high": o * 1.00005, "low": c * 0.99995, "close": c, "volume": 500.0}, index=idx)
    frame["atr_1m_pct"], frame["vol_ratio"], frame["efficiency_30m"] = 0.05, 1.0, 0.9
    frame["dollar_vol_15m"] = 1e6
    frame["edge_long"], frame["edge_short"] = 1.0, -1.0                       # the model keeps shouting LONG into a falling market
    pred = {"BTC": frame, "ETH": frame.copy(), "XRP": frame.copy()}
    res = backtest.replay({}, pred, 0.01, replay_cfg(scalp_confirm_required=True))
    assert res["trades"] == []
    assert res["rejects"].get("confirm_candle_against", 0) > 0
    forced = backtest.replay({}, pred, 0.01, replay_cfg(scalp_confirm_required=False))
    assert len(forced["trades"]) > 0                                         # without the filter it would have bought the falling knife


def test_confirmed_entries_are_filled_one_candle_after_the_signal_never_at_the_signal_price():
    pred = {"BTC": synthetic_pred(n=500, drift=0.0003), "ETH": synthetic_pred(n=500, drift=0.0003, seed=2), "XRP": synthetic_pred(n=500, drift=0.0003, seed=3)}
    res = backtest.replay({}, pred, 0.01, replay_cfg(scalp_confirm_required=True))
    assert res["trades"], "a rising market with a long signal must produce confirmed trades"
    p = pred["BTC"]
    for t in [t for t in res["trades"] if t["symbol"] == "BTC"]:
        i = p.index.get_loc(t["entry_ts"])
        assert i >= 2                                                         # signal row i-2, confirmation candle i-1, fill = open of row i
        assert t["entry_mid"] == pytest.approx(p["open"].iloc[i])


def test_exit_policy_learns_from_the_same_confirmed_entries_live_would_make():
    p = synthetic_pred(n=600, drift=0.0003)
    base = {**config.DEFAULT_SETTINGS, "scalp_min_edge_pct": 0.01}
    unconfirmed = select_entries(p, 0.01, {**base, "scalp_confirm_required": False}, p.index[0], p.index[-1])
    confirmed = select_entries(p, 0.01, {**base, "scalp_confirm_required": True}, p.index[0], p.index[-1])
    assert confirmed and unconfirmed
    for ts, d, edge in confirmed:                                              # each is the confirmation row: the previous candle was a green follow-through
        i = p.index.get_loc(ts)
        assert p["close"].iloc[i] > p["open"].iloc[i] and d == 1


# ---------------------------------------------------------------- risk: size honours every constraint
def entry_ctx(**kw):
    return {"has_position": False, "open_count": 0, "equity": 10_000.0, "free_cash": 10_000.0, "day_pnl_pct": 0.0, **kw}


def good_row(**kw):
    return {"atr_1m_pct": 0.08, "vol_ratio": 1.0, "efficiency_30m": 0.2, "ret_1m": 0.01, "ret_5m": 0.02, "ret_15m": 0.03, "dollar_vol_15m": 5e6, **kw}


def decide(cfg, row=None, ctx=None, spread=0.01, edges=None, state=None, **kw):
    state = state or engine.new_gate_state()
    return engine.entry_decision("BTC", row or good_row(), edges or {1: 0.5, -1: -0.5}, 100.0, spread, TS, state, ctx or entry_ctx(), cfg, None, 0.0, **kw)


@pytest.fixture
def cheap(cfg):
    return {**cfg, "trading_fee_pct": 0.02, "slippage_pct": 0.005, "scalp_min_edge_pct": 0.01}


class TestRiskSizing:
    def test_a_full_stop_out_never_loses_more_than_the_risk_setting_costs_included(self, cheap):
        rng = np.random.default_rng(0)
        for _ in range(300):
            c = {**cheap, "scalp_risk_pct": float(rng.uniform(0.1, 2.0)), "scalp_position_pct": float(rng.uniform(5, 50)),
                 "slippage_pct": float(rng.uniform(0, 0.3)), "trading_fee_pct": float(rng.uniform(0, 0.5))}
            equity, stop = float(rng.uniform(500, 1e6)), float(rng.uniform(0.05, 1.5))
            liq, unit, spread = float(rng.uniform(1e5, 1e8)), stop, float(rng.uniform(0, 0.1))
            usd = engine.size_position(equity, equity, stop, c, spread, liq_usd=liq, unit_pct=unit)
            imp = costs.impact_pct(usd, liq, unit, c)
            worst_loss = usd * (stop + costs.round_trip_cost_pct(c, spread) + 2 * imp) / 100
            assert worst_loss <= equity * c["scalp_risk_pct"] / 100 * (1 + 1e-9)

    def test_size_is_capped_by_free_cash_position_pct_liquidity_and_exposure_room(self, cheap):
        c = {**cheap, "scalp_risk_pct": 2.0, "scalp_position_pct": 50.0, "scalp_max_participation_pct": 1.0}
        big = dict(stop_dist_pct=0.05, cfg=c)
        assert engine.size_position(10_000, 300, **big) == pytest.approx(300)                                    # cash
        assert engine.size_position(10_000, 10_000, **big, liq_usd=1e9) <= 5_000 + 1e-6                          # 50% of equity
        assert engine.size_position(10_000, 10_000, **big, liq_usd=200_000) <= 2_000 + 1e-6                      # 1% of 15-min dollar volume
        assert engine.size_position(10_000, 10_000, **big, liq_usd=1e9, exposure_room=750) <= 750 + 1e-6         # what is left of the exposure budget
        assert engine.size_position(0, 0, **big) == 0.0 and engine.size_position(10_000, 10_000, **big, exposure_room=-5) == 0.0

    def test_large_accounts_can_use_large_positions_but_never_more_than_the_caps_allow(self, cheap):
        usd = engine.size_position(1_000_000, 1_000_000, 0.4, cheap, 0.01, liq_usd=5e7, unit_pct=0.4)
        assert usd >= 100_000                                                    # a million-dollar account is not forced into $10 trades
        assert usd <= 1_000_000 * cheap["scalp_position_pct"] / 100

    def test_a_bigger_order_pays_more_slippage(self, cheap):
        small, big = costs.impact_pct(1_000, 1e6, 0.4, cheap), costs.impact_pct(100_000, 1e6, 0.4, cheap)
        assert 0 < small < big
        assert costs.impact_pct(1_000, None, 0.4, cheap) == 0.0 and costs.impact_pct(0, 1e6, 0.4, cheap) == 0.0

    def test_the_fill_includes_the_impact_and_the_exit_charges_it_again(self, cheap):
        d = decide(cheap, row=good_row(dollar_vol_15m=2e5), ctx=entry_ctx(equity=100_000, free_cash=100_000))
        assert d["enter"] and d["impact_pct"] > 0
        pos = engine.open_position("BTC", d, 100.0, TS, good_row(), 0.01, cheap)
        assert pos.slip_pct == pytest.approx(cheap["slippage_pct"] + d["impact_pct"])
        flat_fill = costs.fill(100.0, 1, True, 0.01, cheap["slippage_pct"])
        assert pos.entry_fill > flat_fill
        tr = engine.close_position(pos, "time_stop", 100.0, TS + pd.Timedelta(minutes=5), 0.01, cheap)
        assert tr["exit_price"] == pytest.approx(costs.fill(100.0, 1, False, 0.01, pos.slip_pct))


class TestAccountProtection:
    def test_thin_coins_are_refused(self, cheap):
        r = decide(cheap, row=good_row(dollar_vol_15m=20_000))
        assert not r["enter"] and r["reason"] == "illiquid"

    def test_unknown_liquidity_is_refused_when_the_feed_should_have_it(self, cheap):
        assert decide(cheap, row=good_row(dollar_vol_15m=float("nan")))["reason"] == "liquidity_unknown"

    def test_total_exposure_is_capped(self, cheap):
        c = {**cheap, "scalp_max_exposure_pct": 40.0}
        r = decide(c, ctx=entry_ctx(open_notional=4_000.0))
        assert not r["enter"] and r["reason"] == "exposure_limit"
        ok = decide(c, ctx=entry_ctx(open_notional=1_000.0))
        assert ok["enter"] and ok["usd"] <= 3_000 + 1e-6

    def test_drawdown_from_the_recent_high_halts_new_entries(self, cheap):
        assert decide(cheap, ctx=entry_ctx(drawdown_pct=cheap["scalp_max_drawdown_pct"]))["reason"] == "drawdown_halt"
        assert decide(cheap, ctx=entry_ctx(drawdown_pct=cheap["scalp_max_drawdown_pct"] - 0.1))["enter"]

    def test_daily_loss_limit_and_max_positions_still_hold(self, cheap):
        assert decide(cheap, ctx=entry_ctx(day_pnl_pct=-cheap["scalp_daily_loss_limit_pct"]))["reason"] == "daily_loss_limit"
        assert decide(cheap, ctx=entry_ctx(open_count=cheap["scalp_max_positions"]))["reason"] == "max_positions"

    def test_a_wide_spread_blocks_and_a_target_that_does_not_clear_costs_is_rejected(self, cheap):
        assert decide(cheap, spread=cheap["scalp_max_spread_pct"] + 0.01)["reason"] == "spread_too_wide"
        costly = {**cheap, "trading_fee_pct": 0.6}                                  # round trip 1.2%+ vs a ~0.3% target
        assert decide(costly)["reason"] == "cost_gate"

    def test_settings_are_clamped_to_safe_ranges(self):
        out = config.merge_settings({"scalp_position_pct": 500, "scalp_risk_pct": 100, "trading_fee_pct": -3, "scalp_max_positions": 99,
                                     "scalp_max_exposure_pct": 1000, "scalp_max_drawdown_pct": 0, "slippage_pct": float("nan"),
                                     "starting_balance": -5, "scalp_min_liquidity_usd": -1})
        assert out["scalp_position_pct"] == 50.0 and out["scalp_risk_pct"] == 2.0 and out["trading_fee_pct"] == 0.0 and out["scalp_max_positions"] == 3
        assert out["scalp_max_exposure_pct"] == 100.0 and out["scalp_max_drawdown_pct"] == 1.0 and out["slippage_pct"] == config.DEFAULT_SETTINGS["slippage_pct"]
        assert out["starting_balance"] == 10.0 and out["scalp_min_liquidity_usd"] == 0.0

    def test_large_starting_balances_are_allowed(self):
        assert config.merge_settings({"starting_balance": "250000"})["starting_balance"] == 250000.0

    def test_defaults_never_risk_the_whole_account(self):
        c = config.DEFAULT_SETTINGS
        assert c["scalp_risk_pct"] <= 1.0 and c["scalp_max_exposure_pct"] < 100 and c["scalp_position_pct"] <= 50 and c["scalp_daily_loss_limit_pct"] <= 5
        assert c["scalp_max_drawdown_pct"] <= 15


# ---------------------------------------------------------------- opportunity, not price
def test_no_model_feature_depends_on_the_price_level():
    """A $0.01 coin and a $100,000 coin that move identically (in %) must look identical to the model."""
    rng = np.random.default_rng(4)
    n = 1800
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC", name="ts")
    frames = {}
    for s in ("BTC", "ETH", "XRP"):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
        o = np.r_[c[0], c[:-1]]
        frames[s] = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0003, "low": np.minimum(o, c) * 0.9997, "close": c, "volume": rng.uniform(50, 150, n)}, index=idx)
    scaled = {s: df.assign(open=df.open * 1e-4, high=df.high * 1e-4, low=df.low * 1e-4, close=df.close * 1e-4, volume=df.volume * 1e4) for s, df in frames.items()}
    a, b = features.compute_features(frames, "XRP"), features.compute_features(scaled, "XRP")
    pd.testing.assert_frame_equal(a[features.FEATURES], b[features.FEATURES], rtol=1e-9, atol=1e-9)


def test_candidates_are_opened_by_predicted_net_edge_then_liquidity_not_by_price_or_name():
    pred = {s: synthetic_pred(n=200, drift=0.0004, seed=i) for i, s in enumerate(("BTC", "ETH", "XRP"))}
    pred["XRP"]["edge_long"] = 0.9                                              # XRP has the biggest predicted net edge
    pred["BTC"]["edge_long"] = 0.2
    pred["ETH"]["edge_long"] = 0.5
    res = backtest.replay({}, pred, 0.01, replay_cfg(scalp_confirm_required=False, scalp_max_positions=1))
    first = min(res["trades"], key=lambda t: t["entry_ts"])
    assert first["symbol"] == "XRP"


# ---------------------------------------------------------------- news
def ev(impact, age_min=5, cred=95, conf=90, nov=100, fams=("primary",), imp=70, tier=1, title="x"):
    return {"detected_at": TS - pd.Timedelta(minutes=age_min), "impact": impact, "source_credibility_score": cred, "confidence_score": conf,
            "novelty_score": nov, "origin_tier": tier, "importance_score": imp, "title": title, "details": {"families": list(fams)}}


class TestNews:
    def test_no_news_is_neutral(self, cfg):
        c = news.context([], TS, cfg)
        assert c["bias"] == "none" and news.effect(c, 1, cfg)["allow"] and news.effect(c, 1, cfg)["size_mult"] == 1.0

    def test_agreeing_news_supports_the_trade_and_independent_groups_raise_confidence(self, cfg):
        one = news.context([ev(60)], TS, cfg)
        two = news.context([ev(60), ev(50, fams=("press",), tier=4, cred=55)], TS, cfg)
        assert one["bias"] == "bullish" and one["confirmations"] == 1 and two["confirmations"] == 2
        e1, e2 = news.effect(one, 1, cfg), news.effect(two, 1, cfg)
        assert e1["alignment"] == "supports" and 1.0 < e1["size_mult"] < e2["size_mult"] <= 1.25
        assert news.effect(one, -1, cfg)["alignment"] == "opposes"

    def test_five_copies_of_one_press_release_are_still_one_source_group(self, cfg):
        c = news.context([ev(30, fams=("press",), tier=4, cred=55) for _ in range(5)], TS, cfg)
        assert c["confirmations"] == 1

    def test_strong_opposing_news_blocks_and_weak_opposing_news_halves_the_size(self, cfg):
        strong = news.context([ev(-90)], TS, cfg)
        assert news.effect(strong, 1, cfg) == {"allow": False, "size_mult": 0.0, "reason": "news_opposes", "alignment": "opposes"}
        weak = news.context([ev(-45, cred=80, conf=80)], TS, cfg)
        assert weak["bias"] == "bearish" and abs(weak["net"]) < cfg["scalp_news_block_score"]
        e = news.effect(weak, 1, cfg)
        assert e["allow"] and e["size_mult"] == 0.5

    def test_conflicting_sources_mean_no_trade_either_way(self, cfg):
        c = news.context([ev(80, title="ETF approved"), ev(-75, title="SEC sues exchange", fams=("press",), tier=3, cred=70)], TS, cfg)
        assert c["bias"] == "conflict" and c["conflict"]
        assert not news.effect(c, 1, cfg)["allow"] and not news.effect(c, -1, cfg)["allow"]
        assert news.effect(c, 1, cfg)["reason"] == "news_conflict"

    def test_important_official_news_with_no_direction_halves_the_size(self, cfg):
        c = news.context([ev(0, imp=80, age_min=10)], TS, cfg)
        assert c["bias"] == "unclear" and news.effect(c, 1, cfg)["size_mult"] == 0.5 and news.effect(c, -1, cfg)["allow"]

    def test_old_news_fades_and_future_news_is_never_used(self, cfg):
        fresh, stale = news.context([ev(80, age_min=2)], TS, cfg), news.context([ev(80, age_min=60)], TS, cfg)
        assert stale["net"] < fresh["net"] * 0.5
        assert news.context([ev(80, age_min=cfg["scalp_news_window_min"] + 5)], TS, cfg)["bias"] == "none"
        future = {**ev(95), "detected_at": TS + pd.Timedelta(minutes=3)}                   # detected AFTER the decision: invisible (no look-ahead)
        assert news.context([future], TS, cfg)["n_events"] == 0

    def test_news_can_be_switched_off(self, cfg):
        c = news.context([ev(-95)], TS, cfg)
        assert news.effect(c, 1, {**cfg, "scalp_news_enabled": False})["allow"]

    def test_entry_decision_applies_the_news_effect(self, cheap):
        conflict = news.effects(news.context([ev(80), ev(-75, fams=("press",), tier=3)], TS, cheap), cheap)
        r = decide(cheap, ctx=entry_ctx(news=conflict))
        assert not r["enter"] and r["reason"] == "news_conflict"
        base = decide(cheap, ctx=entry_ctx(equity=100_000.0, free_cash=100_000.0))["usd"]
        half = decide(cheap, ctx=entry_ctx(equity=100_000.0, free_cash=100_000.0, news=news.effects(news.context([ev(0, imp=80)], TS, cheap), cheap)))["usd"]
        assert half == pytest.approx(base * 0.5, rel=0.02)
        boost = decide(cheap, ctx=entry_ctx(equity=100_000.0, free_cash=100_000.0,
                                            news=news.effects(news.context([ev(60), ev(50, fams=("press",), tier=4, cred=55)], TS, cheap), cheap)))["usd"]
        assert boost >= base and boost <= 100_000 * cheap["scalp_position_pct"] / 100 * 1.25 + 1e-6     # a boost can never exceed the risk cap either
        by_risk = 100_000 * cheap["scalp_risk_pct"] / 100 / ((0.08 * np.sqrt(cheap["scalp_hold_bars"]) * cheap["scalp_stop_mult"] + costs.round_trip_cost_pct(cheap, 0.01)) / 100)
        assert boost <= by_risk + 1e-6


# ---------------------------------------------------------------- patterns (study only)
def candles(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="1min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def test_candle_patterns_are_named_from_closed_candles():
    flat = [(10, 10.1, 9.9, 10.0, 1)] * 6
    hammer = candles(flat + [(10.0, 10.02, 9.7, 10.01, 1)])
    assert "hammer" in patterns.candle_patterns(hammer)
    star = candles(flat + [(10.0, 10.3, 9.99, 10.0, 1)])
    assert "shooting_star" in patterns.candle_patterns(star)
    engulf = candles(flat + [(10.1, 10.12, 9.95, 9.96, 1), (9.94, 10.25, 9.93, 10.2, 1)])
    assert "bullish_engulfing" in patterns.candle_patterns(engulf)
    brk = candles(flat + [(10.0, 10.5, 10.0, 10.45, 5)])
    t = patterns.candle_patterns(brk)
    assert "breakout_high" in t and "volume_spike" in t
    assert patterns.candle_patterns(candles(flat[:2])) == []


def test_patterns_do_not_look_ahead():
    a = candles([(10, 10.1, 9.9, 10.0, 1)] * 8)
    b = a.copy()
    assert patterns.candle_patterns(a.iloc[:6]) == patterns.candle_patterns(b.iloc[:6])


# ---------------------------------------------------------------- grading: every prediction, using only the future AFTER the decision
def sig(direction="long", status="failed", reason="candle_against", t=TS, edge=0.2, **kw):
    return {"symbol": "BTC", "direction": direction, "decision_ts": t, "status": status, "reason": reason, "pred_edge_pct": edge, "setup": "momentum",
            "regime": "normal_chop", "features": {}, "news": {}, **kw}


def window(start, closes, step=1):
    idx = pd.date_range(start, periods=len(closes), freq="1min", tz="UTC")
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]] if len(c) else c
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998, "close": c, "volume": 100.0}, index=idx)


class TestGrading:
    def test_not_graded_until_the_whole_hold_window_has_closed(self, cfg):
        c = window(TS + pd.Timedelta(minutes=1), np.linspace(100, 101, cfg["scalp_hold_bars"] - 1))
        assert learn.grade_signal(sig(), c, cfg) is None

    def test_a_correct_long_is_right_and_pays_when_the_move_beats_costs(self, cfg):
        c = window(TS + pd.Timedelta(minutes=1), np.linspace(100, 102, cfg["scalp_hold_bars"]))
        g = learn.grade_signal(sig("long"), c, cfg)
        assert g["outcome"] == "right" and g["profitable"] and g["fwd_gross_pct"] > 1.5 and g["fwd_mfe_pct"] >= g["fwd_gross_pct"]

    def test_a_wrong_short_is_wrong(self, cfg):
        c = window(TS + pd.Timedelta(minutes=1), np.linspace(100, 101, cfg["scalp_hold_bars"]))
        g = learn.grade_signal(sig("short"), c, cfg)
        assert g["outcome"] == "wrong" and not g["profitable"] and g["fwd_gross_pct"] < 0 and g["fwd_mae_pct"] < 0

    def test_right_direction_but_too_small_for_the_costs_is_a_cost_casualty(self, cfg):
        c = window(TS + pd.Timedelta(minutes=1), np.linspace(100, 100.2, cfg["scalp_hold_bars"]))
        g = learn.grade_signal(sig("long"), c, cfg)
        assert g["outcome"] == "right" and not g["profitable"]
        text, tags = learn.signal_lesson(sig("long", status="confirmed", reason="confirmed"), {**g, "hold": 15})
        assert "cost_casualty" in tags and "costs" in text

    def test_grade_uses_only_candles_after_the_decision_and_inside_the_window(self, cfg):
        H = cfg["scalp_hold_bars"]
        c = window(TS - pd.Timedelta(minutes=30), np.linspace(90, 100, 30).tolist() + [100] + np.linspace(100, 101, H).tolist() + [150, 150])
        base = learn.grade_signal(sig("long"), c, cfg)
        poisoned = c.copy()
        poisoned.loc[poisoned.index <= TS, ["open", "high", "low", "close"]] *= 3.0                          # rewrite the past ...
        poisoned.loc[poisoned.index > TS + pd.Timedelta(minutes=H), ["open", "high", "low", "close"]] *= 9.0   # ... and the far future
        assert learn.grade_signal(sig("long"), poisoned, cfg)["fwd_gross_pct"] == pytest.approx(base["fwd_gross_pct"])

    def test_lessons_explain_why_the_gate_helped_or_hurt(self, cfg):
        up = learn.grade_signal(sig("long"), window(TS + pd.Timedelta(minutes=1), np.linspace(100, 102, 15)), cfg)
        down = learn.grade_signal(sig("long"), window(TS + pd.Timedelta(minutes=1), np.linspace(100, 98.5, 15)), cfg)
        t_up, tags_up = learn.signal_lesson(sig("long", status="failed", reason="no_follow_through"), {**up, "hold": 15})
        assert "gate_missed_winner" in tags_up and "did NOT trade" in t_up
        t_dn, tags_dn = learn.signal_lesson(sig("long", status="failed", reason="reversal"), {**down, "hold": 15})
        assert "gate_saved_loss" in tags_dn and "filter correctly kept us out" in t_dn
        t_cw, tags_cw = learn.signal_lesson(sig("long", status="confirmed", reason="confirmed"), {**down, "hold": 15})
        assert "confirmed_but_wrong" in tags_cw
        n = {"bias": "bearish", "net": -55, "confirmations": 2}
        t_nw, tags_nw = learn.signal_lesson(sig("long", status="blocked", reason="news_opposes", news=n), {**down, "hold": 15})
        assert "news_opposed" in tags_nw and "2 independent source group" in t_nw

    def test_insights_say_whether_confirmation_is_helping(self, cfg):
        def g(status, outcome, net, n):
            return [{"symbol": "BTC", "direction": "long", "status": status, "outcome": outcome, "profitable": net > 0, "fwd_net_pct": net, "fwd_gross_pct": net + 1,
                     "decision_ts": TS + pd.Timedelta(minutes=i), "features": {}, "news": {}, "regime": "x", "setup": "momentum"} for i in range(n)]
        good = g("confirmed", "right", 0.3, 25) + g("confirmed", "wrong", -0.4, 15) + g("failed", "wrong", -0.5, 20) + g("failed", "right", 0.1, 10)
        text = " ".join(learn.signal_insights(good, cfg))
        assert "graded predictions" in text and "confirmation is helping" in text
        assert learn.signal_insights([], cfg) == []


# ---------------------------------------------------------------- paper trading only
def test_the_project_is_paper_only_and_refuses_a_real_trading_switch(monkeypatch):
    assert config.PAPER_ONLY is True
    monkeypatch.delenv("REAL_TRADING_ENABLED", raising=False)
    config.require_paper_only()
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="paper-trading only"):
        config.require_paper_only()
    assert "real_trading_enabled" not in config.DEFAULT_SETTINGS and "real_trading_enabled" not in config.USER_EDITABLE


def test_no_source_file_contains_exchange_order_or_wallet_code():
    from crypto_ai import selfcheck
    hits = selfcheck.scan_source_for_real_trading_code()
    assert hits == [], hits
    assert selfcheck.scan_for_http_writes() == []                              # only the documented read-only POST users (OpenRouter prompt, XRPL reads, Reddit token)
    assert selfcheck.HTTP_WRITE_ALLOWED == {"selfcheck.py", "llm.py", "base.py", "reddit.py"}      # widening this list is a deliberate, reviewed act
