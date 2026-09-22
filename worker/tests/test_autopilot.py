"""Autopilot decision rules (pure logic; no database)."""
from datetime import datetime, timedelta, timezone

from crypto_ai import autopilot

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def sig(action, kind="scheduled", bull=0.6, age_min=10, i=1):
    return {"id": i, "action": action, "urgency": "low", "trigger_kind": kind, "bull": bull, "created_at": NOW - timedelta(minutes=age_min)}


def coin(signal=None, held=0.0, lot_age_h=None, last_auto_min=None):
    return {"signal": signal, "held_value": held, "youngest_lot_at": NOW - timedelta(hours=lot_age_h) if lot_age_h is not None else None,
            "last_auto_at": NOW - timedelta(minutes=last_auto_min) if last_auto_min is not None else None}


def go(coins, cash=1000.0, total=1000.0, cfg=None):
    from crypto_ai.config import DEFAULT_SETTINGS
    return autopilot.decide(coins, cash, total, cfg or dict(DEFAULT_SETTINGS), NOW)


def test_buy_when_none_held():
    o = go({"BTC": coin(sig("BUY"))})
    assert len(o) == 1 and o[0]["side"] == "buy" and o[0]["coin"] == "BTC"
    assert o[0]["usd"] == 100.0                        # 10% of a $1000 portfolio at normal confidence


def test_high_confidence_buys_more():
    assert go({"BTC": coin(sig("BUY", bull=0.8))})[0]["usd"] == 200.0


def test_hold_signal_does_nothing():
    assert go({"BTC": coin(sig("HOLD"))}) == []


def test_never_more_than_cash_or_40pct_of_portfolio():
    assert go({"BTC": coin(sig("BUY"))}, cash=30.0)[0]["usd"] == 30.0
    assert go({"BTC": coin(sig("BUY"), held=350.0)})[0]["usd"] == 50.0      # 40% cap = 400, already holds 350
    assert go({"BTC": coin(sig("BUY"), held=400.0)}) == []


def test_below_minimum_order_is_skipped():
    assert go({"BTC": coin(sig("BUY"))}, cash=4.0) == []


def test_cash_is_shared_across_coins():
    o = go({"BTC": coin(sig("BUY")), "ETH": coin(sig("BUY")), "XRP": coin(sig("BUY"))}, cash=150.0)
    assert [x["usd"] for x in o] == [100.0, 50.0]


def test_each_signal_is_acted_on_once():
    assert go({"BTC": coin(sig("BUY", age_min=10), last_auto_min=5)}) == []      # already traded after this signal
    assert len(go({"BTC": coin(sig("BUY", age_min=10), last_auto_min=60)})) == 1  # a newer signal since the last trade


def test_sell_and_reduce():
    s = go({"BTC": coin(sig("SELL", kind="price"), held=200.0, lot_age_h=5)})
    assert s[0]["side"] == "sell" and s[0]["fraction"] == 1.0
    r = go({"BTC": coin(sig("REDUCE", kind="price"), held=200.0, lot_age_h=5)})
    assert r[0]["fraction"] == 0.5


def test_reduce_of_a_tiny_position_sells_it_all():
    assert go({"BTC": coin(sig("REDUCE", kind="price"), held=8.0, lot_age_h=5)})[0]["fraction"] == 1.0


def test_nothing_to_sell_means_no_order():
    assert go({"BTC": coin(sig("SELL", kind="price"), held=0.0)}) == []


def test_ordinary_sell_ignored_on_a_fresh_position_but_sudden_sell_is_not():
    assert go({"BTC": coin(sig("SELL"), held=100.0, lot_age_h=0.5)}) == []
    assert len(go({"BTC": coin(sig("SELL", kind="price"), held=100.0, lot_age_h=0.5)})) == 1


def test_no_signal_no_order():
    assert go({"BTC": coin(None, held=100.0)}) == []
