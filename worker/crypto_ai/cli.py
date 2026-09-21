"""Command line entry point.  python -m crypto_ai.cli <command>

  train [--days N]   fetch history, train + backtest per-coin models, activate them
  tick               one full cycle: collect -> news -> detect -> evaluate -> predict -> paper trade -> metrics
  loop [--every S]   run `tick` forever (local, near-real-time)
  status             show what is in the database
"""
import argparse
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone


from . import coinbase, coingecko, detector, evaluator, metrics, paper, predictor
from .research import registry
from .config import BAR, PRODUCTS, SYMBOLS
from .db import DB, JsonList
from .model import train_symbol


def save_candles(db: DB, symbol: str, gran: int, df) -> None:
    rows = [(symbol, gran, ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume) for ts, r in df.iterrows()]
    db.bulk("insert into candles (symbol, granularity, ts, open, high, low, close, volume) values %s "
            "on conflict (symbol, granularity, ts) do nothing", rows)


def collect(db: DB) -> dict:
    """Live snapshot of price / spread / order book / flow, with CoinGecko as reference + fallback."""
    cg = coingecko.reference_prices()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    snaps = {}
    for s in SYMBOLS:
        try:
            snap = predictor.micro_snapshot(s)
            source = "coinbase"
        except Exception as e:
            if s not in cg:
                print(f"[collect] {s}: Coinbase and CoinGecko both failed ({e})")
                continue
            snap = {"price": cg[s], "bid": None, "ask": None, "spread_pct": None, "volume_24h": None,
                    "ob_imbalance": None, "buy_pressure": None}
            source = "coingecko"
        snaps[s] = snap
        db.insert("market_data", {"symbol": s, "ts": now, "price": snap["price"], "bid": snap.get("bid"),
                                  "ask": snap.get("ask"), "spread_pct": snap.get("spread_pct"),
                                  "volume_24h": snap.get("volume_24h"), "ob_imbalance": snap.get("ob_imbalance"),
                                  "buy_pressure": snap.get("buy_pressure"), "cg_price": cg.get(s), "source": source},
                  on_conflict="on conflict (symbol, ts) do nothing")
        try:
            save_candles(db, s, BAR, coinbase.closed_only(
                coinbase.candles(PRODUCTS[s], BAR, now - timedelta(hours=6), now), BAR, now))
        except Exception as e:
            print(f"[collect] candles {s}: {e}")
    return snaps


def cmd_train(db: DB, days: int, only: str | None = None) -> None:
    cfg = db.settings()
    end = datetime.now(timezone.utc)
    frames = {}
    for s in SYMBOLS:
        print(f"[train] downloading {days}d of 15m candles for {s} ...")
        frames[s] = coinbase.closed_only(coinbase.candles(PRODUCTS[s], BAR, end - timedelta(days=days), end), BAR, end)
        save_candles(db, s, BAR, frames[s])
        print(f"        {len(frames[s])} candles")
    from .research import features as rfeat
    rdata = rfeat.load(db)
    variants = ["market"] + (["research"] if rdata["macro"] else [])
    variants = [v for v in variants if only in (None, v)]
    if "research" not in variants and only != "market":
        print("[train] no macro data yet - run `research --source fred_api --force` first to also train the research variant")
    for variant in variants:
        for s in SYMBOLS:
            print(f"[train] {s} [{variant}]: walk-forward training ...")
            m = train_symbol(s, frames, cfg, variant, rdata)
            db.run("update model_versions set is_active=false where symbol=%s and variant=%s and is_active", [s, variant])
            db.insert("model_versions", {**m, "feature_names": JsonList(m["feature_names"]), "is_active": True})
            for h in ("24", "48"):
                bt = m["backtest_metrics"][h]
                print(f"   {h}h  OOS n={bt['oos_samples']}  dir.acc={bt['directional_accuracy']:.1%} "
                      f"(always-up {bt['always_up_accuracy']:.1%})  AUC={bt['auc']:.3f}  "
                      f"strategy={bt['strategy']['compounded_return_pct']:+.2f}% vs buy&hold {bt['buy_and_hold_return_pct']:+.1f}%")
            print(f"   saved as {m['version']}")


def tick(db: DB) -> None:
    cfg = db.settings()
    steps = []

    def step(name, fn):
        try:
            r = fn()
            steps.append(f"{name}: {r}")
            return r
        except Exception:
            traceback.print_exc()
            steps.append(f"{name}: FAILED")

    snaps = step("collect", lambda: collect(db)) or {}
    step("research", lambda: {k: (v.get("new", v.get("status"))) for k, v in registry.run_due(db).items()})
    step("detect", lambda: [f["text"] for f in detector.run_detector(db, cfg, snaps)])
    prices = {s: v["price"] for s, v in snaps.items()}
    spreads = {s: v.get("spread_pct") for s, v in snaps.items()}
    if len(prices) == len(SYMBOLS):
        step("close_trades", lambda: paper.close_due_positions(db, cfg, prices, spreads, coinbase.price_at_safe))
    n_eval = step("evaluate", lambda: evaluator.evaluate_due(db, cfg))
    step("predict", lambda: len(predictor.run_predictions(db, cfg)))
    if len(prices) == len(SYMBOLS):
        step("portfolio", lambda: round(paper.snapshot_portfolio(db, cfg, prices)["total_value"], 2))
    step("metrics", lambda: metrics.recompute(db))
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] " + " | ".join(steps))


def cmd_status(db: DB) -> None:
    for t in ["market_data", "candles", "model_versions", "predictions", "prediction_results", "paper_trades",
              "portfolio", "news_items", "events", "research_events", "event_duplicates", "macro_data", "legislative_items", "prediction_market_snapshots"]:
        print(f"{t:20s} {db.one(f'select count(*) as n from {t}')['n']}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--days", type=int, default=270)
    t.add_argument("--variant", choices=["market", "research"], help="train only this variant")
    sub.add_parser("tick")
    l = sub.add_parser("loop")
    l.add_argument("--every", type=int, default=300)
    r = sub.add_parser("research", help="poll research sources that are due (FRED, Congress, feeds, prediction markets)")
    r.add_argument("--source", action="append", help="only this source key (repeatable)")
    r.add_argument("--force", action="store_true", help="ignore polling intervals")
    rl = sub.add_parser("research-loop", help="poll research sources forever, each at its own interval")
    rl.add_argument("--every", type=int, default=60, help="how often to check which sources are due (seconds)")
    sub.add_parser("status")
    sub.add_parser("selfcheck", help="read-only end-to-end verification of the running system")
    a = ap.parse_args(argv)
    db = DB()
    if a.cmd == "train":
        cmd_train(db, a.days, a.variant)
    elif a.cmd == "tick":
        tick(db)
    elif a.cmd == "loop":
        while True:
            try:
                tick(db)
            except Exception:
                traceback.print_exc()
                db = DB()
            time.sleep(a.every)
    elif a.cmd == "research":
        for k, v in registry.run_due(db, a.source, a.force).items():
            print(f"{k:16s} {v}")
    elif a.cmd == "research-loop":
        while True:
            try:
                registry.run_due(db)
            except Exception:
                traceback.print_exc()
                db = DB()
            time.sleep(a.every)
    elif a.cmd == "status":
        cmd_status(db)
    elif a.cmd == "selfcheck":
        from . import selfcheck
        raise SystemExit(selfcheck.run(db))


if __name__ == "__main__":
    main()
