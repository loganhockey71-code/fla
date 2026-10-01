"""Command line entry point.  python -m crypto_ai.cli <command>

  scalp-train        download 1m history, store it, train the trade-outcome model, promote it only if it earns net P&L out-of-sample
  ws-collect         real-time microstructure collector (BTC/ETH/XRP best bid/ask, trades, depth) - research only, does not trade
  compact-microstructure  roll aged raw ticks into 1-second bars so the database stays bounded
  micro-research     maker-vs-taker-vs-no-trade edge investigation on collected microstructure data
  scalp-research     the edge investigation: opportunity dataset, baselines vs LightGBM, entry/hold/stop/target grid, sealed holdout
  scalp-backtest     walk-forward backtest of the scalper on real 1m candles (same engine as live) with a full report
  tick [--burst S]   one cycle: collect -> news -> scalper pass (+ learning in the 05:00-23:59 local window); --burst keeps
                     passing every --every s for S seconds so a 5-minute scheduler still gets 1-minute reaction
  scalp              run scalper passes forever (local, ~every 20 s)
  optimize-exits     walk-forward exit-multiplier search; the result is WRITTEN BACK and used by every new live trade
  learn              run the learning pass now (post-mortem stats, exit policy, guarded retrain)
  train              LEGACY: the old 1h/24h/48h direction models (they no longer trade)
  status             show what is in the database
"""
import argparse
import time
import traceback
from datetime import datetime, timedelta, timezone


from . import coinbase, coingecko, evaluator, metrics, paper, predictor, realtime
from . import learning
from .scalp import learn as scalp_learn, live as scalp_live
from .research import registry
from .config import BAR, HORIZONS, PRODUCTS, SYMBOLS, TRAIN_DAYS
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
    """LEGACY direction models (1h/24h/48h). Kept for the old dashboard pages; they no longer place trades."""
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
            for h in [str(h) for h in HORIZONS]:
                bt = m["backtest_metrics"][h]
                print(f"   {h}h  OOS n={bt['oos_samples']}  dir.acc={bt['directional_accuracy']:.1%} "
                      f"(always-up {bt['always_up_accuracy']:.1%})  AUC={bt['auc']:.3f}  "
                      f"strategy={bt['strategy']['compounded_return_pct']:+.2f}% vs buy&hold {bt['buy_and_hold_return_pct']:+.1f}%")
            print(f"   saved as {m['version']}")


def _scalp_line(r: dict) -> str:
    sg = r.get("signals") or {}
    sig = f", signals: {sg.get('raised', 0)} raised / {sg.get('confirmed', 0)} confirmed / {sum((sg.get('failed') or {}).values())} failed confirmation" if sg else ""
    return (f"{len(r['opened'])} opened, {len(r['closed'])} closed{sig} - {r['note']}" + (f" (rejected: {r['rejected']})" if r["rejected"] else "")
            + (f" [graded {r['graded']}]" if r.get("graded") else ""))


def scalp_cycle(db: DB, cfg: dict) -> dict:
    """One scalper pass, then the cheap bookkeeping that must follow it (grading every finished prediction). A grading failure never
    hides the pass result: it is reported and the next pass retries."""
    r = scalp_live.run_pass(db, cfg)
    try:
        r["graded"] = scalp_learn.run_bookkeeping(db, cfg)["graded"]
    except Exception:
        traceback.print_exc()
    return r


def _learn_line(r: dict) -> str:
    return r.get("note") or ", ".join(f"{k}={v.get('decision', v) if isinstance(v, dict) else v}" for k, v in r.items() if k != "active")


