import type { Coin, Quote } from "./manual";

/** Live bid/ask from Coinbase's free public ticker (no key, read-only). Returns null if it can't be reached. */
export async function liveQuote(coin: Coin): Promise<Quote | null> {
  try {
    const r = await fetch(`https://api.exchange.coinbase.com/products/${coin}-USD/ticker`, { signal: AbortSignal.timeout(5000), cache: "no-store" });
    if (!r.ok) return null;
    const j = await r.json();
    const bid = Number(j.bid), ask = Number(j.ask);
    if (!(bid > 0) || !(ask >= bid)) return null;
    const mid = (bid + ask) / 2;
    return { mid, spreadPct: ((ask - bid) / mid) * 100 };
  } catch {
    return null;
  }
}
