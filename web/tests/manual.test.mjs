// Run against a FRESH SCRATCH Postgres (schema.sql + schema_research.sql + schema_manual.sql applied):
//   TEST_DATABASE_URL=postgresql://...  node --test tests/manual.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import postgres from "postgres";
import { executeOrder, fillPrice, cashOf } from "../lib/manual.ts";

const URL = process.env.TEST_DATABASE_URL;
if (!URL || URL.includes("supabase")) { console.log("skipped: set TEST_DATABASE_URL to a scratch Postgres (never Supabase)"); process.exit(0); }
const sql = postgres(URL, { max: 5, prepare: false });
const cfg = { trading_fee_pct: 0.4, slippage_pct: 0.1, use_real_spread: true, starting_balance: 1000 };
const prices = { BTC: 80000, ETH: 2000, XRP: 1.4 };
const q = (mid, spreadPct = 0.02) => ({ mid, spreadPct });
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const manual = () => sql`select * from paper_trades where account='manual' order by id`;
const cashNow = async () => cashOf(cfg.starting_balance, (await manual()).filter((t) => ["open", "closed"].includes(t.status)));
const buy = (coin, amountUsd, mid) => executeOrder(sql, { side: "buy", coin, amountUsd }, q(mid ?? prices[coin]), cfg, prices, { test: true });
const sell = (coin, fraction, mid) => executeOrder(sql, { side: "sell", coin, fraction }, q(mid ?? prices[coin]), cfg, prices, null);

test("fill model matches the AI account", () => {
  near(fillPrice(100, "buy", 0.2, 0.1, true), 100 * 1.002);
  near(fillPrice(100, "sell", 0.2, 0.1, true), 100 * 0.998);
  near(fillPrice(100, "buy", 0.2, 0.1, false), 100.1);
});

test("an AI-account trade never affects the manual account", async () => {
  await sql`insert into paper_trades (symbol, horizon_h, signal, status, amount_invested, quantity, fee_entry, account)
            values ('BTC', 24, 'BUY', 'open', 500, 0.006, 2, 'ai')`;
  assert.equal(await cashNow(), 1000);
});

test("BUY: live price + spread + slippage + fee, no leverage", async () => {
  const r = await buy("ETH", 100);
  assert.ok(r.ok, r.message);
  const [t] = await manual();
  near(t.market_price, 2000); near(t.exec_price, 2000 * (1 + 0.0001 + 0.001));
  near(t.amount_invested + t.fee_entry, 100); near(t.fee_entry, t.amount_invested * 0.004); near(t.quantity * t.exec_price, t.amount_invested);
  assert.equal(t.status, "open"); assert.equal(t.planned_exit_at, null); assert.equal(t.account, "manual"); assert.equal(t.ai_advice.test, true);
  near(await cashNow(), 900);
});

test("rejects bad orders without touching the books", async () => {
  const before = (await manual()).length;
  for (const [label, res] of [
    ["too small", await buy("ETH", 4)], ["over cash", await buy("ETH", 900.5)], ["NaN", await buy("ETH", NaN)],
    ["bad coin", await buy("DOGE", 50)], ["wide spread", await executeOrder(sql, { side: "buy", coin: "ETH", amountUsd: 50 }, q(2000, 3), cfg, prices, null)],
    ["no price", await executeOrder(sql, { side: "buy", coin: "ETH", amountUsd: 50 }, q(0), cfg, prices, null)],
    ["sell nothing", await sell("XRP", 1)], ["sell 0%", await sell("ETH", 0)], ["sell 150%", await sell("ETH", 1.5)],
  ]) assert.equal(res.ok, false, label);
  assert.equal((await manual()).length, before); near(await cashNow(), 900);
});

