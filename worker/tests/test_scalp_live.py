"""The live scalper against a SCRATCH Postgres (never your real Supabase), with synthetic candles and injected prices - no network.

    TEST_DATABASE_URL=postgresql://...scratch... pytest tests/test_scalp_live.py

Proves: entries are opened with adaptive stop/target and full analytics; a stop touched INSIDE a candle between two passes is
still honoured (intrabar / catch-up); the kill switch flattens and stops entries; stale settings rows and a JSON-string
"false" cannot override the code; a closed trade gets its post-mortem row.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="set TEST_DATABASE_URL to a scratch Postgres")
SQL_DIR = Path(__file__).resolve().parents[2] / "supabase"
SCHEMAS = ["schema.sql", "schema_research.sql", "schema_manual.sql", "schema_learning.sql", "schema_cashplan.sql", "schema_short_horizon.sql", "schema_risk.sql", "schema_scalp.sql"]
SYMS = ["BTC", "ETH", "XRP"]
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def db():
    from crypto_ai.db import DB
    if "supabase" in DB_URL:
        pytest.skip("refusing to run against Supabase")
    d = DB(DB_URL)
    if not d.one("select to_regclass('scalp_trades') t")["t"]:
        for f in SCHEMAS:
            if f == "schema_cashplan.sql" and not (SQL_DIR / f).exists():
                continue
            sql = (SQL_DIR / f).read_text(encoding="utf-8")
            if not d.one("select count(*) n from pg_available_extensions where name='pgcrypto'")["n"]:
                sql = sql.replace("create extension if not exists pgcrypto;", "")   # PG13+ has gen_random_uuid() built in
            d.run(sql)
    return d


def cheap_cfg(db, confirm=False):
    """Cheap costs so the cost gate does not hide what is being tested. These mechanics tests (stops, intrabar, kill switch) run the
    UNCONFIRMED entry path so a trade exists after one pass; the next-candle confirmation flow has its own tests (test_scalp_confirm.py)."""
    cfg = db.settings()
    return {**cfg, "trading_fee_pct": 0.05, "slippage_pct": 0.01, "scalp_min_edge_pct": 0.01, "scalp_confirm_required": confirm,
            "scalp_max_exposure_pct": 100.0}   # three 30% positions must all fit here; the exposure cap has its own test


def seed_candles(db, n=1700, end=T0, seed=5):
    """Random-walk 1m candles for every coin ending at `end - 1 min` (the last CLOSED candle at time `end`)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end - timedelta(minutes=n), periods=n, freq="1min", tz="UTC")
    out = {}
    for i, s in enumerate(SYMS):
        c = 100 * (i + 1) * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
        o = np.r_[c[0], c[:-1]]
        df = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0004, "low": np.minimum(o, c) * 0.9996, "close": c, "volume": 100.0}, index=idx)
        from crypto_ai.scalp import data
        data.save_candles(db, s, df)
        out[s] = df
    return out


def add_candle(db, sym, ts, o, h, l, c):
    from crypto_ai.scalp import data
    data.save_candles(db, sym, pd.DataFrame({"open": [o], "high": [h], "low": [l], "close": [c], "volume": [100.0]}, index=pd.DatetimeIndex([ts])))


def prices_of(candles, at_minus=1):
    return {s: {"price": float(df["close"].iloc[-at_minus]), "spread_pct": 0.01} for s, df in candles.items()}


def install_model(db, monkeypatch):
    from crypto_ai.scalp import model
    db.run("delete from scalp_lessons; delete from scalp_signals; delete from scalp_trades; delete from scalp_models; delete from scalp_state; delete from candles where granularity=60")
    monkeypatch.setattr(model, "predict_edges", lambda bundle, X: (np.full(len(X), 0.5), np.full(len(X), 0.0)))   # strong long signal
    from crypto_ai.scalp import learn
    from psycopg2.extras import Json
    db.insert("scalp_models", {"bundle": Json({"long": "x", "short": "x"}), "threshold": 0.0, "meta": Json({}), "is_active": True})


