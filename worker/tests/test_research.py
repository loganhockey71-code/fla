import os

import pandas as pd
import pytest

from crypto_ai.http import SourceError, get_json, redact
from crypto_ai.research import common, features as rf, markets
from crypto_ai.research.congress import relevance, stage_from_actions
from crypto_ai.research.fred import macro_event


# ---------------------------------------------------------------- classification & impacts
def test_official_etf_approval_is_positive_and_strong():
    c = common.classify("SEC approves spot Ethereum ETF", "", 1)
    assert c["category"] == "ETF" and c["sentiment"] == "positive" and "ETH" in c["coins"]
    imp = common.impacts(c["category"], c["sentiment"], c["importance"], c["coins"], c["named"])
    assert imp["ETH"] > 0 and imp["BTC"] == 0 and all(-100 <= v <= 100 for v in imp.values())


def test_xrp_reacts_most_to_lawsuits():
    imp = common.impacts("lawsuit", "negative", 80, ["BTC", "ETH", "XRP"], [])
    assert imp["XRP"] < imp["ETH"] < imp["BTC"] < 0


def test_neutral_events_carry_no_direction():
    assert set(common.impacts("fed", "neutral", 70, ["BTC", "ETH", "XRP"], []).values()) == {0}


def test_media_cannot_outrank_official_and_release_notes_are_network_updates():
    off, med = common.classify("SEC approves spot Ethereum ETF", "", 1), common.classify("SEC approves spot Ethereum ETF", "", 4)
    assert off["importance"] > med["importance"] and med["importance"] <= common.CRED_CAP[4]
    assert common.classify("3.4.0", "Amendments: fixAMM. XRP ledger release", 2, "token_or_network_update")["category"] == "token_or_network_update"


def test_irrelevant_items_are_dropped():
    assert common.classify("Regional bank quarterly dividend announced", "", 3) is None


# ---------------------------------------------------------------- duplicates count once
def test_copies_share_one_family_and_never_add_confirmations():
    assert common.independent_confirmations({"press"}) == 1
    assert common.independent_confirmations({"primary", "press"}) == 2
    assert common.confidence(55, "positive", 1) < common.confidence(55, "positive", 2)
    assert common.similarity("SEC approves spot Ethereum ETF", "SEC approves first spot Ethereum ETFs") >= common.DUP_THRESHOLD
    assert common.similarity("SEC approves spot Ethereum ETF", "Fed holds rates steady") < common.DUP_THRESHOLD


DB_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not DB_URL, reason="set TEST_DATABASE_URL to a scratch Postgres with both schema files applied")
def test_dedupe_grouping_and_promotion_against_postgres():
    from crypto_ai.db import DB
    db = DB(DB_URL)
    for k, t in (("t_gov", 1), ("t_p1", 4), ("t_p2", 4), ("t_p3", 4)):
        db.run("insert into source_registry (key,name,kind,tier,tier_label,credibility_score,poll_interval_s) values (%s,%s,'rss',%s,'x',%s,60) "
               "on conflict (key) do nothing", [k, k, t, common.TIERS[t][1]])
    src = {k: db.one("select * from source_registry where key=%s", [k]) for k in ("t_gov", "t_p1", "t_p2", "t_p3")}
    try:
        def it(i, title, url):
            return {"external_id": i, "title": title, "url": url, "summary": "", "kind": "news"}
        # media story first, then three rewrites from other outlets: ONE event, one confirmation
        assert common.record_event(db, src["t_p1"], it("a1", "SEC approves first spot Ethereum ETF", "http://x/1")) == "new"
        for i, (k, title) in enumerate([("t_p2", "SEC approves spot Ethereum ETFs"), ("t_p3", "SEC approves first spot Ethereum ETF, filings show"),
                                         ("t_p2", "SEC approves first spot Ethereum ETFs after delay")]):
            assert common.record_event(db, src[k], it(f"b{i}", title, f"http://x/b{i}")) == "duplicate"
        ev = db.one("select * from research_events where source_key='t_p1'")
        assert ev["duplicate_count"] == 3 and ev["independent_confirmations"] == 1
        assert common.record_event(db, src["t_p1"], it("a1", "SEC approves first spot Ethereum ETF", "http://x/1")) is None      # re-poll: nothing new
        # the official release arrives late: it becomes the origin; media copies still don't count as confirmations
        assert common.record_event(db, src["t_gov"], it("g1", "SEC approves spot Ethereum ETF listings", "http://sec/1")) == "duplicate"
        ev = db.one("select * from research_events where id=%s", [ev["id"]])
        assert ev["origin_tier"] == 1 and ev["source_key"] == "t_gov" and ev["event_probability"] == 1.0
        assert ev["independent_confirmations"] == 2 and ev["duplicate_count"] == 4        # primary + press, not 5
        assert common.record_event(db, src["t_p1"], it("a1", "SEC approves first spot Ethereum ETF", "http://x/1")) is None      # old origin isn't re-created
    finally:
        db.run("delete from research_events where source_key like 't\\_%'")
        db.run("delete from source_registry where key like 't\\_%'")


