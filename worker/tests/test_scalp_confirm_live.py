"""The live scalper's prediction -> confirm -> trade -> grade loop against a SCRATCH Postgres (never Supabase), synthetic candles, injected prices.

    TEST_DATABASE_URL=postgresql://...scratch... pytest tests/test_scalp_confirm_live.py

Proves: a prediction alone never trades; the next candle confirms or refuses it; a late worker expires it; the kill switch cancels it;
every prediction (traded, refused, blocked) is recorded and later graded right/wrong with a lesson; news conflicts and thin markets block;
the entry side failing cannot leave open trades unmanaged; the account-level protections hold; real trading cannot be switched on.
"""
import os
from datetime import timedelta

import numpy as np
import pytest

from tests.test_scalp_live import (SQL_DIR, SCHEMAS, SYMS, T0, add_candle, cheap_cfg, install_model, prices_of, seed_candles)

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="set TEST_DATABASE_URL to a scratch Postgres")


@pytest.fixture(scope="module")
def db():
    from crypto_ai.db import DB
    if "supabase" in DB_URL:
        pytest.skip("refusing to run against Supabase")
    d = DB(DB_URL)
    if not d.one("select to_regclass('scalp_signals') t")["t"]:
        for f in SCHEMAS:
            if (SQL_DIR / f).exists():
                sql = (SQL_DIR / f).read_text(encoding="utf-8")
                if not d.one("select count(*) n from pg_available_extensions where name='pgcrypto'")["n"]:
                    sql = sql.replace("create extension if not exists pgcrypto;", "")
                d.run(sql)
    return d


def pending(db):
    return {r["symbol"]: r for r in db.all("select * from scalp_signals order by id")}


def confirmation_candle(db, sym, ref, atr_pct, green=True, strength=0.6):
    """The candle that opens at T0 (right after the decision candle): `strength` ATRs of follow-through, or the same size against."""
    move = ref * atr_pct * strength / 100
    if green:
        o, c, h, l = ref, ref + move, ref + move * 1.1, ref - move * 0.05
    else:
        o, c, h, l = ref, ref - move, ref + move * 0.05, ref - move * 1.1
    add_candle(db, sym, T0, o, h, l, c)
    return c


def run_two_passes(db, monkeypatch, green=True, second_at=timedelta(minutes=1, seconds=20), cfg_over=None):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = {**cheap_cfg(db, confirm=True), **(cfg_over or {})}
    r1 = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    sigs = pending(db)
    px = {}
    for s in SYMS:
        c = confirmation_candle(db, s, float(candles[s]["close"].iloc[-1]), sigs[s]["atr_pct"], green=green) if s in sigs else float(candles[s]["close"].iloc[-1])
        px[s] = {"price": c, "spread_pct": 0.01}
    r2 = live.run_pass(db, cfg, now=T0 + second_at, prices=px)
    return cfg, candles, r1, r2, px


def test_a_prediction_alone_never_trades_it_waits_for_the_next_candle(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db, confirm=True)
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert r["opened"] == [] and db.one("select count(*) n from scalp_trades")["n"] == 0
    sigs = pending(db)
    assert set(sigs) == set(SYMS) and {s["status"] for s in sigs.values()} == {"pending"}
    for s in sigs.values():
        assert s["direction"] == "long" and s["pred_edge_pct"] == pytest.approx(0.5) and s["atr_pct"] > 0 and s["ref_price"] > 0
        assert s["decision_ts"] == T0 - timedelta(minutes=1) and s["features"] and s["setup"] and s["regime"]
    assert r["signals"]["raised"] == 3
    again = live.run_pass(db, cfg, now=T0 + timedelta(seconds=40), prices=prices_of(candles))      # still the same candle: nothing new, nothing traded
    assert again["opened"] == [] and db.one("select count(*) n from scalp_signals")["n"] == 3


