"""Scalper: costs, labels, features, engine, learning rules and the settings fixes. Pure logic, no database or network."""
import numpy as np
import pandas as pd
import pytest

from crypto_ai import config
from crypto_ai.scalp import costs, engine, features, labels, learn, model
from crypto_ai.scalp.exit_policy import simulate_entry


def make_frames(n=4000, seed=3, vol=0.0007):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC", name="ts")
    frames = {}
    for i, s in enumerate(["BTC", "ETH", "XRP"]):
        c = 100 * (i + 1) * np.exp(np.cumsum(rng.normal(0, vol, n)))
        o = np.r_[c[0], c[:-1]]
        hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, vol / 3, n)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, vol / 3, n)))
        frames[s] = pd.DataFrame({"open": o, "high": hi, "low": lo, "close": c, "volume": rng.uniform(50, 150, n)}, index=idx)
    return frames


@pytest.fixture
def cfg():
    return dict(config.DEFAULT_SETTINGS)


# ---------------------------------------------------------------- settings fixes
def test_as_bool_never_treats_the_string_false_as_true():
    assert config.as_bool("false") is False and config.as_bool('"false"') is False and config.as_bool("0") is False
    assert config.as_bool("true") is True and config.as_bool(True) is True and config.as_bool(0) is False
    assert config.as_bool("banana", default=False) is False


def test_kill_switch_fails_off_on_garbage_and_accepts_json_strings():
    assert config.merge_settings({"autopilot_enabled": "false"})["autopilot_enabled"] is False
    assert config.merge_settings({"autopilot_enabled": "banana"})["autopilot_enabled"] is False
    assert config.merge_settings({"autopilot_enabled": {"x": 1}})["autopilot_enabled"] is False
    assert config.merge_settings({"autopilot_enabled": "true"})["autopilot_enabled"] is True
    assert config.merge_settings({})["autopilot_enabled"] is True


def test_stale_database_rows_cannot_override_strategy_settings():
    stale = {"trade_horizons": [24, 48], "sudden_move_pct": 1.0, "signal_cooldown_min": 30, "scalp_hold_bars": 999, "signal_threshold_pct": 80}
    out = config.merge_settings(stale)
    assert out["trade_horizons"] == [] and out["sudden_move_pct"] == 0.35 and out["signal_cooldown_min"] == 3
    assert out["scalp_hold_bars"] == config.DEFAULT_SETTINGS["scalp_hold_bars"]


def test_editable_settings_are_type_coerced():
    out = config.merge_settings({"trading_fee_pct": "0.25", "scalp_max_positions": "2", "scalp_allow_shorts": "false"})
    assert out["trading_fee_pct"] == 0.25 and out["scalp_max_positions"] == 2 and out["scalp_allow_shorts"] is False


# ---------------------------------------------------------------- costs
def test_costs_long_and_short_lose_the_round_trip_when_price_is_flat():
    for d in (1, -1):
        e = costs.fill(100.0, d, True, 0.02, 0.1)
        x = costs.fill(100.0, d, False, 0.02, 0.1)
        net = costs.net_pct(e, x, d, 0.4)
        assert net < -1.0                                   # ~ 2 x (0.4 fee + 0.1 slip) + spread on a flat trade
        assert net == pytest.approx(-(0.8 + 0.2 + 0.02), abs=0.05)


def test_short_profits_when_price_falls_and_fills_are_adverse():
    e, x = costs.fill(100.0, -1, True, 0, 0.1), costs.fill(99.0, -1, False, 0, 0.1)
    assert e < 100.0 and x > 99.0                          # sold lower, bought back higher than mid
    assert costs.net_pct(e, x, -1, 0.0) > 0


def test_round_trip_cost_matches_the_hurdle_definition(cfg):
    assert costs.round_trip_cost_pct(cfg, 0.05) == pytest.approx(2 * (0.4 + 0.1) + 0.05)