# ---------------------------------------------------------------- point-in-time features
def _macro(values, lag_days):
    dates = pd.date_range("2025-01-01", periods=len(values), freq="MS")
    return pd.DataFrame({"obs_date": dates.date, "value": values, "available_at": dates + pd.Timedelta(days=lag_days)})


def test_macro_value_invisible_before_release():
    data = {"macro": {"UNRATE": _macro([4.0, 4.1, 4.5], 37)}, "events": pd.DataFrame(), "pm": pd.DataFrame(), "start": None}
    idx = pd.DatetimeIndex(["2025-03-10", "2025-03-25", "2025-04-30"], tz="UTC")     # Mar-1 obs is released Apr 7
    got = rf.macro_frame(data, idx)["macro_unrate"].tolist()
    assert got[0] == 4.1 and got[1] == 4.1 and got[2] == 4.5                       # 4.5 appears only after Apr 7


def test_event_features_nan_before_collection_and_ignore_the_future():
    ev = pd.DataFrame([{"detected_at": pd.Timestamp("2026-01-02 12:00", tz="UTC"), "affected_coins": ["XRP"], "btc_impact_score": 0,
                        "eth_impact_score": 0, "xrp_impact_score": 60, "source_credibility_score": 95, "novelty_score": 100,
                        "confidence_score": 90, "independent_confirmations": 1, "origin_tier": 1}])
    data = {"macro": {}, "events": ev, "pm": pd.DataFrame(), "start": ev["detected_at"].min()}
    idx = pd.DatetimeIndex(["2026-01-01 12:00", "2026-01-02 11:00", "2026-01-02 13:00", "2026-01-03 13:00"], tz="UTC")
    f = rf.event_frame(data, "XRP", idx)
    assert f["ev_impact_24h"].iloc[:2].isna().all()                                 # before we ever saw an event: unknown, not zero
    assert f["ev_impact_24h"].iloc[2] > 0 and f["ev_official_24h"].iloc[2] == 1
    assert f["ev_impact_24h"].iloc[3] == 0 and f["ev_impact_72h"].iloc[3] > 0        # aged out of 24h, still inside 72h
    assert rf.event_frame(data, "BTC", idx)["ev_impact_24h"].iloc[2] == 0            # other coins unaffected


# ---------------------------------------------------------------- sources
def test_fred_macro_events():
    d = pd.Timestamp
    assert macro_event("DGS10", "10-Year Treasury yield", d("2026-01-05").date(), 4.60, [(d("2026-01-02").date(), 4.45)])["sentiment"] == "negative"
    assert macro_event("DGS10", "x", d("2026-01-05").date(), 4.46, [(d("2026-01-02").date(), 4.45)]) is None


def test_congress_relevance_and_stage():
    assert relevance("Digital Asset Market Clarity Act") == 90 and relevance("Retirement Simplification and Clarity Act") == 0
    assert relevance("CFTC Reauthorization Act") == 50
    assert stage_from_actions([{"text": "Referred to the Committee on Financial Services"}]) == "committee"
    assert stage_from_actions([{"text": "Passed/agreed to in House"}, {"text": "Passed Senate"}]) == "passed_both"
    assert stage_from_actions([{"text": "Became Public Law No: 119-1"}]) == "law"


def test_prediction_market_direction_and_no_price_ladders():
    assert markets.direction("Will the Fed cut rates in December?") == 1
    assert markets.direction("Will no Fed rate cuts happen in 2026?") == -1
    assert markets.direction("Will a US recession start in 2026?") == -1
    assert markets._relevant("Will Bitcoin be above $70,000 on September 30?") is False
    assert markets._relevant("Crypto market structure bill signed into law in 2026?") is True


# ---------------------------------------------------------------- secrets never leak
def test_api_keys_are_redacted(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "supersecretkey123")
    assert "supersecretkey123" not in redact("GET https://x?api_key=supersecretkey123 failed for supersecretkey123")
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("https://api.stlouisfed.org/?api_key=supersecretkey123 refused")
    monkeypatch.setattr("crypto_ai.http._S.get", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(SourceError) as e:
        get_json("https://api.stlouisfed.org/fred/series/observations", {"api_key": "supersecretkey123"})
    assert "supersecretkey123" not in str(e.value) and "supersecretkey123" not in repr(e.value.__cause__)
