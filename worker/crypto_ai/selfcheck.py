"""End-to-end verification of the running system. READ-ONLY: it never inserts, updates or deletes real data.
    python -m crypto_ai.cli selfcheck
Each check prints PASS / FAIL / WAIT (not enough data yet - not a failure) / INFO."""
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

from . import coinbase, coingecko, evaluator, paper
from .config import HORIZONS, PAPER_ONLY, PRODUCTS, SYMBOLS
from .http import SourceError, get_conditional
from .research import common
from .research.congress import api as congress_api
from .research.fred import fetch as fred_fetch

ROOT = Path(__file__).resolve().parents[2]
RESULTS: list[tuple[str, str, str]] = []


def rec(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    print(f"  [{status:4s}] {name}" + (f" - {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def now() -> datetime:
    return datetime.now(timezone.utc)


def age_min(ts) -> float:
    return (now() - ts).total_seconds() / 60


# ------------------------------------------------------------------ 1. live prices
def check_prices(db) -> None:
    section("1. Live prices (Coinbase, CoinGecko reference)")
    cg = coingecko.reference_prices()
    for s in SYMBOLS:
        try:
            t = coinbase.ticker(PRODUCTS[s])
            ok = t["bid"] <= t["price"] <= t["ask"] and t["bid"] > 0
            rec("PASS" if ok else "FAIL", f"{s} Coinbase live ticker", f"mid {t['price']:.4f}, spread {t['spread_pct']:.4f}%")
        except Exception as e:
            rec("FAIL", f"{s} Coinbase live ticker", type(e).__name__)
            continue
        if s in cg:
            d = abs(cg[s] / t["price"] - 1) * 100
            rec("PASS" if d < 1.5 else "FAIL", f"{s} Coinbase vs CoinGecko", f"differ by {d:.2f}%")
        else:
            rec("WAIT", f"{s} CoinGecko reference", "no response (rate limited?)")
        row = db.one("select price, ts, source, cg_price from market_data where symbol=%s order by ts desc limit 1", [s])
        if not row:
            rec("FAIL", f"{s} stored snapshot", "market_data is empty")
            continue
        a = age_min(row["ts"])
        rec("PASS" if a < 15 else "FAIL", f"{s} stored snapshot is fresh", f"{a:.1f} min old, source={row['source']}, drift vs live {abs(row['price'] / t['price'] - 1) * 100:.2f}%")
        c = db.one("select max(ts) t from candles where symbol=%s and granularity=900", [s])["t"]
        rec("PASS" if c and age_min(c) < 45 else "FAIL", f"{s} 15m candles are fresh", f"newest candle opened {age_min(c):.0f} min ago" if c else "none")
    try:
        r = requests.get("https://api.binance.com/api/v3/ping", timeout=8)
        rec("INFO", "Binance", f"HTTP {r.status_code} (the app does not use Binance; api.binance.com blocks US IPs)")
    except Exception as e:
        rec("INFO", "Binance", f"unreachable ({type(e).__name__}); not used by the app")


# ------------------------------------------------------------------ 2. FRED + Congress
def check_keys() -> None:
    section("2. FRED and Congress.gov (keys from environment)")
    for env in ("FRED_API_KEY", "CONGRESS_API_KEY"):
        rec("PASS" if os.environ.get(env) else "FAIL", f"{env} is set in the environment")
    try:
        obs = fred_fetch("DGS10", (now() - timedelta(days=14)).date().isoformat())
        rec("PASS", "FRED live call", f"latest 10Y yield {obs[-1][1]}% on {obs[-1][0]}")
    except SourceError as e:
        rec("FAIL", "FRED live call", str(e))
    try:
        j = congress_api("bill/119", limit=1)
        rec("PASS", "Congress.gov live call", f"bills endpoint ok ({len(j.get('bills', []))} returned)")
    except SourceError as e:
        rec("FAIL", "Congress.gov live call", str(e))


# ------------------------------------------------------------------ 3. research feeds
def registry_hint(key):
    from .research.feeds import HINTS
    return HINTS.get(key)


def check_feeds(db) -> None:
    section("3. Research feeds are ingesting")
    for src in db.all("select * from source_registry order by tier, key"):
        n_events = db.one("select count(*) n from research_events where source_key=%s", [src["key"]])["n"]
        n_dups = db.one("select count(*) n from event_duplicates where source_key=%s", [src["key"]])["n"]
        extra = ""
        if src["kind"] == "rss":
            try:
                st, body, _, _ = get_conditional(src["url"], None, None)
                entries = feedparser.parse(body).entries
                rel = 0
                for e in entries:
                    c = common.classify(e.get("title", ""), re.sub(r"<[^>]+>", " ", e.get("summary", "") or ""), src["tier"], registry_hint(src["key"]))
                    rel += bool(c and c["importance"] >= common.MIN_IMPORTANCE)
                extra = f", live feed: {len(entries)} entries read, {rel} relevant to BTC/ETH/XRP"
            except SourceError as e:
                rec("FAIL", f"{src['key']} live fetch", str(e))
                continue
        if src["last_polled_at"] is None:
            rec("FAIL", f"{src['key']}", "never polled")
            continue
        stale = age_min(src["last_success_at"]) > max(3 * src["poll_interval_s"] / 60, 30) if src["last_success_at"] else True
        ok = src["last_status"] in ("ok", "not_modified") and not stale
        rec("PASS" if ok else "FAIL", f"{src['key']} ({src['tier_label']})",
            f"status={src['last_status']}, last ok {age_min(src['last_success_at']):.0f} min ago, {n_events} events + {n_dups} folded copies{extra}"
            + (f", error={src['last_error']}" if src["last_error"] else ""))
    rec("INFO", "Ripple corporate announcements", "no public RSS feed exists (ripple.com feed URLs return 404); XRP is covered by the XRPL rippled release feed only")
    for tbl in ("macro_data", "legislative_items", "prediction_market_snapshots"):
        n = db.one(f"select count(*) n from {tbl}")["n"]
        rec("PASS" if n else "FAIL", f"{tbl} has data", f"{n} rows")


# ------------------------------------------------------------------ 4. duplicates
def check_duplicates(db) -> None:
    section("4. Research events saved without duplicates")
    q = lambda sql: db.one(sql)["n"]
    rec("PASS" if q("select count(*) n from (select source_key, external_id from research_events group by 1,2 having count(*)>1) x") == 0 else "FAIL", "no repeated (source, external id)")
    rec("PASS" if q("select count(*) n from (select source_url from research_events where source_url is not null and kind='news' group by 1 having count(*)>1) x") == 0 else "FAIL", "no repeated news URL")
    rec("PASS" if q("select count(*) n from event_duplicates d join research_events e on e.source_url = d.url and e.id = d.event_id") == 0 else "FAIL", "no event is also its own duplicate")
    rows = db.all("select id, title, detected_at from research_events where kind='news' order by detected_at desc limit 400")
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if abs((a["detected_at"] - b["detected_at"]).total_seconds()) < 72 * 3600 and common.similarity(a["title"], b["title"]) >= common.DUP_THRESHOLD:
                pairs.append((a["title"][:60], b["title"][:60]))
    rec("PASS" if not pairs else "FAIL", "no near-identical headlines left un-grouped (last 400 news events)", f"{len(pairs)} suspect pairs" + (f", e.g. {pairs[0]}" if pairs else ""))
    bad = db.all("""select l.external_id, count(*) n from research_events e
                    join legislative_items l on e.details->>'bill' = l.external_id where e.kind='legislation' group by l.external_id having count(*) > 3""")
    rec("PASS" if not bad else "FAIL", "unchanged bills do not re-emit events", f"{len(bad)} bills with >3 events" if bad else "ok")
    rec("PASS" if q("select count(*) n from (select external_id from legislative_items group by 1 having count(*)>1) x") == 0 else "FAIL", "one row per bill/hearing")


# ------------------------------------------------------------------ 5/6. predictions
def check_predictions(db, cfg) -> None:
    section("5-6. Predictions generated and permanently logged")
    latest = db.one("select run_id, max(created_at) t from predictions where run_id is not null group by run_id order by t desc limit 1")
    if not latest:
        rec("FAIL", "any prediction run exists")
        return
    rows = db.all("select symbol, horizon_h, variant from predictions where run_id=%s", [latest["run_id"]])
    have = {(r["symbol"], r["horizon_h"], r["variant"]) for r in rows}
    want = {(s, h, v) for s in SYMBOLS for h in HORIZONS for v in ("market", "research")}
    rec("PASS" if have >= want else "FAIL", "latest run covers BTC/ETH/XRP x 24h/48h x both variants", f"{len(have & want)}/12" + (f", missing {sorted(want - have)}" if want - have else ""))
    a = age_min(latest["t"])
    interval = cfg["prediction_interval_h"] * 60
    rec("PASS" if a < interval + 45 else "FAIL", "predictions are being made on schedule", f"latest run {a / 60:.1f} h ago (interval {cfg['prediction_interval_h']} h)")

    bad = db.all("""select id, symbol, horizon_h, variant, concat_ws(',',
        case when created_at is null then 'created_at' end, case when price_at_prediction is null or price_at_prediction<=0 then 'price' end,
        case when signal not in ('BUY','HOLD','SELL') then 'signal' end,
        case when abs(bullish_prob+bearish_prob-1)>1e-6 then 'probs!=1' end,
        case when abs(confidence-greatest(bullish_prob,bearish_prob))>1e-6 then 'confidence' end,
        case when not (range_low < price_at_prediction and price_at_prediction < range_high) then 'range' end,
        case when model_version is null or model_version_id is null then 'model' end,
        case when features is null or features->'model_inputs' is null then 'features' end,
        case when research_features is null or research_features->'macro' is null or research_features->'events' is null then 'research_features' end,
        case when data_cutoff > created_at then 'LOOKAHEAD cutoff' end,
        case when abs(extract(epoch from (target_time - created_at))/3600 - horizon_h) > 0.01 then 'target_time' end,
        case when (signal='BUY' and bullish_prob < %s) or (signal='SELL' and bearish_prob < %s) then 'signal vs threshold' end) as problems
        from predictions""", [cfg["signal_threshold_pct"] / 100 - 1e-9, cfg["signal_threshold_pct"] / 100 - 1e-9])
    bad = [b for b in bad if b["problems"]]
    n = db.one("select count(*) n from predictions")["n"]
    rec("PASS" if not bad else "FAIL", f"all {n} predictions carry every required field and pass sanity checks", f"{len(bad)} bad: {bad[:2]}" if bad else "")
    orphan = db.one("select count(*) n from predictions p left join model_versions m on m.id=p.model_version_id and m.version=p.model_version where m.id is null")["n"]
    rec("PASS" if orphan == 0 else "FAIL", "every prediction's model version exists in model_versions")
    snap = db.one("select research_features from predictions where variant='research' order by created_at desc limit 1")
    if snap:
        rf = snap["research_features"]
        rec("PASS" if rf.get("used_by_model") is True and rf.get("asof") else "FAIL", "research snapshot stored with prediction", f"{len([v for v in rf['macro'].values() if v is not None])}/14 macro, "
            f"{len([v for v in rf['events'].values() if v is not None])}/8 event features, {len(rf.get('top_events_24h', []))} top events")
    trig = {r["tgname"] for r in db.all("select t.tgname from pg_trigger t join pg_class c on c.oid=t.tgrelid where c.relname in ('predictions','prediction_results') and not t.tgisinternal")}
    rec("PASS" if {"predictions_no_update", "predictions_no_truncate", "results_no_update"} <= trig else "FAIL", "immutability triggers installed", ", ".join(sorted(trig)))
    try:
        db.run("update predictions set signal = signal where id = (select id from predictions limit 1)")
        rec("FAIL", "UPDATE on predictions is rejected")
    except Exception as e:
        rec("PASS", "UPDATE on predictions is rejected", str(e).splitlines()[0][:90])


# ------------------------------------------------------------------ 7/8. trades + portfolio
def check_paper(db, cfg, legacy: bool = True) -> None:
    section("7-8. Paper trades and the fake portfolio")
    trades = db.all("select * from paper_trades where account='ai' order by id")
    real = [t for t in trades if t["status"] in ("open", "closed")]
    fee = cfg["trading_fee_pct"] / 100
    if not real:
        rec("WAIT", "fill prices / fees / slippage", "no trade has been opened yet (no BUY from the trading model). Verified by tests/test_e2e.py on a scratch database")
    for t in real:
        problems = []
        exp_min = t["market_price"] * (1 + cfg["slippage_pct"] / 100)
        if t["exec_price"] < exp_min * (1 - 1e-9):
            problems.append("fill better than mid+slippage")
        if abs(t["fee_entry"] - t["amount_invested"] * fee) > 1e-6:
            problems.append("entry fee")
        if abs(t["quantity"] * t["exec_price"] - t["amount_invested"]) > 1e-4:
            problems.append("quantity")
        if t["status"] == "closed":
            if abs(t["fee_exit"] - t["quantity"] * t["exit_price"] * fee) > 1e-4:
                problems.append("exit fee")
            cost = t["amount_invested"] + t["fee_entry"]
            pnl = t["quantity"] * t["exit_price"] - t["fee_exit"] - cost
            if abs(pnl - t["pnl_usd"]) > 1e-4:
                problems.append("pnl")
        rec("PASS" if not problems else "FAIL", f"trade #{t['id']} {t['symbol']} {t['status']}", ", ".join(problems) or f"filled {t['exec_price']:.4f} vs market {t['market_price']:.4f}")
    start = cfg["starting_balance"]
    last = db.one("select * from portfolio order by ts desc limit 1")
    if legacy and not last:
        rec("FAIL", "portfolio snapshot exists")
        return
    if legacy:
        cash = paper.cash_balance(start, trades)
        rec("PASS" if abs(cash - last["cash"]) < 0.01 or age_min(last["ts"]) > 5 else "FAIL", "snapshot cash equals starting balance minus trades", f"recomputed {cash:.2f} vs stored {last['cash']:.2f}")
        rec("PASS" if abs(last["cash"] + last["positions_value"] - last["total_value"]) < 0.01 else "FAIL", "total = cash + open positions")
    else:
        rec("INFO", "legacy AI-account portfolio snapshots", "retired with the 24h/48h prediction engine; the scalper keeps its own books (see the scalper section)")
    rec("INFO", "starting balance", f"${start:,.0f}")
    mt = db.all("select * from paper_trades where account='manual' order by id")
    if mt:
        mreal = [t for t in mt if t["status"] in ("open", "closed")]
        mcash = paper.cash_balance(start, mreal)
        bad = [t["id"] for t in mreal if abs(t["fee_entry"] - t["amount_invested"] * fee) > 1e-6 or t["exec_price"] < t["market_price"] * (1 - 1e-9)]
        rec("PASS" if mcash >= -1e-6 and not bad else "FAIL", "manual account: cash never negative, fees correct", f"{len(mreal)} manual trades, cash {mcash:.2f}")
        rec("PASS" if all(t["planned_exit_at"] is None for t in mt) else "FAIL", "manual positions have no automatic exit (you sell them)")
    else:
        rec("INFO", "manual account", "no manual trades yet")
    over = db.one("select count(*) n from paper_trades where status='open' and account='ai' and planned_exit_at < now() - interval '15 minutes'")["n"]
    rec("PASS" if over == 0 else "FAIL", "no open position is overdue for exit", f"{over} overdue")
    if legacy:
        rec("PASS" if age_min(last["ts"]) < 20 else "FAIL", "portfolio is being snapshotted", f"last {age_min(last['ts']):.1f} min ago")


# ------------------------------------------------------------------ 9/10. evaluation
def check_evaluation(db, cfg) -> None:
    section("9-10. Automatic evaluation")
    overdue = db.one("select count(*) n from predictions p left join prediction_results r on r.prediction_id=p.id where r.prediction_id is null and p.target_time < now() - interval '15 minutes'")["n"]
    rec("PASS" if overdue == 0 else "FAIL", "no prediction is past its 24h/48h window without a result", f"{overdue} overdue")
    pending = db.one("select count(*) n, min(target_time) t from predictions p left join prediction_results r on r.prediction_id=p.id where r.prediction_id is null")
    done = db.one("select count(*) n from prediction_results")["n"]
    rec("INFO", "evaluation queue", f"{done} scored, {pending['n']} pending" + (f", first resolves {pending['t']:%Y-%m-%d %H:%M} UTC" if pending["t"] else ""))
    if not done:
        rec("WAIT", "results marked correct/wrong", "first predictions resolve 24h after they were made. Verified by tests/test_e2e.py on a scratch database")
        return
    bad = 0
    for r in db.all("select p.*, r.actual_price, r.signal_correct, r.directional_correct, r.in_range, r.high_confidence from predictions p join prediction_results r on r.prediction_id=p.id"):
        exp = evaluator.score_prediction(r, r["actual_price"], cfg)
        if (exp["signal_correct"], exp["directional_correct"], exp["in_range"]) != (r["signal_correct"], r["directional_correct"], r["in_range"]):
            bad += 1
    rec("PASS" if bad == 0 else "FAIL", "stored results match an independent re-score of every prediction", f"{bad} mismatches")
    rec("PASS" if db.one("select count(*) n from prediction_results r left join predictions p on p.id=r.prediction_id where p.id is null")["n"] == 0 else "FAIL", "every result points at an existing, untouched prediction")


# ------------------------------------------------------------------ 11. metrics
def check_metrics(db, cfg) -> None:
    section("11. Metrics recomputed independently")
    rows = db.all("select p.symbol, p.horizon_h, p.variant, p.signal, r.directional_correct d, r.signal_correct s, r.high_confidence hc from predictions p join prediction_results r on r.prediction_id=p.id where p.variant='market'")
    perf = {(m["scope"], m["horizon_h"], m["slice"]): m for m in db.all("select * from performance_metrics where variant='market'")}
    if not rows:
        rec("WAIT", "accuracy metrics", "no scored predictions yet")
    else:
        def acc(f):
            sel = [r for r in rows if f(r)]
            return (sum(r["d"] for r in sel) / len(sel), len(sel)) if sel else (None, 0)
        for label, key, f in [("overall", ("ALL", None, "all"), lambda r: True), ("24h", ("ALL", 24, "all"), lambda r: r["horizon_h"] == 24),
                              ("48h", ("ALL", 48, "all"), lambda r: r["horizon_h"] == 48), ("high-confidence", ("ALL", None, "high_conf"), lambda r: r["hc"])]:
            exp, n = acc(f)
            got = perf.get(key)
            ok = got is not None and got["total_predictions"] == n and ((exp is None and got["directional_accuracy"] is None) or abs((got["directional_accuracy"] or 0) - (exp or 0)) < 1e-9)
            rec("PASS" if ok else "FAIL", f"{label} accuracy", f"stored {got and got['directional_accuracy']} vs recomputed {exp} (n={n})")
    trades = [t for t in db.all("select * from paper_trades where status='closed' and account='ai'")]
    pm = {(m["scope"]): m for m in db.all("select * from performance_metrics where slice='paper'")}
    total = sum(t["pnl_usd"] for t in trades)
    if not trades:
        rec("WAIT", "P/L, return %, per-coin P/L, win rate, avg win/loss, max drawdown", "no closed paper trade yet. Verified by tests/test_e2e.py on a scratch database")
        return
    start = cfg["starting_balance"]
    snap = db.one("select total_value from portfolio order by ts desc limit 1")["total_value"]
    rec("PASS" if abs(start + total - snap) < 0.01 or db.one("select count(*) n from paper_trades where status='open' and account='ai'")["n"] else "FAIL", "portfolio value = start + closed P/L", f"start {start} + {total:.2f} vs {snap:.2f}")
    for c in SYMBOLS:
        exp = sum(t["pnl_usd"] for t in trades if t["symbol"] == c)
        got = pm.get(c, {}).get("extra", {}).get("pnl_usd_total")
        rec("PASS" if got is not None and abs(got - exp) < 1e-6 else "FAIL", f"{c} P/L", f"stored {got} vs recomputed {exp:.4f}")


# ------------------------------------------------------------------ 12. security
FORBIDDEN = [r"/orders?\b", r"CB-ACCESS", r"api/v3/order", r"withdraw", r"\bwallet\b", r"private[_ ]?key", r"seed phrase", r"mnemonic", r"web3", r"\bethers\b", r"signTransaction",
             r"place[_ ]?order", r"create[_ ]?order", r"submit[_ ]?order", r"apiSecret", r"ccxt", r"alpaca", r"robinhood", r"interactive ?brokers"]


def _source_files() -> list[Path]:
    return (list((ROOT / "worker" / "crypto_ai").rglob("*.py")) + list((ROOT / "web" / "app").rglob("*.ts*")) + list((ROOT / "web" / "lib").rglob("*.ts*"))
            + list((ROOT / "web" / "components").rglob("*.ts*")))


def scan_source_for_real_trading_code() -> list[str]:
    """Lines anywhere in the worker or the dashboard that look like exchange order / withdrawal / wallet / broker code. Must stay empty."""
    hits = []
    for p in _source_files():
        if p.name == "selfcheck.py":
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(txt.splitlines(), 1):
            is_keyword_list = p.name == "common.py" and line.strip().startswith('("whale_activity"')   # news-classification keywords, not wallet code
            if any(re.search(pat, line, re.I) for pat in FORBIDDEN) and not is_keyword_list and not re.search(r"no (wallet|order|account)|never|never places|read-only|does not|fake money|nothing here|paper|labelled|look up|lookup", line, re.I):
                hits.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:90]}")
    return hits


# Worker files that use HTTP POST, and why each is read-only in effect. Everything else only GETs.
#   llm.py                    the optional OpenRouter client: sends a text prompt to a $0 model for commentary; never sees an account or a trade
#   datasources/base.py       http_post_json: the XRP Ledger's public JSON-RPC takes read queries (ledger, fee) as POST
#   datasources/reddit.py     the standard OAuth client-credentials token request for Reddit's read-only API
# (An order/withdrawal/wallet pattern anywhere in the source is caught separately by scan_source_for_real_trading_code.)
HTTP_WRITE_ALLOWED = {"selfcheck.py", "llm.py", "base.py", "reddit.py"}


def scan_for_http_writes() -> list[str]:
    return [str(p.relative_to(ROOT)) for p in (ROOT / "worker" / "crypto_ai").rglob("*.py")
            if re.search(r"\.(post|put|delete|patch)\(", p.read_text(encoding="utf-8", errors="ignore")) and p.name not in HTTP_WRITE_ALLOWED]


def check_security(db) -> None:
    section("12. No real-money trading, wallets or exchange permissions")
    rec("PASS" if PAPER_ONLY else "FAIL", "config.PAPER_ONLY is set", "every trade is simulated")
    hits = scan_source_for_real_trading_code()
    rec("PASS" if not hits else "FAIL", "no order/withdraw/wallet/broker code in worker or web source", f"{len(hits)} hits: {hits[:3]}" if hits else "")
    posts = scan_for_http_writes()
    rec("PASS" if not posts else "FAIL", "worker makes no unexpected POST/PUT/DELETE HTTP calls (documented read-only exceptions: OpenRouter prompt, XRPL JSON-RPC reads, Reddit OAuth token)", ", ".join(posts))
    secrets_in_repo = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and "node_modules" not in p.parts and ".next" not in p.parts and ".cache" not in p.parts
                       and p.suffix in (".py", ".ts", ".tsx", ".yml", ".md", ".json", ".sql", ".example")
                       and any(os.environ.get(k) and os.environ[k] in p.read_text(encoding="utf-8", errors="ignore") for k in ("FRED_API_KEY", "CONGRESS_API_KEY"))]
    rec("PASS" if not secrets_in_repo else "FAIL", "API key values do not appear in any source/config file", ", ".join(secrets_in_repo))
    gi = (ROOT / ".gitignore").read_text()
    rec("PASS" if ".env" in gi else "FAIL", ".env is git-ignored")
    web_env = [k for k in ("FRED_API_KEY", "CONGRESS_API_KEY") if any(k in p.read_text(encoding="utf-8", errors="ignore") for p in _source_files() if p.suffix != ".py")]
    rec("PASS" if not web_env else "FAIL", "web app never references the research API keys", ",".join(web_env))
    rls = db.all("select tablename from pg_tables where schemaname='public' and not rowsecurity")
    rec("PASS" if not rls else "FAIL", "row-level security enabled on every table (Supabase REST API locked)", ", ".join(r["tablename"] for r in rls))
    grants = db.all("""select table_name from information_schema.role_table_grants where grantee in ('anon','authenticated') and table_schema='public'
                       and privilege_type in ('INSERT','UPDATE','DELETE') group by 1""")
    rec("INFO", "anon/authenticated write grants (blocked by RLS while no policies exist)", f"{len(grants)} tables have grants; policies: {db.one('select count(*) n from pg_policies where schemaname=' + chr(39) + 'public' + chr(39))['n']}")
    rec("INFO", "dashboard auth", "APP_PASSWORD is enforced in production builds; in `npm run dev` the dashboard is open on localhost (fine locally, never deploy that way)")