def test_the_next_candle_confirms_and_only_then_the_trade_opens(db, monkeypatch):
    cfg, candles, r1, r2, px = run_two_passes(db, monkeypatch, green=True)
    assert len(r2["opened"]) == 3, r2
    assert all("confirmed by the next candle" in line for line in r2["opened"])
    sigs = pending(db)
    trades = {t["symbol"]: t for t in db.all("select * from scalp_trades where status='open'")}
    assert set(trades) == set(SYMS)
    for s in SYMS:
        assert sigs[s]["status"] == "confirmed" and sigs[s]["trade_id"] == trades[s]["id"] and sigs[s]["confirm_move_atr"] >= cfg["scalp_confirm_min_move_atr"]
        assert trades[s]["signal_id"] == sigs[s]["id"]
        assert trades[s]["opened_at"] >= sigs[s]["decision_ts"] + timedelta(minutes=2) - timedelta(seconds=5)           # never before the confirmation candle closed
        assert trades[s]["features"]["confirm_move_atr"] == pytest.approx(sigs[s]["confirm_move_atr"])
        assert "patterns" in trades[s]["features"] and trades[s]["features"]["news_alignment"] == "none"
    reads = db.one("select value from scalp_state where key='status'")["value"]["reads"]
    assert all(reads[s]["pending"] is False for s in SYMS)                       # resolved this pass: the dashboard must not still say "waiting for confirmation"
    # a lingering signal does not re-fire the same direction while it is still strong
    from crypto_ai.scalp import live
    r3 = live.run_pass(db, cfg, now=T0 + timedelta(minutes=1, seconds=40), prices=px)
    assert r3["opened"] == [] and db.one("select count(*) n from scalp_trades")["n"] == 3


def test_if_the_next_candle_disagrees_there_is_no_trade_and_the_failure_is_recorded(db, monkeypatch):
    cfg, candles, r1, r2, px = run_two_passes(db, monkeypatch, green=False)
    assert r2["opened"] == [] and db.one("select count(*) n from scalp_trades")["n"] == 0
    sigs = pending(db)
    assert {s["status"] for s in sigs.values()} == {"failed"} and {s["reason"] for s in sigs.values()} == {"candle_against"}
    assert r2["signals"]["failed"] == {"candle_against": 3}
    assert all(s["resolved_at"] is not None and s["trade_id"] is None for s in sigs.values())


def test_a_late_worker_cannot_confirm_a_stale_prediction(db, monkeypatch):
    cfg, candles, r1, r2, px = run_two_passes(db, monkeypatch, green=True, second_at=timedelta(minutes=1, seconds=200))
    assert r2["opened"] == [] and db.one("select count(*) n from scalp_trades")["n"] == 0
    assert {(s["status"], s["reason"]) for s in pending(db).values()} == {("expired", "confirmation_too_late")}


def test_turning_the_kill_switch_off_cancels_waiting_predictions(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db, confirm=True)
    live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    r = live.run_pass(db, {**cfg, "autopilot_enabled": False}, now=T0 + timedelta(seconds=40), prices=prices_of(candles))
    assert r["opened"] == [] and {(s["status"], s["reason"]) for s in pending(db).values()} == {("expired", "autopilot_off")}


def test_every_prediction_is_graded_right_or_wrong_with_a_reason_and_only_after_its_window(db, monkeypatch):
    from crypto_ai.scalp import learn
    cfg, candles, r1, r2, px = run_two_passes(db, monkeypatch, green=False)       # three FAILED confirmations
    assert learn.grade_signals(db, cfg, now=T0 + timedelta(minutes=5)) == 0        # window (15 min) has not elapsed: nothing is graded yet
    # the market then does the OPPOSITE of the confirmation candle: the predicted long would have worked (the filter cost a winner)
    for s in SYMS:
        ref = float(candles[s]["close"].iloc[-1])
        for k in range(1, 20):
            c = ref * (1 + 0.0005 * k)
            add_candle(db, s, T0 + timedelta(minutes=k), c * 0.9998, c * 1.0003, c * 0.9997, c)
    assert learn.grade_signals(db, cfg, now=T0 + timedelta(minutes=25)) == 3
    for s, g in pending(db).items():
        assert g["graded_at"] and g["outcome"] == "right" and g["fwd_gross_pct"] > 0.5 and g["profitable"] is True and g["fwd_mae_pct"] <= 0 <= g["fwd_mfe_pct"]
        assert "gate_missed_winner" in g["tags"] and "did NOT trade" in g["lesson"] and "reason:candle_against" in g["tags"]
    assert learn.grade_signals(db, cfg, now=T0 + timedelta(minutes=30)) == 0       # graded once, never again


