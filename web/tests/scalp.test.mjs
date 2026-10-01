// Run: node --experimental-strip-types --test tests/scalp.test.mjs   (Node 22+)
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { clampSetting, heartbeatTone, leanOf, readIsFresh, signalStats, SETTING_BOUNDS } from "../lib/scalp.ts";

test("the dashboard's setting limits are exactly the worker's (a typo can never reach the trading engine either way)", () => {
  const py = readFileSync(new URL("../../worker/crypto_ai/config.py", import.meta.url), "utf8");
  const block = py.slice(py.indexOf("SETTING_BOUNDS = {"), py.indexOf("PAPER_ONLY"));
  const worker = {};
  for (const m of block.matchAll(/"(\w+)":\s*\(([-\d.e+]+),\s*([-\d.e+]+)\)/g)) worker[m[1]] = [Number(m[2]), Number(m[3])];
  // every limit the dashboard enforces must equal the worker's; the worker may have extra limits the dashboard has no field for
  for (const [k, [lo, hi]] of Object.entries(SETTING_BOUNDS)) {
    assert.ok(worker[k], `worker has no bound for ${k}`);
    assert.deepEqual([lo, hi], worker[k], k);
  }
});

test("clampSetting keeps values in range and refuses garbage", () => {
  assert.equal(clampSetting("scalp_position_pct", 500), 50);
  assert.equal(clampSetting("scalp_position_pct", -3), 0.5);
  assert.equal(clampSetting("scalp_risk_pct", 100), 2);
  assert.equal(clampSetting("starting_balance", 250000), 250000);
  assert.equal(clampSetting("trading_fee_pct", -1), 0);
  assert.ok(Number.isNaN(clampSetting("trading_fee_pct", NaN)));
  assert.equal(clampSetting("unknown_key", 7), 7);
});

const read = (o = {}) => ({ long: 0.02, short: -0.01, threshold: 0.05, news: "none", news_net: 0, pending: false, price: 100, at: new Date().toISOString(), ...o });

test("leanOf: a lean needs the predicted NET edge to clear the threshold", () => {
  assert.equal(leanOf(read()).lean, "none");
  assert.match(leanOf(read()).text, /below the \+0\.050% needed/);
  const l = leanOf(read({ long: 0.2 }));
  assert.equal(l.lean, "long");
  assert.equal(l.edge, 0.2);
  assert.equal(leanOf(read({ long: -0.1, short: 0.3 })).lean, "short");
  assert.equal(leanOf(read({ long: -0.1, short: 0.3 }), false).lean, "none");           // shorts switched off
  assert.match(leanOf(read({ long: 0.2, pending: true })).text, /Waiting for the next candle to confirm/);
  assert.equal(leanOf(null).lean, "none");
});

test("readIsFresh", () => {
  const now = Date.parse("2026-03-02T12:00:00Z");
  assert.equal(readIsFresh("2026-03-02T11:57:00Z", now), true);
  assert.equal(readIsFresh("2026-03-02T11:40:00Z", now), false);
  assert.equal(readIsFresh(undefined, now), false);
});

const sig = (status, outcome, net, reason = null) => ({ status, reason, outcome, profitable: outcome == null ? null : net > 0, fwd_net_pct: net });

test("signalStats separates traded, refused and blocked predictions and says whether confirmation helps", () => {
  const rows = [
    ...Array.from({ length: 12 }, () => sig("confirmed", "right", 0.3)), ...Array.from({ length: 6 }, () => sig("confirmed", "wrong", -0.4)),
    ...Array.from({ length: 6 }, () => sig("failed", "right", 0.1, "no_follow_through")), ...Array.from({ length: 14 }, () => sig("failed", "wrong", -0.5, "reversal")),
    sig("blocked", "wrong", -0.2, "illiquid"), sig("pending", null, null), sig("expired", null, null, "confirmation_too_late"),
  ];
  const s = signalStats(rows);
  assert.equal(s.total, rows.length);
  assert.equal(s.pending, 1);
  assert.equal(s.confirmed.n, 18);
  assert.equal(s.failed.n, 20);
  assert.ok(s.confirmed.right > s.failed.right);
  assert.match(s.verdict, /Confirmation is helping/);
  assert.deepEqual(s.reasons, { no_follow_through: 6, reversal: 14, illiquid: 1, confirmation_too_late: 1 });
  assert.equal(signalStats([]).confirmed.n, 0);
  assert.match(signalStats([]).verdict, /Not enough graded/);
});

