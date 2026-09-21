export const usd = (n: number | null | undefined, d = 2) =>
  n == null ? "—" : (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

export const signedUsd = (n: number | null | undefined) => (n == null ? "—" : (n >= 0 ? "+" : "") + usd(n));

export const pct = (n: number | null | undefined, d = 1) => (n == null ? "—" : `${(n * 100).toFixed(d)}%`);
export const pctPts = (n: number | null | undefined, d = 2) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(d)}%`);

export function price(n: number | null | undefined) {
  if (n == null) return "—";
  return usd(n, n >= 100 ? 2 : n >= 1 ? 3 : 4);
}

export const when = (d: Date | string | null | undefined) =>
  d ? new Date(d).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }) + " UTC" : "—";

export const tone = (n: number | null | undefined) => (n == null || n === 0 ? "" : n > 0 ? "pos" : "neg");