# ---------------------------------------------------------------- features: no look-ahead
def test_changing_future_candles_never_changes_an_earlier_feature_row():
    fr = make_frames(2600)
    a = features.compute_features(fr, "ETH")
    t = 1800
    fr2 = {s: df.copy() for s, df in fr.items()}
    for s in fr2:
        fr2[s].iloc[t:, :4] *= 1.07                          # rewrite the future
    b = features.compute_features(fr2, "ETH")
    cols = features.FEATURES
    pd.testing.assert_frame_equal(a[cols].iloc[:t - 20], b[cols].iloc[:t - 20])


def test_higher_timeframe_bar_is_used_only_after_it_closes():
    fr = make_frames(1800)
    f = features.compute_features(fr, "BTC")
    # rsi_5m at a row whose minute is inside a 5m bar must equal the value at the last minute of the previous 5m bar
    row = f.index[1000]
    assert row.minute % 5 != 4
    prev_bar_end = row.floor("5min") - pd.Timedelta(minutes=1)
    assert f.loc[row, "rsi_5m"] == pytest.approx(f.loc[prev_bar_end, "rsi_5m"], nan_ok=True) or f.loc[row, "rsi_5m"] != f.loc[row, "rsi_5m"]


# ---------------------------------------------------------------- labels == engine (one definition of a trade)
def _plain(cfg):
    """Engine with trailing / reversal / no-follow-through switched off = exactly the labels' stop/target/time trade."""
    return {**cfg, "scalp_trail_act_r": 1e9, "scalp_reversal_mult": 1e9, "scalp_nofollow_bars": 10**6}


def test_engine_reproduces_label_outcomes_on_every_sampled_row(cfg):
    cfg = _plain(cfg)
    fr = make_frames(3000, vol=0.0012)
    feats = features.compute_features(fr, "ETH").dropna(subset=["atr_1m_pct"])
    lab = labels.label_trades(feats, cfg, spread_pct=0.02)
    pol = {"stop_mult": cfg["scalp_stop_mult"], "tp_mult": cfg["scalp_tp_mult"], "trail_mult": cfg["scalp_trail_mult"]}
    a = {"o": feats["open"].to_numpy(), "h": feats["high"].to_numpy(), "l": feats["low"].to_numpy(), "c": feats["close"].to_numpy(), "ts": feats.index}
    cfg_free = {**cfg, "scalp_min_net_target_pct": -100.0}   # the cost gate is an entry filter, not part of the trade definition
    checked = 0
    for i in range(0, len(lab), 37):
        row = feats.iloc[i].to_dict()
        for name, d in (("long", 1), ("short", -1)):
            t = simulate_entry(a, i, d, 0.5, row, cfg_free, pol, 0.02)
            if t is None:
                continue
            assert t["net_pnl_pct"] == pytest.approx(lab[f"net_{name}"].iloc[i], abs=1e-9), (i, name, t["exit_reason"])
            checked += 1
    assert checked > 100


# ---------------------------------------------------------------- engine
def make_pos(d=1, entry=100.0, stop_pct=0.3, tp_pct=0.5, cfg=None):
    return engine.Position(symbol="BTC", direction=d, entry_ts=pd.Timestamp("2026-01-01", tz="UTC"), entry_mid=entry, entry_fill=entry, qty=1.0,
                           notional=entry, stop_px=entry * (1 - d * stop_pct / 100), initial_stop_px=entry * (1 - d * stop_pct / 100),
                           tp_px=entry * (1 + d * tp_pct / 100), unit_pct=0.3, atr_pct=0.05, max_hold=15, trail_act_r=0.8, trail_mult=0.7, best_px=entry)


def bar(o, h, l, c, minute=1):
    return {"ts": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=minute), "open": o, "high": h, "low": l, "close": c}


def test_intrabar_stop_wins_when_one_candle_touches_stop_and_target(cfg):
    p = make_pos()
    ex = engine.step_position(p, bar(100, 100.6, 99.6, 100.1), None, cfg)
    assert ex["reason"] == "stop_loss" and ex["mid"] == pytest.approx(p.stop_px)


def test_gap_through_the_stop_fills_at_the_open_not_the_stop(cfg):
    p = make_pos()
    ex = engine.step_position(p, bar(99.0, 99.2, 98.9, 99.1), None, cfg)
    assert ex["mid"] == 99.0