# ------------------------------------------------------------------ 14. the scalper itself
def check_scalper(db, cfg) -> None:
    section("14. Scalper (paper): heartbeat, books, confirmation, signals and learning")
    on = cfg["autopilot_enabled"]
    rec("INFO", "kill switch (autopilot_enabled)", "ON" if on is True else "OFF" if on is False else f"unreadable: {on!r}")
    beat = db.one("select value->>'at' as hb from scalp_state where key='heartbeat'")
    if not beat or not beat["hb"]:
        rec("WAIT", "worker heartbeat", "the scalper has not run yet")
        return
    age = age_min(datetime.fromisoformat(beat["hb"]))
    rec("PASS" if age < 15 else "FAIL", "scalper worker is running", f"last pass {age:.1f} min ago")
    m = db.all("select symbol, max(ts) t from candles where granularity=60 group by 1")
    stale = [r["symbol"] for r in m if age_min(r["t"]) > 8]
    rec("PASS" if len(m) == len(SYMBOLS) and not stale else "FAIL", "1-minute candles are fresh for every coin", f"stale: {stale}" if stale else f"{len(m)} coins")
    active = db.one("select count(*) n, bool_or(threshold is null) nothr from scalp_models where is_active")
    rec("INFO" if active["n"] else "WAIT", "active trade-outcome model", "none yet: the scalper is not trading until `scalp-train` promotes one" if not active["n"]
        else "present, but no threshold earned a positive net edge: it deliberately does not trade" if active["nothr"] else "present, with a validated edge threshold")

    open_ = db.all("select * from scalp_trades where status='open'")
    bad = []
    if len(open_) > cfg["scalp_max_positions"]:
        bad.append(f"{len(open_)} open > max {cfg['scalp_max_positions']}")
    if len({t["symbol"] for t in open_}) != len(open_):
        bad.append("two open trades in one coin")
    for t in open_:
        long = t["direction"] == "long"
        if (long and t["stop_px"] < t["initial_stop_px"] - 1e-9) or (not long and t["stop_px"] > t["initial_stop_px"] + 1e-9):
            bad.append(f"#{t['id']} stop was widened")
        if (long and not t["initial_stop_px"] < t["entry_mid"] < t["take_profit_px"]) or (not long and not t["initial_stop_px"] > t["entry_mid"] > t["take_profit_px"]):
            bad.append(f"#{t['id']} stop/target on the wrong side of entry")
        if age_min(t["opened_at"]) > cfg["scalp_hold_bars"] + 10 and age < 15:
            bad.append(f"#{t['id']} is overdue for its time exit")
    rec("PASS" if not bad else "FAIL", "open trades obey the rules (max positions, one per coin, stops never widened, nothing overdue)", "; ".join(bad) or f"{len(open_)} open")
    equity = cfg["starting_balance"] + db.one("select coalesce(sum(net_pnl_usd),0) x from scalp_trades where status='closed'")["x"]
    exposure = sum(t["notional"] for t in open_)
    rec("PASS" if exposure <= max(equity, 1) * cfg["scalp_max_exposure_pct"] / 100 * 1.02 else "FAIL", "total exposure is within the cap", f"{exposure:,.2f} USD of {equity:,.2f} equity (cap {cfg['scalp_max_exposure_pct']:.0f}%)")
    rec("PASS" if exposure <= equity + 1e-6 else "FAIL", "no leverage: open notional never exceeds the account")

    closed = db.all("select id, gross_pnl_usd, slippage_usd, fees_usd, net_pnl_usd, closed_at from scalp_trades where status='closed' order by id desc limit 500")
    off = [c["id"] for c in closed if abs(c["gross_pnl_usd"] - c["slippage_usd"] - c["fees_usd"] - c["net_pnl_usd"]) > 1e-6 * max(1.0, abs(c["gross_pnl_usd"]))]
    rec("PASS" if not off else "FAIL", "every closed trade's P&L adds up (gross - slippage - fees = net)", f"{len(closed)} checked" + (f", off: {off[:5]}" if off else ""))
    missing = db.one("""select count(*) n from scalp_trades t left join scalp_lessons l on l.trade_id = t.id
                        where t.status='closed' and l.id is null and t.closed_at < %s""", [now() - timedelta(minutes=5)])["n"]
    rec("PASS" if missing == 0 else "FAIL", "every closed trade has a post-mortem lesson", f"{missing} without")

    if cfg["scalp_confirm_required"]:
        early = db.all("""select t.id from scalp_trades t join scalp_signals s on s.id = t.signal_id
                          where t.opened_at < s.decision_ts + interval '2 minutes' - interval '5 seconds'""")
        rec("PASS" if not early else "FAIL", "no trade opened before its confirmation candle closed", f"{len(early)} early: {[e['id'] for e in early[:5]]}" if early else "")
        unlinked = db.one("select count(*) n from scalp_trades t where t.signal_id is null and t.opened_at > (select coalesce(min(created_at), now()) from scalp_signals)")["n"]
        rec("PASS" if unlinked == 0 else "FAIL", "every trade since signals began belongs to a recorded, confirmed signal", f"{unlinked} without")
    conf_no_trade = db.one("select count(*) n from scalp_signals where status='confirmed' and trade_id is null and reason = 'confirmed'")["n"]
    rec("PASS" if conf_no_trade == 0 else "FAIL", "every confirmed signal produced a trade", f"{conf_no_trade} without")
    stuck = db.one("select count(*) n from scalp_signals where status='pending' and decision_ts < %s", [now() - timedelta(minutes=10)])["n"]
    rec("PASS" if stuck == 0 else "FAIL", "no prediction is stuck waiting for confirmation", f"{stuck} stuck")
    lag = db.one("""select count(*) n from scalp_signals where graded_at is null and status <> 'pending' and decision_ts < %s""",
                 [now() - timedelta(minutes=cfg["scalp_hold_bars"] + 30)])["n"]
    rec("PASS" if lag == 0 else "FAIL", "every finished prediction (traded or not) gets graded right/wrong", f"{lag} ungraded past their window")
    tot = db.one("select count(*) n, count(*) filter (where status='confirmed') c, count(*) filter (where status='failed') f, count(*) filter (where status='blocked') b, count(*) filter (where graded_at is not null) g from scalp_signals")
    rec("INFO", "predictions recorded", f"{tot['n']} total: {tot['c']} confirmed, {tot['f']} failed confirmation, {tot['b']} blocked by a gate, {tot['g']} graded")


