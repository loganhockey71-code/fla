"""The research harness must measure trades exactly the way the live engine would manage them."""
import numpy as np
import pandas as pd
import pytest

from crypto_ai import config
from crypto_ai.scalp import costs, engine, research as R
from crypto_ai.scalp.opportunities import HORIZONS, build_opportunities
from tests.test_scalp import make_frames


def _engine_net(arr, i, d, h, sl, tp, trail, cfg, spread):
    e = arr["o"][i + 1]
    stop = e * (1 - d * sl / 100)
    p = engine.Position(symbol="BTC", direction=d, entry_ts=pd.Timestamp("2026-01-01", tz="UTC"), entry_mid=e,
                        entry_fill=costs.fill(e, d, True, spread, cfg["slippage_pct"]), qty=1.0, notional=e, stop_px=stop, initial_stop_px=stop,
                        tp_px=e * (1 + d * tp / 100), unit_pct=sl, atr_pct=0.05, max_hold=h, trail_act_r=0.8 if trail else 1e9, trail_mult=0.7, best_px=e)
    plain = {**cfg, "scalp_reversal_mult": 1e9, "scalp_nofollow_bars": 10**6}
    for j in range(h + 2):
        b = i + 1 + j
        ex = engine.step_position(p, {"ts": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=j), "open": arr["o"][b], "high": arr["h"][b],
                                      "low": arr["l"][b], "close": arr["c"][b]}, None, plain)
        if ex:
            return engine.close_position(p, ex["reason"], ex["mid"], p.entry_ts, spread, cfg)["net_pnl_pct"]
    raise AssertionError("no exit")


@pytest.mark.parametrize("trail", [False, True])
def test_vectorised_simulator_equals_the_engine(trail):
    cfg = dict(config.DEFAULT_SETTINGS)
    fr = make_frames(2500, vol=0.0015)
    f = fr["ETH"]
    arr = {"o": f["open"].to_numpy(), "h": f["high"].to_numpy(), "l": f["low"].to_numpy(), "c": f["close"].to_numpy()}
    rng = np.random.default_rng(0)
    pos = rng.integers(50, 2400, 300)
    for d in (1, -1):
        dirs = np.full(len(pos), d)
        for h, sl, tp in ((5, 0.3, 0.6), (10, 0.15, 1.0), (3, 0.5, 0.3)):
            r = R.simulate_paths(arr, pos, dirs, h, sl, tp, trail, cfg, 0.02)
            for k in range(0, 300, 7):
                assert r["net"][k] == pytest.approx(_engine_net(arr, int(pos[k]), d, h, sl, tp, trail, cfg, 0.02), abs=1e-9), (d, h, sl, tp, k)


def test_opportunity_dataset_records_the_path_not_just_the_endpoint():
    cfg = dict(config.DEFAULT_SETTINGS)
    o = build_opportunities(make_frames(3200, vol=0.001), cfg)
    for c in ("long_mfe_5", "long_mae_5", "short_mfe_15", "long_net_3", "long_best_tp", "long_worst_adverse", "long_tp_first_sl0.3_tp0.6", "regime", "symbol", "z3"):
        assert c in o.columns
    assert (o["long_mfe_5"] >= 0).all() and (o["long_mae_5"] <= 0).all()
    assert (o["long_mfe_15"] >= o["long_mfe_1"] - 1e-12).all() and (o["long_mae_15"] <= o["long_mae_1"] + 1e-12).all()   # excursions only grow with time
    assert ((o["long_gross_5"] + o["short_gross_5"]).abs() < 1e-9).all()                                               # long and short are exact mirrors before costs
    assert (o["long_net_5"] < o["long_gross_5"]).all()                                                                   # costs always subtract


def test_split_never_lets_the_holdout_leak_into_dev():
    o = build_opportunities(make_frames(30 * 1440), dict(config.DEFAULT_SETTINGS))
    sp = R.split(o)
    assert sp["A"].index.max() < sp["B"].index.min() and sp["dev"].index.max() < sp["H"].index.min()
    assert len(sp["A"]) + len(sp["B"]) + len(sp["H"]) == len(o)
