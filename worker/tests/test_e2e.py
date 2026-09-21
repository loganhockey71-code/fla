"""End-to-end test of the paper-trading loop on a SCRATCH Postgres (never your real Supabase).

    TEST_DATABASE_URL=postgresql://...scratch... pytest tests/test_e2e.py

Uses LIVE Coinbase prices and independently recomputes every number (fills, fees, P/L, portfolio, scoring, metrics).
It needs a fresh scratch database with schema.sql + schema_research.sql applied, because predictions are append-only.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="set TEST_DATABASE_URL to a scratch Postgres")


@pytest.fixture(scope="module")
def db():
    from crypto_ai.db import DB
    if "supabase" in DB_URL:
        pytest.skip("refusing to run destructive-looking e2e test against Supabase")
    d = DB(DB_URL)
    if d.one("select count(*) n from predictions")["n"]:
        pytest.skip("needs a fresh scratch database (predictions is append-only)")
    return d


def mk_pred(db, mv, symbol, h, signal, bull, created, variant="market", price=None):
    from crypto_ai import coinbase
    from crypto_ai.config import PRODUCTS
    from crypto_ai.db import JsonList
    price = price or coinbase.price_at(PRODUCTS[symbol], created) or coinbase.ticker(PRODUCTS[symbol])["price"]
    row = {"created_at": created, "symbol": symbol, "horizon_h": h, "target_time": created + timedelta(hours=h), "price_at_prediction": price,
           "signal": signal, "bullish_prob": bull, "bearish_prob": 1 - bull, "confidence": max(bull, 1 - bull), "range_low": price * 0.97,
           "range_high": price * 1.03, "reasons": JsonList(["e2e"]), "explanation": "e2e", "model_version_id": mv["id"], "model_version": mv["version"],
           "features": {"model_inputs": {"x": 1.0}}, "data_cutoff": created - timedelta(seconds=1), "variant": variant, "run_id": uuid.uuid4(),
           "research_features": {"macro": {}, "events": {}}}
    row["id"] = db.insert("predictions", row, returning="id")
    return row


def test_full_paper_trading_loop(db):
    from crypto_ai import coinbase, evaluator, metrics, paper
    from crypto_ai.config import PRODUCTS, SYMBOLS
    from crypto_ai.db import JsonList
    from crypto_ai.predictor import micro_snapshot

    cfg = db.settings()
    assert cfg["starting_balance"] == 1000 and cfg["trading_fee_pct"] == 0.4 and cfg["slippage_pct"] == 0.10
    fee, slip = cfg["trading_fee_pct"] / 100, cfg["slippage_pct"] / 100
    mid_id = db.insert("model_versions", {"symbol": "BTC", "version": "e2e-model", "feature_names": JsonList(["x"]), "model_blob": {"24": ""},
                                          "is_active": False, "variant": "market"})
    mv = {"id": mid_id, "version": "e2e-model"}

    # live market data used for every fill
    snaps = {s: micro_snapshot(s) for s in SYMBOLS}
    prices = {s: v["price"] for s, v in snaps.items()}
    spreads = {s: v["spread_pct"] for s, v in snaps.items()}
    now = datetime.now(timezone.utc)

    def snapshot():
        return paper.snapshot_portfolio(db, cfg, prices)

    assert snapshot()["total_value"] == 1000.0

    # ---- 7. normal BUY: 10% of $1,000, real price + spread + slippage + fee
    btc = mk_pred(db, mv, "BTC", 24, "BUY", 0.60, now)
    paper.act_on_prediction(db, btc, snaps["BTC"], prices, cfg)
    t = db.one("select * from paper_trades where prediction_id=%s", [btc["id"]])
    s = snaps["BTC"]
    exp_fill = s["price"] * (1 + s["spread_pct"] / 2 / 100 + slip)
    assert t["status"] == "open" and t["market_price"] == pytest.approx(s["price"])           # real market price recorded
    assert t["exec_price"] == pytest.approx(exp_fill) and t["exec_price"] > t["market_price"]    # buyer pays more than mid
    assert t["amount_invested"] + t["fee_entry"] == pytest.approx(100.0)                         # 10% of $1,000, fee included
    assert t["fee_entry"] == pytest.approx(t["amount_invested"] * fee) and t["quantity"] * t["exec_price"] == pytest.approx(t["amount_invested"])
    assert t["slippage_entry_pct"] == 0.10 and t["planned_exit_at"] - t["opened_at"] == timedelta(hours=24)
    st = snapshot()
    assert st["cash"] == pytest.approx(900.0) and st["total_value"] == pytest.approx(1000 - t["fee_entry"] - t["quantity"] * (t["exec_price"] - s["price"]), abs=1e-6)

    # ---- 7. high-confidence BUY: 20% cap, never more than cash
    tv_before = paper.portfolio_state(db, cfg, prices)["total_value"]
    eth = mk_pred(db, mv, "ETH", 24, "BUY", 0.80, now)
    paper.act_on_prediction(db, eth, snaps["ETH"], prices, cfg)
    te = db.one("select * from paper_trades where prediction_id=%s", [eth["id"]])
    assert te["amount_invested"] + te["fee_entry"] == pytest.approx(0.20 * tv_before)
    snapshot()

    # ---- HOLD never trades; SELL with nothing to sell is skipped (spot only, no shorting)
    xrp = mk_pred(db, mv, "XRP", 24, "SELL", 0.30, now)
    paper.act_on_prediction(db, xrp, snaps["XRP"], prices, cfg)
    sk = db.one("select * from paper_trades where prediction_id=%s", [xrp["id"]])
    assert sk["status"] == "skipped" and "no open long" in sk["skip_reason"]
    hold = mk_pred(db, mv, "XRP", 48, "HOLD", 0.50, now)
    paper.act_on_prediction(db, hold, snaps["XRP"], prices, cfg)
    assert db.one("select count(*) n from paper_trades where prediction_id=%s", [hold["id"]])["n"] == 0

    # ---- 7/8. SELL signal closes the BTC long early at the bid side; P/L recomputed independently
    sell = mk_pred(db, mv, "BTC", 48, "SELL", 0.30, now)
    paper.act_on_prediction(db, sell, snaps["BTC"], prices, cfg)
    t = db.one("select * from paper_trades where id=%s", [t["id"]])
    exp_exit = s["price"] * (1 - s["spread_pct"] / 2 / 100 - slip)
    assert t["status"] == "closed" and t["exit_reason"] == "sell_signal" and t["exit_price"] == pytest.approx(exp_exit)
    cost = t["amount_invested"] + t["fee_entry"]
    exp_pnl = t["quantity"] * exp_exit * (1 - fee) - cost
    assert t["pnl_usd"] == pytest.approx(exp_pnl) and t["pnl_pct"] == pytest.approx(exp_pnl / cost * 100) and t["pnl_usd"] < 0    # flat market: costs only
    snapshot()

    # ---- 8. horizon exit at a known price (+3%) closes the ETH long
    db.run("update paper_trades set planned_exit_at = now() - interval '1 minute' where id=%s", [te["id"]])
    exit_mid = te["market_price"] * 1.03
    assert paper.close_due_positions(db, cfg, prices, spreads, lambda sym, when: exit_mid) == 1
    te = db.one("select * from paper_trades where id=%s", [te["id"]])
    cost_e = te["amount_invested"] + te["fee_entry"]
    exit_px = exit_mid * (1 - spreads["ETH"] / 2 / 100 - slip)
    exp_e = te["quantity"] * exit_px * (1 - fee) - cost_e
    assert te["status"] == "closed" and te["exit_reason"] == "horizon" and te["pnl_usd"] == pytest.approx(exp_e) and te["pnl_usd"] > 0
    st = snapshot()
    closed = db.all("select * from paper_trades where status='closed' order by closed_at")
    total_pnl = sum(x["pnl_usd"] for x in closed)
    assert st["positions_value"] == 0 and st["cash"] == pytest.approx(1000 + total_pnl) and st["total_value"] == pytest.approx(1000 + total_pnl)
    assert te["balance_after"] == pytest.approx(1000 + total_pnl)                          # portfolio value stored on the closing trade
    with pytest.raises(Exception, match="immutable"):
        db.run("update paper_trades set pnl_usd = 999 where id=%s", [te["id"]])         # a closed trade can never be edited

    # ---- 9/10. evaluation: backdated predictions resolved against REAL historical prices
    old = now - timedelta(hours=26)
    cases = [("BTC", 24, "BUY", 0.60, "market", old), ("ETH", 24, "SELL", 0.30, "market", old), ("XRP", 24, "HOLD", 0.50, "market", old),
             ("BTC", 48, "BUY", 0.80, "market", now - timedelta(hours=50)), ("ETH", 48, "HOLD", 0.50, "market", now - timedelta(hours=50)),
             ("BTC", 24, "BUY", 0.60, "research", old)]
    preds = [mk_pred(db, mv, sym, h, sig, b, created, var) for sym, h, sig, b, var, created in cases]
    before = {p["id"]: dict(db.one("select * from predictions where id=%s", [p["id"]])) for p in preds}
    assert evaluator.evaluate_due(db, cfg) == 6
    assert evaluator.evaluate_due(db, cfg) == 0                                            # never scored twice
    for p in preds:
        r = db.one("select * from prediction_results where prediction_id=%s", [p["id"]])
        actual = coinbase.price_at(PRODUCTS[p["symbol"]], p["target_time"])
        ret = actual / p["price_at_prediction"] - 1
        band = cfg[f"hold_band_pct_{p['horizon_h']}h"]
        exp_correct = ret > 0 if p["signal"] == "BUY" else ret < 0 if p["signal"] == "SELL" else abs(ret) * 100 < band
        assert r["actual_price"] == pytest.approx(actual) and r["signal_correct"] == exp_correct
        assert r["directional_correct"] == ((ret > 0) == (p["bullish_prob"] > 0.5)) and r["high_confidence"] == (p["confidence"] >= 0.75)
        assert dict(db.one("select * from predictions where id=%s", [p["id"]])) == before[p["id"]]      # the prediction itself is untouched
    with pytest.raises(Exception, match="append-only"):
        db.run("update predictions set signal='BUY' where id=%s", [preds[0]["id"]])
    with pytest.raises(Exception, match="append-only"):
        db.run("update prediction_results set signal_correct = not signal_correct where prediction_id=%s", [preds[0]["id"]])
    with pytest.raises(Exception, match="append-only"):
        db.run("delete from predictions where id=%s", [preds[0]["id"]])

    # ---- 11. metrics recomputed independently
    metrics.recompute(db)
    rows = db.all("select p.horizon_h, p.symbol, p.signal, r.directional_correct d, r.signal_correct s, r.high_confidence hc from predictions p "
                  "join prediction_results r on r.prediction_id=p.id where p.variant='market'")
    perf = {(m["scope"], m["horizon_h"], m["slice"]): m for m in db.all("select * from performance_metrics where variant='market'")}

    def mean_d(sel):
        return sum(r["d"] for r in sel) / len(sel)
    assert perf[("ALL", None, "all")]["directional_accuracy"] == pytest.approx(mean_d(rows)) and perf[("ALL", None, "all")]["total_predictions"] == 5
    assert perf[("ALL", 24, "all")]["directional_accuracy"] == pytest.approx(mean_d([r for r in rows if r["horizon_h"] == 24]))
    assert perf[("ALL", 48, "all")]["directional_accuracy"] == pytest.approx(mean_d([r for r in rows if r["horizon_h"] == 48]))
    assert perf[("ALL", None, "high_conf")]["total_predictions"] == 1 and perf[("ALL", None, "high_conf")]["directional_accuracy"] == pytest.approx(mean_d([r for r in rows if r["hc"]]))
    assert perf[("ALL", None, "all")]["correct_predictions"] == sum(r["s"] for r in rows)
    assert db.one("select count(*) n from performance_metrics where variant='research' and slice='all' and scope='ALL' and horizon_h is null and total_predictions=1")["n"] == 1

    pm = {m["scope"]: m for m in db.all("select * from performance_metrics where slice='paper'")}
    pcts = [x["pnl_pct"] for x in closed]
    wins, losses = [x for x in pcts if x > 0], [x for x in pcts if x <= 0]
    usd_w, usd_l = sum(x["pnl_usd"] for x in closed if x["pnl_usd"] > 0), sum(x["pnl_usd"] for x in closed if x["pnl_usd"] <= 0)
    a = pm["ALL"]
    assert a["total_predictions"] == 2 and a["win_rate"] == pytest.approx(len(wins) / 2)
    assert a["avg_win"] == pytest.approx(sum(wins) / len(wins)) and a["avg_loss"] == pytest.approx(sum(losses) / len(losses))
    assert a["profit_factor"] == pytest.approx(usd_w / abs(usd_l)) and a["best_trade"] == pytest.approx(max(x["pnl_usd"] for x in closed))
    assert a["worst_trade"] == pytest.approx(min(x["pnl_usd"] for x in closed)) and a["extra"]["pnl_usd_total"] == pytest.approx(total_pnl)
    for sym in SYMBOLS:
        assert pm[sym]["extra"]["pnl_usd_total"] == pytest.approx(sum(x["pnl_usd"] for x in closed if x["symbol"] == sym))
    eq = [r["total_value"] for r in db.all("select total_value from portfolio order by ts")]
    peak, dd = 0, 0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak * 100)
    assert a["max_drawdown"] == pytest.approx(dd) and dd > 0
    # headline numbers exactly as the dashboard derives them
    last = db.one("select total_value from portfolio order by ts desc limit 1")["total_value"]
    assert last - 1000 == pytest.approx(total_pnl) and (last - 1000) / 1000 * 100 == pytest.approx(total_pnl / 10)
