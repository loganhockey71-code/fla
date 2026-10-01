"""Microstructure collector, compaction, maker-fill simulator and feature builder. All pure/offline: no socket, no DB."""
import numpy as np
import pandas as pd
import pytest

from crypto_ai.config import DEFAULT_SETTINGS
from crypto_ai.scalp import maker, micro_collect as MC, micro_compact as MP, micro_features as MF, micro_setups


# ---------------------------------------------------------------- parsing + throttling
def test_parse_ticker_extracts_a_two_sided_quote_and_rejects_garbage():
    msg = {"product_id": "BTC-USD", "time": "2026-01-01T00:00:01Z", "best_bid": "100.0", "best_ask": "100.2",
           "best_bid_size": "1.5", "best_ask_size": "2.0", "price": "100.1", "sequence": 5}
    row = MC.parse_ticker(msg)
    assert row["symbol"] == "BTC" and row["mid"] == pytest.approx(100.1) and row["spread_pct"] == pytest.approx(0.2 / 100.1 * 100)
    assert MC.parse_ticker({"product_id": "BTC-USD", "time": "x", "best_bid": "0", "best_ask": "100"}) is None    # zero/garbage price
    assert MC.parse_ticker({"product_id": "BTC-USD", "time": "x", "best_bid": "101", "best_ask": "100"}) is None  # crossed book


def test_parse_match_flips_maker_side_to_get_the_aggressor():
    row = MC.parse_match({"trade_id": 7, "product_id": "ETH-USD", "time": "t", "price": "10", "size": "2", "side": "buy"})
    assert row["aggressor"] == "sell" and row["symbol"] == "ETH"    # maker was buying => a seller crossed the spread
    assert MC.parse_match({"trade_id": 8, "product_id": "ETH-USD", "time": "t", "price": "10", "size": "2", "side": "sell"})["aggressor"] == "buy"


def test_quote_changed_only_on_a_real_touch_move_or_a_long_heartbeat():
    q1 = {"best_bid": 100.0, "best_ask": 100.1}
    assert MC.quote_changed(None, q1, 0.15, 0.0) is True
    prev = {**q1, "_written_at": 10.0}
    assert MC.quote_changed(prev, q1, 0.15, 10.05) is False                       # unchanged, too soon
    assert MC.quote_changed(prev, {"best_bid": 100.0, "best_ask": 100.2}, 0.15, 10.05) is True   # touch moved
    assert MC.quote_changed(prev, q1, 0.15, 20.0) is True                          # unchanged but long enough for a heartbeat


def test_depth_from_book_computes_bands_and_imbalance():
    bids = [["100.00", "2.0"], ["99.90", "5.0"], ["99.00", "50.0"]]
    asks = [["100.10", "1.0"], ["100.20", "1.0"], ["101.00", "50.0"]]
    d = depth = MC.depth_from_book(bids, asks, [5, 25])
    assert d["mid"] == pytest.approx(100.05) and d["spread_pct"] == pytest.approx(0.1 / 100.05 * 100)
    assert d["bid_depth_5bp"] == pytest.approx(2.0) and d["ask_depth_5bp"] == pytest.approx(1.0)   # 5bp of 100.05 ~= 0.05
    assert d["imbalance_5bp"] == pytest.approx((2 - 1) / 3)
    assert d["bid_depth_25bp"] > d["bid_depth_5bp"]   # a wider band always captures at least as much
    assert MC.depth_from_book([], asks, [5]) == {}