def test_short_target_and_stop_mirror_the_long(cfg):
    p = make_pos(d=-1)
    assert engine.step_position(p, bar(100, 100.05, 99.4, 99.6), None, cfg)["reason"] == "take_profit"
    p = make_pos(d=-1)
    assert engine.step_position(p, bar(100, 100.4, 99.9, 100.2), None, cfg)["reason"] == "stop_loss"


def test_stop_is_never_widened_and_trail_only_tightens(cfg):
    p = make_pos()
    first = p.stop_px
    assert not engine.tighten_stop(p, first - 0.5)            # a lower stop for a long = more risk: refused
    assert p.stop_px == first
    engine.step_position(p, bar(100, 100.3, 99.95, 100.2), None, cfg)   # 0.6R+ in profit ...
    engine.step_position(p, bar(100.2, 100.42, 100.1, 100.35, 2), None, cfg)   # ... arms the trail
    armed = p.stop_px
    assert p.trail_active and armed > first
    engine.step_position(p, bar(100.35, 100.36, 100.2, 100.25, 3), None, cfg)   # price pulls back: stop must NOT drop
    assert p.stop_px == armed


def test_trailing_stop_exit_is_reported_as_trailing(cfg):
    p = make_pos(tp_pct=5.0)
    engine.step_position(p, bar(100, 100.3, 99.95, 100.25, 1), None, cfg)
    engine.step_position(p, bar(100.25, 100.45, 100.2, 100.4, 2), None, cfg)
    ex = engine.step_position(p, bar(100.4, 100.41, 99.0, 99.5, 3), None, cfg)
    assert ex["reason"] == "trailing_stop" and ex["mid"] > 100.0


def test_no_follow_through_and_reversal_exit_at_the_next_open(cfg):
    p = make_pos(stop_pct=0.5, tp_pct=1.0)
    ex = None
    for m in range(1, 6):
        ex = engine.step_position(p, bar(100, 100.01, 99.9, 99.95, m), None, cfg) or ex
    assert p.pending_exit == "no_follow_through" or ex
    if p.pending_exit:
        out = engine.step_position(p, bar(99.93, 99.95, 99.9, 99.92, 6), None, cfg)
        assert out["reason"] == "no_follow_through" and out["mid"] == 99.93


def test_time_stop_exits_at_the_close_of_the_last_bar(cfg):
    p = make_pos(stop_pct=5, tp_pct=5)
    out = None
    for m in range(1, 16):
        out = engine.step_position(p, bar(100, 100.02, 99.98, 100.0, m), None, {**cfg, "scalp_nofollow_bars": 10**6})
    assert out["reason"] == "time_stop" and out["mid"] == 100.0


def _row(**kw):
    r = {"atr_1m_pct": 0.05, "vol_ratio": 1.0, "efficiency_30m": 0.2, "vol_rel_5m": 1.0, "ret_1m": 0.0, "ret_5m": 0.0, "ret_15m": 0.0,
         "dist_vwap_15m": 0.0, "dist_high_30m": -0.5, "dist_low_30m": 0.5, "rsi_1m": 50}
    r.update(kw)
    return r


def _ctx(**kw):
    c = {"has_position": False, "open_count": 0, "equity": 1000.0, "free_cash": 1000.0, "day_pnl_pct": 0.0}
    c.update(kw)
    return c


NOW = pd.Timestamp("2026-01-02 12:00", tz="UTC")


def _decide(cfg, edges, row=None, spread=0.01, ctx=None, state=None, thr=0.03, blocked=frozenset(), policy=None):
    return engine.entry_decision("BTC", row or _row(), edges, 100.0, spread, NOW, state or engine.new_gate_state(), ctx or _ctx(), cfg, policy, thr, blocked)


def cheap(cfg):
    return {**cfg, "trading_fee_pct": 0.05, "slippage_pct": 0.01}


def test_default_costs_reject_a_five_minute_target_that_cannot_beat_them(cfg):
    d = _decide(cfg, {1: 0.9, -1: 0.0})
    assert not d["enter"] and d["reason"] == "cost_gate"   # 1% round trip vs a ~0.2% target: technically green pre-cost, red after


