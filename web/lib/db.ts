import postgres from "postgres";

const g = globalThis as unknown as { __sql?: ReturnType<typeof postgres> };

export function sql() {
  if (!process.env.DATABASE_URL) throw new Error("DATABASE_URL is not set");
  // Use Supabase's SESSION pooler (port 5432) for the dashboard: the transaction pooler (6543) stalls this driver under
  // concurrent queries. prepare:false stays on so either string at least connects.
  const local = /@(localhost|127\.0\.0\.1)[:/]/.test(process.env.DATABASE_URL);
  return (g.__sql ??= postgres(process.env.DATABASE_URL, { ssl: local ? false : "require", max: Number(process.env.DB_POOL_MAX ?? 3), prepare: false, idle_timeout: 20, connect_timeout: 15 }));
}

/** Run a page's data loading; on failure show a setup hint instead of crashing. */
export async function safe<T>(fn: () => Promise<T>): Promise<{ data?: T; error?: string }> {
  try {
    return { data: await fn() };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export type Settings = {
  starting_balance: number;
  trading_fee_pct: number;
  slippage_pct: number;
  use_real_spread: boolean;
  normal_position_pct: number;
  high_conf_position_pct: number;
  high_confidence_threshold: number;
  signal_threshold_pct: number;
  prediction_interval_h: number;
  max_spread_pct_to_trade: number;
  hold_band_pct_24h: number;
  hold_band_pct_48h: number;
  sudden_move_pct: number;
  volume_spike_x: number;
  retrain_min_days: number;
  retrain_min_new_scored: number;
  retrain_holdout_days: number;
  pattern_min_examples: number;
  news_trigger_importance: number;
  signal_cooldown_min: number;
};

export async function getSettings(): Promise<Settings> {
  const rows = await sql()`select key, value from settings`;
  const s: Record<string, unknown> = {
    starting_balance: 1000, trading_fee_pct: 0.4, slippage_pct: 0.1, use_real_spread: true, normal_position_pct: 10,
    high_conf_position_pct: 20, high_confidence_threshold: 75, signal_threshold_pct: 52, prediction_interval_h: 6,
    max_spread_pct_to_trade: 0.3, hold_band_pct_24h: 1.5, hold_band_pct_48h: 2.0, sudden_move_pct: 1.0, volume_spike_x: 3.0,
    retrain_min_days: 30, retrain_min_new_scored: 100, retrain_holdout_days: 14, pattern_min_examples: 30, news_trigger_importance: 60, signal_cooldown_min: 30,
  };
  for (const r of rows) s[r.key as string] = r.value;
  return s as unknown as Settings;
}