def tick(db: DB, burst: int = 0, every: int = 20) -> None:
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
    step("sudden", lambda: [f["text"][:80] for f in realtime.run_watch_cycle(db, cfg, snaps)])   # information only: it no longer trades
    prices = {s: v["price"] for s, v in snaps.items()}
    spreads = {s: v.get("spread_pct") for s, v in snaps.items()}
    if len(prices) == len(SYMBOLS):                       # let any position opened by the retired engines finish instead of orphaning it
        step("close_trades", lambda: paper.close_due_positions(db, cfg, prices, spreads, coinbase.price_at_safe))
        step("risk_exits_ai", lambda: paper.manage_open_positions(db, cfg, prices, spreads))
    step("scalp", lambda: _scalp_line(scalp_cycle(db, cfg)))
    step("scalp_learn", lambda: _learn_line(scalp_learn.run_learning(db, cfg)))
    if cfg["legacy_predictions_enabled"]:
        step("evaluate", lambda: evaluator.evaluate_due(db, cfg))
        step("predict", lambda: len(predictor.run_predictions(db, cfg)))
        step("standing", lambda: realtime.publish_standing(db, cfg))
        if len(prices) == len(SYMBOLS):
            step("portfolio", lambda: round(paper.snapshot_portfolio(db, cfg, prices)["total_value"], 2))
        step("metrics", lambda: metrics.recompute(db))
        step("learn", lambda: learning.run_all(db, cfg))
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] " + " | ".join(steps), flush=True)
    if burst > 0:                                         # keep reacting minute by minute for the rest of this scheduler slot
        end = time.time() + burst
        while time.time() + every < end:
            time.sleep(every)
            try:
                r = scalp_cycle(db, db.settings())
                for line in r["opened"] + r["closed"]:
                    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] SCALP {line}", flush=True)
            except Exception:
                traceback.print_exc()
                db = _reconnect()


def scalp_forever(db: DB, every: int, collect_every_s: int = 300) -> None:
    """The self-contained local runner (`cli scalp`): everything the scalper needs, so it does not depend on `tick` also running.
    Each loop: refresh the news/research sources that are due (the scalper's news layer reads them; each source keeps its own poll
    interval, so this is cheap), refresh the price snapshots the dashboard shows every few minutes, then one scalper pass + grading +
    the learning pass. A failure in one part never stops the others."""
    last_collect = 0.0
    while True:
        try:
            cfg = db.settings()
            try:
                registry.run_due(db)
            except Exception:
                traceback.print_exc()
            if time.time() - last_collect > collect_every_s:
                try:
                    collect(db)
                except Exception:
                    traceback.print_exc()
                last_collect = time.time()
            r = scalp_cycle(db, cfg)
            for line in r["opened"] + r["closed"]:
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] SCALP {line}", flush=True)
            if r.get("error"):
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] SCALP entry side failed: {r['error']}", flush=True)
            scalp_learn.run_learning(db, cfg)
        except Exception:
            traceback.print_exc()
            db = _reconnect()
        time.sleep(every)


def _reconnect(max_wait: int = 300) -> DB:
    """Keep retrying with backoff instead of raising - a DNS/network blip on an unattended machine must
    never kill the whole loop/watch process. Used after any tick/pass fails, including the DB itself."""
    wait = 5
    while True:
        try:
            return DB()
        except Exception:
            traceback.print_exc()
            print(f"[reconnect] retrying in {wait}s ...", flush=True)
            time.sleep(wait)
            wait = min(wait * 2, max_wait)


def watch(db: DB, every: int) -> None:
    """Near-real-time loop: news/shock alerts, official news feeds every 90 s, and a scalper pass every pass."""
    official = ["sec_press", "sec_statements", "fed_press", "fed_speeches", "cftc_press", "cftc_enforcement", "congress_api", "xrpl_rippled", "eth_foundation", "eth_geth"]
    while True:
        try:
            cfg = db.settings()
            registry.run_due(db, only=official, interval_s=90)
            snaps = {s: predictor.micro_snapshot(s) for s in SYMBOLS}
            fresh = realtime.run_watch_cycle(db, cfg, snaps)               # news/shock alerts for the dashboard; they do not trade
            for f in fresh:
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {f['urgency'].upper()} {f['text']}", flush=True)
            r = scalp_cycle(db, cfg)
            for line in r["opened"] + r["closed"]:
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] SCALP {line}", flush=True)
        except Exception:
            traceback.print_exc()
            db = _reconnect()
        time.sleep(every)