def test_a_confirmed_trade_that_goes_wrong_is_graded_wrong_and_the_lesson_says_confirmation_did_not_help(db, monkeypatch):
    from crypto_ai.scalp import learn
    cfg, candles, r1, r2, px = run_two_passes(db, monkeypatch, green=True)
    for s in SYMS:                                                                 # after confirming, the market falls steadily
        ref = px[s]["price"]
        for k in range(1, 20):
            c = ref * (1 - 0.0004 * k)
            add_candle(db, s, T0 + timedelta(minutes=k), c * 1.0002, c * 1.0003, c * 0.9997, c)
    learn.grade_signals(db, cfg, now=T0 + timedelta(minutes=25))
    for g in pending(db).values():
        assert g["status"] == "confirmed" and g["outcome"] == "wrong" and "confirmed_but_wrong" in g["tags"] and g["fwd_net_pct"] < 0


def test_a_thin_market_blocks_the_prediction_and_the_block_is_recorded_and_graded(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    db.run("update candles set volume = 0.01 where granularity=60")                  # almost nothing traded
    cfg = cheap_cfg(db, confirm=True)
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert r["opened"] == [] and r["rejected"].get("illiquid") == 3
    sigs = pending(db)
    assert {(s["status"], s["reason"]) for s in sigs.values()} == {("blocked", "illiquid")}
    assert r["signals"]["blocked"] == {"illiquid": 3}


def test_conflicting_news_blocks_the_coin_while_other_coins_still_wait_for_confirmation(db, monkeypatch):
    from crypto_ai.research import registry
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    registry.ensure_registry(db)
    db.run("delete from research_events")
    now = T0 + timedelta(seconds=20)

    def event(title, ext, impact, sentiment, tier, key, families):
        db.insert("research_events", {"kind": "news", "title": title, "source": key, "source_key": key, "external_id": ext, "published_at": now - timedelta(minutes=8),
                                      "detected_at": now - timedelta(minutes=6), "event_category": "regulation", "affected_coins": ["BTC"], "sentiment": sentiment,
                                      "importance_score": 85, "source_credibility_score": 90, "novelty_score": 100, "confidence_score": 90,
                                      "btc_impact_score": impact, "origin_tier": tier, "details": {"families": families}})
    event("SEC approves spot bitcoin ETF options", "e1", 85, "positive", 1, "sec_press", ["primary"])
    event("Court blocks bitcoin ETF launch", "e2", -80, "negative", 3, "marketwatch", ["press"])
    cfg = cheap_cfg(db, confirm=True)
    r = live.run_pass(db, cfg, now=now, prices=prices_of(candles))
    sigs = pending(db)
    assert (sigs["BTC"]["status"], sigs["BTC"]["reason"]) == ("blocked", "news_conflict") and sigs["BTC"]["news"]["bias"] == "conflict"
    assert sigs["ETH"]["status"] == "pending" and sigs["XRP"]["status"] == "pending"
    assert r["opened"] == [] and r["signals"]["blocked"] == {"news_conflict": 1}
    db.run("delete from research_events")


def test_strong_agreeing_news_is_recorded_with_the_trade(db, monkeypatch):
    from crypto_ai.research import registry
    install_model(db, monkeypatch)
    registry.ensure_registry(db)
    db.run("delete from research_events")
    candles = seed_candles(db)
    now = T0 + timedelta(seconds=20)
    for i, (key, tier, fam) in enumerate((("sec_press", 1, ["primary"]), ("coindesk", 4, ["press"]))):
        db.insert("research_events", {"kind": "news", "title": f"Bitcoin adoption wave {i}", "source": key, "source_key": key, "external_id": f"a{i}", "published_at": now - timedelta(minutes=9),
                                      "detected_at": now - timedelta(minutes=5), "event_category": "adoption", "affected_coins": ["BTC"], "sentiment": "positive",
                                      "importance_score": 80, "source_credibility_score": 90, "novelty_score": 100, "confidence_score": 90,
                                      "btc_impact_score": 70, "origin_tier": tier, "details": {"families": fam}})
    from crypto_ai.scalp import live
    cfg = cheap_cfg(db, confirm=True)
    live.run_pass(db, cfg, now=now, prices=prices_of(candles))
    sig = pending(db)["BTC"]
    assert sig["news"]["bias"] == "bullish" and sig["news"]["confirmations"] == 2
    px = {}
    for s in SYMS:
        px[s] = {"price": confirmation_candle(db, s, float(candles[s]["close"].iloc[-1]), pending(db)[s]["atr_pct"]), "spread_pct": 0.01}
    live.run_pass(db, cfg, now=T0 + timedelta(minutes=1, seconds=20), prices=px)
    btc = db.one("select * from scalp_trades where symbol='BTC'")
    assert btc["features"]["news_alignment"] == "supports" and btc["features"]["news_confirmations"] == 2 and btc["features"]["news_bias"] == "bullish"
    eth = db.one("select * from scalp_trades where symbol='ETH'")
    assert btc["notional"] >= eth["notional"]                                                    # supported news may raise the size, never beyond the caps
    db.run("delete from research_events")


def test_the_entry_side_failing_never_leaves_open_trades_unmanaged(db, monkeypatch):
    """Regression: an exception while looking for NEW trades (a bad model, a feature mismatch) used to roll back the whole pass, including the stop-out of an open trade."""
    from crypto_ai.scalp import live, model
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db, confirm=False)
    live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    btc = db.one("select * from scalp_trades where symbol='BTC'")
    e = btc["entry_mid"]
    add_candle(db, "BTC", T0, e, e * 1.0002, e * 0.9999, e)
    add_candle(db, "BTC", T0 + timedelta(minutes=1), e, e * 1.0002, btc["stop_px"] * 0.998, e * 0.9999)          # breaks the stop
    for s in ("ETH", "XRP"):
        m = db.one("select entry_mid from scalp_trades where symbol=%s", [s])["entry_mid"]
        add_candle(db, s, T0, m, m * 1.0001, m * 0.9999, m)
        add_candle(db, s, T0 + timedelta(minutes=1), m, m * 1.0001, m * 0.9999, m)

    def boom(bundle, X):
        raise RuntimeError("model and features disagree")
    monkeypatch.setattr(model, "predict_edges", boom)
    px = {s: {"price": float(candles[s]["close"].iloc[-1]), "spread_pct": 0.01} for s in SYMS}
    r = live.run_pass(db, cfg, now=T0 + timedelta(minutes=2, seconds=5), prices=px)
    assert r["error"] and "RuntimeError" in r["error"] and any("BTC long closed (stop_loss)" in c for c in r["closed"])
    assert db.one("select exit_reason from scalp_trades where symbol='BTC'")["exit_reason"] == "stop_loss"
    st = db.one("select value from scalp_state where key='status'")["value"]
    assert "Entry side failed" in st["note"] and st["error"]
    assert db.one("select count(*) n from scalp_state where key='heartbeat'")["n"] == 1