# ---------------------------------------------------------------- 1-second bar compaction (causal, no gaps invented)
def test_build_1s_bars_aggregates_quotes_and_trades_per_active_second():
    t0 = pd.Timestamp("2026-01-01T00:00:00Z")
    quotes = pd.DataFrame({"symbol": ["BTC"] * 3, "ts": [t0, t0 + pd.Timedelta(milliseconds=400), t0 + pd.Timedelta(seconds=2)],
                          "mid": [100.0, 100.2, 101.0], "spread_pct": [0.1, 0.1, 0.2]})
    trades = pd.DataFrame({"symbol": ["BTC"] * 3, "ts": [t0, t0 + pd.Timedelta(milliseconds=100), t0 + pd.Timedelta(seconds=2)],
                          "price": [100.0, 100.1, 101.0], "size": [1.0, 2.0, 3.0], "aggressor": ["buy", "sell", "buy"]})
    bars = MP.build_1s_bars(quotes, trades)
    assert len(bars) == 2                                     # second 0 and second 2 had activity; second 1 did not appear
    r0 = bars[bars.ts == t0].iloc[0]
    assert r0["mid_open"] == 100.0 and r0["mid_close"] == pytest.approx(100.2) and r0["n_quotes"] == 2 and r0["n_trades"] == 2
    assert r0["buy_volume"] == pytest.approx(1.0) and r0["sell_volume"] == pytest.approx(2.0)
    assert r0["vwap"] == pytest.approx((100.0 * 1 + 100.1 * 2) / 3)


def test_build_1s_bars_empty_input_is_empty_output():
    empty = pd.DataFrame(columns=["symbol", "ts", "mid", "spread_pct"])
    et = pd.DataFrame(columns=["symbol", "ts", "price", "size", "aggressor"])
    assert MP.build_1s_bars(empty, et).empty


# ---------------------------------------------------------------- regular grid + features (causality)
def make_bars(n_seconds=200, seed=1, gap_at=None):
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-01-01T00:00:00Z")
    secs = [t0 + pd.Timedelta(seconds=i) for i in range(n_seconds) if gap_at is None or i not in gap_at]
    mid = 100 * np.exp(np.cumsum(rng.normal(0, 0.0004, len(secs))))
    buy = rng.uniform(0, 2, len(secs))
    sell = rng.uniform(0, 2, len(secs))
    return pd.DataFrame({"symbol": "BTC", "ts": secs, "mid_open": mid, "mid_high": mid * 1.0001, "mid_low": mid * 0.9999, "mid_close": mid,
                         "spread_avg": rng.uniform(0.01, 0.05, len(secs)), "spread_max": rng.uniform(0.02, 0.06, len(secs)),
                         "n_quotes": rng.integers(1, 4, len(secs)), "n_trades": rng.integers(0, 3, len(secs)),
                         "buy_volume": buy, "sell_volume": sell, "vwap": mid})


def test_regular_grid_fills_gaps_without_inventing_price_moves():
    bars = make_bars(60, gap_at={10, 11, 12})
    g = MF.regular_grid(bars)
    assert len(g) == 60   # every second from first to last is present now
    gap_rows = g[(g.ts >= bars.ts.iloc[0] + pd.Timedelta(seconds=10)) & (g.ts <= bars.ts.iloc[0] + pd.Timedelta(seconds=12))]
    assert (gap_rows["n_trades"] == 0).all() and (gap_rows["buy_volume"] == 0).all()          # missing second = no activity, not NaN
    assert gap_rows["mid_close"].nunique() == 1                                                # price held at the last known quote


def test_features_use_only_past_data():
    bars = make_bars(500, seed=2)
    g = MF.compute_features(MF.add_depth(MF.regular_grid(bars), pd.DataFrame(columns=["ts", "imbalance_5bp", "imbalance_25bp"])))
    future_bars = bars.copy()
    future_bars.loc[future_bars.index[-50:], ["mid_open", "mid_high", "mid_low", "mid_close", "vwap"]] *= 1.05
    g2 = MF.compute_features(MF.add_depth(MF.regular_grid(future_bars), pd.DataFrame(columns=["ts", "imbalance_5bp", "imbalance_25bp"])))
    pd.testing.assert_frame_equal(g[MF.FEATURES].iloc[:-60], g2[MF.FEATURES].iloc[:-60])


def test_outcomes_are_non_causal_and_mfe_mae_bracket_the_close():
    bars = make_bars(400, seed=3)
    g = MF.compute_features(MF.add_depth(MF.regular_grid(bars), pd.DataFrame(columns=["ts", "imbalance_5bp", "imbalance_25bp"])))
    out = MF.add_outcomes(g, horizons=[1, 5, 30])
    assert "gross_long_5s" in out.columns and len(out) == len(g) - max([1, 5, 30]) - 1
    assert (out["mfe_long_30s"] >= out["gross_long_30s"] - 1e-9).all()
    assert (out["mae_long_30s"] <= out["gross_long_30s"] + 1e-9).all()


