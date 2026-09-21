import { ReactNode } from "react";

const ICON: Record<string, { bg: string; glyph: string }> = { BTC: { bg: "#f7931a", glyph: "₿" }, ETH: { bg: "#627eea", glyph: "Ξ" }, XRP: { bg: "#23292f", glyph: "✕" } };

export function CoinIcon({ coin, size = 40 }: { coin: string; size?: number }) {
  const i = ICON[coin] ?? { bg: "#333", glyph: coin[0] };
  return <span className="coinicon" style={{ width: size, height: size, background: i.bg, fontSize: size * 0.5 }} aria-hidden>{i.glyph}</span>;
}

/** Tiny 24h price chart drawn as inline SVG (no client JavaScript). */
export function Spark({ points, up, width = 190, height = 64 }: { points: number[]; up: boolean; width?: number; height?: number }) {
  if (points.length < 2) return <div className="spark muted" style={{ width, height }} />;
  const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
  const step = width / (points.length - 1);
  const xy = points.map((p, i) => [i * step, height - 4 - ((p - min) / span) * (height - 8)] as const);
  const line = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  const color = up ? "#2ee59d" : "#ff5c7a";
  const id = `g${up ? "u" : "d"}${points.length}`;
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="24 hour price chart">
      <defs><linearGradient id={id} x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor={color} stopOpacity=".28" /><stop offset="1" stopColor={color} stopOpacity="0" /></linearGradient></defs>
      <path d={`${line} L${width} ${height} L0 ${height} Z`} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export const Card = ({ title, action, children, className = "" }: { title: string; action?: ReactNode; children: ReactNode; className?: string }) => (
  <section className={`card ${className}`}>
    <div className="cardhead"><h3>{title}</h3>{action}</div>
    {children}
  </section>
);
