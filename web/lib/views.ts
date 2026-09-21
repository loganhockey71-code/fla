// Read-only data for the dashboard and for cash plans. One source of truth so the two never disagree.
import type postgres from "postgres";
import { COINS, cashOf } from "./manual";
import type { Coin } from "./manual";
import type { Action, CoinView } from "./cashplan";

export type CoinSnap = {
  coin: Coin; price: number | null; change24h: number | null; spark: number[];
  action: Action; confidence: number | null; bull: number | null; reason: string; sudden: boolean; urgency: "high" | "medium" | "low" | null; asOf: Date | null;
  news: { title: string; impact: number; tier: number }[];
};

const short = (s: string, n = 90) => (s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s);

export async function coinSnapshots(sql: postgres.Sql): Promise<CoinSnap[]> {
  const [prices, candles, sigs, preds, news] = await Promise.all([
    sql`select distinct on (symbol) symbol, price, ts from market_data order by symbol, ts desc`,
    sql`select symbol, ts, close from candles where granularity = 900 and ts > now() - interval '25 hours' order by symbol, ts`,
    sql`select distinct on (symbol) * from live_signals order by symbol, created_at desc`,
    sql`select distinct on (symbol) symbol, signal, bullish_prob, confidence, reasons, created_at from predictions where variant = 'market' and horizon_h = 24 order by symbol, created_at desc`,
    sql`select title, origin_tier, btc_impact_score, eth_impact_score, xrp_impact_score, affected_coins from research_events
        where kind in ('news','legislation','macro') and detected_at > now() - interval '6 hours'
          and (kind <> 'news' or coalesce(published_at, detected_at) > now() - interval '6 hours') order by importance_score desc limit 30`,
  ]);
  const col = { BTC: "btc_impact_score", ETH: "eth_impact_score", XRP: "xrp_impact_score" } as const;
  return COINS.map((coin) => {
    const price = (prices.find((r) => r.symbol === coin)?.price as number) ?? null;
    const series = candles.filter((r) => r.symbol === coin).map((r) => r.close as number);
    const first = series[0];
    const change24h = price != null && first ? price / first - 1 : null;
    const sig = sigs.find((r) => r.symbol === coin);
    const pred = preds.find((r) => r.symbol === coin);
    const suddenLive = !!sig && sig.trigger_kind !== "scheduled" && new Date(sig.expires_at as Date).getTime() > Date.now();
    let action: Action = "HOLD", bull: number | null = null, reason = "No prediction yet", asOf: Date | null = null;
    if (pred) {
      action = pred.signal as Action; bull = pred.bullish_prob as number; asOf = pred.created_at as Date;
      const first0 = ((pred.reasons as string[]) ?? [])[0]?.replace(/\s*\((bullish|bearish)\)\s*$/, "");
      reason = action === "HOLD" ? `No clear edge (${Math.round(bull * 100)}% up / ${Math.round((1 - bull) * 100)}% down)`
        : `${action === "BUY" ? "Leans up" : "Leans down"}${first0 ? `: ${first0}` : ""}`;
    }
    if (suddenLive) {                                                       // a live shock overrides the regular read
      action = sig!.action as Action; asOf = sig!.created_at as Date;
      if (sig!.model_bull_prob != null) bull = sig!.model_bull_prob as number;
      reason = (sig!.cause as string) ? `Sudden ${sig!.trigger_kind}: ${sig!.cause}` : ((sig!.reasons as string[])[0] ?? "Sudden event");
    }
    return {
      coin, price, change24h, spark: series, action, bull, confidence: bull != null ? Math.max(bull, 1 - bull) : null, reason: short(reason),
      sudden: suddenLive, urgency: suddenLive ? (sig!.urgency as "high" | "medium" | "low") : null, asOf,
      news: news.filter((n) => (n.affected_coins as string[]).includes(coin)).map((n) => ({ title: n.title as string, impact: n[col[coin]] as number, tier: (n.origin_tier as number) ?? 4 })),
    };
  });
}

export function toCoinViews(snaps: CoinSnap[], held: Record<Coin, number>): CoinView[] {
  return snaps.map((s) => ({ coin: s.coin, action: s.action, confidence: s.confidence ?? 0.5, bull: s.bull, sudden: s.sudden, urgency: s.urgency, change24h: s.change24h, news: s.news, heldValue: held[s.coin] ?? 0 }));
}

export async function heldValues(sql: postgres.Sql, prices: Record<Coin, number>): Promise<Record<Coin, number>> {
  const rows = await sql`select symbol, sum(quantity)::float q from paper_trades where account = 'manual' and status = 'open' group by symbol`;
  return Object.fromEntries(COINS.map((c) => [c, ((rows.find((r) => r.symbol === c)?.q as number) ?? 0) * (prices[c] ?? 0)])) as Record<Coin, number>;
}

export async function latestNews(sql: postgres.Sql, n = 5) {
  return sql`select id, title, source, source_url, coalesce(published_at, detected_at) as at, affected_coins, sentiment, importance_score from research_events
             where importance_score >= 45 and (kind <> 'legislation' or details->>'change' is distinct from 'new') and kind <> 'prediction_market'
             order by coalesce(published_at, detected_at) desc limit ${n}`;
}

/** The user's own paper account: value, cash, P/L and a plain list of buys and sells. */
export async function manualPortfolio(sql: postgres.Sql, startingBalance: number, mids: Record<Coin, number>) {
  const trades = await sql`select * from paper_trades where account = 'manual' and status in ('open','closed') order by id`;
  const cash = cashOf(startingBalance, trades as never);
  const positions = trades.filter((t) => t.status === "open").reduce((a, t) => a + (t.quantity as number) * (mids[t.symbol as Coin] ?? 0), 0);
  const total = cash + positions;
  // a buy that was partly sold is stored as several rows sharing the same open time: show it once as one BUY
  const buys = new Map<string, { at: Date; coin: string; qty: number; price: number }>();
  for (const t of trades) {
    const k = `${t.symbol}|${(t.opened_at as Date).toISOString()}|${t.exec_price}`;
    const b = buys.get(k) ?? { at: t.opened_at as Date, coin: t.symbol as string, qty: 0, price: t.exec_price as number };
    b.qty += t.quantity as number;
    buys.set(k, b);
  }
  const events = [
    ...[...buys.values()].map((b) => ({ at: b.at, coin: b.coin, side: "BUY" as const, qty: b.qty, price: b.price, pnl: null as number | null })),
    ...sellsGrouped(trades),
  ].sort((a, b) => +new Date(b.at) - +new Date(a.at));
  return { cash, positions, total, pnl: total - startingBalance, events };
}


/** One "sell all" that closed several buy lots is ONE sale to the user: merge rows sharing coin, time and price. */
function sellsGrouped(trades: Record<string, unknown>[]) {
  const m = new Map<string, { at: Date; coin: string; side: "SELL"; qty: number; price: number; pnl: number }>();
  for (const t of trades.filter((x) => x.status === "closed")) {
    const k = `${t.symbol}|${(t.closed_at as Date).toISOString()}|${t.exit_price}`;
    const g = m.get(k) ?? { at: t.closed_at as Date, coin: t.symbol as string, side: "SELL" as const, qty: 0, price: t.exit_price as number, pnl: 0 };
    g.qty += t.quantity as number;
    g.pnl += t.pnl_usd as number;
    m.set(k, g);
  }
  return [...m.values()];
}
