"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

// FAKE-money order buttons. They call /api/trade, which only edits paper-trading rows in the database.
function useOrder() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  async function send(body: Record<string, unknown>) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch("/api/trade", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json();
      setMsg({ ok: !!j.ok, text: j.message ?? "Unknown response" });
      if (j.ok) router.refresh();
    } catch {
      setMsg({ ok: false, text: "Could not reach the server. Nothing was traded." });
    } finally {
      setBusy(false);
    }
  }
  return { busy, msg, send };
}

const Msg = ({ msg }: { msg: { ok: boolean; text: string } | null }) =>
  msg ? <div className={`why ${msg.ok ? "pos" : "neg"}`} role="status" style={{ marginTop: 8 }}>{msg.text}</div> : null;

/** Buy + sell controls for one coin. Sell buttons are always visible; if you hold nothing they say why instead of just greying out. */
export default function OrderForms({ coin, cash, holding, heldValue, live }: { coin: string; cash: number; holding: number; heldValue: number; live: boolean }) {
  const { busy, msg, send } = useOrder();
  const [amount, setAmount] = useState(String(Math.min(100, Math.floor(cash))));
  const [sellUsd, setSellUsd] = useState("");
  const off = busy || !live;
  const chip = (label: string, onClick: () => void) => <button type="button" key={label} className="chip" disabled={off} onClick={onClick}>{label}</button>;

  return (
    <div className="orders">
      <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em", marginBottom: 6 }}>Buy {coin}</div>
      <div className="order-row">
        <span className="dollar">$</span>
        <input type="number" min={5} step="any" value={amount} onChange={(e) => setAmount(e.target.value)} aria-label={`Dollar amount to buy ${coin}`} />
        {[25, 50, 100].filter((v) => v <= cash).map((v) => chip(`$${v}`, () => setAmount(String(v))))}
        {chip("Max", () => setAmount(cash.toFixed(2)))}
      </div>
      <button className="buy" disabled={off || !(cash >= 5)} onClick={() => send({ coin, side: "buy", amountUsd: Number(amount) })}>
        {busy ? "Working…" : `Buy ${coin} (paper)`}
      </button>
      {cash < 5 && <div className="why">No cash left to buy with. Sell something to free up cash.</div>}

      <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em", margin: "16px 0 6px" }}>Sell {coin}</div>
      {holding > 0 ? (
        <>
          <div className="order-row">
            {[0.25, 0.5, 0.75, 1].map((f) => (
              <button key={f} className="sell" disabled={off} onClick={() => send({ coin, side: "sell", fraction: f })}>{f === 1 ? "Sell all" : `Sell ${f * 100}%`}</button>
            ))}
          </div>
          <div className="order-row">
            <span className="dollar">$</span>
            <input type="number" min={1} step="any" placeholder="amount" value={sellUsd} onChange={(e) => setSellUsd(e.target.value)} aria-label={`Dollar amount of ${coin} to sell`} />
            {chip("Max", () => setSellUsd(heldValue.toFixed(2)))}
            <button className="sell" style={{ flex: "0 0 auto" }} disabled={off || !(Number(sellUsd) > 0)} onClick={() => send({ coin, side: "sell", amountUsd: Number(sellUsd) })}>Sell this much</button>
          </div>
        </>
      ) : (
        <div className="why">You don't hold any {coin} yet. Buy some above and the sell buttons will work here.</div>
      )}
      {!live && <div className="why">Live price unavailable: trading is paused until Coinbase answers.</div>}
      <Msg msg={msg} />
    </div>
  );
}

/** One-click exit from everything you hold (asks to confirm first). */
export function SellEverything({ positions, live }: { positions: number; live: boolean }) {
  const { busy, msg, send } = useOrder();
  const [confirm, setConfirm] = useState(false);
  if (positions <= 0) return <span className="muted">Nothing to sell yet</span>;
  return (
    <span>
      {!confirm ? (
        <button className="sell" style={{ flex: "0 0 auto" }} disabled={busy || !live} onClick={() => setConfirm(true)}>Sell everything</button>
      ) : (
        <span className="order-row" style={{ display: "inline-flex" }}>
          <span className="warn">Sell all your coins at the current price?</span>
          <button className="sell" style={{ flex: "0 0 auto" }} disabled={busy} onClick={async () => { await send({ side: "sell_all" }); setConfirm(false); }}>{busy ? "Selling…" : "Yes, sell everything"}</button>
          <button className="chip" disabled={busy} onClick={() => setConfirm(false)}>Cancel</button>
        </span>
      )}
      <Msg msg={msg} />
    </span>
  );
}

/** Compact sell buttons for a row of the holdings table. */
export function QuickSell({ coin, live }: { coin: string; live: boolean }) {
  const { busy, msg, send } = useOrder();
  return (
    <span>
      <span className="order-row" style={{ display: "inline-flex", marginBottom: 0 }}>
        <button className="chip" disabled={busy || !live} onClick={() => send({ coin, side: "sell", fraction: 0.5 })}>Sell 50%</button>
        <button className="sell" style={{ flex: "0 0 auto", padding: "4px 12px" }} disabled={busy || !live} onClick={() => send({ coin, side: "sell", fraction: 1 })}>Sell all {coin}</button>
      </span>
      <Msg msg={msg} />
    </span>
  );
}
