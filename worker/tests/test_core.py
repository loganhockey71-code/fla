import numpy as np
import pandas as pd
import pytest

from crypto_ai import detector, evaluator, events, paper
from crypto_ai.features import MODEL_FEATURES, compute_features, make_labels
from crypto_ai.model import train_symbol
from crypto_ai.predictor import decide_signal
from tests.conftest import synthetic_frames


# ---------------------------------------------------------------- look-ahead bias
def test_features_ignore_the_future(frames):
    """Rewriting every candle after row K must not change any feature row at or before K."""
    K = 2000
    base = compute_features(frames, "ETH")
    tampered = {s: f.copy() for s, f in frames.items()}
    for f in tampered.values():
        f.iloc[K + 1:, :] = f.iloc[K + 1:, :] * 3.7
    changed = compute_features(tampered, "ETH")
    pd.testing.assert_frame_equal(base.iloc[: K + 1][MODEL_FEATURES], changed.iloc[: K + 1][MODEL_FEATURES])


def test_asof_is_candle_close(frames):
    f = compute_features(frames, "BTC")
    assert f.index[0] == frames["BTC"].index[0] + pd.Timedelta(minutes=15)


def test_labels_are_the_only_forward_looking_thing(frames):
    f = compute_features(frames, "BTC")
    lab = make_labels(f, 24)
    assert lab["fwd_ret"].iloc[-96:].isna().all()          # last 24h have no outcome yet
    assert not lab["fwd_ret"].iloc[:-96].isna().any()


# ---------------------------------------------------------------- paper trading maths
def test_fill_prices_cross_spread_and_slippage():
    buy = paper.fill_price(100, "buy", 0.2, 0.1, True)
    sell = paper.fill_price(100, "sell", 0.2, 0.1, True)
    assert buy == pytest.approx(100 * (1 + 0.001 + 0.001))
    assert sell == pytest.approx(100 * (1 - 0.001 - 0.001))
    assert paper.fill_price(100, "buy", 0.2, 0.1, False) == pytest.approx(100.1)


def test_sizing_10_and_20_percent_and_no_leverage(cfg):
    assert paper.position_budget(100_000, 100_000, 0.60, cfg) == pytest.approx(10_000)
    assert paper.position_budget(100_000, 100_000, 0.80, cfg) == pytest.approx(20_000)
    assert paper.position_budget(100_000, 3_000, 0.80, cfg) == pytest.approx(3_000)   # capped at cash


def test_round_trip_costs_and_pnl(cfg):
    plan = paper.plan_buy(100.0, 0.0, 10_000, cfg)
    assert plan["amount_invested"] + plan["fee_entry"] == pytest.approx(10_000)
    trade = {**plan, "symbol": "BTC", "status": "closed"}
    # flat market: we lose fees + slippage both ways
    res = paper.plan_sell(trade, 100.0, 0.0, cfg)
    assert res["pnl_usd"] < 0
    assert res["pnl_pct"] == pytest.approx(-(0.4 + 0.4 + 0.1 + 0.1), abs=0.05)
    # +5% market: still profitable after costs
    assert paper.plan_sell(trade, 105.0, 0.0, cfg)["pnl_usd"] > 0


def test_cash_accounting_ties_out(cfg):
    plan = paper.plan_buy(50.0, 0.0, 10_000, cfg)
    open_t = {**plan, "symbol": "ETH", "status": "open"}
    assert paper.cash_balance(100_000, [open_t]) == pytest.approx(90_000)
    closed = {**open_t, "status": "closed", **paper.plan_sell(open_t, 55.0, 0.0, cfg)}
    final = paper.cash_balance(100_000, [closed])
    assert final == pytest.approx(100_000 + closed["pnl_usd"])


# ---------------------------------------------------------------- evaluator
def _pred(signal, bull=0.6, h=24, price=100.0):
    return {"id": "x", "signal": signal, "horizon_h": h, "price_at_prediction": price, "bullish_prob": bull,
            "bearish_prob": 1 - bull, "confidence": max(bull, 1 - bull), "range_low": 95, "range_high": 105}


