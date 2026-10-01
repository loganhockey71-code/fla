import { revalidatePath } from "next/cache";
import { safe, sql, getSettings } from "@/lib/db";
import { SETTING_BOUNDS, clampSetting } from "@/lib/scalp";
import { SetupError } from "@/components/Ui";

type Field = { key: string; label: string; help: string; step?: string };
// Only settings the worker actually reads from the database (config.USER_EDITABLE). Anything else is owned by the code so a stale row
// can never silently override the strategy. The limits come from lib/scalp.ts SETTING_BOUNDS, which a test keeps identical to the worker's.
const FIELDS: Field[] = [
  { key: "starting_balance", label: "Paper account size ($)", help: "How much fake money the scalper starts with. Position sizes scale with it, so set this to the amount you would really trade. Profit and loss is measured from this number.", step: "100" },
  { key: "trading_fee_pct", label: "Trading fee per side (%)", help: "Default 0.4. Charged on entry and again on exit; the scalper only trades when its target clears this.", step: "0.01" },
  { key: "slippage_pct", label: "Slippage per side (%)", help: "Default 0.10. Fills are made worse by this amount (large orders pay extra on top, see the liquidity limits).", step: "0.01" },
  { key: "scalp_min_edge_pct", label: "Minimum predicted net edge to enter (%)", help: "After costs. The model's validated threshold can only raise this, never lower it.", step: "0.01" },
  { key: "scalp_position_pct", label: "Max position (% of account)", help: "Default 30. The biggest single trade. No leverage, ever.", step: "1" },
  { key: "scalp_risk_pct", label: "Max loss per trade (% of account)", help: "Default 0.5. Position size is cut so a full stop-out (fees, spread, slippage and your own market impact included) never loses more.", step: "0.05" },
  { key: "scalp_max_positions", label: "Max open positions", help: "Default 3 (one per coin).", step: "1" },
  { key: "scalp_max_exposure_pct", label: "Max total exposure (% of account)", help: "Default 60. All open positions together never exceed this, so the whole account is never in the market at once.", step: "5" },
  { key: "scalp_daily_loss_limit_pct", label: "Daily loss limit (%)", help: "No new entries for the rest of the UTC day once the account is down this much (open losses count).", step: "0.5" },
  { key: "scalp_max_drawdown_pct", label: "Drawdown halt (%)", help: "Default 8. No new entries while the account is this far below its 7-day high. A cooling-off: it lifts when the account recovers or the high ages out.", step: "1" },
  { key: "scalp_min_liquidity_usd", label: "Minimum liquidity ($ traded in 15 min)", help: "Default 50,000. A coin that traded less than this in the last 15 minutes is too thin to trade right now.", step: "10000" },
  { key: "scalp_max_participation_pct", label: "Max share of 15-minute volume (%)", help: "Default 1. A position is never more than this share of what the market traded in the last 15 minutes: the market has to be able to absorb it.", step: "0.1" },
];

async function save(form: FormData) {
  "use server";
  const db = sql();
  const vals: Record<string, unknown> = {};
  for (const f of FIELDS) {
    const n = Number(form.get(f.key));
    if (form.get(f.key) !== "" && Number.isFinite(n)) vals[f.key] = clampSetting(f.key, n);
  }
  vals.use_real_spread = form.get("use_real_spread") === "on";
  vals.scalp_allow_shorts = form.get("scalp_allow_shorts") === "on";
  for (const [key, value] of Object.entries(vals)) {
    await db`insert into settings (key, value, updated_at) values (${key}, ${db.json(value as never)}, now())
             on conflict (key) do update set value = excluded.value, updated_at = now()`;
  }
  revalidatePath("/settings");
}

export default async function SettingsView() {
  const { data, error } = await safe(getSettings);
  if (error || !data) return <><h2 className="viewtitle">Settings</h2><SetupError error={error ?? "unknown"} /></>;
  const cfg = data as unknown as Record<string, number | boolean>;
  return (
    <>
      <h2 className="viewtitle">Settings</h2>
      <p className="sub">Changes apply to trades made from now on. History is never rewritten. The scalper on/off switch is on the Trades page. Every number is clamped to a safe range, here and in the worker.</p>
      <div className="note">Paper trading only: nothing here can place a real order, and there is no real-trading switch. A trade also only happens after the <b>next candle confirms</b> the prediction; that rule is built into the code and cannot be turned off from a settings row.</div>
      <form action={save} className="settings">
        {FIELDS.map((f) => {
          const [min, max] = SETTING_BOUNDS[f.key];
          return (
            <label key={f.key}><span>{f.label}</span>
              <input type="number" name={f.key} defaultValue={cfg[f.key] as number} min={min} max={max} step={f.step ?? "any"} />
              <small>{f.help}</small></label>
          );
        })}
        <label><span><input type="checkbox" name="use_real_spread" defaultChecked={!!cfg.use_real_spread} /> Use the live bid/ask spread in fills</span>
          <small>Recommended. Buys fill at the ask side, sells at the bid side.</small></label>
        <label><span><input type="checkbox" name="scalp_allow_shorts" defaultChecked={cfg.scalp_allow_shorts !== false} /> Allow simulated shorts</span>
          <small>Paper-only: sell first, buy back later, same fees, no funding cost (holds are minutes). Off = long-only.</small></label>
        <div><button className="primary" type="submit">Save settings</button></div>
      </form>
    </>
  );
}
