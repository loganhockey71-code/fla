// "Is this real skill or luck?" helpers. Deliberately conservative.

function erf(x: number) {
  const t = 1 / (1 + 0.3275911 * Math.abs(x));
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return x >= 0 ? y : -y;
}
const normCdf = (z: number) => 0.5 * (1 + erf(z / Math.SQRT2));

/** Two-sided p-value: could `hits` out of `n` have happened by chance if the true hit rate were `p0`? */
export function binomialP(hits: number, n: number, p0 = 0.5): number {
  if (n === 0) return 1;
  const z = (hits - n * p0) / Math.sqrt(n * p0 * (1 - p0));
  return Math.min(1, 2 * (1 - normCdf(Math.abs(z))));
}

export type Verdict = { label: "Too early" | "No edge detected" | "Possible edge" | "Likely real edge"; tone: "muted" | "bad" | "warn" | "good"; text: string };

export function verdict(n: number, hits: number, baseline: number): Verdict {
  const acc = n ? hits / n : 0;
  const p = binomialP(hits, n, baseline);
  if (n < 100)
    return { label: "Too early", tone: "muted", text: `Only ${n} scored predictions. Anything below ~100 is noise; below ~300 is still weak evidence.` };
  if (p >= 0.1 || acc <= baseline)
    return { label: "No edge detected", tone: "bad", text: `${(acc * 100).toFixed(1)}% vs ${(baseline * 100).toFixed(1)}% by chance (p = ${p.toFixed(2)}). Indistinguishable from random so far.` };
  if (p >= 0.01 || n < 300)
    return { label: "Possible edge", tone: "warn", text: `${(acc * 100).toFixed(1)}% vs ${(baseline * 100).toFixed(1)}% by chance (p = ${p.toFixed(3)}). Promising but not proven — keep collecting data.` };
  return { label: "Likely real edge", tone: "good", text: `${(acc * 100).toFixed(1)}% vs ${(baseline * 100).toFixed(1)}% by chance (p = ${p.toFixed(4)}). Now check the paper P/L: accuracy only pays if it survives fees.` };
}

export const MILESTONES = [30, 60, 90, 180];

// ---- A/B: does adding research data improve on the market-only model? ---------------------------------------
export type Scored = { symbol: string; horizon_h: number; signal: string; variant: string; run_id: string | null; directional_correct: boolean; signal_correct: boolean; ret: number };
const COST_PCT = 1.0; // 0.4% fee + 0.1% slippage, both ways

function variantStats(rows: Scored[]) {
  const buys = rows.filter((r) => r.signal === "BUY");
  return {
    n: rows.length,
    dir: rows.length ? rows.filter((r) => r.directional_correct).length / rows.length : null,
    sig: rows.length ? rows.filter((r) => r.signal_correct).length / rows.length : null,
    buys: buys.length,
    netBuy: buys.length ? buys.reduce((a, r) => a + r.ret - COST_PCT, 0) / buys.length : null,
  };
}

/** Compares the two variants on the SAME prediction moments (paired by run_id + coin + horizon). */
export function compareVariants(rows: Scored[]) {
  const m = rows.filter((r) => r.variant === "market"), rs = rows.filter((r) => r.variant === "research");
  const key = (r: Scored) => `${r.run_id}|${r.symbol}|${r.horizon_h}`;
  const rmap = new Map(rs.map((r) => [key(r), r]));
  let both = 0, onlyM = 0, onlyR = 0, neither = 0;
  for (const a of m) {
    const b = rmap.get(key(a));
    if (!b || !a.run_id) continue;
    if (a.directional_correct && b.directional_correct) both++;
    else if (a.directional_correct) onlyM++;
    else if (b.directional_correct) onlyR++;
    else neither++;
  }
  const pairs = both + onlyM + onlyR + neither, disc = onlyM + onlyR;
  const p = disc ? binomialP(onlyR, disc, 0.5) : 1;     // McNemar-style: among disagreements, is one model right more often?
  let label: "Too early" | "No measurable difference" | "Research helps" | "Research hurts" = "Too early";
  let text = `${pairs} paired predictions so far. Below ~200 pairs (and ~30 disagreements) the difference is noise.`;
  if (pairs >= 200 && disc >= 30) {
    if (p < 0.05 && onlyR > onlyM) { label = "Research helps"; text = `Research was right and market-only wrong ${onlyR} times vs ${onlyM} the other way (p = ${p.toFixed(3)}). Check it also survives fees before trusting it.`; }
    else if (p < 0.05) { label = "Research hurts"; text = `Market-only won ${onlyM} disagreements to ${onlyR} (p = ${p.toFixed(3)}). Research features are adding noise.`; }
    else { label = "No measurable difference"; text = `Disagreements split ${onlyR} (research) vs ${onlyM} (market-only), p = ${p.toFixed(2)}. Research adds nothing detectable yet.`; }
  }
  return { market: variantStats(m), research: variantStats(rs), pairs, both, onlyM, onlyR, neither, p, label, text };
}