# ------------------------------------------------------------------ 13. real-time signals + self-learning
def check_learning(db, cfg, legacy: bool = True) -> None:
    section("13. Real-time signals and self-learning")
    if not legacy:
        rec("INFO", "legacy prediction post-mortems / live-signal freshness", "skipped: the 24h/48h prediction engine is retired (the scalper's learning is checked in the scalper section)")
        rec("PASS" if db.one("select to_regclass('cash_plans') t")["t"] else "FAIL", "table cash_plans exists (\"what should I do with the cash?\")")
        stuck = db.one("select count(*) n from cash_plans where status = 'executing' and created_at < now() - interval '10 minutes'")["n"]
        rec("PASS" if stuck == 0 else "FAIL", "no cash plan is stuck half-executed", f"{stuck} stuck")
        unasked = db.one("select count(*) n from paper_trades t where t.account = 'manual' and t.ai_advice->>'source' = 'cash_plan' and not exists (select 1 from cash_plans c where c.id = (t.ai_advice->>'plan_id')::bigint and c.status = 'confirmed')")["n"]
        rec("PASS" if unasked == 0 else "FAIL", "every trade made from a cash plan belongs to a plan the user confirmed", f"{unasked} without confirmation")
        return
    for tbl in ("live_signals", "post_mortems", "learned_patterns", "model_challenges"):
        rec("PASS" if db.one("select to_regclass(%s) t", [tbl])["t"] else "FAIL", f"table {tbl} exists")
    trig = {r["tgname"] for r in db.all("select t.tgname from pg_trigger t join pg_class c on c.oid=t.tgrelid where c.relname='post_mortems' and not t.tgisinternal")}
    rec("PASS" if "post_mortems_no_update" in trig else "FAIL", "post-mortems are append-only (trigger installed)")
    bad = db.all("select id from live_signals where action not in ('BUY','HOLD','REDUCE','SELL')")
    rec("PASS" if not bad else "FAIL", "every signal is one of BUY / HOLD / REDUCE / SELL")
    last = db.one("select max(created_at) t from live_signals")["t"]
    rec("PASS" if last and age_min(last) < 7 * 60 + 60 else "FAIL", "a current signal exists for the coins", f"latest {age_min(last):.0f} min ago" if last else "none")
    empty = db.one("select count(*) n from live_signals where reasons is null or jsonb_array_length(reasons) = 0 or explanation = ''")["n"]
    rec("PASS" if empty == 0 else "FAIL", "every signal states its reasons", f"{empty} without")
    missing = db.one("""select count(*) n from prediction_results r left join post_mortems m on m.prediction_id = r.prediction_id
                        where r.signal_correct = false and m.id is null""")["n"]
    rec("PASS" if missing <= 5 else "FAIL", "wrong predictions get post-mortems", f"{missing} waiting")
    orphan = db.one("select count(*) n from post_mortems m join prediction_results r on r.prediction_id=m.prediction_id where r.signal_correct = true")["n"]
    rec("PASS" if orphan == 0 else "FAIL", "no post-mortem exists for a correct prediction")
    unexplained = db.all("select v.version from model_versions v where v.backtest_metrics ? 'promotion' and not exists (select 1 from model_challenges c where c.challenger_version = v.version and c.decision = 'promoted')")
    rec("PASS" if not unexplained else "FAIL", "every automatic model replacement was a recorded, promoted challenge", f"{len(unexplained)} unexplained" if unexplained else "")
    early = db.all("""select c.id from model_challenges c where c.decision in ('promoted','rejected') and c.trigger_info ? 'days_since_training'
                      and (c.trigger_info->>'days_since_training')::float < %s and coalesce((c.trigger_info->>'forced')::boolean, false) = false""", [cfg["retrain_min_days"]])
    rec("PASS" if not early else "FAIL", "no retrain was attempted before the required days/examples (forced test runs excluded)", f"{len(early)} early")
    active = db.all("select symbol, variant, count(*) n from model_versions where is_active group by 1,2 having count(*) <> 1")
    rec("PASS" if not active else "FAIL", "exactly one active model per coin and variant")
    rec("PASS" if db.one("select to_regclass('cash_plans') t")["t"] else "FAIL", "table cash_plans exists (\"what should I do with the cash?\")")
    stuck = db.one("select count(*) n from cash_plans where status = 'executing' and created_at < now() - interval '10 minutes'")["n"]
    rec("PASS" if stuck == 0 else "FAIL", "no cash plan is stuck half-executed", f"{stuck} stuck")
    unasked = db.one("select count(*) n from paper_trades t where t.account = 'manual' and t.ai_advice->>'source' = 'cash_plan' and not exists (select 1 from cash_plans c where c.id = (t.ai_advice->>'plan_id')::bigint and c.status = 'confirmed')")["n"]
    rec("PASS" if unasked == 0 else "FAIL", "every trade made from a cash plan belongs to a plan the user confirmed", f"{unasked} without confirmation")
    rec("INFO", "learning status", f"{db.one('select count(*) n from post_mortems')['n']} post-mortems, {db.one('select count(*) n from learned_patterns')['n']} patterns tracked, {db.one('select count(*) n from model_challenges')['n']} retrain attempts")