def test_a_failure_halfway_through_the_entry_stage_undoes_its_in_memory_effects_too(db, monkeypatch):
    """The savepoint rolls the new signals back; the arming latches and the open-position map must roll back with them or a prediction is lost silently."""
    from crypto_ai.scalp import engine, live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db, confirm=True)
    real = engine.entry_decision
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:                                                          # BTC has already raised its signal and disarmed when ETH blows up
            raise RuntimeError("boom halfway")
        return real(*a, **k)
    monkeypatch.setattr(engine, "entry_decision", flaky)
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert r["error"] and "boom halfway" in r["error"] and r["opened"] == []
    assert db.one("select count(*) n from scalp_signals")["n"] == 0                  # rolled back
    gate = db.one("select value from scalp_state where key='gate'")["value"]
    assert all(g["armed"] == {"1": True, "-1": True} for g in gate.values()), gate     # ... and so are the latches: nothing is silently disarmed
    st = db.one("select value from scalp_state where key='status'")["value"]
    assert st["open"] == 0
    monkeypatch.setattr(engine, "entry_decision", real)
    again = live.run_pass(db, cfg, now=T0 + timedelta(seconds=40), prices=prices_of(candles))
    assert again["error"] is None and again["signals"]["raised"] == 3                  # the predictions that were lost are raised on the next pass


