"use client";
import { useEffect, useRef, useState } from "react";
import { Card, CoinIcon } from "@/components/Visuals";

const COINS = ["BTC", "ETH", "XRP"] as const;
type Coin = (typeof COINS)[number];
type Candle = { t: string; o: number; h: number; l: number; c: number };

const POLL_MS = 15_000;   // Coinbase 1-minute candles: no point polling faster than that

function CandlestickSVG({ candles, width = 760, height = 260 }: { candles: Candle[]; width?: number; height?: number }) {
  if (candles.length < 2) return <div className="muted" style={{ padding: 30 }}>Loading live candles…</div>;
  const pad = { top: 10, bottom: 22, left: 6, right: 6 };
  const innerH = height - pad.top - pad.bottom;
  const lo = Math.min(...candles.map((c) => c.l)), hi = Math.max(...candles.map((c) => c.h));
  const span = hi - lo || 1;
  const y = (v: number) => pad.top + innerH - ((v - lo) / span) * innerH;
  const slot = (width - pad.left - pad.right) / candles.length;
  const bodyW = Math.max(2, slot * 0.6);

  return (
    <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="live candlestick chart">
      {candles.map((c, i) => {
        const x = pad.left + i * slot + slot / 2;
        const up = c.c >= c.o;
        const color = up ? "#2ee59d" : "#ff5c7a";
        const bodyTop = y(Math.max(c.o, c.c)), bodyBot = y(Math.min(c.o, c.c));
        return (
          <g key={c.t}>
            <line x1={x} x2={x} y1={y(c.h)} y2={y(c.l)} stroke={color} strokeWidth={1} />
            <rect x={x - bodyW / 2} y={bodyTop} width={bodyW} height={Math.max(1, bodyBot - bodyTop)} fill={color} />
          </g>
        );
      })}
      {candles.length > 1 && (
        <text x={pad.left} y={height - 6} fill="#8a94a8" fontSize={10}>
          {new Date(candles[0].t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
        </text>
      )}
      <text x={width - pad.right} y={height - 6} fill="#8a94a8" fontSize={10} textAnchor="end">
        {new Date(candles[candles.length - 1].t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
      </text>
    </svg>
  );
}

export function LiveCandles() {
  const [coin, setCoin] = useState<Coin>("BTC");
  const [candles, setCandles] = useState<Candle[]>([]);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [stale, setStale] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const r = await fetch(`/api/candles?symbol=${coin}&limit=90`, { cache: "no-store" });
        const j = await r.json();
        if (cancelled) return;
        if (j.ok && j.candles?.length) {
          setCandles(j.candles);
          setUpdatedAt(new Date());
          setStale(false);
        } else {
          setStale(true);
        }
      } catch {
        if (!cancelled) setStale(true);
      }
    }
    setCandles([]);
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => { cancelled = true; if (timer.current) clearInterval(timer.current); };
  }, [coin]);

  return (
    <Card title="Live price chart" action={<span className="muted small">1-min candles · updates every 15s{updatedAt ? ` · last ${updatedAt.toLocaleTimeString()}` : ""}</span>}>
      <div className="tabs">
        {COINS.map((c) => (
          <a key={c} className={c === coin ? "on" : ""} onClick={() => setCoin(c)} style={{ cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
            <CoinIcon coin={c} size={18} />{c}
          </a>
        ))}
      </div>
      {stale && !candles.length ? <div className="muted small">Couldn't reach Coinbase right now. Retrying…</div> : <CandlestickSVG candles={candles} />}
    </Card>
  );
}