def test_bounce_stats_reports_negative_autocorrelation_for_an_alternating_series():
    idx = pd.date_range("2026-01-01", periods=200, freq="1s", tz="UTC")
    r = pd.Series(np.tile([0.01, -0.01], 100), index=idx)
    b = MF.bounce_stats(pd.DataFrame({"ret_1s": r}))
    assert b["autocorr_lag1"] < -0.9 and b["flip_share"] > 0.9


# ---------------------------------------------------------------- maker-fill simulator
def trades_df(rows):
    return pd.DataFrame(rows, columns=["ts", "price", "size", "aggressor"])


T0 = pd.Timestamp("2026-01-01T00:00:00Z")


def test_maker_buy_fills_once_cumulative_opposing_volume_clears_the_queue():
    trades = trades_df([(T0 + pd.Timedelta(seconds=1), 99.9, 1.0, "sell"), (T0 + pd.Timedelta(seconds=2), 99.9, 1.5, "sell"),
                        (T0 + pd.Timedelta(seconds=3), 99.9, 1.0, "sell")])
    r = maker.simulate_maker_fill("buy", 100.0, queue_ahead=2.0, trades=trades, t0=T0, horizon_s=10)
    assert r.filled and r.fill_time == T0 + pd.Timedelta(seconds=2) and r.fill_price == 100.0    # 1.0+1.5=2.5 >= 2.0 ahead


def test_maker_buy_ignores_same_side_and_worse_price_trades_correctly():
    trades = trades_df([(T0 + pd.Timedelta(seconds=1), 100.5, 5.0, "buy"),                 # buy aggressor: can't fill a resting buy
                        (T0 + pd.Timedelta(seconds=2), 100.05, 5.0, "sell"),                # sell but ABOVE our price: doesn't reach us
                        (T0 + pd.Timedelta(seconds=3), 99.5, 1.0, "sell")])                 # sell trading THROUGH our price: fills
    r = maker.simulate_maker_fill("buy", 100.0, queue_ahead=0.5, trades=trades, t0=T0, horizon_s=10)
    assert r.filled and r.fill_time == T0 + pd.Timedelta(seconds=3)


def test_maker_sell_is_the_mirror_of_buy():
    trades = trades_df([(T0 + pd.Timedelta(seconds=1), 100.1, 3.0, "buy")])
    r = maker.simulate_maker_fill("sell", 100.0, queue_ahead=2.0, trades=trades, t0=T0, horizon_s=10)
    assert r.filled and r.fill_price == 100.0


def test_maker_order_that_never_gets_enough_flow_times_out_without_being_a_loss():
    trades = trades_df([(T0 + pd.Timedelta(seconds=1), 99.9, 0.1, "sell")])
    r = maker.simulate_maker_fill("buy", 100.0, queue_ahead=5.0, trades=trades, t0=T0, horizon_s=10)
    assert not r.filled and r.reason == "timeout" and r.fill_price is None


def test_maker_fill_respects_the_horizon_cutoff():
    trades = trades_df([(T0 + pd.Timedelta(seconds=20), 99.9, 10.0, "sell")])   # plenty of volume, but too late
    r = maker.simulate_maker_fill("buy", 100.0, queue_ahead=1.0, trades=trades, t0=T0, horizon_s=10)
    assert not r.filled


def test_mid_at_is_as_of_not_interpolated():
    s = pd.Series([100.0, 101.0, 102.0], index=[T0, T0 + pd.Timedelta(seconds=5), T0 + pd.Timedelta(seconds=10)])
    assert maker.mid_at(s, T0 + pd.Timedelta(seconds=7)) == 101.0
    assert maker.mid_at(s, T0 - pd.Timedelta(seconds=1)) is None


