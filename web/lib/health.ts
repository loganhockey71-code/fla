import { sql, getSettings } from "./db";

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
  if (!db) return [...out, ...["FRED", "Congress.gov", "Research feeds", "Prediction engine", "Paper trading", "Evaluation engine"].map((n) => ({ name: n, status: "error" as const, detail: "needs Supabase" }))];

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
  const bad = feeds.filter((s) => !s.last_polled_at || s.last_status === "error" || ageMin(s.last_success_at as Date) > Math.max((3 * (s.poll_interval_s as number)) / 60, 30));
  if (bad.length) push("Research feeds", "error", `${bad.length}/${feeds.length} failing: ${bad.map((s) => `${s.key}${s.last_error ? ` (${s.last_error})` : ""}`).join("; ")}`);
  else {
    const [ev] = await db`select count(*)::int n, count(*) filter (where detected_at > now() - interval '24 hours')::int d from research_events`;
    push("Research feeds", "ok", `all ${feeds.length} sources (SEC, Fed, CFTC, XRPL, Ethereum, news, Polymarket, Kalshi) polled on schedule; ${ev.n} events saved (${ev.d} in the last 24 h). No Ripple corporate feed exists.`);
  }

  // ---- prediction engine
  const [run] = await db`select run_id, max(created_at) t, count(*)::int n, count(distinct (symbol, horizon_h, variant))::int combos from predictions where run_id is not null group by run_id order by t desc limit 1`;
  const [models] = await db`select count(*)::int n from model_versions where is_active`;
  if (!run) push("Prediction engine", "waiting", "no predictions yet: run `train` then `tick`");
  else {
    const a = ageMin(run.t as Date), limit = cfg.prediction_interval_h * 60 + 45;
    if (run.combos < 12) push("Prediction engine", "error", `latest run only produced ${run.combos}/12 coin x horizon x variant predictions`);
    else if (a > limit) push("Prediction engine", "error", `latest run ${fmt(a)} ago (should be every ${cfg.prediction_interval_h} h)`);
    else push("Prediction engine", "ok", `12/12 predictions (BTC/ETH/XRP x 24h/48h x 2 models) ${fmt(a)} ago; ${models.n} active models`);
  }

  // ---- paper trading
  const trades = await db`select * from paper_trades where account = 'ai'`;
  const [snap] = await db`select ts, cash, positions_value, total_value from portfolio order by ts desc limit 1`;
  if (!snap) push("Paper trading", "waiting", "no portfolio snapshot yet");
  else {
    let cash = cfg.starting_balance;
    for (const t of trades) {
      if (t.status === "open" || t.status === "closed") cash -= (t.amount_invested as number) + (t.fee_entry as number);
      if (t.status === "closed") cash += (t.quantity as number) * (t.exit_price as number) - (t.fee_exit as number);
    }
    const overdue = trades.filter((t) => t.status === "open" && ageMin(t.planned_exit_at as Date) > 15).length;
    const open = trades.filter((t) => t.status === "open").length, closed = trades.filter((t) => t.status === "closed").length;
    const sumOk = Math.abs((snap.cash as number) + (snap.positions_value as number) - (snap.total_value as number)) < 0.01;
    const cashOk = Math.abs(cash - (snap.cash as number)) < 0.5 || ageMin(snap.ts as Date) > 6;
    if (ageMin(snap.ts as Date) > 20) push("Paper trading", "error", `portfolio last updated ${fmt(ageMin(snap.ts as Date))} ago: the worker is not running`);
    else if (!sumOk || !cashOk) push("Paper trading", "error", `books do not balance: cash ${(snap.cash as number).toFixed(2)} vs ${cash.toFixed(2)} recomputed from trades`);
    else if (overdue) push("Paper trading", "error", `${overdue} open position(s) past their exit time`);
    else push("Paper trading", "ok", `portfolio $${(snap.total_value as number).toFixed(2)} (start $${cfg.starting_balance}); books balance; ${open} open, ${closed} closed trades${trades.length === 0 ? "; no BUY signal has fired yet" : ""}`);
  }

  // ---- evaluation engine
  const [ev] = await db`select count(*) filter (where r.prediction_id is null and p.target_time < now() - interval '15 minutes')::int overdue,
                          count(*) filter (where r.prediction_id is null)::int pending, count(r.prediction_id)::int scored, min(p.target_time) filter (where r.prediction_id is null) nxt
                          from predictions p left join prediction_results r on r.prediction_id = p.id`;
  if (ev.overdue > 0) push("Evaluation engine", "error", `${ev.overdue} prediction(s) past their 24h/48h window without a result`);
  else if (ev.scored === 0) push("Evaluation engine", ev.pending ? "waiting" : "waiting", `nothing has reached its window yet; ${ev.pending} pending, first resolves ${ev.nxt ? new Date(ev.nxt as Date).toUTCString() : "n/a"}`);
  else push("Evaluation engine", "ok", `${ev.scored} scored, ${ev.pending} pending, none overdue`);
  return out;
}