test("partial SELL splits the lot; books always balance", async () => {
  const r = await sell("ETH", 0.5, 2100);                        // price rose 5%
  assert.ok(r.ok, r.message);
  const rows = await manual();
  const closed = rows.filter((t) => t.status === "closed"), open = rows.filter((t) => t.status === "open");
  assert.equal(closed.length, 1); assert.equal(open.length, 1);
  const exit = 2100 * (1 - 0.0001 - 0.001);
  near(closed[0].exit_price, exit); near(closed[0].fee_exit, closed[0].quantity * exit * 0.004);
  const cost = closed[0].amount_invested + closed[0].fee_entry;
  near(closed[0].pnl_usd, closed[0].quantity * exit * (1 - 0.004) - cost); assert.ok(closed[0].pnl_usd > 0);
  near(closed[0].pnl_pct, (closed[0].pnl_usd / cost) * 100);
  near(closed[0].amount_invested + open[0].amount_invested, 100 / 1.004);                    // nothing created or lost by the split
  near(closed[0].quantity, open[0].quantity);
  const openCost = open.reduce((a, t) => a + t.amount_invested + t.fee_entry, 0);
  near(await cashNow(), 1000 + closed.reduce((a, t) => a + t.pnl_usd, 0) - openCost);        // cash = start + realised - cost still held
});

test("SELL all closes the rest; profit matches an independent calculation", async () => {
  const before = await manual();
  const heldQty = before.filter((t) => t.status === "open").reduce((a, t) => a + t.quantity, 0), heldCost = before.filter((t) => t.status === "open").reduce((a, t) => a + t.amount_invested + t.fee_entry, 0);
  const r = await sell("ETH", 1, 1900);                          // price fell
  assert.ok(r.ok, r.message);
  const rows = await manual();
  assert.equal(rows.filter((t) => t.status === "open").length, 0);
  const last = rows.filter((t) => t.exit_price && Math.abs(t.exit_market_price - 1900) < 1e-9);
  const exit = 1900 * (1 - 0.0001 - 0.001);
  near(last[0].pnl_usd, heldQty * exit * 0.996 - heldCost); assert.ok(last[0].pnl_usd < 0);
  const realised = rows.filter((t) => t.status === "closed").reduce((a, t) => a + t.pnl_usd, 0);
  near(await cashNow(), 1000 + realised); near(last[0].balance_after, await cashNow());
  assert.equal((await sell("ETH", 1)).ok, false);                // nothing left
});

test("closed trades are frozen by the database", async () => {
  const [c] = await sql`select id from paper_trades where account='manual' and status='closed' limit 1`;
  await assert.rejects(sql`update paper_trades set pnl_usd = 999 where id = ${c.id}`, /immutable/);
});

test("double-clicks racing each other can never overspend", async () => {
  const big = postgres(URL, { max: 20, prepare: false });          // many real connections so transactions truly overlap
  try {
    for (let round = 0; round < 5; round++) {
      const before = await cashNow();
      if (before < 40) break;
      const amount = before / 2.5;                                  // only 2 of these can ever fit
      const results = await Promise.all(Array.from({ length: 20 }, () => executeOrder(big, { side: "buy", coin: "BTC", amountUsd: amount }, q(prices.BTC), cfg, prices, null)));
      const wins = results.filter((r) => r.ok).length;
      assert.equal(wins, 2, `round ${round}: expected exactly 2 of 20 to fit in ${before.toFixed(2)} cash, got ${wins}`);
      assert.ok((await cashNow()) >= -1e-9, "cash went negative");
      // free the cash again so the next round is a fresh race: sell everything at an unchanged price
      await executeOrder(sql, { side: "sell", coin: "BTC", fraction: 1 }, q(prices.BTC), cfg, prices, null);
    }
  } finally { await big.end(); }
});

test("Max spends everything but never a cent more", async () => {
  const cash = await cashNow();
  const r = await buy("XRP", cash);
  assert.ok(r.ok, r.message);
  near(await cashNow(), 0, 1e-6);
});

test.after(async () => { await sql.end(); });