def cmd_optimize_exits(db: DB) -> None:
    cfg = db.settings()
    res = scalp_learn.maybe_learn_exits(db, cfg, datetime.now(timezone.utc), force=True)
    print(f"[optimize-exits] {res['decision']}" + (f": {res['reason']}" if res.get("reason") else ""))
    st = scalp_learn._state_get(db, "exit_policy", {}) or {}
    for k, v in (st.get("info") or {}).items():
        print(f"   {k:22s} entries={v.get('entries'):>4}  {v.get('reason')}")
    print(f"   policy now in force (used by every new live trade): {st.get('policy') or 'defaults'}")


def cmd_scalp_train(db: DB, days: int) -> None:
    """Bootstrap: store 1m history in the database, then run the guarded champion/challenger training."""
    from .scalp import data as sdata
    cfg = db.settings()
    frames = sdata.load_frames(days)
    for s, df in frames.items():
        sdata.save_candles(db, s, df)
    print("[scalp-train] candles stored; training the trade-outcome model ...")
    res = scalp_learn.maybe_retrain(db, cfg, datetime.now(timezone.utc), force=True, frames=frames)
    print(f"[scalp-train] {res.get('decision')}: {res.get('reason')}")
    if res.get("holdout"):
        print(f"   holdout challenger={res['holdout']['challenger']} champion={res['holdout']['champion']} p={res['holdout']['p']:.2f}")


def cmd_scalp_backtest(a) -> None:
    import json
    from .config import DEFAULT_SETTINGS
    from .scalp import backtest, data as sdata, report
    cfg = dict(DEFAULT_SETTINGS)
    if a.fee is not None:
        cfg["trading_fee_pct"] = a.fee
    if a.slip is not None:
        cfg["slippage_pct"] = a.slip
    if a.hold_bars:
        cfg["scalp_hold_bars"] = a.hold_bars
    if a.no_confirm:                                      # COMPARISON ONLY: what trading on the prediction alone (no next-candle confirmation) would have done
        cfg["scalp_confirm_required"] = False
    if a.diagnostic_top:                                  # DIAGNOSTIC: bypass validation, edge floor and cost gate to show what forced trading costs
        cfg["scalp_min_edge_pct"] = -100.0
        cfg["scalp_min_net_target_pct"] = -100.0
    frames = sdata.load_frames(a.days)
    res = backtest.walk_forward(frames, cfg, a.train_days, a.val_days, a.test_days, learn_exits=not a.no_exit_learning,
                                force_top_quantile=a.diagnostic_top)
    if a.diagnostic_top:
        print(f"*** DIAGNOSTIC RUN: trading the model's top {a.diagnostic_top:.2%} signals with NO validation, NO edge floor and NO cost gate. This is NOT the strategy; it shows what forced trading costs. ***")
    print(report.render(res, cfg))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(report.to_jsonable(res), fh, indent=1, default=str)


def cmd_scalp_research(a) -> None:
    from .config import DEFAULT_SETTINGS
    from .scalp import research_run
    research_run.run(dict(DEFAULT_SETTINGS), a.section or ["facts", "timing", "ic", "baselines", "grid", "model", "holdout"], a.days)


def cmd_ws_collect(db: DB, seconds: float | None) -> None:
    import asyncio
    from .scalp import micro_collect
    asyncio.run(micro_collect.run(db, run_seconds=seconds))


def cmd_compact_microstructure(db: DB) -> None:
    from .scalp import micro_compact
    print(micro_compact.compact_due(db, db.settings()))


def cmd_micro_research(db: DB) -> None:
    from .config import SYMBOLS
    from .scalp import micro_research
    micro_research.run(db, db.settings(), SYMBOLS)


