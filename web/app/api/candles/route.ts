import { NextRequest, NextResponse } from "next/server";
import { COINS, Coin } from "@/lib/data";

export const dynamic = "force-dynamic";

type Candle = { t: string; o: number; h: number; l: number; c: number };

// Coinbase returns rows as [time, low, high, open, close, volume], newest first.
type RawRow = [number, number, number, number, number, number];

/** Live 1-minute candles straight from Coinbase's free public API (no key, read-only) - always fresh,
 * independent of the worker's own tick cadence, so the dashboard chart can visibly move on its own. */
export async function GET(req: NextRequest) {
  const symbol = req.nextUrl.searchParams.get("symbol") ?? "";
  if (!COINS.includes(symbol as Coin)) return NextResponse.json({ ok: false, message: "Unknown coin." }, { status: 400 });
  const limit = Math.min(Math.max(Number(req.nextUrl.searchParams.get("limit") ?? 90), 10), 300);

  try {
    const end = new Date();
    const start = new Date(end.getTime() - limit * 60_000);
    const url = `https://api.exchange.coinbase.com/products/${symbol}-USD/candles` +
      `?granularity=60&start=${start.toISOString()}&end=${end.toISOString()}`;
    const r = await fetch(url, { signal: AbortSignal.timeout(5000), cache: "no-store", headers: { "User-Agent": "crypto-ai-lab-web/1.0" } });
    if (!r.ok) return NextResponse.json({ ok: false, message: "Coinbase unavailable." }, { status: 502 });
    const rows = (await r.json()) as RawRow[];
    const candles: Candle[] = rows
      .map(([time, low, high, open, close]) => ({ t: new Date(time * 1000).toISOString(), o: open, h: high, l: low, c: close }))
      .sort((a, b) => a.t.localeCompare(b.t));
    return NextResponse.json({ ok: true, candles });
  } catch {
    return NextResponse.json({ ok: false, message: "Could not reach Coinbase." }, { status: 502 });
  }
}