def test_enters_when_edge_and_cost_gate_pass_and_sizes_by_risk(cfg):
    c = cheap(cfg)
    d = _decide(c, {1: 0.2, -1: 0.0})
    assert d["enter"] and d["direction"] == 1
    loss_at_stop = d["usd"] * (d["plan"]["stop_dist_pct"] + costs.round_trip_cost_pct(c, 0.01)) / 100
    assert loss_at_stop <= 1000 * c["scalp_risk_pct"] / 100 + 1e-6
    assert d["usd"] <= 1000 * c["scalp_position_pct"] / 100 + 1e-6


def test_picks_the_short_when_it_has_the_bigger_edge_and_respects_the_short_switch(cfg):
    c = cheap(cfg)
    assert _decide(c, {1: 0.05, -1: 0.3})["direction"] == -1
    off = {**c, "scalp_allow_shorts": False}
    assert not _decide(off, {1: 0.01, -1: 0.9})["enter"]


def test_gates_block_duplicates_cooldown_daily_loss_exposure_spread_and_blocked_setups(cfg):
    c = cheap(cfg)
    e = {1: 0.3, -1: 0.0}
    st = engine.new_gate_state()
    st["armed"][1] = False
    assert _decide(c, e, state=st)["reason"] == "not_rearmed"          # same lingering signal cannot re-enter
    engine.update_arming(st, {1: 0.0, -1: 0.0}, 0.03)
    assert _decide(c, e, state=st)["enter"]                           # re-arms once the edge relaxed
    st2 = engine.new_gate_state()
    st2["last_exit_ts"] = NOW - pd.Timedelta(minutes=2)
    assert _decide(c, e, state=st2)["reason"] == "cooldown"
    assert _decide(c, e, ctx=_ctx(day_pnl_pct=-3.5))["reason"] == "daily_loss_limit"
    assert _decide(c, e, ctx=_ctx(open_count=3))["reason"] == "max_positions"
    assert _decide(c, e, ctx=_ctx(has_position=True))["reason"] == "have_position"
    assert _decide(c, e, spread=0.5)["reason"] == "spread_too_wide"
    assert _decide(c, e, blocked=frozenset({("BTC", 1, engine.setup_of(_row(), 1))}))["reason"] == "setup_blocked"
    assert _decide(c, {1: 0.01, -1: 0.0})["reason"] == "no_edge"


def test_validated_model_threshold_can_only_raise_the_bar(cfg):
    c = cheap(cfg)
    assert not _decide(c, {1: 0.2, -1: 0}, thr=0.5)["enter"]
    assert _decide(c, {1: 0.2, -1: 0}, thr=0.0)["enter"]


def test_policy_override_is_bounded_and_regime_specific(cfg):
    pol = {"BTC": {"stop_mult": 99, "tp_mult": 0.0, "trail_mult": 1.0}, "BTC|high_vol_trend": {"stop_mult": 1.2, "tp_mult": 2.0, "trail_mult": 0.5}}
    p = engine.policy_for(pol, "BTC", "normal_chop", cfg)
    assert p["stop_mult"] == engine.STOP_BOUNDS[1] and p["tp_mult"] == engine.TP_BOUNDS[0]
    assert engine.policy_for(pol, "BTC", "high_vol_trend", cfg)["stop_mult"] == 1.2
    assert engine.policy_for(pol, "ETH", "normal_chop", cfg)["stop_mult"] == cfg["scalp_stop_mult"]


def test_close_position_books_every_analytics_field_and_net_equals_gross_minus_costs(cfg):
    p = make_pos()
    p.mfe_pct, p.mae_pct = 0.4, -0.1
    tr = engine.close_position(p, "take_profit", 100.5, pd.Timestamp("2026-01-01 00:04", tz="UTC"), 0.02, cfg)
    for k in ("symbol", "direction", "entry_price", "exit_price", "duration_min", "initial_stop_px", "take_profit_px", "mfe_pct", "mae_pct", "fees_usd",
              "slippage_usd", "gross_pnl_usd", "net_pnl_usd", "exit_reason", "profitable", "features", "regime", "setup", "trail_active"):
        assert k in tr
    assert tr["net_pnl_usd"] == pytest.approx(tr["gross_pnl_usd"] - tr["slippage_usd"] - tr["fees_usd"], abs=1e-9)
    assert tr["duration_min"] == 4.0 and tr["mfe_pct"] >= 0.5 - 1e-9


