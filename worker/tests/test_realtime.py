import numpy as np
import pandas as pd
import pytest

from crypto_ai import detector, realtime


def trig(pct, minutes=12, spike=1.0, imb=None):
    return {"pct": pct, "minutes": minutes, "window": 15, "volume_spike": spike, "imbalance": imb, "reasons": ["price"]}


def broad(pct):
    return detector.diagnose("ETH", trig(pct), {"BTC": pct * 0.8, "XRP": pct * 0.9}, None, [])


def only_me(pct):
    return detector.diagnose("ETH", trig(pct), {"BTC": 0.0, "XRP": 0.0}, None, [])


def news(impact, tier=1, importance=80, title="SEC statement"):
    return {"title": title, "impact": impact, "tier": tier, "importance": importance, "source": "SEC"}


# ---------------------------------------------------------------- what to do NOW
def test_small_dip_is_just_watched(cfg):
    r = realtime.decide(trig(-1.0), only_me(-1.0), [], 0.5, cfg)
    assert r["action"] == "HOLD" and r["urgency"] == "medium"


def test_broad_selloff_with_volume_reduces(cfg):
    r = realtime.decide(trig(-2.1, spike=4.2), broad(-2.1), [], 0.5, cfg)
    assert r["action"] == "REDUCE" and r["score"] < -0.8
    assert any("market-wide" in x for x in r["reasons"]) and any("Volume 4.2x" in x for x in r["reasons"])


def test_crash_sells(cfg):
    r = realtime.decide(trig(-5.0, spike=6.0, imb=-0.8), broad(-5.0), [], 0.47, cfg)
    assert r["action"] == "SELL" and r["urgency"] == "high"


def test_severe_official_negative_news_exits_even_without_a_price_move(cfg):
    r = realtime.decide(None, None, [news(-75, tier=1, importance=90, title="SEC sues Ripple")], 0.5, cfg)
    assert r["action"] == "SELL" and r["reasons"][0].startswith("Severe negative official news")


def test_moderate_negative_news_reduces(cfg):
    assert realtime.decide(None, None, [news(-45, importance=70)], 0.5, cfg)["action"] == "REDUCE"


def test_rally_without_a_catalyst_is_not_chased(cfg):
    r = realtime.decide(trig(3.0, spike=4.0), only_me(3.0), [], 0.55, cfg)
    assert r["action"] == "HOLD" and any("half weight" in x for x in r["reasons"])


def test_rally_with_positive_official_news_and_model_agreement_buys(cfg):
    r = realtime.decide(trig(3.0, spike=4.0), only_me(3.0), [news(55, importance=80, title="SEC approves ETF")], 0.55, cfg)
    assert r["action"] == "BUY"


def test_positive_news_never_buys_against_a_bearish_model(cfg):
    assert realtime.decide(trig(3.0, spike=4.0), only_me(3.0), [news(55)], 0.40, cfg)["action"] != "BUY"


def test_media_news_counts_half_as_much_as_an_official_source(cfg):
    off = realtime.decide(None, None, [news(-45, tier=1)], None, cfg)["score"]
    med = realtime.decide(None, None, [news(-45, tier=4)], None, cfg)["score"]
    assert med == pytest.approx(off / 2)


def test_every_score_term_is_explained(cfg):
    r = realtime.decide(trig(-2.0, spike=3.5, imb=-0.75), broad(-2.0), [news(-30)], 0.45, cfg)
    assert len(r["reasons"]) >= 5 and abs(sum(float(x.rsplit("(", 1)[1].rstrip(")")) for x in r["reasons"]) - r["score"]) < 0.05


def test_standing_action_follows_model_threshold(cfg):
    assert [realtime.standing_action(p, cfg) for p in (0.60, 0.50, 0.40, None)] == ["BUY", "HOLD", "SELL", "HOLD"]


def test_actions_are_only_the_four_requested(cfg):
    seen = {realtime.decide(trig(p, spike=s), broad(p), n, b, cfg)["action"] for p in (-6, -3, -1.2, 1.5, 4) for s in (1, 5) for n in ([], [news(-60)], [news(60)]) for b in (0.4, 0.5, 0.6)}
    assert seen <= {"BUY", "HOLD", "REDUCE", "SELL"} and {"SELL", "REDUCE", "HOLD"} <= seen


# ---------------------------------------------------------------- triggers
def series(moves):
    idx = pd.date_range("2026-01-01", periods=len(moves), freq="1min", tz="UTC")
    return pd.Series(100 * np.cumprod(1 + np.array(moves)), index=idx)


def test_huge_volume_spike_alone_triggers(cfg):
    closes = series([0.0001] * 100)
    vols = pd.Series([10.0] * 95 + [90.0] * 5, index=closes.index)                 # 9x normal for 5 min, price flat
    t = detector.find_trigger(closes, vols, None, cfg)
    assert t and "volume" in t["reasons"] and "price" not in t["reasons"]


def test_modest_volume_without_a_move_does_not_trigger(cfg):
    closes = series([0.0001] * 100)
    vols = pd.Series([10.0] * 95 + [40.0] * 5, index=closes.index)                 # 4x, flat price: not enough on its own
    assert detector.find_trigger(closes, vols, None, cfg) is None
