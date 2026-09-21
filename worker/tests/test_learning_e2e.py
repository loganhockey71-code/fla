"""Database-level tests for self-learning and the real-time event system.

    TEST_DATABASE_URL=postgresql://...scratch... pytest tests/test_learning_e2e.py     (or: python worker/tests/run_scratch_tests.py)

Needs a FRESH scratch Postgres with all four schema files applied. Never point it at Supabase.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="set TEST_DATABASE_URL to a scratch Postgres")

from tests.conftest import synthetic_frames  # noqa: E402


@pytest.fixture(scope="module")
def db():
    from crypto_ai.db import DB
    if "supabase" in DB_URL:
        pytest.skip("never run against Supabase")
    d = DB(DB_URL)
    if d.one("select count(*) n from predictions")["n"]:
        pytest.skip("needs a fresh scratch database")
    return d


@pytest.fixture(scope="module")
def world(db):
    """90 days of candles ending now, with a crash in BTC+ETH about 40h ago, all stored in the candles table."""
    from crypto_ai.config import DEFAULT_SETTINGS
    from crypto_ai.db import JsonList
    from crypto_ai.model import train_symbol
    from tests.test_learning import inject_crash

    n = 90 * 96
    start = (datetime.now(timezone.utc) - timedelta(minutes=15 * (n + 8))).replace(second=0, microsecond=0)
    start = start.replace(minute=(start.minute // 15) * 15)
    fr = synthetic_frames(n=n, seed=21, start=start)
    crash_k = n - 40 * 4
    fr = inject_crash(fr, ["BTC", "ETH"], crash_k)
    for s, df in fr.items():
        db.bulk("insert into candles (symbol, granularity, ts, open, high, low, close, volume) values %s on conflict do nothing",
                [(s, 900, ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume) for ts, r in df.iterrows()])
    cfg = dict(DEFAULT_SETTINGS)
    early = {s: f.iloc[: 68 * 96] for s, f in fr.items()}                      # champion is trained on the first 68 days only
    m = train_symbol("BTC", early, cfg)
    mid = db.insert("model_versions", {**m, "feature_names": JsonList(m["feature_names"]), "is_active": True,
                                       "trained_at": datetime.now(timezone.utc) - timedelta(days=40)})
    return {"frames": fr, "crash_k": crash_k, "cfg": cfg, "model_id": mid, "model_version": m["version"]}


def add_prediction(db, w, symbol, k, signal, bull, h=24, variant="market", fresh=False):
    from crypto_ai import evaluator
    from crypto_ai.db import JsonList
    from crypto_ai.features import MODEL_FEATURES, compute_features
    df = w["frames"][symbol]
    created = datetime.now(timezone.utc) + timedelta(seconds=5) if fresh else df.index[k] + pd.Timedelta(minutes=15)
    target = created + pd.Timedelta(hours=h)
    price0 = float(df["close"].iloc[k])
    price1 = float(df[df.index < target]["close"].iloc[-1]) if not fresh else price0
    feats = compute_features({s: f.iloc[: k + 1] for s, f in w["frames"].items()}, symbol).iloc[-1]
    row = {"created_at": created, "symbol": symbol, "horizon_h": h, "target_time": target, "price_at_prediction": price0, "signal": signal, "bullish_prob": bull,
           "bearish_prob": 1 - bull, "confidence": max(bull, 1 - bull), "range_low": price0 * 0.95, "range_high": price0 * 1.05, "reasons": JsonList(["t"]), "explanation": "t",
           "model_version_id": w["model_id"], "model_version": w["model_version"], "data_cutoff": created - timedelta(seconds=1), "variant": variant,
           "features": {"model_inputs": {c: (None if pd.isna(feats[c]) else float(feats[c])) for c in MODEL_FEATURES},
                        "drivers": [{"feature": "ret_24h", "text": "24h return", "pushes": "bullish"}]}, "research_features": {"macro": {}, "events": {}}}
    row["id"] = db.insert("predictions", row, returning="id")
    if fresh:
        return row, None                                                          # a just-made prediction has no outcome yet
    res = evaluator.score_prediction(row, price1, w["cfg"])
    db.insert("prediction_results", res, returning="prediction_id")
    return row, res


def snapshot_models(db):
    return db.all("select id, version, is_active, trained_at, calibration::text c, model_blob::text b from model_versions order by id")


def test_post_mortems_only_for_wrong_predictions_and_nothing_else_changes(db, world):
    from crypto_ai import learning
    k = world["crash_k"]
    wrong, r1 = add_prediction(db, world, "ETH", k - 20, "BUY", 0.56)          # BUY just before a crash: wrong
    xrp = world["frames"]["XRP"]["close"]
    calm = next(i for i in range(600, k - 200) if abs(xrp.iloc[i + 96] / xrp.iloc[i] - 1) < 0.008)    # a genuinely quiet day for XRP
    right, r2 = add_prediction(db, world, "XRP", calm, "HOLD", 0.50)             # HOLD through a quiet day is right
    assert not r1["signal_correct"] and r2["signal_correct"]
    models_before = snapshot_models(db)
    preds_before = db.all("select * from predictions order by id")
    out = learning.run_all(db, world["cfg"])
    assert out["post_mortems"] == 1
    pmrow = db.one("select * from post_mortems")
    assert str(pmrow["prediction_id"]) == str(wrong["id"]) and pmrow["error_class"] == "market_led_move"
    assert "volume_shock" in pmrow["tags"] and pmrow["findings"]["similar"]["k"] == 25 and "wrong" in pmrow["summary"]
    # ONE mistake changes nothing: same models, same predictions, no retrain attempted
    assert snapshot_models(db) == models_before and db.all("select * from predictions order by id") == preds_before
    assert out["retrain"] == "nothing due" and db.one("select count(*) n from model_challenges")["n"] == 0
    assert learning.run_all(db, world["cfg"])["post_mortems"] == 0                # idempotent
    with pytest.raises(Exception, match="append-only"):
        db.run("update post_mortems set error_class = 'x'")


def test_patterns_are_reported_as_insufficient_until_there_are_enough_examples(db, world):
    rows = db.all("select * from learned_patterns")
    assert len(rows) == 6 * 6 and all(r["status"] == "insufficient" and r["suggestion"] is None for r in rows)
    assert max(r["n_with_tag"] for r in rows) < world["cfg"]["pattern_min_examples"]


def test_many_examples_produce_a_confirmed_pattern_only_with_real_evidence(db, world):
    """Add many volume-shock/no-shock predictions where shocks clearly cause errors, and check the statistics respond."""
    from crypto_ai import learning
    k = world["crash_k"]
    for j in range(40):                                                          # 40 BUYs launched just before the crash (all wrong, all in a volume shock)
        add_prediction(db, world, "BTC", k - 10 - j, "BUY", 0.58)
    for j in range(120):                                                         # calm-period calls that are right about half the time by construction
        kk = 400 + j * 7
        df = world["frames"]["BTC"]
        went_up = df["close"].iloc[kk + 96] > df["close"].iloc[kk]
        add_prediction(db, world, "BTC", kk, "BUY" if went_up else "SELL", 0.58 if went_up else 0.42)
    learning.run_all(db, world["cfg"])
    p = db.one("select * from learned_patterns where key = 'BTC|both|volume_shock'")
    assert p["n_with_tag"] >= 30 and p["error_rate_with"] > 0.9 and p["error_rate_without"] < 0.1 and p["p_value"] < 0.001
    assert p["status"] == "confirmed" and p["suggestion"]
    assert db.one("select count(*) n from post_mortems where symbol='BTC'")["n"] >= 40
    assert db.one("select count(*) n from model_challenges")["n"] == 0            # patterns are suggestions: still no retrain


def test_forced_challenge_is_recorded_and_the_champion_survives_unless_beaten(db, world):
    from crypto_ai.learning import retrain
    before = db.one("select version from model_versions where symbol='BTC' and variant='market' and is_active")["version"]
    res = retrain.maybe_retrain(db, world["cfg"], force=True, only=("BTC", "market"))
    row = db.one("select * from model_challenges order by id desc limit 1")
    assert res[0]["decision"] == row["decision"] and row["decision"] in ("rejected", "promoted", "insufficient_data")
    assert row["champion_version"] == before and row["metrics"] is not None
    active = db.all("select version from model_versions where symbol='BTC' and variant='market' and is_active")
    assert len(active) == 1
    assert (active[0]["version"] == before) == (row["decision"] != "promoted")   # replaced ONLY when the gate said promoted


def test_challenges_are_rate_limited(db, world):
    from crypto_ai.learning import retrain
    ok, info, _ = retrain.due(db, world["cfg"], "BTC", "market")
    assert ok is False and ("holdout window" in info["why"] or "new scored" in info["why"])


# ---------------------------------------------------------------- real-time system
def one_minute(now, crash: bool):
    idx = pd.date_range(end=now - timedelta(minutes=2), periods=95, freq="1min", tz="UTC", name="ts").floor("1min")
    close = np.full(95, 100.0)
    vol = np.full(95, 10.0)
    if crash:
        close[-15:] = np.linspace(100, 96.5, 15)
        vol[-8:] = 70.0
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": vol}, index=idx)


def test_sudden_move_is_investigated_signalled_and_not_repeated(db, world, monkeypatch):
    from crypto_ai import coinbase, realtime
    calls = {}

    def fake_candles(product, gran, start, end=None):
        calls[product] = calls.get(product, 0) + 1
        return one_minute(datetime.now(timezone.utc), crash=product in ("BTC-USD", "ETH-USD"))
    snap = {"price": 96.5, "bid": 96.49, "ask": 96.51, "spread_pct": 0.02, "volume_24h": 1.0, "ob_imbalance": -0.8, "buy_pressure": 0.2}
    monkeypatch.setattr(coinbase, "candles", fake_candles)
    monkeypatch.setattr(realtime, "_fresh_model_read", lambda db_, coins, now: {c: 0.47 for c in coins})
    snaps = {s: dict(snap) for s in ("BTC", "ETH", "XRP")}
    out = realtime.run_watch_cycle(db, world["cfg"], snaps)
    got = {o["symbol"]: o for o in out}
    assert set(got) == {"BTC", "ETH"} and all(o["action"] in ("REDUCE", "SELL") and o["urgency"] in ("medium", "high") for o in out)
    rows = db.all("select * from live_signals where trigger_kind in ('price','volume','orderbook') order by id")
    assert len(rows) == 2 and rows[0]["move_pct"] < -1 and rows[0]["scope"] == "partial" and rows[0]["model_bull_prob"] == pytest.approx(0.47)
    assert rows[0]["reasons"] and rows[0]["explanation"].split()[0] in ("REDUCE", "SELL") and rows[0]["expires_at"] > rows[0]["created_at"]
    assert db.one("select count(*) n from events where kind='sudden_move'")["n"] == 2
    assert realtime.run_watch_cycle(db, world["cfg"], snaps) == []               # cooldown: the same shock isn't re-announced every minute
    # a calm market produces nothing
    monkeypatch.setattr(coinbase, "candles", lambda *a, **k: one_minute(datetime.now(timezone.utc), crash=False))
    assert realtime.run_watch_cycle(db, world["cfg"], {**snaps, "XRP": {**snap, "ob_imbalance": 0.0}}) == []


def test_major_official_news_triggers_a_signal_once(db, world, monkeypatch):
    from crypto_ai import coinbase, realtime
    from crypto_ai.research import common
    db.run("insert into source_registry (key,name,kind,tier,tier_label,credibility_score,poll_interval_s) values ('t_sec','SEC','rss',1,'official_government',95,60) on conflict do nothing")
    src = db.one("select * from source_registry where key='t_sec'")
    assert common.record_event(db, src, {"external_id": "n1", "title": "SEC sues Ripple over XRP sales, seeks penalties", "url": "http://sec/x", "summary": "lawsuit enforcement action XRP",
                                         "kind": "news", "published_at": datetime.now(timezone.utc)}) == "new"
    ev = db.one("select * from research_events where external_id='n1'")
    assert ev["importance_score"] >= 60 and "XRP" in ev["affected_coins"] and ev["xrp_impact_score"] < 0
    monkeypatch.setattr(coinbase, "candles", lambda *a, **k: one_minute(datetime.now(timezone.utc), crash=False))
    monkeypatch.setattr(realtime, "_fresh_model_read", lambda db_, coins, now: {c: 0.5 for c in coins})
    snap = {"price": 100.0, "bid": 99.99, "ask": 100.01, "spread_pct": 0.02, "volume_24h": 1.0, "ob_imbalance": 0.0, "buy_pressure": 0.5}
    snaps = {s: dict(snap) for s in ("BTC", "ETH", "XRP")}
    out = realtime.run_watch_cycle(db, world["cfg"], snaps)
    xrp = [o for o in out if o["symbol"] == "XRP"]
    assert xrp and xrp[0]["action"] in ("REDUCE", "SELL")
    sig = db.one("select * from live_signals where symbol='XRP' and trigger_kind='news'")
    assert sig["news_event_id"] == ev["id"] and "SEC" in (sig["cause"] or "")
    assert [o for o in realtime.run_watch_cycle(db, world["cfg"], snaps) if o["symbol"] == "XRP"] == []       # same news never signalled twice
    # an old story we only noticed late (published 10h ago) must not be blamed for anything
    assert common.record_event(db, src, {"external_id": "n2", "title": "SEC charges exchange in crypto fraud case, seeks penalties", "url": "http://sec/old", "summary": "enforcement fraud bitcoin",
                                         "kind": "news", "published_at": datetime.now(timezone.utc) - timedelta(hours=10)}) == "new"
    titles = [n["title"] for n in realtime._news_for(db, "BTC", datetime.now(timezone.utc) - timedelta(hours=6))]
    assert not any("exchange in crypto fraud" in t for t in titles)


def test_standing_signal_is_published_once_per_prediction(db, world):
    from crypto_ai import realtime
    add_prediction(db, world, "ETH", len(world["frames"]["ETH"]) - 100, "BUY", 0.60, fresh=True)   # a brand-new 24h market prediction for ETH
    n = realtime.publish_standing(db, world["cfg"])
    assert n >= 1
    assert realtime.publish_standing(db, world["cfg"]) == 0
    row = db.one("select * from live_signals where symbol='ETH' and trigger_kind='scheduled' order by id desc limit 1")
    assert row["action"] == "BUY" and row["urgency"] == "low"