def test_settings_stale_rows_and_json_string_false_cannot_override(db):
    db.run("delete from settings where key in ('trade_horizons','autopilot_enabled','sudden_move_pct')")
    db.run("insert into settings (key, value) values ('trade_horizons','[24,48]'), ('sudden_move_pct','1.0'), ('autopilot_enabled','\"false\"')")
    cfg = db.settings()
    assert cfg["trade_horizons"] == [] and cfg["sudden_move_pct"] == 0.35
    assert cfg["autopilot_enabled"] is False                     # a JSON *string* "false" is off, not truthy
    db.run("delete from settings where key in ('trade_horizons','autopilot_enabled','sudden_move_pct')")
    assert db.settings()["autopilot_enabled"] is True


def test_open_manage_intrabar_stop_kill_switch_and_lessons(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db)

    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert len(r["opened"]) == 3, r
    rows = db.all("select * from scalp_trades where status='open' order by symbol")
    assert [x["symbol"] for x in rows] == ["BTC", "ETH", "XRP"]
    for t in rows:
        assert t["direction"] == "long" and t["stop_px"] < t["entry_mid"] < t["take_profit_px"] and t["initial_stop_px"] == t["stop_px"]
        assert t["notional"] <= cfg["starting_balance"] * cfg["scalp_position_pct"] / 100 + 1e-6
        assert t["features"] and t["regime"] and t["setup"] and t["stop_dist_pct"] > 0
    # same signal, same minute: must not stack duplicates (open positions + disarmed)
    again = live.run_pass(db, cfg, now=T0 + timedelta(seconds=40), prices=prices_of(candles))
    assert again["opened"] == [] and db.one("select count(*) n from scalp_trades")["n"] == 3

    # BTC: a candle that dips through the stop and closes back ABOVE it, then a calm one. Polling only at closes would miss it.
    btc = next(t for t in rows if t["symbol"] == "BTC")
    e = btc["entry_mid"]
    # (the candle covering the entry minute began before we were in, so the engine deliberately ignores it: candle T0+1 is the first that counts)
    add_candle(db, "BTC", T0, e, e * 1.0002, btc["stop_px"] * 0.999, e * 1.0001)                       # ignored: pre-entry low
    add_candle(db, "BTC", T0 + timedelta(minutes=1), e, e * 1.0002, btc["stop_px"] * 0.999, e * 1.0001)  # dips through the stop, closes back above
    add_candle(db, "BTC", T0 + timedelta(minutes=2), e, e * 1.0002, e * 0.9999, e)
    later = T0 + timedelta(minutes=3, seconds=5)
    for s in ("ETH", "XRP"):                                    # the other coins just keep drifting sideways
        m = next(t for t in rows if t["symbol"] == s)["entry_mid"]
        add_candle(db, s, T0, m, m * 1.0001, m * 0.9999, m)
        add_candle(db, s, T0 + timedelta(minutes=1), m, m * 1.0001, m * 0.9999, m)
        add_candle(db, s, T0 + timedelta(minutes=2), m, m * 1.0001, m * 0.9999, m)
    px = {s: {"price": float(candles[s]["close"].iloc[-1]), "spread_pct": 0.01} for s in SYMS}
    px["BTC"]["price"] = e
    r2 = live.run_pass(db, cfg, now=later, prices=px)
    closed = db.one("select * from scalp_trades where symbol='BTC' and status='closed'")
    assert closed and closed["exit_reason"] == "stop_loss", r2
    assert closed["net_pnl_usd"] < 0 and closed["mae_pct"] < 0 and closed["fees_usd"] > 0 and closed["duration_min"] is not None
    assert closed["gross_pnl_usd"] - closed["slippage_usd"] - closed["fees_usd"] == pytest.approx(closed["net_pnl_usd"], abs=1e-6)
    assert closed["exit_price"] <= closed["stop_px"] * 1.0001          # never a better fill than the stop
    lesson = db.one("select * from scalp_lessons where trade_id=%s", [closed["id"]])
    assert lesson and lesson["symbol"] == "BTC" and "BTC long" in lesson["lesson"]

    # kill switch: nothing new, and every open trade is flattened at the live price
    r3 = live.run_pass(db, {**cfg, "autopilot_enabled": False}, now=later + timedelta(minutes=1), prices=px)
    assert r3["opened"] == [] and db.one("select count(*) n from scalp_trades where status='open'")["n"] == 0
    assert {t["exit_reason"] for t in db.all("select exit_reason from scalp_trades where symbol in ('ETH','XRP')")} == {"kill_switch"}
    st = db.one("select value from scalp_state where key='status'")["value"]
    assert st["enabled"] is False and "OFF" in st["note"]