def run(db) -> int:
    cfg = db.settings()
    legacy = bool(cfg["legacy_predictions_enabled"])
    checks = [lambda: check_prices(db), check_keys, lambda: check_feeds(db), lambda: check_duplicates(db)]
    if legacy:
        checks += [lambda: check_predictions(db, cfg)]
    else:
        checks += [lambda: rec("INFO", "legacy 24h/48h prediction engine", "retired (legacy_predictions_enabled is off): prediction, evaluation and accuracy checks are skipped")]
    checks += [lambda: check_paper(db, cfg, legacy)]
    if legacy:
        checks += [lambda: check_evaluation(db, cfg), lambda: check_metrics(db, cfg)]
    checks += [lambda: check_learning(db, cfg, legacy), lambda: check_scalper(db, cfg), lambda: check_security(db)]
    for fn in checks:
        try:
            fn()
        except Exception as e:                                   # a crashing check is a failed check
            rec("FAIL", f"{getattr(fn, '__name__', 'check')} crashed", f"{type(e).__name__}: {str(e)[:120]}")
    c = {k: sum(1 for s, _, _ in RESULTS if s == k) for k in ("PASS", "FAIL", "WAIT", "INFO")}
    print(f"\nSUMMARY: {c['PASS']} passed, {c['FAIL']} failed, {c['WAIT']} waiting for data, {c['INFO']} info")
    return 1 if c["FAIL"] else 0
