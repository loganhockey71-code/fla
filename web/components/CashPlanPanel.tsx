"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";

type Alloc = { coin: string; usd: number };
type Option = { key: string; title: string; keepCash: number; buys: Alloc[]; note?: string };
type Plan = { amount: number; recommended: Option; safer: Option; aggressive: Option; reason: string; caution: string | null };

const money = (n: number) => `$${n.toLocaleString("en-US", { minimumFractionDigits: n % 1 ? 2 : 0, maximumFractionDigits: 2 })}`;
const line = (o: Option) => [o.keepCash > 0.004 ? `Keep ${money(o.keepCash)} cash` : null, ...o.buys.map((b) => `Invest ${money(b.usd)} into ${b.coin}`)].filter(Boolean).join(" · ") || "Keep everything as cash";

/**
 * "What should I do with the cash?" Shown after a sale. It only RECOMMENDS. Each button below is an explicit
 * confirmation, and nothing is bought until one is pressed. Paper money only.
 */
export default function CashPlanPanel({ id, soldCoins, plan, cash }: { id: number; soldCoins: string[]; plan: Plan; cash: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [done, setDone] = useState(false);

  async function choose(choice: string) {
    setBusy(choice); setMsg(null);
    try {
      const r = await fetch("/api/cashplan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, choice }) });
      const j = await r.json();
      setMsg({ ok: !!j.ok, text: j.message ?? "Unknown response" });
      if (j.ok) { setDone(true); router.refresh(); }
    } catch { setMsg({ ok: false, text: "Could not reach the server. Nothing was bought." }); }
    finally { setBusy(null); }
  }

  if (done) return <div className="cashplan done"><div className={msg?.ok ? "pos" : "neg"} role="status">{msg?.text}</div></div>;
  const sameAsRec = (o: Option) => JSON.stringify([o.keepCash, o.buys]) === JSON.stringify([plan.recommended.keepCash, plan.recommended.buys]);
  const alts = [plan.safer, plan.aggressive].filter((o) => !sameAsRec(o));

  return (
    <div className="cashplan" role="region" aria-label="What should I do with the cash?">
      <div className="cashplan-head">
        <div>
          <div className="cp-kicker">What should I do with the cash?</div>
          <div className="cp-title">You sold {money(plan.amount)} of {soldCoins.join(" and ") || "your coins"}</div>
        </div>
        <button className="chip" disabled={!!busy} onClick={() => choose("dismiss")}>No thanks, keep it as cash</button>
      </div>

      <div className="cp-main">
        <div className="cp-label">Recommended next move</div>
        <div className="cp-big">{line(plan.recommended)}</div>
        <div className="cp-reason"><b>Reason:</b> {plan.reason}</div>
        {plan.caution && <div className="cp-caution">{plan.caution}</div>}
        <button className="buy" style={{ marginTop: 12, maxWidth: 360 }} disabled={!!busy} onClick={() => choose("recommended")}>
          {busy === "recommended" ? "Working…" : plan.recommended.buys.length ? "Confirm recommended plan" : "Confirm: keep it as cash"}
        </button>
      </div>

      {alts.length > 0 && (
        <div className="cp-alts">
          {alts.map((o) => (
            <div className="cp-alt" key={o.key}>
              <div className="cp-label">{o.key === "safer" ? "Safer option" : "More aggressive option"}</div>
              <div style={{ fontWeight: 600 }}>{line(o)}</div>
              {o.note && <div className="why">{o.note}</div>}
              <button className={o.key === "safer" ? "chip" : "sell"} style={{ marginTop: 8, flex: "0 0 auto" }} disabled={!!busy} onClick={() => choose(o.key)}>
                {busy === o.key ? "Working…" : o.key === "safer" ? "Choose this (keep cash)" : "Choose this instead"}
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="why" style={{ marginTop: 10 }}>Paper money only. Nothing is invested until you press a confirm button. You have {money(cash)} in cash in total.</div>
      {msg && !msg.ok && <div className="why neg" role="status" style={{ marginTop: 8 }}>{msg.text}</div>}
    </div>
  );
}