def test_adverse_selection_sign_matches_the_direction_that_hurts_the_position():
    mid = pd.Series([100.0, 99.5, 99.0], index=[T0, T0 + pd.Timedelta(seconds=1), T0 + pd.Timedelta(seconds=5)])
    a = maker.adverse_selection("buy", 100.0, T0, mid, [1, 5])
    assert a["drift_1s_pct"] < 0 and a["drift_5s_pct"] < 0   # bought, then price kept falling: adverse
    b = maker.adverse_selection("sell", 100.0, T0, mid, [5])
    assert b["drift_5s_pct"] > 0                             # sold, then price fell: favourable for the short


def test_compare_execution_never_fabricates_a_maker_fill_and_prices_a_market_order_with_real_spread():
    cfg = dict(DEFAULT_SETTINGS)
    mid = pd.Series([100.0, 100.0, 100.0], index=[T0, T0 + pd.Timedelta(seconds=30), T0 + pd.Timedelta(seconds=60)])   # flat: isolates the cost
    no_flow = trades_df([])
    r = maker.compare_execution("buy", T0, 100.0, 0.1, 99.98, 5.0, no_flow, mid, cfg, fill_horizon_s=30, hold_after_fill_s=30)
    assert r["maker_filled"] is False and r["maker_net_pct"] is None and r["market_net_pct"] is not None
    round_trip_cost = 2 * (0.1 / 200 + cfg["slippage_pct"] / 100) * 100 + 2 * cfg["trading_fee_pct"]   # ~2x(half-spread+slip) + 2x fee
    assert r["market_net_pct"] == pytest.approx(-round_trip_cost, abs=0.02)


# ---------------------------------------------------------------- simple strategy setups (shape + directionality only)
def test_micro_setups_return_boolean_masks_of_the_right_length_and_never_both_directions_at_once():
    bars = make_bars(400, seed=4)
    g = MF.compute_features(MF.add_depth(MF.regular_grid(bars), pd.DataFrame(columns=["ts", "imbalance_5bp", "imbalance_25bp"])))
    for name, fn in micro_setups.setups().items():
        lm, sm = fn(g)
        assert lm.dtype == bool and sm.dtype == bool and len(lm) == len(g) == len(sm), name
        assert not (lm & sm).any(), name


# ---------------------------------------------------------------- micro_research: signal selection + taker costing
def test_take_signals_picks_one_trade_at_a_time_per_symbol_and_mirrors_short():
    from crypto_ai.scalp import micro_research as MR
    idx = pd.date_range("2026-01-01", periods=10, freq="1s", tz="UTC")
    df = pd.DataFrame({"symbol": ["BTC"] * 10, "mid_close": np.linspace(100, 101, 10), "spread_pct": 0.05,
                       "gross_long_2s": [1.0, 2.0, -1.0, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]}, index=idx)
    long_m = np.array([True, True, False, False, False, False, False, False, False, False])
    short_m = np.zeros(10, bool)
    t = MR.take_signals(df, long_m, short_m, h=2)
    assert len(t) == 1 and t.iloc[0]["dir"] == "long" and t.iloc[0]["gross"] == 1.0     # second signal at i=1 falls inside the first trade's hold and is skipped
    short_m2 = np.array([True, True, False, False, False, False, False, False, False, False])
    t2 = MR.take_signals(df, np.zeros(10, bool), short_m2, h=2)
    assert t2.iloc[0]["dir"] == "short" and t2.iloc[0]["gross"] == -1.0                  # short mirrors the long gross


def test_net_taker_matches_costs_module_on_a_flat_move():
    from crypto_ai import config
    from crypto_ai.scalp import micro_research as MR, costs
    cfg = dict(config.DEFAULT_SETTINGS)
    entries = pd.DataFrame({"dir": ["long"], "gross": [0.0], "mid0": [100.0], "spread0": [0.1]})
    net = MR.net_taker(entries, cfg).iloc[0]
    ef = costs.fill(100.0, 1, True, 0.1, cfg["slippage_pct"])
    xf = costs.fill(100.0, 1, False, 0.1, cfg["slippage_pct"])
    assert net == pytest.approx(costs.net_pct(ef, xf, 1, cfg["trading_fee_pct"]), abs=1e-9)