def test_scoring_rules(cfg):
    assert evaluator.score_prediction(_pred("BUY"), 103, cfg)["signal_correct"]
    assert not evaluator.score_prediction(_pred("BUY"), 99, cfg)["signal_correct"]
    assert evaluator.score_prediction(_pred("SELL", 0.3), 97, cfg)["signal_correct"]
    assert evaluator.score_prediction(_pred("HOLD", 0.5), 100.8, cfg)["signal_correct"]       # inside 1.5% band
    assert not evaluator.score_prediction(_pred("HOLD", 0.5), 103, cfg)["signal_correct"]
    r = evaluator.score_prediction(_pred("BUY", 0.8), 110, cfg)
    assert r["high_confidence"] and not r["in_range"] and r["directional_correct"]


# ---------------------------------------------------------------- signals
def test_hold_is_default_without_edge(cfg):
    assert decide_signal(0.515, 0.02, cfg)[0] == "HOLD"
    assert decide_signal(0.49, 0.02, cfg)[0] == "HOLD"
    assert decide_signal(0.60, 0.02, cfg)[0] == "BUY"
    assert decide_signal(0.30, 0.02, cfg)[0] == "SELL"
    assert decide_signal(0.90, 0.9, cfg)[0] == "HOLD"        # spread gate


# ---------------------------------------------------------------- sudden moves
def _series(moves):
    idx = pd.date_range("2026-01-01", periods=len(moves), freq="1min", tz="UTC")
    return pd.Series(100 * np.cumprod(1 + np.array(moves)), index=idx)


def test_detector_flags_drop_and_explains_broad_selling(cfg):
    moves = [0.0001] * 90 + [-0.004] * 5                        # ~ -2% in 5 minutes
    closes = _series(moves)
    vols = pd.Series([10.0] * 85 + [60.0] * 10, index=closes.index)
    trig = detector.find_trigger(closes, vols, None, cfg)
    assert trig and trig["pct"] < -1.0 and "price" in trig["reasons"] and "volume" in trig["reasons"]
    d = detector.diagnose("XRP", trig, {"BTC": -1.2, "ETH": -1.5}, 0.2, [])
    assert d["scope"] == "broad" and "broad crypto selling" in d["cause"]
    assert "XRP dropped" in detector.headline("XRP", trig, d)


def test_detector_quiet_market_is_quiet(cfg):
    closes = _series([0.0002, -0.0002] * 50)
    assert detector.find_trigger(closes, pd.Series([10.0] * 100, index=closes.index), None, cfg) is None


def test_official_news_outranks_market_move():
    trig = {"pct": -2.0, "minutes": 10, "window": 15, "volume_spike": 1.0}
    news = [{"title": "SEC sues Ripple", "source": "SEC", "source_tier": "official", "importance": 85}]
    d = detector.diagnose("XRP", trig, {"BTC": 0.0, "ETH": 0.0}, None, news)
    assert d["scope"] == "coin_specific" and "official announcement" in d["cause"]


# ---------------------------------------------------------------- events
def test_official_source_outranks_media_for_same_headline():
    title = "SEC approves spot Ethereum ETF"
    off = events.classify(title, "", "official")
    med = events.classify(title, "", "media")
    assert off["coins"] == ["ETH"] and off["sentiment"] == "positive"
    assert off["importance"] > med["importance"] and med["importance"] <= 65
    assert off["confidence"] > med["confidence"]


def test_unrelated_official_item_is_not_important():
    c = events.classify("SEC charges individual in offering fraud scheme involving real estate", "", "official")
    assert c["importance"] < 40


def test_generic_lists_are_not_events():
    c = events.classify("Most-Viewed Bills - Week of September 20", "crypto digital asset bills", "official")
    assert c["importance"] < 40


# ---------------------------------------------------------------- training smoke test (random walk => no edge)
def test_training_pipeline_runs_and_does_not_invent_an_edge(cfg):
    fr = synthetic_frames(n=24000, seed=3)
    m = train_symbol("BTC", fr, cfg)
    for h in ("24", "48"):
        bt = m["backtest_metrics"][h]
        assert 0.4 < bt["auc"] < 0.6                       # random walk: AUC must hover near 0.5
        assert bt["share_p_above_threshold"] < 0.5
        assert m["calibration"][h]["a"] >= 0          # calibration can never flip the model
    assert set(m["model_blob"]) == {"24", "48"}
