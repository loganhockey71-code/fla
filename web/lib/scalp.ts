// Pure helpers for the scalper pages (no I/O, no React): settings limits, the live read, and what the graded predictions say.
// PAPER TRADING ONLY.

/** Mirror of worker/crypto_ai/config.py SETTING_BOUNDS for the settings the dashboard may edit. tests/scalp.test.mjs fails if the two drift apart. */
export const SETTING_BOUNDS: Record<string, [number, number]> = {
  starting_balance: [10, 1e9],
  trading_fee_pct: [0, 5],
  slippage_pct: [0, 5],
  max_spread_pct_to_trade: [0, 2],
  scalp_position_pct: [0.5, 50],
  scalp_risk_pct: [0.05, 2],
  scalp_max_positions: [1, 3],
  scalp_daily_loss_limit_pct: [0.5, 20],
  scalp_min_edge_pct: [0, 2],
  scalp_max_exposure_pct: [5, 100],
  scalp_max_drawdown_pct: [1, 50],
  scalp_min_liquidity_usd: [0, 1e9],
  scalp_max_participation_pct: [0.01, 10],
};

export function clampSetting(key: string, n: number): number {
  const b = SETTING_BOUNDS[key];
  if (!Number.isFinite(n)) return NaN;
  return b ? Math.min(b[1], Math.max(b[0], n)) : n;
}

/** The 24h/48h models were retired when the scalper took over, so their newest row only ever gets older. A read older than this is NOT "what the AI says now". */
export const PRED_FRESH_H = 12;

export type Read = { long: number; short: number; threshold: number; news: string; news_net: number; pending: boolean; price: number; at: string };
export type Lean = { lean: "long" | "short" | "none"; edge: number; text: string };

/** What the model currently leans toward for one coin, in plain words. A lean needs the predicted NET edge to clear the threshold. */
export function leanOf(read: Read | undefined | null, allowShorts = true): Lean {
  if (!read) return { lean: "none", edge: 0, text: "No live read yet" };
  const short = allowShorts ? read.short : -Infinity;
  const best = Math.max(read.long, short);
  if (!(best >= read.threshold)) return { lean: "none", edge: best, text: `No edge: best predicted net edge ${fmt(best)}% is below the ${fmt(read.threshold)}% needed` };
  const lean = read.long >= short ? "long" : "short";
  const wait = read.pending ? " Waiting for the next candle to confirm it." : "";
  return { lean, edge: best, text: `Leans ${lean}: predicted net edge ${fmt(best)}% after costs.${wait}` };
}
const fmt = (n: number) => (Number.isFinite(n) ? (n >= 0 ? "+" : "") + n.toFixed(3) : "n/a");

export function minutesSince(iso: string | Date | null | undefined, now = Date.now()): number {
  if (!iso) return Infinity;
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? Math.max(0, (now - t) / 60_000) : Infinity;
}
export const readIsFresh = (at: string | undefined, now = Date.now(), maxMin = 6) => minutesSince(at, now) <= maxMin;

export type SignalRow = {
  status: "pending" | "confirmed" | "failed" | "expired" | "blocked"; reason: string | null; outcome: "right" | "wrong" | "flat" | null;
  profitable: boolean | null; fwd_net_pct: number | null;
};
export type Group = { n: number; right: number | null; paid: number | null; meanNet: number | null };
const group = (rows: SignalRow[]): Group => {
  const g = rows.filter((r) => r.outcome);
  return {
    n: g.length,
    right: g.length ? g.filter((r) => r.outcome === "right").length / g.length : null,
    paid: g.length ? g.filter((r) => r.profitable).length / g.length : null,
    meanNet: g.length ? g.reduce((a, r) => a + (r.fwd_net_pct ?? 0), 0) / g.length : null,
  };
};

/** What the graded predictions say about the model and about waiting for the next candle. Needs real counts before it opines. */
export function signalStats(rows: SignalRow[]) {
  const by = (s: SignalRow["status"]) => rows.filter((r) => r.status === s);
  const confirmed = group(by("confirmed")), failed = group(by("failed")), blocked = group(by("blocked")), all = group(rows);
  const reasons: Record<string, number> = {};
  for (const r of rows) if (r.reason && ["failed", "blocked", "expired"].includes(r.status)) reasons[r.reason] = (reasons[r.reason] ?? 0) + 1;
  let verdict = "Not enough graded predictions yet to say whether waiting for the next candle helps.";
  if (confirmed.n >= 10 && failed.n >= 10 && confirmed.meanNet != null && failed.meanNet != null && confirmed.right != null && failed.right != null) {
    verdict = confirmed.meanNet > failed.meanNet && confirmed.right > failed.right
      ? `Confirmation is helping: confirmed predictions were right ${(confirmed.right * 100).toFixed(0)}% of the time vs ${(failed.right * 100).toFixed(0)}% for the ones it rejected.`
      : `Confirmation is not separating winners from losers yet (confirmed ${(confirmed.right * 100).toFixed(0)}% right vs ${(failed.right * 100).toFixed(0)}% rejected).`;
  }
  return {
    total: rows.length, pending: by("pending").length, expired: by("expired").length, all, confirmed, failed, blocked, reasons, verdict,
  };
}

export type HealthTone = "ok" | "error" | "waiting";
/** Is the worker alive? It writes a heartbeat on every pass (every ~20 s in a burst, at least every scheduler slot). */
export function heartbeatTone(beatAt: string | undefined | null, now = Date.now(), staleMin = 10): { tone: HealthTone; text: string } {
  if (!beatAt) return { tone: "waiting", text: "the scalper has not run yet" };
  const m = minutesSince(beatAt, now);
  return m <= staleMin ? { tone: "ok", text: `last pass ${m < 1.5 ? "just now" : `${m.toFixed(0)} min ago`}` }
    : { tone: "error", text: `last pass ${m < 90 ? `${m.toFixed(0)} min` : `${(m / 60).toFixed(1)} h`} ago: the worker looks stalled, so open positions are only checked when it runs` };
}
