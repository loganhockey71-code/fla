// "What should I do with the cash?" Pure recommendation logic (no I/O). PAPER TRADING ONLY.
// It never invests anything by itself: it produces options, and the user must confirm one.
import type { Coin } from "./manual";

export type Action = "BUY" | "HOLD" | "REDUCE" | "SELL";
export type NewsItem = { title: string; impact: number; tier: number };
export type CoinView = {
  coin: Coin; action: Action; confidence: number;            // confidence = the model's probability for the side it leans (0.5 = no edge)
  bull: number | null; sudden: boolean; urgency: "high" | "medium" | "low" | null; change24h: number | null;
  news: NewsItem[]; heldValue: number;
};
export type PlanInput = { soldCoins: Coin[]; amount: number; cash: number; totalValue: number; coins: CoinView[] };
export type Alloc = { coin: Coin; usd: number };
export type Option = { key: "recommended" | "safer" | "aggressive"; title: string; keepCash: number; buys: Alloc[]; note?: string };
export type Plan = { amount: number; recommended: Option; safer: Option; aggressive: Option; reason: string; scores: { coin: Coin; score: number; parts: string[] }[]; caution: string | null };

const MIN_BUY = 5;
const MAX_COIN_SHARE = 0.4;            // never let one coin exceed 40% of the whole paper portfolio
const ACTION_SCORE: Record<Action, number> = { BUY: 0.6, HOLD: 0, REDUCE: -0.6, SELL: -1.2 };
const cents = (n: number) => Math.floor(n * 100 + 1e-9) / 100;
export const usd = (n: number) => `$${n.toLocaleString("en-US", { minimumFractionDigits: n % 1 ? 2 : 0, maximumFractionDigits: 2 })}`;

export function scoreCoin(c: CoinView, stress: boolean): { score: number; parts: string[] } {
  const parts: string[] = [];
  let s = 0;
  if (c.bull != null) { const v = (c.bull - 0.5) * 4; s += v; parts.push(`model lean ${v >= 0 ? "+" : ""}${v.toFixed(2)}`); }
  const a = ACTION_SCORE[c.action];
  if (a) { s += a; parts.push(`${c.action} signal ${a > 0 ? "+" : ""}${a.toFixed(1)}`); }
  const cat = c.news.reduce<NewsItem | null>((b, n) => (!b || Math.abs(n.impact) * (n.tier <= 2 ? 1 : 0.5) > Math.abs(b.impact) * (b.tier <= 2 ? 1 : 0.5) ? n : b), null);
  if (cat && Math.abs(cat.impact) >= 10) {
    const v = Math.max(-0.5, Math.min(0.5, (cat.impact / 100) * (cat.tier <= 2 ? 1 : 0.5)));
    s += v; parts.push(`news ${v >= 0 ? "+" : ""}${v.toFixed(2)}`);
  }
  if (c.sudden && c.urgency === "high" && (c.action === "REDUCE" || c.action === "SELL")) { s -= 0.4; parts.push("sudden-event risk -0.40"); }
  if (stress) { s -= 0.3; parts.push("market stress -0.30"); }
  return { score: Math.round(s * 1000) / 1000, parts };
}

const word = (c: CoinView) => (c.action === "BUY" ? "bullish" : c.action === "HOLD" ? "neutral" : "bearish");

