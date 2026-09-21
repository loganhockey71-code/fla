"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

// FAKE-money order buttons. They call /api/trade, which only edits paper-trading rows in the database.
export default function OrderForms({ coin, cash, holding, live }: { coin: string; cash: number; holding: number; live: boolean }) {
  const router = useRouter();
  const [amount, setAmount] = useState(String(Math.min(100, Math.floor(cash))));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function send(body: Record<string, unknown>) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch("/api/trade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ coin, ...body }) });
      const j = await r.json();
      setMsg({ ok: !!j.ok, text: j.message ?? "Unknown response" });
      if (j.ok) router.refresh();
    } catch {
      setMsg({ ok: false, text: "Could not reach the server. Nothing was traded." });
    } finally {
      setBusy(false);
    }
  }

  const off = busy || !live;
  const chip = (label: string, value: number) => (
    <button type="button" key={label} className="chip" disabled={off} onClick={() => setAmount(value.toFixed(2))}>{label}</button>
  );
  return (
    <div className="orders">
      <div className="order-row">
        <span className="dollar">$</span>
        <input type="number" min={5} step="any" value={amount} onChange={(e) => setAmount(e.target.value)} aria-label={`Dollar amount to buy ${coin}`} />
        {[25, 50, 100].filter((v) => v <= cash).map((v) => chip(`$${v}`, v))}
        {chip("Max", cash)}
      </div>
      <button className="buy" disabled={off || !(cash >= 5)} onClick={() => send({ side: "buy", amountUsd: Number(amount) })}>
        {busy ? "Working…" : `Buy ${coin} (paper)`}
      </button>
      <div className="order-row" style={{ marginTop: 10 }}>
        {[0.25, 0.5, 1].map((f) => (
          <button key={f} className="sell" disabled={off || holding <= 0} onClick={() => send({ side: "sell", fraction: f })}>
            Sell {f === 1 ? "all" : `${f * 100}%`}
          </button>
        ))}
      </div>
      {!live && <div className="why">Live price unavailable: trading is paused until Coinbase answers.</div>}
      {msg && <div className={`why ${msg.ok ? "pos" : "neg"}`} role="status" style={{ marginTop: 8 }}>{msg.text}</div>}
    </div>
  );
}
