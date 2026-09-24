import test from "node:test";
import assert from "node:assert/strict";
import { summarizeAutopilot } from "../lib/autopilotStats.ts";

const closedWin = { status: "closed", exit_reason: "autopilot_sell", pnl_usd: 5, pnl_pct: 10, ai_advice: { source: "autopilot" } };
const closedLoss = { status: "closed", exit_reason: "autopilot_sell", pnl_usd: -2, pnl_pct: -4, ai_advice: { source: "autopilot" } };
const manualSellOfAutoBuy = { status: "closed", exit_reason: "manual_sell", pnl_usd: 100, pnl_pct: 50, ai_advice: { source: "autopilot" } };
const openAuto = { status: "open", exit_reason: null, pnl_usd: null, pnl_pct: null, ai_advice: { source: "autopilot" } };
const openManual = { status: "open", exit_reason: null, pnl_usd: null, pnl_pct: null, ai_advice: null };

test("no autopilot activity yet", () => {
  const s = summarizeAutopilot([]);
  assert.deepEqual(s, { closed: 0, wins: 0, winRate: null, avgPnlPct: null, realized: 0, openPositions: 0 });
});

test("counts only autopilot-initiated sells as closed, ignores manual sells", () => {
  const s = summarizeAutopilot([closedWin, closedLoss, manualSellOfAutoBuy]);
  assert.equal(s.closed, 2);
  assert.equal(s.wins, 1);
  assert.equal(s.winRate, 0.5);
  assert.equal(s.avgPnlPct, 3);          // (10 + -4) / 2
  assert.equal(s.realized, 3);           // 5 + -2
});

test("open positions only count ones the autopilot itself opened", () => {
  const s = summarizeAutopilot([openAuto, openManual]);
  assert.equal(s.openPositions, 1);
});

test("all wins gives a 100% win rate", () => {
  assert.equal(summarizeAutopilot([closedWin]).winRate, 1);
});
