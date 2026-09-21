"use client";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const axis = { stroke: "#8a94a8", fontSize: 11 };
const tip = { contentStyle: { background: "#171d2b", border: "1px solid #232b3d", borderRadius: 8, color: "#e6eaf2" }, labelStyle: { color: "#8a94a8" } };

type Pt = { t: string; v: number | null; n?: number };

const fmtT = (t: string) => new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" });

export function TimeChart({ data, kind = "line", refY, unit = "", color = "#6ea8ff", domain, height = 240 }:
  { data: Pt[]; kind?: "line" | "area"; refY?: number; unit?: string; color?: string; domain?: [number | "auto", number | "auto"]; height?: number }) {
  if (!data.length) return <div className="muted" style={{ padding: 30 }}>No data yet.</div>;
  const common = (
    <>
      <CartesianGrid stroke="#232b3d" vertical={false} />
      <XAxis dataKey="t" tickFormatter={fmtT} {...axis} minTickGap={40} />
      <YAxis {...axis} domain={domain ?? ["auto", "auto"]} width={62} tickFormatter={(v) => `${Number(v).toLocaleString("en-US", { maximumFractionDigits: 1 })}${unit}`} />
      <Tooltip {...tip} labelFormatter={(t) => new Date(t as string).toLocaleString()} formatter={(v: number, _n, p) => [`${Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 })}${unit}${p?.payload?.n != null ? `  (n=${p.payload.n})` : ""}`, ""]} />
      {refY != null && <ReferenceLine y={refY} stroke="#8a94a8" strokeDasharray="4 4" />}
    </>
  );
  return (
    <ResponsiveContainer width="100%" height={height}>
      {kind === "area" ? (
        <AreaChart data={data}>{common}<Area dataKey="v" stroke={color} fill={color} fillOpacity={0.15} strokeWidth={2} dot={false} isAnimationActive={false} /></AreaChart>
      ) : (
        <LineChart data={data}>{common}<Line dataKey="v" stroke={color} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} /></LineChart>
      )}
    </ResponsiveContainer>
  );
}

export function AccuracyBars({ data }: { data: { name: string; acc: number | null; n: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data.map((d) => ({ ...d, v: d.acc == null ? 0 : d.acc * 100 }))}>
        <CartesianGrid stroke="#232b3d" vertical={false} />
        <XAxis dataKey="name" {...axis} />
        <YAxis {...axis} domain={[30, 70]} unit="%" width={48} />
        <Tooltip {...tip} formatter={(v: number, _n, p) => [`${v.toFixed(1)}%  (n=${p.payload.n})`, "directional accuracy"]} />
        <ReferenceLine y={50} stroke="#f5b942" strokeDasharray="4 4" label={{ value: "coin flip", fill: "#f5b942", fontSize: 11, position: "insideTopRight" }} />
        <Bar dataKey="v" radius={[4, 4, 0, 0]} isAnimationActive={false}>
          {data.map((d, i) => <Cell key={i} fill={d.acc != null && d.acc > 0.5 ? "#2ecc8f" : "#ff5c6c"} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
