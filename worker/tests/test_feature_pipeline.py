"""feature_pipeline.py: causal as-of joins (no look-ahead) and the out-of-sample feature-evaluation gate."""
import numpy as np
import pandas as pd
import pytest

from crypto_ai.datasources import datalake, feature_pipeline as fp


def make_base(n=200, freq="1min", seed=1):
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"close": 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))}, index=idx)


def test_asof_join_never_uses_a_future_value():
    base = make_base(20)
    other = pd.DataFrame({"ts": [base.index[0] + pd.Timedelta(minutes=i) for i in (0, 5, 12)], "v": [1.0, 2.0, 3.0]})
    out = fp.asof_join(base, other, "ts", ["v"], "x")
    assert out.loc[base.index[0], "x_v"] == 1.0
    assert out.loc[base.index[4], "x_v"] == 1.0     # before minute 5's value arrives
    assert out.loc[base.index[5], "x_v"] == 2.0     # exactly at, allowed (not AFTER)
    assert out.loc[base.index[11], "x_v"] == 2.0
    assert out.loc[base.index[12], "x_v"] == 3.0


def test_asof_join_rewriting_the_future_never_changes_the_past():
    base = make_base(50)
    other = pd.DataFrame({"ts": base.index[::3], "v": np.arange(len(base.index[::3]), dtype=float)})
    out1 = fp.asof_join(base, other, "ts", ["v"], "x")
    other2 = other.copy()
    other2.loc[other2["ts"] > base.index[30], "v"] *= 1000   # rewrite everything after minute 30
    out2 = fp.asof_join(base, other2, "ts", ["v"], "x")
    pd.testing.assert_series_equal(out1["x_v"].loc[:base.index[29]], out2["x_v"].loc[:base.index[29]])


def test_asof_join_handles_a_source_with_no_data_yet():
    base = make_base(10)
    out = fp.asof_join(base, pd.DataFrame(columns=["ts", "v"]), "ts", ["v"], "x")
    assert out["x_v"].isna().all() and len(out) == len(base)


def test_load_candidate_features_only_adds_columns_and_reads_from_the_lake(tmp_path):
    lake = str(tmp_path / "lake")
    base = make_base(120)
    funding = pd.DataFrame({"symbol": ["BTC"] * 3, "ts": base.index[::40], "funding_rate": [0.0001, 0.0002, -0.0001]})
    datalake.write(funding, "funding_rates", "okx_funding_rate", base.index[0], root=lake)
    out = fp.load_candidate_features("BTC", base.index[0], base.index[-1], base, lake_root=lake)
    assert "close" in out.columns and "funding_funding_rate" in out.columns
    assert len(out) == len(base)
    assert out["funding_funding_rate"].iloc[0] == 0.0001


def test_load_candidate_features_never_crashes_when_every_source_is_empty(tmp_path):
    lake = str(tmp_path / "empty_lake")
    base = make_base(30)
    out = fp.load_candidate_features("XRP", base.index[0], base.index[-1], base, lake_root=lake)
    assert len(out) == len(base) and "funding_funding_rate" in out.columns and out["funding_funding_rate"].isna().all()


# ---------------------------------------------------------------- out-of-sample feature evaluation gate
def test_evaluate_candidate_feature_flags_insufficient_data():
    df = pd.DataFrame({"x": [1.0] * 10, "y": [1.0] * 10}, index=pd.date_range("2026-01-01", periods=10, freq="1min", tz="UTC"))
    r = fp.evaluate_candidate_feature(df, "x", "y")
    assert r["verdict"] == "insufficient_data"


def test_evaluate_candidate_feature_promotes_a_consistently_informative_feature():
    idx = pd.date_range("2026-01-01", periods=400, freq="1min", tz="UTC")
    rng = np.random.default_rng(3)
    x = rng.normal(0, 1, 400)
    y = x * 0.5 + rng.normal(0, 0.1, 400)   # genuinely, consistently informative in both halves
    df = pd.DataFrame({"x": x, "y": y}, index=idx)
    r = fp.evaluate_candidate_feature(df, "x", "y")
    assert r["verdict"] == "promising_check_holdout" and r["ic_a"] > 0.5 and r["ic_b"] > 0.5


def test_evaluate_candidate_feature_rejects_a_feature_that_only_worked_in_one_half():
    idx = pd.date_range("2026-01-01", periods=400, freq="1min", tz="UTC")
    rng = np.random.default_rng(4)
    x = rng.normal(0, 1, 400)
    y = np.r_[x[:200] * 0.9, -x[200:] * 0.9] + rng.normal(0, 0.05, 400)   # flips sign halfway through: not stable
    df = pd.DataFrame({"x": x, "y": y}, index=idx)
    r = fp.evaluate_candidate_feature(df, "x", "y")
    assert r["verdict"] == "not_shown_to_help"


def test_evaluate_candidate_feature_rejects_pure_noise():
    idx = pd.date_range("2026-01-01", periods=400, freq="1min", tz="UTC")
    rng = np.random.default_rng(5)
    df = pd.DataFrame({"x": rng.normal(0, 1, 400), "y": rng.normal(0, 1, 400)}, index=idx)
    r = fp.evaluate_candidate_feature(df, "x", "y")
    assert r["verdict"] == "not_shown_to_help"
