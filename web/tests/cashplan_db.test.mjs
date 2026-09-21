// FRESH SCRATCH Postgres with all schema files (incl. schema_cashplan.sql):
//   TEST_DATABASE_URL=postgresql://...  node --test tests/cashplan_db.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import postgres from "postgres";
import { executeOrder, cashOf } from "../lib/manual.ts";
import { makePlan, pendingPlan, dismissPlan, executeChoice, recentDecision } from "../lib/cashplan_db.ts";

const URL = process.env.TEST_DATABASE_URL;
if (!URL || URL.includes("supabase")) { console.log("skipped: set TEST_DATABASE_URL to a scratch Postgres (never Supabase)"); process.exit(0); }
const sql = postgres(URL, { max: 10, prepare: false });
const cfg = { trading_fee_pct: 0.4, slippage_pct: 0.1, use_real_spread: true, starting_balance: 1000 };
const px = { BTC: 80000, ETH: 2000, XRP: 1.4 };
const q = (m) => ({ mid: m, spreadPct: 0.02 });
const quotes = { BTC: q(80000), ETH: q(2000), XRP: q(1.4) };
const view = (coin, action, bull, extra = {}) => ({ coin, action, confidence: Math.max(bull, 1 - bull), bull, sudden: false, urgency: null, change24h: 0, news: [], heldValue: 0, ...extra });
const market = () => [view("BTC", "HOLD", 0.5), view("ETH", "BUY", 0.81), view("XRP", "SELL", 0.29)];
const trades = () => sql`select * from paper_trades where account='manual' order by id`;
const cash = async () => cashOf(cfg.starting_balance, (await trades()).filter((t) => ["open", "closed"].includes(t.status)));
const plans = () => sql`select * from cash_plans order by id`;

async function sellBtc(spend = 500) {
  assert.ok((await executeOrder(sql, { side: "buy", coin: "BTC", amountUsd: spend }, quotes.BTC, cfg, px, null)).ok);
  const r = await executeOrder(sql, { side: "sell", coin: "BTC", fraction: 1 }, quotes.BTC, cfg, px, null);
  assert.ok(r.ok && r.proceeds > 0, r.message);
  return r.proceeds;
}

test("a sale produces a saved recommendation and invests NOTHING by itself", async () => {
  const proceeds = await sellBtc();
  assert.ok(proceeds > 490 && proceeds < 500);                                     // about $500 minus costs
  const before = (await trades()).length, cash0 = await cash();
  const { id, plan } = await makePlan(sql, cfg, ["BTC"], proceeds, market());
  assert.deepEqual(plan.recommended.buys.map((b) => b.coin), ["ETH"]);
  assert.equal((await trades()).length, before); assert.equal(await cash(), cash0);   // recommendation only: no trade, no cash moved
  const row = await pendingPlan(sql);
  assert.equal(row.id, id); assert.equal(row.status, "pending"); near(row.amount, proceeds);
  assert.deepEqual(row.sold_coins, ["BTC"]);
});