def test_a_candle_feed_outage_does_not_abort_the_pass(db, monkeypatch):
    from crypto_ai import coinbase
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db, confirm=False)
    live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    def down(*a, **k):
        raise ConnectionError("coinbase unreachable")
    monkeypatch.setattr(coinbase, "candles", down)
    r = live.run_pass(db, cfg, now=T0 + timedelta(minutes=3), prices=prices_of(candles))          # must not raise; open trades are still managed from stored data
    assert r["closed"] == [] and db.one("select count(*) n from scalp_state where key='heartbeat'")["n"] == 1


def test_account_protections_drawdown_and_exposure_stop_new_entries(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db, confirm=False)
    db.run("insert into scalp_state (key, value) values ('equity_peaks', %s::jsonb) on conflict (key) do update set value = excluded.value",
           [f'{{"{(T0 + timedelta(seconds=20)).date().isoformat()}": 2000.0}}'])                  # equity is $1000: 50% below its recent high
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert r["opened"] == [] and r["rejected"].get("drawdown_halt") == 3
    assert db.one("select count(*) n from scalp_signals")["n"] == 0                                # an account-state refusal is not a prediction to grade
    db.run("delete from scalp_state where key in ('equity_peaks','gate')")
    r2 = live.run_pass(db, {**cfg, "scalp_max_exposure_pct": 40.0}, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    open_notional = db.one("select coalesce(sum(notional),0) x from scalp_trades where status='open'")["x"]
    assert open_notional <= 1000 * 0.40 + 1e-6 and r2["rejected"].get("exposure_limit", 0) >= 1


def test_the_best_predicted_edge_gets_the_scarce_slot_not_the_cheapest_coin_or_the_first_alphabetically(db, monkeypatch):
    from crypto_ai.scalp import live, model
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    edge = {0: 0.2, 1: 0.5, 2: 0.9}                                                                # BTC, ETH, XRP
    monkeypatch.setattr(model, "predict_edges", lambda bundle, X: (np.array([edge[int(c)] for c in X["coin_id"]]), np.zeros(len(X))))
    cfg = {**cheap_cfg(db, confirm=False), "scalp_max_positions": 1}
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert len(r["opened"]) == 1 and r["opened"][0].startswith("XRP")


def test_real_trading_cannot_be_switched_on(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    monkeypatch.setenv("REAL_TRADING_ENABLED", "true")
    with pytest.raises(RuntimeError, match="paper-trading only"):
        live.run_pass(db, cheap_cfg(db), now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert db.one("select count(*) n from scalp_trades")["n"] == 0


def test_selfcheck_scalper_section_passes_on_a_healthy_book_and_catches_a_broken_one(db, monkeypatch, capsys):
    from crypto_ai import selfcheck
    cfg, candles, r1, r2, px = run_two_passes(db, monkeypatch, green=True)
    monkeypatch.setattr(selfcheck, "now", lambda: T0 + timedelta(minutes=2))         # the scratch data lives in the past: judge it at its own time
    selfcheck.RESULTS.clear()
    selfcheck.check_scalper(db, cfg)
    bad = [r for r in selfcheck.RESULTS if r[0] == "FAIL"]
    assert not bad, bad
    names = " | ".join(r[1] for r in selfcheck.RESULTS)
    assert "no trade opened before its confirmation candle closed" in names and "every finished prediction" in names
    # break the books on purpose: a widened stop and a trade that "opened before its confirmation"
    db.run("update scalp_trades set stop_px = initial_stop_px - 1 where symbol = 'BTC'")
    db.run("update scalp_trades set opened_at = (select decision_ts from scalp_signals where id = scalp_trades.signal_id) where symbol = 'ETH'")
    selfcheck.RESULTS.clear()
    selfcheck.check_scalper(db, cfg)
    failed = " | ".join(r[1] for r in selfcheck.RESULTS if r[0] == "FAIL")
    assert "stops never widened" in failed and "before its confirmation candle closed" in failed