export function buildPlan(inp: PlanInput): Plan {
  const amount = Math.max(0, Math.min(inp.amount, inp.cash));
  const stressed = inp.coins.filter((c) => c.action === "REDUCE" || c.action === "SELL" || (c.change24h != null && c.change24h <= -0.05)).length >= 2;
  const scored = inp.coins.map((c) => ({ c, ...scoreCoin(c, stressed) })).sort((a, b) => b.score - a.score);
  const room = (c: CoinView) => Math.max(0, MAX_COIN_SHARE * inp.totalValue - c.heldValue);     // concentration cap
  const eligible = scored.filter((x) => x.score >= 0.4 && x.c.action !== "SELL" && x.c.action !== "REDUCE");
  const best = eligible[0];

  // ---- recommended
  let frac = 0;
  if (best) frac = best.score >= 1.0 ? 0.6 : best.score >= 0.7 ? 0.4 : 0.2;
  if (stressed) frac /= 2;
  const spread = (list: typeof scored, total: number, fixed?: number[]): Alloc[] => {
    const weights = fixed ?? list.map((x) => Math.max(x.score, 0.05));
    const wsum = weights.reduce((a, b) => a + b, 0);
    const out: Alloc[] = [];
    for (const [i, x] of list.entries()) {
      const want = cents((total * weights[i]) / wsum);
      const usdAmt = cents(Math.min(want, room(x.c)));
      if (usdAmt >= MIN_BUY) out.push({ coin: x.c.coin, usd: usdAmt });
    }
    return out;
  };
  const group = best ? eligible.filter((x) => best.score - x.score <= 0.35).slice(0, 2) : [];
  const recBuys = frac > 0 ? spread(group, cents(amount * frac)) : [];
  const recSpent = recBuys.reduce((a, b) => a + b.usd, 0);
  const recommended: Option = {
    key: "recommended", title: recBuys.length === 0 ? "Keep as cash" : recBuys.length > 1 ? "Split between coins" : `Buy ${recBuys[0].coin}`,
    keepCash: cents(amount - recSpent), buys: recBuys,
  };

  // ---- safer: everything stays cash
  const safer: Option = { key: "safer", title: "Safer: keep all as cash", keepCash: amount, buys: [], note: recBuys.length ? undefined : "Same as the recommendation" };

  // ---- more aggressive: bigger bet on the top one or two coins, even if the signals are lukewarm
  const top = scored.filter((x) => x.c.action !== "SELL").slice(0, 2);
  const aggFrac = best ? Math.min(0.8, Math.max(frac * 1.7, 0.4)) : 0.3;
  const aggGroup = top.length > 1 && top[1].score >= -0.2 ? top : top.slice(0, 1);
  const aggBuys = aggGroup.length ? spread(aggGroup, cents(amount * aggFrac), aggGroup.length > 1 ? [0.6, 0.4] : undefined) : [];   // a real 60/40 split, not a token amount
  const aggSpent = aggBuys.reduce((a, b) => a + b.usd, 0);
  const aggressive: Option = {
    key: "aggressive", title: aggBuys.length > 1 ? "More aggressive: split" : aggBuys.length ? `More aggressive: buy ${aggBuys[0].coin}` : "More aggressive (nothing fits your limits)",
    keepCash: cents(amount - aggSpent), buys: aggBuys,
    note: best ? undefined : "The current signals do not support this. It is a bet, not a recommendation.",
  };

  // ---- the explanation, in plain words
  const lead = scored[0];
  const others = scored.slice(1).map((x) => `${x.c.coin} is ${word(x.c)}`);
  const conf = (c: CoinView) => `${Math.round(c.confidence * 100)}%`;
  let reason: string;
  if (recBuys.length) {
    const names = recBuys.map((b) => b.coin).join(" and ");
    reason = `${names} ${recBuys.length > 1 ? "have" : "has"} the strongest ${best!.c.action} signal at ${conf(best!.c)} confidence, while ${others.filter((o) => !o.startsWith(best!.c.coin)).join(" and ")}.`;
  } else {
    reason = `No coin has a strong enough BUY signal right now (best: ${lead.c.coin} ${lead.c.action} at ${conf(lead.c)}), so holding cash is the low-risk choice.`;
  }
  const catNews = lead.c.news.find((n) => Math.abs(n.impact) >= 30);
  if (catNews) reason += ` Recent news: "${catNews.title.slice(0, 80)}" (${catNews.impact > 0 ? "positive" : "negative"} for ${lead.c.coin}).`;
  if (stressed) reason += " The market looks stressed, so the plan holds back.";
  const caution = scored.every((x) => x.c.confidence < 0.55) ? "Every signal is near 50%, meaning the AI sees no real edge. Its advice is unproven, so weigh this lightly." : null;

  return { amount, recommended, safer, aggressive, reason, scores: scored.map((x) => ({ coin: x.c.coin, score: x.score, parts: x.parts })), caution };
}

/** e.g. "Keep $300 cash · Invest $200 into ETH" */
export function summarizeOption(o: Option): string {
  const parts: string[] = [];
  if (o.keepCash > 0.004) parts.push(`Keep ${usd(o.keepCash)} cash`);
  for (const b of o.buys) parts.push(`Invest ${usd(b.usd)} into ${b.coin}`);
  return parts.join(" · ") || "Keep everything as cash";
}
