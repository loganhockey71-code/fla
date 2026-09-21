import { revalidatePath } from "next/cache";
import { safe, sql, getSettings } from "@/lib/db";
import { SetupError } from "@/components/Ui";

export const dynamic = "force-dynamic";

type Field = { key: string; label: string; help: string; min: number; max: number; step?: string };
const FIELDS: Field[] = [
  { key: "trading_fee_pct", label: "Trading fee per side (%)", help: "Default 0.4. Charged on entry and again on exit.", min: 0, max: 5, step: "0.01" },
  { key: "slippage_pct", label: "Slippage per side (%)", help: "Default 0.10. Fills are made worse by this amount.", min: 0, max: 5, step: "0.01" },
  { key: "normal_position_pct", label: "Normal signal position (% of portfolio)", help: "Default 10.", min: 0.1, max: 20, step: "0.1" },
  { key: "high_conf_position_pct", label: "High-confidence position (% of portfolio)", help: "Default 20. Hard-capped at 20 — no leverage, ever.", min: 0.1, max: 20, step: "0.1" },
  { key: "high_confidence_threshold", label: "High-confidence threshold (%)", help: "Default 75. Used for sizing and the separate high-confidence stats.", min: 50, max: 99, step: "1" },
  { key: "signal_threshold_pct", label: "Signal threshold (%)", help: "BUY/SELL only when the model's calibrated probability reaches this; otherwise HOLD. Higher = more HOLDs.", min: 50, max: 90, step: "0.5" },
  { key: "max_spread_pct_to_trade", label: "Max spread to trade (%)", help: "Wider than this forces HOLD.", min: 0.01, max: 5, step: "0.01" },
  { key: "prediction_interval_h", label: "Hours between predictions", help: "Per coin & horizon. Frequent predictions overlap and inflate sample counts without adding independent evidence.", min: 1, max: 48, step: "1" },
  { key: "hold_band_pct_24h", label: "HOLD counts as correct if 24h move is under (%)", help: "Default 1.5.", min: 0.1, max: 10, step: "0.1" },
  { key: "hold_band_pct_48h", label: "HOLD counts as correct if 48h move is under (%)", help: "Default 2.0.", min: 0.1, max: 10, step: "0.1" },
  { key: "sudden_move_pct", label: "Sudden move trigger (%)", help: "Price move within 30 minutes that starts an investigation.", min: 0.2, max: 10, step: "0.1" },
  { key: "volume_spike_x", label: "Volume spike (× normal)", help: "5-minute volume vs the prior hour's average.", min: 1.5, max: 20, step: "0.5" },
];

async function save(form: FormData) {
  "use server";
  const db = sql();
  const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));
  const vals: Record<string, unknown> = {};
  for (const f of FIELDS) {
    const n = Number(form.get(f.key));
    if (Number.isFinite(n)) vals[f.key] = clamp(n, f.min, f.max);
  }
  if ((vals.normal_position_pct as number) > (vals.high_conf_position_pct as number)) vals.normal_position_pct = vals.high_conf_position_pct;
  vals.use_real_spread = form.get("use_real_spread") === "on";
  for (const [key, value] of Object.entries(vals)) {
    await db`insert into settings (key, value, updated_at) values (${key}, ${db.json(value as never)}, now())
             on conflict (key) do update set value = excluded.value, updated_at = now()`;
  }
  revalidatePath("/settings");
}

export default async function Settings() {
  const { data, error } = await safe(getSettings);
  if (error || !data) return <><h1>Settings</h1><SetupError error={error ?? "unknown"} /></>;
  const cfg = data as unknown as Record<string, number | boolean>;
  return (
    <>
      <h1>Settings</h1>
      <p className="sub">Changes apply to trades and predictions made from now on. History is never rewritten.</p>
      <div className="note">Starting balance is fixed at <b>${Number(cfg.starting_balance).toLocaleString()}</b> so the P/L record stays comparable. To use a different amount, start a fresh database.</div>
      <form action={save} className="settings">
        {FIELDS.map((f) => (
          <label key={f.key}><span>{f.label}</span>
            <input type="number" name={f.key} defaultValue={cfg[f.key] as number} min={f.min} max={f.max} step={f.step ?? "any"} />
            <small>{f.help}</small></label>
        ))}
        <label><span><input type="checkbox" name="use_real_spread" defaultChecked={!!cfg.use_real_spread} /> Use the live bid/ask spread in fills</span>
          <small>Recommended. Buys fill at the ask side, sells at the bid side.</small></label>
        <div><button className="primary" type="submit">Save settings</button></div>
      </form>
    </>
  );
}
