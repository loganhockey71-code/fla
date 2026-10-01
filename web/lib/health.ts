import { sql, getSettings } from "./db";
import { heartbeatTone } from "./scalp";

export type Check = { name: string; status: "ok" | "error" | "waiting" | "unused"; detail: string };
const MIN = 60_000;
const ageMin = (d: Date | string | null | undefined) => (d ? (Date.now() - new Date(d).getTime()) / MIN : Infinity);
const fmt = (m: number) => (m < 90 ? `${m.toFixed(0)} min` : `${(m / 60).toFixed(1)} h`);

async function ping(url: string): Promise<{ ok: boolean; status: number | string; ms: number }> {
  const t = Date.now();
  try {
    const r = await fetch(url, { signal: AbortSignal.timeout(6000), cache: "no-store" });
    return { ok: r.ok, status: r.status, ms: Date.now() - t };
  } catch (e) {
    return { ok: false, status: e instanceof Error ? e.name : "error", ms: Date.now() - t };
  }
}

export async function runChecks(): Promise<Check[]> {
  const out: Check[] = [];
  const push = (name: string, status: Check["status"], detail: string) => out.push({ name, status, detail });

  // ---- Supabase first: everything else reads from it
  let db: ReturnType<typeof sql> | null = null;
  try {
    const t = Date.now();
    db = sql();
    await db`select 1`;
    push("Supabase", "ok", `query answered in ${Date.now() - t} ms`);
  } catch (e) {
    push("Supabase", "error", `cannot connect (${e instanceof Error ? e.message.split("\n")[0].slice(0, 120) : "unknown"})`);
  }

  // ---- live exchange APIs (checked from this server, right now)
  const [cb, bn] = await Promise.all([ping("https://api.exchange.coinbase.com/products/BTC-USD/ticker"), ping("https://api.binance.com/api/v3/ping")]);
  const md = db ? await db`select symbol, max(ts) ts from market_data group by symbol`.catch(() => []) : [];
  const stalest = md.length === 3 ? Math.max(...md.map((r) => ageMin(r.ts as Date))) : Infinity;
  if (!cb.ok) push("Coinbase", "error", `live API HTTP ${cb.status} after ${cb.ms} ms`);
  else if (stalest > 15) push("Coinbase", "error", `API is up (${cb.ms} ms) but stored prices are ${Number.isFinite(stalest) ? fmt(stalest) + " old" : "missing"}: the worker is not collecting`);
  else push("Coinbase", "ok", `live API ${cb.ms} ms; BTC/ETH/XRP prices stored ${fmt(stalest)} ago`);
  push("Binance", "unused", `${bn.ok ? "reachable" : `HTTP ${bn.status}`}. The app does not use Binance (api.binance.com blocks US IPs); Coinbase + CoinGecko are the sources, so this has no effect`);
  if (!db) return [...out, ...["FRED", "Congress.gov", "Research feeds", "Scalper worker", "Paper trading", "Prediction grading"].map((n) => ({ name: n, status: "error" as const, detail: "needs Supabase" }))];

  const cfg = await getSettings();
  const src = await db`select * from source_registry order by tier, key`;
  const srcCheck = (key: string, label: string) => {
    const s = src.find((r) => r.key === key);
    if (!s || !s.last_polled_at) return push(label, "waiting", "not polled yet: run the worker");
    const stale = ageMin(s.last_success_at as Date) > Math.max((3 * (s.poll_interval_s as number)) / 60, 30);
    if (s.last_status === "error") return push(label, "error", `last poll failed: ${s.last_error}`);
    if (stale) return push(label, "error", `last success ${fmt(ageMin(s.last_success_at as Date))} ago (expected every ${fmt((s.poll_interval_s as number) / 60)})`);
    push(label, "ok", `last success ${fmt(ageMin(s.last_success_at as Date))} ago`);
  };
  srcCheck("fred_api", "FRED");
  srcCheck("congress_api", "Congress.gov");

  const feeds = src.filter((s) => !["fred_api", "congress_api"].includes(s.key as string));
  const neverPolled = feeds.filter((s) => !s.last_polled_at);
  const bad = feeds.filter((s) => !!s.last_polled_at && (s.last_status === "error" || ageMin(s.last_success_at as Date) > Math.max((3 * (s.poll_interval_s as number)) / 60, 30)));
  if (neverPolled.length === feeds.length) push("Research feeds", "waiting", "no news source has been polled yet: start the worker (`python -m crypto_ai.cli tick`) and they fill in");
  else if (bad.length || neverPolled.length) push("Research feeds", "error", `${bad.length + neverPolled.length}/${feeds.length} failing or never polled: ${[...bad, ...neverPolled].map((s) => `${s.key}${s.last_error ? ` (${s.last_error})` : ""}`).join("; ")}`);
  else {
    const [ev] = await db`select count(*)::int n, count(*) filter (where detected_at > now() - interval '24 hours')::int d from research_events`;
    push("Research feeds", "ok", `all ${feeds.length} sources (SEC, Fed, CFTC, XRPL, Ethereum, news, Polymarket, Kalshi) polled on schedule; ${ev.n} events saved (${ev.d} in the last 24 h). No Ripple corporate feed exists.`);
  }

  // ---- the scalper (the only thing that trades)
  const states = await db`select key, value, updated_at from scalp_state`.catch(() => []);
  const st = (k: string) => states.find((r) => r.key === k)?.value as Record<string, unknown> | undefined;
  const beat = heartbeatTone((st("heartbeat") as { at?: string } | undefined)?.at);
  const status = st("status") as { note?: string; fresh_data?: boolean; error?: string | null; enabled?: boolean } | undefined;
  push("Scalper worker", beat.tone, beat.tone === "ok" ? `${beat.text}${status?.note ? `: ${status.note}` : ""}` : beat.text);
  if (status?.error) push("Scalper entry side", "error", `the last pass could not look for new trades (${status.error}); open trades are still managed`);

  const m1rows = await db`select symbol, max(ts) newest from candles where granularity = 60 group by symbol`.catch(() => []);
  const stalestCandle = m1rows.length ? Math.max(...m1rows.map((r) => ageMin(r.newest as Date))) : Infinity;
  if (m1rows.length < 3) push("1-minute candles", "waiting", "not stored for every coin yet: run `scalp-train` once, then the worker keeps them current");
  else if (stalestCandle > 8) push("1-minute candles", "error", `the stalest coin's newest candle is ${fmt(stalestCandle)} old: the scalper cannot trade on stale data`);
  else push("1-minute candles", "ok", `all 3 coins current (stalest ${fmt(stalestCandle)} old)`);

  const [mdl] = await db`select id, threshold, created_at from scalp_models where is_active`.catch(() => []);
  const retrain = ((st("retrain_log") ?? []) as unknown as { at: string; decision: string; reason?: string }[]);
  const lastTrain = retrain[retrain.length - 1];
  if (!mdl) push("Trade-outcome model", "waiting", `no active model yet, so the scalper is not trading${lastTrain ? ` (last check: ${lastTrain.decision}${lastTrain.reason ? `: ${lastTrain.reason}` : ""})` : ". Run \`python -m crypto_ai.cli scalp-train\` once"}`);
  else if (mdl.threshold == null) push("Trade-outcome model", "waiting", "active, but no edge threshold earned a positive net result out-of-sample, so it deliberately does not trade");
  else push("Trade-outcome model", "ok", `model #${mdl.id} active, edge threshold ${(mdl.threshold as number).toFixed(2)}%, trained ${fmt(ageMin(mdl.created_at as Date))} ago`);

  const [sg] = await db`select count(*)::int n, count(*) filter (where status = 'pending')::int pend, count(*) filter (where status = 'pending' and decision_ts < now() - interval '10 minutes')::int stuck,
                          count(*) filter (where status = 'confirmed')::int conf, count(*) filter (where status = 'failed')::int fail, count(*) filter (where status = 'blocked')::int blocked,
                          count(*) filter (where graded_at is not null)::int graded,
                          count(*) filter (where graded_at is null and status <> 'pending' and decision_ts < now() - make_interval(mins => ${cfg.scalp_hold_bars + 30}))::int lag
                         from scalp_signals where decision_ts > now() - interval '7 days'`.catch(() => [null]);
  if (!sg) push("Predictions and confirmation", "waiting", "run `supabase/schema_scalp.sql` in Supabase to create the scalp_signals table");
  else if (sg.stuck > 0) push("Predictions and confirmation", "error", `${sg.stuck} prediction(s) have been waiting for their next-candle confirmation for over 10 minutes: the worker is not resolving them`);
  else push("Predictions and confirmation", sg.n ? "ok" : "waiting", sg.n ? `last 7 days: ${sg.n} predictions: ${sg.conf} confirmed and traded, ${sg.fail} refused by the next candle, ${sg.blocked} blocked by a risk/news/liquidity gate, ${sg.pend} waiting` : "no prediction has cleared the model's threshold yet (normal while the model finds no edge)");
  if (sg) push("Prediction grading", sg.lag > 0 ? "error" : sg.n ? "ok" : "waiting", sg.lag > 0 ? `${sg.lag} finished prediction(s) are still ungraded: the learning step is behind` : sg.n ? `${sg.graded} of ${sg.n} graded right/wrong against what the market did afterwards (only candles after the decision are used)` : "nothing to grade yet");

  // ---- the paper book: invariants that must always hold
  const open = await db`select symbol, direction, notional, stop_px, initial_stop_px, entry_mid, take_profit_px, opened_at from scalp_trades where status = 'open'`.catch(() => []);
  const [pl] = await db`select coalesce(sum(net_pnl_usd), 0)::float net, count(*)::int n,
                         count(*) filter (where abs(gross_pnl_usd - slippage_usd - fees_usd - net_pnl_usd) > 1e-6 * greatest(1, abs(gross_pnl_usd)))::int off
                        from scalp_trades where status = 'closed'`.catch(() => [{ net: 0, n: 0, off: 0 }]);
  const equity = cfg.starting_balance + (pl?.net ?? 0);
  const exposure = open.reduce((a, t) => a + (t.notional as number), 0);
  const widened = open.filter((t) => (t.direction === "long" ? (t.stop_px as number) < (t.initial_stop_px as number) - 1e-9 : (t.stop_px as number) > (t.initial_stop_px as number) + 1e-9)).length;
  const overdue = open.filter((t) => ageMin(t.opened_at as Date) > cfg.scalp_hold_bars + 10 && beat.tone === "ok").length;
  if (pl?.off) push("Paper trading", "error", `${pl.off} closed trade(s) whose gross - slippage - fees does not equal the booked net: the books are wrong`);
  else if (widened) push("Paper trading", "error", `${widened} open trade(s) had their stop moved wider (a stop may only tighten)`);
  else if (overdue) push("Paper trading", "error", `${overdue} open trade(s) are past their time exit`);
  else if (exposure > Math.max(equity, 1) * (cfg.scalp_max_exposure_pct / 100) * 1.02) push("Paper trading", "error", `open exposure $${exposure.toFixed(2)} exceeds the ${cfg.scalp_max_exposure_pct}% cap on $${equity.toFixed(2)}`);
  else push("Paper trading", "ok", `paper account $${equity.toFixed(2)} (start $${cfg.starting_balance}); books balance on ${pl?.n ?? 0} closed trades; ${open.length} open, exposure $${exposure.toFixed(2)} of the ${cfg.scalp_max_exposure_pct}% cap`);

  // ---- self-learning
  const [lr] = await db`select (select count(*)::int from scalp_lessons) lessons,
                         (select count(*)::int from scalp_trades t left join scalp_lessons l on l.trade_id = t.id where t.status = 'closed' and l.id is null and t.closed_at < now() - interval '5 minutes') missing`.catch(() => [null]);
  const learnLast = (st("learn_last") as { at?: string } | undefined)?.at;
  if (!lr) push("Self-learning", "waiting", "scalper tables are missing");
  else if (lr.missing > 0) push("Self-learning", "error", `${lr.missing} closed trade(s) have no post-mortem lesson: the learning job is behind`);
  else push("Self-learning", lr.lessons || learnLast ? "ok" : "waiting", `${lr.lessons} trade lessons; ${sg?.graded ?? 0} graded predictions; last analysis ${learnLast ? fmt(ageMin(learnLast)) + " ago" : "not yet (runs 05:00-23:59 local time)"}; every retrain is a guarded champion/challenger test on unseen data`);

  // ---- the old 24h/48h prediction engine
  push("Old 24h/48h prediction engine", "unused", "retired: it no longer predicts or trades (the scalper replaced it), so its checks are not run. Old predictions stay in the database for reference.");
  // ---- news: information for the dashboard AND context the scalper weighs before every trade
  const [sig] = await db`select max(created_at) t, count(*)::int n, count(*) filter (where trigger_kind <> 'scheduled')::int events from live_signals`.catch(() => [{ t: null, n: 0, events: 0 }]);
  const [lastSrc] = await db`select max(last_polled_at) t from source_registry`;
  const lastCheck = ageMin(lastSrc?.t as Date);
  if (!Number.isFinite(lastCheck)) push("News watcher", "waiting", "the worker has not polled any news source yet");
  else if (lastCheck > 90) push("News watcher", "error", `last news poll ${fmt(lastCheck)} ago: the scalper is not seeing fresh news`);
  else push("News watcher", "ok", `news sources polled ${fmt(lastCheck)} ago; ${sig?.events ?? 0} sudden-event alerts recorded. The scalper reads the same news, weighs it by source credibility and independent confirmation, and blocks or shrinks a trade when it conflicts.`);
  return out;
}
