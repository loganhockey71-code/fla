import { sql } from "./db";

export const COINS = ["BTC", "ETH", "XRP"] as const;
export type Coin = (typeof COINS)[number];

export async function latestPrices() {
  const rows = await sql()`select distinct on (symbol) symbol, price, spread_pct, ts from market_data order by symbol, ts desc`;
  return Object.fromEntries(rows.map((r) => [r.symbol as string, r as unknown as { price: number; spread_pct: number | null; ts: Date }]));
}

export async function latestPredictions() {
  return sql()`select distinct on (symbol, horizon_h) * from predictions where variant = 'market' order by symbol, horizon_h, created_at desc`;
}

export async function perfMetrics() {
  const rows = await sql()`select * from performance_metrics`;
  const find = (scope: string, horizon: number | null, slice: string, variant = "market") =>
    rows.find((r) => r.variant === variant && r.scope === scope && (r.horizon_h ?? null) === horizon && r.slice === slice);
  return { rows, find };
}

export async function portfolioSummary(startingBalance: number) {
  const [last] = await sql()`select ts, cash, positions_value, total_value from portfolio order by ts desc limit 1`;
  const byCoin = await sql()`select symbol, coalesce(sum(pnl_usd),0)::float as pnl, count(*)::int as n from paper_trades where status='closed' and account='ai' group by symbol`;
  const closedTotal = byCoin.reduce((a, r) => a + (r.pnl as number), 0);
  const current = last ? (last.total_value as number) : startingBalance;
  return {
    starting: startingBalance, current, cash: last?.cash as number | undefined, positions: last?.positions_value as number | undefined,
    pnl: current - startingBalance, ret: (current - startingBalance) / startingBalance, closedPnl: closedTotal,
    byCoin: Object.fromEntries(COINS.map((c) => [c, byCoin.find((r) => r.symbol === c) ?? { pnl: 0, n: 0 }])) as Record<Coin, { pnl: number; n: number }>,
  };
}

/** Every scored prediction, oldest first. Used for the accuracy-over-time and milestone views. */
export async function scoredResults() {
  return sql()`
    select p.symbol, p.horizon_h, p.signal, p.created_at, p.target_time, p.variant, p.run_id, r.directional_correct, r.signal_correct,
           r.high_confidence, r.actual_return_pct::float as ret
    from prediction_results r join predictions p on p.id = r.prediction_id order by p.target_time`;
}

/** The most recent "what to do now" signal per coin (BUY / HOLD / REDUCE / SELL). Paper trading only. */
export async function latestSignals() {
  const rows = await sql()`select distinct on (symbol) * from live_signals order by symbol, created_at desc`;
  return Object.fromEntries(rows.map((r) => [r.symbol as string, r])) as Record<string, Record<string, any>>;
}

export const ACTION_HELP: Record<string, string> = {
  BUY: "open or add a small position",
  HOLD: "do nothing",
  REDUCE: "sell about half of what you hold",
  SELL: "sell everything you hold",
};