function near(a, b, eps = 1e-6) { assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`); }

test("Confirm buys exactly the recommended amounts at live prices, then the plan is closed", async () => {
  const row = await pendingPlan(sql);
  const opt = row.plan.recommended;
  const cash0 = await cash();
  const r = await executeChoice(sql, row.id, "recommended", quotes, cfg, px);
  assert.ok(r.ok, r.message);
  const eth = (await trades()).filter((t) => t.symbol === "ETH" && t.status === "open");
  assert.equal(eth.length, 1);
  near(eth[0].amount_invested + eth[0].fee_entry, opt.buys[0].usd);                 // the confirmed dollar amount, fee included
  assert.ok(eth[0].exec_price > eth[0].market_price);                               // realistic fill: spread + slippage
  assert.equal(eth[0].ai_advice.source, "cash_plan"); assert.equal(eth[0].ai_advice.plan_id, row.id);
  near(cash0 - (await cash()), opt.buys[0].usd);
  const done = (await plans()).find((p) => p.id === row.id);
  assert.equal(done.status, "confirmed"); assert.equal(done.chosen, "recommended"); assert.equal(done.executed.bought.length, 1);
  assert.equal(await pendingPlan(sql), null);
});

test("a plan can never run twice, even with a double click or a stale second tab", async () => {
  const p = await sellBtc(300);
  const { id } = await makePlan(sql, cfg, ["BTC"], p, market());
  const before = (await trades()).length;
  const results = await Promise.all(Array.from({ length: 8 }, () => executeChoice(sql, id, "recommended", quotes, cfg, px)));
  assert.equal(results.filter((r) => r.ok).length, 1, "exactly one confirm wins");
  assert.equal((await trades()).length, before + 1);                                // one purchase, not eight
  assert.equal((await executeChoice(sql, id, "aggressive", quotes, cfg, px)).ok, false);
});

test("choosing the safer option keeps all the cash and buys nothing", async () => {
  const p = await sellBtc(200);
  const { id } = await makePlan(sql, cfg, ["BTC"], p, market());
  const before = (await trades()).length, cash0 = await cash();
  const r = await executeChoice(sql, id, "safer", quotes, cfg, px);
  assert.ok(r.ok); assert.match(r.message, /Kept all/);
  assert.equal((await trades()).length, before); assert.equal(await cash(), cash0);
  assert.equal((await plans()).find((x) => x.id === id).chosen, "safer");
});

test("dismissing leaves the money as cash, and a dismissed plan can't be confirmed", async () => {
  const p = await sellBtc(100);
  const { id } = await makePlan(sql, cfg, ["BTC"], p, market());
  assert.ok((await dismissPlan(sql, id)).ok);
  assert.equal((await recentDecision(sql)).id, id);                              // the page can show what you decided
  const before = (await trades()).length;
  assert.equal((await executeChoice(sql, id, "recommended", quotes, cfg, px)).ok, false);
  assert.equal((await trades()).length, before);
  assert.equal((await dismissPlan(sql, id)).ok, false);
});

test("a newer plan replaces an unanswered older one", async () => {
  const a = await makePlan(sql, cfg, ["BTC"], 100, market());
  const b = await makePlan(sql, cfg, ["ETH"], 100, market());
  assert.equal((await pendingPlan(sql)).id, b.id);
  assert.equal((await plans()).find((p) => p.id === a.id).status, "dismissed");
  assert.notEqual((await recentDecision(sql))?.id, a.id);                          // a replaced plan is not shown as "your decision"
});

test("if the cash was spent elsewhere, confirming fails cleanly and the plan stays usable", async () => {
  const p = await sellBtc(400);
  const { id } = await makePlan(sql, cfg, ["BTC"], p, market());
  const c = await cash();
  assert.ok((await executeOrder(sql, { side: "buy", coin: "XRP", amountUsd: c }, quotes.XRP, cfg, px, null)).ok);   // spend everything else first
  const before = (await trades()).length;
  const r = await executeChoice(sql, id, "recommended", quotes, cfg, px);
  assert.equal(r.ok, false); assert.match(r.message, /Nothing was bought/);
  assert.equal((await trades()).length, before);
  assert.equal((await plans()).find((x) => x.id === id).status, "pending");          // not burned: the user can pick "safer" or wait
  assert.ok((await executeChoice(sql, id, "safer", quotes, cfg, px)).ok);
});

test("a coin with no live price is skipped without buying anything else by accident", async () => {
  await executeOrder(sql, { side: "sell", coin: "XRP", fraction: 1 }, quotes.XRP, cfg, px, null);     // free up cash again
  const p = await sellBtc(150);
  const { id } = await makePlan(sql, cfg, ["BTC"], p, market());
  const before = (await trades()).length;
  const r = await executeChoice(sql, id, "recommended", { ...quotes, ETH: null }, cfg, px);
  assert.equal(r.ok, false); assert.equal((await trades()).length, before);
  assert.equal((await plans()).find((x) => x.id === id).status, "pending");
});

test.after(async () => { await sql.end(); });