def test_no_trade_without_a_validated_model_or_with_stale_data(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = cheap_cfg(db)
    db.run("update scalp_models set threshold = null")            # trained, but no threshold ever earned a positive net edge
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert r["opened"] == [] and "no threshold" in r["note"]
    db.run("update scalp_models set threshold = 0")
    from crypto_ai import coinbase
    monkeypatch.setattr(coinbase, "candles", lambda *a, **k: coinbase.to_frame([]))            # feed is down: nothing new can be fetched
    r = live.run_pass(db, cfg, now=T0 + timedelta(minutes=30), prices=prices_of(candles))   # newest candle is 30 min old
    assert r["opened"] == []


def test_costs_at_default_settings_keep_the_scalper_flat(db, monkeypatch):
    from crypto_ai.scalp import live
    install_model(db, monkeypatch)
    candles = seed_candles(db)
    cfg = db.settings()                                            # default 0.4% fee + 0.1% slippage: a minutes-long target cannot beat ~1%
    r = live.run_pass(db, cfg, now=T0 + timedelta(seconds=20), prices=prices_of(candles))
    assert r["opened"] == [] and r["rejected"].get("cost_gate", 0) == 3


def test_learning_pass_writes_stats_blocks_bad_setups_and_logs_a_guarded_retrain(db, monkeypatch):
    from crypto_ai.scalp import learn
    from psycopg2.extras import Json
    db.run("delete from scalp_lessons; delete from scalp_signals; delete from scalp_trades; delete from scalp_models; delete from scalp_state; delete from candles where granularity=60")
    seed_candles(db, n=8 * 1440, end=T0)
    # 40 closed losing 'reversion' longs on BTC: confidently negative expectancy after costs => must get blocked
    for i in range(40):
        tid = db.insert("scalp_trades", {"symbol": "BTC", "direction": "long", "status": "closed", "opened_at": T0 - timedelta(hours=2, minutes=i), "closed_at": T0 - timedelta(hours=1, minutes=i),
                                         "duration_min": 3.0, "net_pnl_usd": -1.0 - 0.01 * (i % 4), "gross_pnl_usd": -0.4, "net_pnl_pct": -0.5 - 0.01 * (i % 4), "fees_usd": 0.4, "slippage_usd": 0.2,
                                         "exit_reason": "stop_loss", "setup": "reversion", "regime": "normal_chop", "mfe_r": 0.1, "mae_r": -1.0, "features": Json({}), "state": Json({})})
        learn.record_lesson(db, db.one("select * from scalp_trades where id=%s", [tid]))
    cfg = {**db.settings(), "local_timezone": "UTC", "scalp_train_days": 9, "scalp_min_train_rows": 3000, "scalp_holdout_days": 2, "scalp_setup_min_trades": 30}
    out = learn.run_learning(db, cfg, now=T0, force=True)
    assert out["active"] and out["stats"]["blocked"] >= 1
    assert ("BTC", 1, "reversion") in learn.load_blocked(db)
    assert any("negative expectancy after fees" in t for t in db.one("select value from scalp_state where key='insights'")["value"])
    log = db.one("select value from scalp_state where key='retrain_log'")["value"]
    assert log and log[-1]["decision"] in ("no_edge", "kept_champion", "promoted", "waiting")
    assert out["exits"]["decision"] in ("waiting", "not_due", "updated", "unchanged")     # no validated model on noise => nothing to tune, and it says so
    # outside the 05:00-23:59 local window nothing runs (unless forced)
    assert learn.run_learning(db, cfg, now=T0.replace(hour=3))["active"] is False
