import { runChecks } from "@/lib/health";
import { Pill } from "@/components/Ui";


const LABEL = { ok: "Working", error: "Error", waiting: "Waiting", unused: "Not used" } as const;
const KIND = { ok: "good", error: "bad", waiting: "warn", unused: "muted" } as const;

export default async function HealthView() {
  const checks = await runChecks();
  const errors = checks.filter((c) => c.status === "error").length;
  const waiting = checks.filter((c) => c.status === "waiting").length;
  return (
    <>
      <h2 className="viewtitle">System health</h2>
      <p className="sub">Live checks, run every time you load this page. Nothing here says the AI is accurate: it only shows that data is being collected and predictions are being logged.</p>
      <div className="note" style={{ borderLeftColor: errors ? "var(--neg)" : waiting ? "var(--warn)" : "var(--pos)" }}>
        <b>{errors ? `${errors} problem${errors > 1 ? "s" : ""} found.` : waiting ? "No errors. Some parts are waiting for their first data." : "Everything is working."}</b>
        {" "}Checked {new Date().toUTCString()}.
      </div>
      <div className="scroll"><table>
        <thead><tr><th>Component</th><th>Status</th><th>Details</th></tr></thead>
        <tbody>{checks.map((c) => (
          <tr key={c.name}><td><b>{c.name}</b></td><td><Pill kind={KIND[c.status]}>{LABEL[c.status]}</Pill></td><td className="muted">{c.detail}</td></tr>))}</tbody>
      </table></div>
      <p className="muted" style={{ marginTop: 14 }}>FRED, Congress.gov and the news feeds are judged from the worker's own poll records, so this page never needs your API keys. “Waiting” means no data has reached that stage yet, which is not a failure.</p>
    </>
  );
}
