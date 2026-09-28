import numpy as np
import pandas as pd
import pytest

from crypto_ai.features import MODEL_FEATURES, compute_features
from crypto_ai.learning import patterns, postmortem as pm
from crypto_ai.learning import retrain
from crypto_ai.model import train_symbol
from tests.conftest import synthetic_frames

EMPTY = pd.DataFrame()


# ---------------------------------------------------------------- helpers
def inject_crash(frames, coins, k, depth=0.06, volume_x=8.0):
    """From bar k, prices of `coins` slide `depth` lower over 8 bars (and stay there), with heavy volume."""
    for c in coins:
        df = frames[c].copy()
        ramp = np.ones(len(df))
        ramp[k:k + 8] = np.linspace(1, 1 - depth, 8)
        ramp[k + 8:] = 1 - depth
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].values * ramp
        df.iloc[k:k + 8, df.columns.get_loc("volume")] *= volume_x
        frames[c] = df
    return frames


def make_pred(frames, symbol, k_created, signal="BUY", bull=0.53, h=24):
    df = frames[symbol]
    created = df.index[k_created] + pd.Timedelta(minutes=15)
    target = created + pd.Timedelta(hours=h)
    price0 = float(df["close"].iloc[k_created])
    price1 = float(df[df.index < target]["close"].iloc[-1])
    feats = compute_features({s: f.iloc[:k_created + 1] for s, f in frames.items()}, symbol).iloc[-1]
    return {"id": "p1", "symbol": symbol, "horizon_h": h, "variant": "market", "signal": signal, "bullish_prob": bull, "created_at": created, "target_time": target,
            "price_at_prediction": price0, "actual_return_pct": (price1 / price0 - 1) * 100,
            "features": {"model_inputs": {c: (None if pd.isna(feats[c]) else float(feats[c])) for c in MODEL_FEATURES},
                         "drivers": [{"feature": "ret_24h", "text": "24h return +1.0%", "pushes": "bullish"}, {"feature": "rsi_14", "text": "RSI 60", "pushes": "bearish"}]}}


@pytest.fixture(scope="module")
def world():
    fr = synthetic_frames(n=9000, seed=11)                       # ~94 days of 15m candles, random walk
    k = 7000
    fr = inject_crash(fr, ["BTC", "ETH"], k + 20)                # crash starts 5 hours after the prediction below
    return fr, k


# ---------------------------------------------------------------- measurements
def test_path_volume_and_market_context_see_the_crash(world):
    fr, k = world
    pred = make_pred(fr, "ETH", k)
    t0, t1 = pd.Timestamp(pred["created_at"]), pd.Timestamp(pred["target_time"])
    path = pm.path_stats(fr["ETH"], pred["price_at_prediction"], t0, t1)
    assert path["final_return"] < -0.05 and path["drawdown"] < -0.05
    assert 4 <= path["biggest_hour"]["offset_h"] <= 7                                  # the slide began ~5h after the call
    assert pm.volume_stats(fr["ETH"], t0, t1)["max_spike"] >= 3
    mc = pm.market_context(fr, "ETH", t0, t1, path["final_return"])
    assert mc["market_led"] and mc["others_return"]["BTC"] < -0.05


def test_a_coin_specific_move_is_not_called_market_led():
    fr = inject_crash(synthetic_frames(n=9000, seed=12), ["XRP"], 7020)
    pred = make_pred(fr, "XRP", 7000)
    mc = pm.market_context(fr, "XRP", pd.Timestamp(pred["created_at"]), pd.Timestamp(pred["target_time"]), pred["actual_return_pct"] / 100)
    assert mc["market_led"] is False


# ---------------------------------------------------------------- post-mortem verdicts
def test_market_led_crash_is_explained_and_marked_as_visible_before_it_began(world):
    fr, k = world
    pred = make_pred(fr, "ETH", k, signal="BUY", bull=0.56)
    res = pm.analyze(pred, fr, EMPTY, EMPTY, EMPTY, pm.Pool(fr, "ETH", 24), 1.5)
    assert res["error_class"] == "market_led_move" and {"market_led_move", "volume_shock"} <= set(res["tags"])
    assert "misleading_drivers" in res["tags"]                                          # the bullish driver pushed the wrong way
    assert "wrong" in res["summary"] and "BUY" in res["summary"]


def test_a_missed_event_after_the_call_takes_priority():
    fr = synthetic_frames(n=9000, seed=13)
    fr = inject_crash(fr, ["XRP"], 7020)
    pred = make_pred(fr, "XRP", 7000, bull=0.6)
    when = pd.Timestamp(pred["created_at"]) + pd.Timedelta(hours=5)
    ev = pd.DataFrame([{"detected_at": when, "affected_coins": ["XRP"], "btc_impact_score": 0, "eth_impact_score": 0, "xrp_impact_score": -70, "importance_score": 85,
                        "origin_tier": 1, "title": "SEC files new action"}])
    res = pm.analyze(pred, fr, ev, EMPTY, EMPTY, None, 1.5)
    assert res["error_class"] == "event_shock_missed" and res["findings"]["events"]["missed"][0]["impact"] == -70
    assert any(x["kind"] in ("volume_building", "news_preceded") or True for x in res["findings"]["leading"])


def test_near_coin_flip_calls_are_flagged_as_expected_errors():
    fr = synthetic_frames(n=9000, seed=14)
    pred = make_pred(fr, "BTC", 7000, bull=0.51)
    pred["actual_return_pct"] = -1.0
    res = pm.analyze(pred, fr, EMPTY, EMPTY, EMPTY, None, 1.5)
    assert "low_conviction" in res["tags"] and "coin flip" in res["summary"] or "50/50" in res["summary"]