def cmd_collect_datasources(db: DB, loop: bool = False, every: int = 30) -> None:
    """Each adapter still only actually fetches when its own `poll_every_s` is due (e.g. funding every 5 min, order
    books every 30s) - `every` here is just how often this loop CHECKS which ones are due, so it should be <= the
    shortest poll_every_s in use (30s covers everything currently registered)."""
    from datetime import datetime, timezone
    from .datasources import datalake, registry
    while True:
        try:
            res = datalake.run_due(registry.ALL, datetime.now(timezone.utc), db=db)
            for name, r in res.items():
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {name:28s} {'ok' if r['ok'] else 'FAILED'} rows={r.get('rows', '-')} {r.get('error', '') or r.get('path', '')}", flush=True)
        except Exception:
            traceback.print_exc()
            db = _reconnect()
        if not loop:
            return
        time.sleep(every)


def cmd_ask(question: str) -> None:
    from . import llm
    if not llm.enabled():
        print("OPENROUTER_API_KEY is not set - the AI analysis layer is disabled. See .env.example.")
        return
    free = llm.free_models()
    if not free:
        print("No $0 OpenRouter models are currently available (or the key/network failed) - nothing to ask.")
        return
    ans = llm.research_question(question)
    print(ans or "(no answer - the request failed or timed out; this is analysis-only and never affects trading)")


