// Dashboard settings: pure (no database). The same rules the worker applies in config.merge_settings. PAPER TRADING ONLY.
import { clampSetting } from "./scalp.ts";

/** Strict boolean: a JSON string "false" must be false (Boolean("false") is true - the old kill-switch bug). Unparseable => fallback. */
export function asBool(v: unknown, fallback: boolean): boolean {
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") {
    const t = v.trim().replace(/^"|"$/g, "").toLowerCase();
    if (["true", "t", "1", "yes", "on"].includes(t)) return true;
    if (["false", "f", "0", "no", "off", ""].includes(t)) return false;
  }
  return fallback;
}

export type Settings = {
  starting_balance: number;
  trading_fee_pct: number;
  slippage_pct: number;
  use_real_spread: boolean;
  max_spread_pct_to_trade: number;
  autopilot_enabled: boolean;
  local_timezone: string;
  learn_window_start_h: number; learn_window_end_h: number;
  scalp_min_edge_pct: number; scalp_position_pct: number; scalp_risk_pct: number; scalp_max_positions: number; scalp_daily_loss_limit_pct: number; scalp_allow_shorts: boolean;
  scalp_max_exposure_pct: number; scalp_max_drawdown_pct: number; scalp_min_liquidity_usd: number; scalp_max_participation_pct: number;
  scalp_hold_bars: number; scalp_confirm_required: boolean;
  // ---- sizing used by the manual account's "what should I do" advice (code-owned in the worker, shown here read-only)
  normal_position_pct: number; high_conf_position_pct: number; high_confidence_threshold: number;
  prediction_interval_h: number; hold_band_pct_24h: number; hold_band_pct_48h: number;
  retrain_min_days: number; retrain_min_new_scored: number; retrain_holdout_days: number; pattern_min_examples: number;
  news_trigger_importance: number; sudden_move_pct: number; volume_spike_x: number; signal_cooldown_min: number; signal_threshold_pct: number;
  legacy_predictions_enabled: boolean;
};

// Same defaults as worker/crypto_ai/config.py DEFAULT_SETTINGS (tests/scalp.test.mjs checks the editable limits match).
const DEFAULTS: Settings = {
  starting_balance: 1000, trading_fee_pct: 0.4, slippage_pct: 0.1, use_real_spread: true, max_spread_pct_to_trade: 0.3, autopilot_enabled: true,
  local_timezone: "", learn_window_start_h: 5, learn_window_end_h: 24,
  scalp_min_edge_pct: 0.03, scalp_position_pct: 30, scalp_risk_pct: 0.5, scalp_max_positions: 3, scalp_daily_loss_limit_pct: 3, scalp_allow_shorts: true,
  scalp_max_exposure_pct: 60, scalp_max_drawdown_pct: 8, scalp_min_liquidity_usd: 50000, scalp_max_participation_pct: 1,
  scalp_hold_bars: 15, scalp_confirm_required: true,
  normal_position_pct: 10, high_conf_position_pct: 20, high_confidence_threshold: 75, prediction_interval_h: 6, hold_band_pct_24h: 1.5, hold_band_pct_48h: 2.0,
  retrain_min_days: 30, retrain_min_new_scored: 100, retrain_holdout_days: 14, pattern_min_examples: 30, news_trigger_importance: 60,
  sudden_move_pct: 0.35, volume_spike_x: 3.0, signal_cooldown_min: 3, signal_threshold_pct: 52, legacy_predictions_enabled: false,
};

/** Keys the dashboard (or a hand-edited row) may change: the worker's config.USER_EDITABLE. Every other settings row is IGNORED here exactly as the worker ignores it. */
export const EDITABLE: (keyof Settings)[] = [
  "starting_balance", "trading_fee_pct", "slippage_pct", "use_real_spread", "autopilot_enabled", "max_spread_pct_to_trade", "scalp_allow_shorts", "scalp_position_pct",
  "scalp_risk_pct", "scalp_max_positions", "scalp_daily_loss_limit_pct", "scalp_min_edge_pct", "local_timezone", "learn_window_start_h", "learn_window_end_h",
  "scalp_max_exposure_pct", "scalp_max_drawdown_pct", "scalp_min_liquidity_usd", "scalp_max_participation_pct",
];

/** Defaults overlaid ONLY by allow-listed rows, each coerced to the default's type and clamped to its safe range; garbage is dropped; the kill switch fails to OFF. */
export function mergeSettings(rows: { key: string; value: unknown }[]): Settings {
  const s: Record<string, unknown> = { ...DEFAULTS };
  for (const r of rows) {
    const k = r.key as keyof Settings;
    if (!EDITABLE.includes(k)) continue;
    const d = DEFAULTS[k];
    if (typeof d === "boolean") s[k] = asBool(r.value, k === "autopilot_enabled" ? false : d);
    else if (typeof d === "number") {
      const n = Number(typeof r.value === "string" ? r.value.replace(/^"|"$/g, "") : r.value);
      if (Number.isFinite(n)) s[k] = clampSetting(k, n);
    } else if (typeof r.value === "string") s[k] = r.value;
  }
  if (!rows.some((r) => r.key === "autopilot_enabled")) s.autopilot_enabled = true;
  else if (!["boolean", "string", "number"].includes(typeof rows.find((r) => r.key === "autopilot_enabled")?.value)) s.autopilot_enabled = false;
  return s as unknown as Settings;
}