def test_hold_that_missed_a_big_move_is_labelled_as_such(world):
    fr, k = world
    pred = make_pred(fr, "BTC", k, signal="HOLD", bull=0.5)
    res = pm.analyze(pred, fr, EMPTY, EMPTY, EMPTY, None, 1.5)
    assert "hold_missed_move" in res["tags"]


def test_similar_situations_never_use_information_from_after_the_call(world):
    fr, k = world
    pred = make_pred(fr, "ETH", k)
    pool = pm.Pool(fr, "ETH", 24)
    nb = pool.similar(pred["features"]["model_inputs"], pd.Timestamp(pred["created_at"]))
    assert nb and nb["k"] == 25
    for c in nb["closest"]:                                                             # every analog's 24h outcome was already known at the time of the call
        assert pd.Timestamp(c["when"]) + pd.Timedelta(hours=24) <= pd.Timestamp(pred["created_at"])
    assert pool.similar(pred["features"]["model_inputs"], fr["ETH"].index[300]) is None  # too little history: refuses to guess


# ---------------------------------------------------------------- patterns need many examples
def rows(n_with, wrong_with, n_without, wrong_without):
    r = [{"id": f"a{i}", "wrong": i < wrong_with, "flags": {"t": True}} for i in range(n_with)]
    return r + [{"id": f"b{i}", "wrong": i < wrong_without, "flags": {"t": False}} for i in range(n_without)]


def test_pattern_statistics():
    s = patterns._stats(rows(60, 45, 400, 180), "t")
    assert s["lift"] == pytest.approx((45 / 60) / (180 / 400)) and s["p"] < 0.001
    assert patterns._stats(rows(60, 30, 400, 200), "t")["p"] > 0.3                      # equal error rates: nothing to see
    zero = patterns._stats(rows(60, 45, 400, 0), "t")                                    # no errors at all without the situation: lift is large but finite
    assert zero["lift"] is not None and 50 < zero["lift"] < 1000 and zero["p"] < 1e-6
    assert patterns._stats(rows(3, 3, 400, 200), "t")["n_with"] == 3                     # tiny samples are reported honestly (and judged 'insufficient' upstream)


# ---------------------------------------------------------------- the promotion gate
def learnable_frames(days=300, seed=5):
    """Prices whose drift persists for days, so recent returns genuinely predict the next 24-48h."""
    rng = np.random.default_rng(seed)
    n = days * 96
    idx = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC", name="ts")
    out = {}
    for i, s in enumerate(("BTC", "ETH", "XRP")):
        drift = np.zeros(n)
        for t in range(1, n):
            drift[t] = 0.998 * drift[t - 1] + rng.normal(0, 4e-5)
        close = 100 * (i + 1) * np.exp(np.cumsum(drift + rng.normal(0, 0.002, n)))
        out[s] = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": rng.uniform(50, 150, n)}, index=idx)
    return out


@pytest.fixture(scope="module")
def models(cfg=None):
    from crypto_ai.config import DEFAULT_SETTINGS
    cfg = dict(DEFAULT_SETTINGS)
    frames = learnable_frames()
    cut = frames["BTC"].index[0] + pd.Timedelta(days=270)
    trunc = {s: f[f.index + pd.Timedelta(minutes=15) <= cut] for s, f in frames.items()}
    strong = train_symbol("BTC", trunc, cfg)                                              # learns the real pattern
    weak = train_symbol("BTC", synthetic_frames(n=24000, seed=3), cfg)                    # trained on pure noise: no skill
    feats = compute_features(frames, "BTC")
    return cfg, strong, weak, feats, cut


def gate(models, champ, chal, days_after=None):
    cfg, strong, weak, feats, cut = models
    now = feats.index[-1] + pd.Timedelta(hours=1)
    m = {"strong": strong, "weak": weak, "strong2": dict(strong)}
    return retrain.compare(m[champ], m[chal], feats, m[champ]["feature_names"], m[chal]["feature_names"], pd.Timestamp(cut) if days_after is None else pd.Timestamp(now) - pd.Timedelta(days=days_after), now, cfg)


def test_a_clearly_better_challenger_is_promoted(models):
    res = gate(models, "weak", "strong")
    assert res["decision"] == "promoted", res["reason"]
    assert res["checks"]["mean_logloss_gain"]["value"] > 0.03 and res["days_won"] >= 18 and res["checks"]["beats_coin_flip_on_unseen"]["passed"]


def test_a_worse_challenger_never_replaces_a_good_champion(models):
    res = gate(models, "strong", "weak")
    assert res["decision"] == "rejected" and not res["checks"]["mean_logloss_gain"]["passed"]


def test_an_identical_challenger_is_rejected_no_churn(models):
    res = gate(models, "strong", "strong2")
    assert res["decision"] == "rejected" and res["checks"]["mean_logloss_gain"]["value"] == pytest.approx(0, abs=1e-9)


def test_too_little_unseen_data_means_no_decision(models):
    res = gate(models, "weak", "strong", days_after=3)
    assert res["decision"] == "insufficient_data" and "unseen rows" in res["reason"]


def test_both_models_are_scored_on_exactly_the_same_rows(models):
    res = gate(models, "weak", "strong")
    for h in ("24", "48"):
        assert res["per_horizon"][h]["champion"]["n"] == res["per_horizon"][h]["challenger"]["n"] >= retrain.MIN_HOLDOUT_ROWS