def cmd_status(db: DB) -> None:
    for t in ["scalp_trades", "scalp_lessons", "scalp_models", "scalp_state", "market_data", "candles", "model_versions", "predictions", "prediction_results", "paper_trades",
              "portfolio", "news_items", "events", "live_signals", "post_mortems", "learned_patterns", "model_challenges", "research_events", "event_duplicates", "macro_data", "legislative_items", "prediction_market_snapshots"]:
        print(f"{t:20s} {db.one(f'select count(*) as n from {t}')['n']}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("scalp-train", help="download + store 1m history and train the trade-outcome model (guarded promotion)")
    st.add_argument("--days", type=int, default=30)
    sb = sub.add_parser("scalp-backtest", help="walk-forward backtest on real 1m candles through the live engine")
    sb.add_argument("--days", type=int, default=75)
    sb.add_argument("--fee", type=float, help="override trading_fee_pct per side (default: your setting, 0.4)")
    sb.add_argument("--slip", type=float, help="override slippage_pct per side (default: your setting, 0.1)")
    sb.add_argument("--train-days", type=float, default=30)
    sb.add_argument("--val-days", type=float, default=6)
    sb.add_argument("--test-days", type=float, default=6)
    sb.add_argument("--no-exit-learning", action="store_true")
    sb.add_argument("--hold-bars", type=int, help="max minutes per trade (default 15)")
    sb.add_argument("--no-confirm", action="store_true", help="COMPARISON ONLY: trade on the prediction alone, without waiting for the next candle to confirm it (live always confirms)")
    sb.add_argument("--diagnostic-top", type=float, metavar="Q", help="DIAGNOSTIC ONLY: force-trade the model's top Q fraction of signals (e.g. 0.005), bypassing validation and the cost gate")
    sb.add_argument("--out", help="write the full result as JSON")
    wc = sub.add_parser("ws-collect", help="real-time microstructure collector (research only; run continuously on your own machine)")
    wc.add_argument("--seconds", type=float, help="stop after this many seconds (default: forever)")
    sub.add_parser("compact-microstructure", help="roll aged raw ticks into 1-second bars, keep the database bounded")
    sub.add_parser("micro-research", help="maker vs taker vs no-trade edge investigation on collected microstructure data")
    cd_ = sub.add_parser("collect-datasources", help="poll due free data-source adapters (funding, OI, liquidations, basis, on-chain, ...) into the local Parquet data lake")
    cd_.add_argument("--loop", action="store_true", help="keep running forever instead of a single pass")
    cd_.add_argument("--every", type=int, default=30, help="seconds between due-checks in --loop mode (each adapter still respects its own poll_every_s)")
    sr = sub.add_parser("scalp-research", help="edge investigation on cached 1m candles (the last 14 days stay sealed until `holdout`)")
    sr.add_argument("--days", type=int, default=75)
    sr.add_argument("--section", action="append", choices=["facts", "timing", "ic", "baselines", "grid", "model", "holdout"])
    sc = sub.add_parser("scalp", help="run scalper passes forever")
    sc.add_argument("--every", type=int, default=20)
    t = sub.add_parser("train", help="LEGACY direction models")
    t.add_argument("--days", type=int, default=TRAIN_DAYS)
    t.add_argument("--variant", choices=["market", "research"], help="train only this variant")
    tk = sub.add_parser("tick")
    tk.add_argument("--burst", type=int, default=0, help="after the tick, keep running scalper passes for this many seconds")
    tk.add_argument("--every", type=int, default=20, help="seconds between burst passes")
    l = sub.add_parser("loop")
    l.add_argument("--every", type=int, default=300)
    r = sub.add_parser("research", help="poll research sources that are due (FRED, Congress, feeds, prediction markets)")
    r.add_argument("--source", action="append", help="only this source key (repeatable)")
    r.add_argument("--force", action="store_true", help="ignore polling intervals")
    rl = sub.add_parser("research-loop", help="poll research sources forever, each at its own interval")
    rl.add_argument("--every", type=int, default=60, help="how often to check which sources are due (seconds)")
    lr = sub.add_parser("learn", help="scalper learning pass now (stats, exit policy, guarded retrain); legacy prediction learning too if enabled")
    lr.add_argument("--force-retrain", action="store_true", help="legacy models: attempt a champion/challenger comparison now")
    w = sub.add_parser("watch", help="real-time watcher: news/shock alerts + a scalper pass, one every N seconds")
    w.add_argument("--every", type=int, default=60)
    sub.add_parser("optimize-exits", help="walk-forward exit-multiplier search; the result is applied to new live trades")
    ask = sub.add_parser("ask", help="ad-hoc research question via OpenRouter ($0 models only; analysis only, never trading)")
    ask.add_argument("question")
    sub.add_parser("status")
    sub.add_parser("selfcheck", help="read-only end-to-end verification of the running system")
    a = ap.parse_args(argv)
    if a.cmd == "ask":
        return cmd_ask(a.question)
    if a.cmd == "scalp-backtest":                         # needs no database
        return cmd_scalp_backtest(a)
    if a.cmd == "scalp-research":
        return cmd_scalp_research(a)
    if a.cmd == "ws-collect":
        return cmd_ws_collect(DB(), a.seconds)
    if a.cmd == "compact-microstructure":
        return cmd_compact_microstructure(DB())
    if a.cmd == "micro-research":
        return cmd_micro_research(DB())
    if a.cmd == "collect-datasources":
        return cmd_collect_datasources(DB(), a.loop, a.every)
    db = DB()
    if a.cmd == "train":
        cmd_train(db, a.days, a.variant)
    elif a.cmd == "scalp-train":
        cmd_scalp_train(db, a.days)
    elif a.cmd == "scalp":
        scalp_forever(db, a.every)
    elif a.cmd == "tick":
        tick(db, a.burst, a.every)
    elif a.cmd == "loop":
        while True:
            try:
                tick(db)
            except Exception:
                traceback.print_exc()
                db = _reconnect()
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
                db = _reconnect()
            time.sleep(a.every)
    elif a.cmd == "learn":
        cfg = db.settings()
        print("scalper:", scalp_learn.run_learning(db, cfg, force=True))
        if cfg["legacy_predictions_enabled"]:
            print(learning.run_all(db, cfg, force_retrain=a.force_retrain, verbose=True))
    elif a.cmd == "watch":
        watch(db, a.every)
    elif a.cmd == "optimize-exits":
        cmd_optimize_exits(db)
    elif a.cmd == "status":
        cmd_status(db)
    elif a.cmd == "selfcheck":
        from . import selfcheck
        raise SystemExit(selfcheck.run(db))


if __name__ == "__main__":
    main()
