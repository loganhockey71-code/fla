import { NextRequest, NextResponse } from "next/server";
import { getSettings, sql } from "@/lib/db";
import { COINS, Coin, Quote, executeOrder, sellEverything } from "@/lib/manual";
import { liveQuote } from "@/lib/quotes";

export const dynamic = "force-dynamic";

// Paper trading only. This endpoint moves FAKE money between a database cash balance and fake coin lots.
export async function POST(req: NextRequest) {
  // CSRF guard: the browser sends the Basic-auth password automatically, so require a same-origin JSON request.
  const origin = req.headers.get("origin");
  if (!origin || new URL(origin).host !== req.headers.get("host") || !(req.headers.get("content-type") ?? "").startsWith("application/json"))
    return NextResponse.json({ ok: false, message: "Blocked: request must come from this site." }, { status: 403 });

  let body: { side?: string; coin?: string; amountUsd?: number; fraction?: number };
  try { body = await req.json(); } catch { return NextResponse.json({ ok: false, message: "Bad request." }, { status: 400 }); }
  const side = body.side;
  const coin = body.coin as Coin;
  if (side !== "buy" && side !== "sell" && side !== "sell_all")
    return NextResponse.json({ ok: false, message: "Only buying or selling is possible." }, { status: 400 });
  if (side !== "sell_all" && !COINS.includes(coin))
    return NextResponse.json({ ok: false, message: "Only BTC, ETH and XRP can be traded." }, { status: 400 });

  try {
    const db = sql();
    const cfg = await getSettings();
    const quotes = await Promise.all(COINS.map((c) => liveQuote(c)));
    const byCoin = Object.fromEntries(COINS.map((c, i) => [c, quotes[i]])) as Record<Coin, Quote | null>;
    const stored = await db`select distinct on (symbol) symbol, price from market_data order by symbol, ts desc`;
    const prices = Object.fromEntries(COINS.map((c, i) => [c, quotes[i]?.mid ?? (stored.find((r) => r.symbol === c)?.price as number) ?? 0])) as Record<Coin, number>;

    if (side === "sell_all") {
      const res = await sellEverything(db, byCoin, cfg, prices);
      return NextResponse.json(res, { status: res.ok ? 200 : 422 });
    }
    const q = quotes[COINS.indexOf(coin)];
    if (!q) return NextResponse.json({ ok: false, message: "Could not get a live Coinbase price. Nothing was traded." }, { status: 503 });
    // what the AI was advising at this moment, saved with the trade so "did following the AI help?" can be answered later
    const preds = await db`select distinct on (horizon_h) id, horizon_h, signal, confidence, bullish_prob, created_at from predictions
                           where symbol = ${coin} and variant = 'market' order by horizon_h, created_at desc`;
    const advice = { at: new Date().toISOString(), predictions: preds.map((p) => ({ id: p.id, horizon_h: p.horizon_h, signal: p.signal, confidence: p.confidence, bullish_prob: p.bullish_prob, made_at: p.created_at })) };
    const res = await executeOrder(db, { side, coin, amountUsd: body.amountUsd, fraction: body.fraction }, q, cfg, prices, advice);
    return NextResponse.json(res, { status: res.ok ? 200 : 422 });
  } catch (e) {
    return NextResponse.json({ ok: false, message: `Trade failed, nothing was changed (${e instanceof Error ? e.message.split("\n")[0].slice(0, 100) : "error"}).` }, { status: 500 });
  }
}