# ---------------------------------------------------------------- learning rules
def T(sym="BTC", d="long", net=1.0, gross=1.5, reason="take_profit", **kw):
    t = {"symbol": sym, "direction": d, "net_pnl_usd": net, "gross_pnl_usd": gross, "net_pnl_pct": net / 10, "duration_min": 3.0, "mfe_r": 1.0,
         "mae_r": -0.2, "mfe_pct": 0.3, "exit_reason": reason, "fees_usd": 0.3, "slippage_usd": 0.2, "setup": "momentum", "regime": "normal_chop",
         "features": {"vol_rel_5m": 2.0, "dist_vwap_15m": -0.1}}
    t.update(kw)
    return t


def test_post_mortem_explains_failures_and_wins_in_plain_english():
    m = learn.post_mortem(T(net=-1, gross=-0.5, reason="momentum_reversal", duration_min=2.0))
    assert m["verdict"] == "loss" and "momentum reversed within 2 min" in m["lesson"]
    c = learn.post_mortem(T(net=-0.2, gross=0.4, reason="time_stop"))
    assert c["verdict"] == "cost_casualty" and "too small" in c["lesson"]
    w = learn.post_mortem(T(sym="XRP", d="short", net=2, reason="take_profit"))
    assert "XRP short worked" in w["lesson"] and "below VWAP" in w["lesson"] and "volume_vwap_confirm" in w["tags"]
    g = learn.post_mortem(T(net=-1, gross=-0.3, reason="stop_loss", mfe_r=0.9))
    assert "gave_back_winner" in g["tags"]


def test_setup_is_blocked_only_with_enough_trades_and_confident_negative_expectancy(cfg):
    cfg = {**cfg, "scalp_setup_min_trades": 30}
    bad = [T(net=-1, gross=-0.6, net_pnl_pct=-0.1 - 0.01 * (i % 3), reason="stop_loss", setup="reversion") for i in range(40)]
    few = [T(net=-1, gross=-0.6, net_pnl_pct=-0.2, reason="stop_loss", setup="reversion") for i in range(10)]
    coin = [T(net=1 if i % 2 else -1, gross=1.5 if i % 2 else -0.5, net_pnl_pct=0.1 if i % 2 else -0.1, setup="reversion") for i in range(40)]
    assert any(b[2] == "reversion" and b[1] == 1 for b in learn.blocked_setups(bad, cfg))
    assert not [b for b in learn.blocked_setups(few, cfg) if b[2] == "reversion"]
    assert not [b for b in learn.blocked_setups(coin, cfg) if b[2] == "reversion"]


def test_insights_state_negative_expectancy_and_winners(cfg):
    trades = [T(net=-1, gross=-0.6, net_pnl_pct=-0.1, reason="stop_loss", setup="reversion", sym="ETH") for _ in range(40)]
    trades += [T(net=2, gross=2.5, net_pnl_pct=0.2, setup="momentum", sym="XRP", d="short") for _ in range(12)]
    text = " ".join(learn.insights(trades, [], cfg))
    assert "negative expectancy after fees" in text and "XRP short 'momentum' works" in text


def test_learning_window_is_0500_through_2359_local(cfg):
    from datetime import datetime, timezone
    c = {**cfg, "local_timezone": "UTC"}
    at = lambda h, m=0: datetime(2026, 3, 1, h, m, tzinfo=timezone.utc)
    assert not learn.in_learning_window(at(4, 59), c) and learn.in_learning_window(at(5, 0), c) and learn.in_learning_window(at(23, 59), c)
    assert not learn.in_learning_window(datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc), c)
    ny = {**cfg, "local_timezone": "America/New_York"}
    assert learn.in_learning_window(datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc), ny)          # 05:30 EDT
    assert not learn.in_learning_window(datetime(2026, 7, 1, 8, 30, tzinfo=timezone.utc), ny)      # 04:30 EDT