test("signalStats does not invent a verdict from a handful of predictions", () => {
  assert.match(signalStats([sig("confirmed", "right", 1), sig("failed", "wrong", -1)]).verdict, /Not enough graded/);
});

test("heartbeatTone: alive, stalled, never run", () => {
  const now = Date.parse("2026-03-02T12:00:00Z");
  assert.equal(heartbeatTone("2026-03-02T11:59:50Z", now).tone, "ok");
  assert.equal(heartbeatTone("2026-03-02T11:30:00Z", now).tone, "error");
  assert.match(heartbeatTone("2026-03-02T11:30:00Z", now).text, /stalled/);
  assert.equal(heartbeatTone(null, now).tone, "waiting");
});

// ---------------------------------------------------------------- settings (same rules as the worker's config.merge_settings)
import { mergeSettings, asBool } from "../lib/settings.ts";

test("settings: stale database rows cannot override strategy settings (they did before the pivot)", () => {
  const s = mergeSettings([{ key: "sudden_move_pct", value: 1.0 }, { key: "signal_cooldown_min", value: 30 }, { key: "trade_horizons", value: [24, 48] }, { key: "scalp_hold_bars", value: 999 }]);
  assert.equal(s.sudden_move_pct, 0.35);
  assert.equal(s.signal_cooldown_min, 3);
  assert.equal(s.scalp_hold_bars, 15);
  assert.equal(s.legacy_predictions_enabled, false);
  assert.equal(s.scalp_confirm_required, true);
});

test("settings: the kill switch is strict - the JSON string \"false\" is OFF, garbage is OFF, missing means ON", () => {
  assert.equal(mergeSettings([{ key: "autopilot_enabled", value: "false" }]).autopilot_enabled, false);
  assert.equal(mergeSettings([{ key: "autopilot_enabled", value: '"false"' }]).autopilot_enabled, false);
  assert.equal(mergeSettings([{ key: "autopilot_enabled", value: { x: 1 } }]).autopilot_enabled, false);
  assert.equal(mergeSettings([{ key: "autopilot_enabled", value: "banana" }]).autopilot_enabled, false);
  assert.equal(mergeSettings([{ key: "autopilot_enabled", value: true }]).autopilot_enabled, true);
  assert.equal(mergeSettings([]).autopilot_enabled, true);
  assert.equal(asBool("0", true), false);
});

test("settings: editable numbers are coerced, clamped to safe ranges, and garbage is dropped", () => {
  const s = mergeSettings([
    { key: "scalp_position_pct", value: 500 }, { key: "scalp_risk_pct", value: "0.75" }, { key: "trading_fee_pct", value: -3 }, { key: "scalp_max_positions", value: 99 },
    { key: "slippage_pct", value: "abc" }, { key: "starting_balance", value: 250000 }, { key: "scalp_max_exposure_pct", value: 1000 }, { key: "scalp_min_liquidity_usd", value: -1 },
  ]);
  assert.equal(s.scalp_position_pct, 50);
  assert.equal(s.scalp_risk_pct, 0.75);
  assert.equal(s.trading_fee_pct, 0);
  assert.equal(s.scalp_max_positions, 3);
  assert.equal(s.slippage_pct, 0.1);
  assert.equal(s.starting_balance, 250000);          // large accounts are allowed
  assert.equal(s.scalp_max_exposure_pct, 100);
  assert.equal(s.scalp_min_liquidity_usd, 0);
});