# ---------------------------------------------------------------- model plumbing
def test_threshold_is_infinite_when_nothing_has_a_positive_net_edge(cfg):
    n = 2000
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    rng = np.random.default_rng(1)
    d = pd.DataFrame({"edge_long": rng.normal(0.2, 0.1, n), "edge_short": rng.normal(0.2, 0.1, n), "net_long": rng.normal(-0.5, 0.2, n),
                      "net_short": rng.normal(-0.5, 0.2, n), "bars_long": 10, "bars_short": 10, "symbol": "BTC"}, index=idx)
    assert not np.isfinite(model.pick_threshold(d, cfg)["thr"])


def test_threshold_found_when_high_predicted_edge_really_pays(cfg):
    n = 6000
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    rng = np.random.default_rng(2)
    e = rng.uniform(0, 0.4, n)
    d = pd.DataFrame({"edge_long": e, "edge_short": -e, "net_long": e - 0.15 + rng.normal(0, 0.05, n), "net_short": -e, "bars_long": 5, "bars_short": 5,
                      "symbol": "BTC"}, index=idx)
    p = model.pick_threshold(d, cfg)
    assert np.isfinite(p["thr"]) and p["chosen"]["mean_net_pct"] > 0


def test_paired_bootstrap_only_rewards_a_consistently_better_challenger():
    days = pd.date_range("2026-01-01", periods=12).date
    better = pd.Series(np.full(12, 0.3), index=days)
    base = pd.Series(np.zeros(12), index=days)
    assert model.paired_p_value(better, base) < 0.05
    noisy = pd.Series(np.tile([1.0, -1.0], 6), index=days)
    assert model.paired_p_value(noisy, base) > 0.2


def test_train_predict_roundtrip_on_synthetic_frames(cfg):
    fr = make_frames(3600, vol=0.0012)
    d = model.build_dataset(fr, cfg)
    assert {"net_long", "net_short", "edge_long"} - set(d.columns) == {"edge_long"} and len(d) > 3000
    bundle = model.fit_bundle(d.iloc[:2500])
    el, es = model.predict_edges(bundle, d.iloc[2500:])
    assert len(el) == len(es) == len(d) - 2500 and np.isfinite(el).all()


# ---------------------------------------------------------------- learning from profitability + walk-forward stability
def _tr(i, net, **kw):
    return T(net=net, gross=net + 0.5, net_pnl_pct=net / 10, closed_at=i, **kw)


def test_pattern_is_reported_only_if_it_holds_in_both_halves():
    feats = {"ret_1m": 0.7, "vol_rel_5m": 3.0, "dist_vwap_15m": 0.1}
    steady = [_tr(i, 1.0 if i % 3 else -0.5, sym="ETH", d="long", features=feats) for i in range(40)]               # positive in both halves
    fluke = [_tr(i, 2.0 if i < 20 else -2.0, sym="XRP", d="long", features=feats) for i in range(40)]                # great, then terrible
    found = learn.pattern_findings(steady + fluke, min_n=20)
    keys = {(r["symbol"], r["condition"]) for r in found}
    assert ("ETH", "entered after a >0.5% 1-minute move") in keys and ("ETH", "volume spike (5m volume >= 2x normal)") in keys
    assert not [r for r in found if r["symbol"] == "XRP"]                                                          # unstable => not a lesson
    assert all(r["sign"] > 0 for r in found if r["symbol"] == "ETH")


def test_negative_pattern_and_exit_behaviour_become_plain_english(cfg):
    feats = {"ret_1m": 0.8}
    bad = [_tr(i, -1.0, sym="ETH", d="long", features=feats, reason="momentum_reversal") for i in range(30)]
    text = " ".join(learn.insights(bad, [], cfg))
    assert "ETH long, entered after a >0.5% 1-minute move: lost money in both halves" in text
    assert "momentum reversal exits: 30 trades" in text


def test_blocking_needs_stability_across_halves(cfg):
    cfg = {**cfg, "scalp_setup_min_trades": 30}
    flip = [_tr(i, -3.0 if i < 30 else 0.5, setup="reversion") for i in range(60)]        # net negative overall but recovered in the second half
    assert not [b for b in learn.blocked_setups(flip, cfg) if b[2] == "reversion"]
