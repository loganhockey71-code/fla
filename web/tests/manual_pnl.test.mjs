// Run against a FRESH SCRATCH Postgres (all schema files applied), separate from manual.test.mjs:
//   TEST_DATABASE_URL=postgresql://...  node --test tests/manual_pnl.test.mjs
// Covers: sell by dollar amount, "sell everything", and profit tracking across sell -> rebuy -> sell.
import test from "node:test";
import assert from "node:assert/strict";
import postgres from "postgres";
import { executeOrder, sellEverything, summarize, cashOf, COINS } from "../lib/manual.ts";

const URL = process.env.TEST_DATABASE_URL;
if (!URL || URL.includes("supabase")) { console.log("skipped: set TEST_DATABASE_URL to a scratch Postgres (never Supabase)"); process.exit(0); }
const sql = postgres(URL, { max: 5, prepare: false });
const cfg = { trading_fee_pct: 0.4, slippage_pct: 0.1, use_real_spread: true, starting_balance: 1000 };
const SPREAD = 0.02;
const q = (mid) => ({ mid, spreadPct: SPREAD });
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const rows = () => sql`select * from paper_trades where account='manual' order by id`;
const cash = async () => cashOf(cfg.starting_balance, (await rows()).filter((t) => ["open", "closed"].includes(t.status)));
const px = { BTC: 80000, ETH: 2000, XRP: 1.4 };
const buy = (coin, usd, mid) => executeOrder(sql, { side: "buy", coin, amountUsd: usd }, q(mid), cfg, px, null);
const sell = (coin, frac, mid) => executeOrder(sql, { side: "sell", coin, fraction: frac }, q(mid), cfg, px, null);
// what one full round trip should return, computed independently of the engine
const fill = (mid, side) => mid * (1 + (side === "buy" ? 1 : -1) * (SPREAD / 2 / 100 + 0.001));
const roundTrip = (spend, buyMid, sellMid) => {
  const notional = spend / 1.004, qty = notional / fill(buyMid, "buy"), gross = qty * fill(sellMid, "sell");
  return { qty, pnl: gross - gross * 0.004 - spend };
};

test("SELL by dollar amount sells exactly that much and refuses more than you hold", async () => {
  assert.ok((await buy("ETH", 200, 2000)).ok);
  const held = (await rows()).find((t) => t.status === "open").quantity;
  const r = await executeOrder(sql, { side: "sell", coin: "ETH", amountUsd: 50 }, q(2000), cfg, px, null);
  assert.ok(r.ok, r.message);
  const all = await rows();
  const closed = all.filter((t) => t.status === "closed"), open = all.filter((t) => t.status === "open");
  near(closed[0].quantity * 2000, 50, 1e-6);                                   // $50 worth at the market price
  near(closed[0].quantity + open[0].quantity, held);                            // nothing created or lost
  const tooMuch = await executeOrder(sql, { side: "sell", coin: "ETH", amountUsd: 5000 }, q(2000), cfg, px, null);
  assert.equal(tooMuch.ok, false); assert.match(tooMuch.message, /only hold about/);
  const rest = await executeOrder(sql, { side: "sell", coin: "ETH", amountUsd: open[0].quantity * 2000 }, q(2000), cfg, px, null);   // exactly the remaining value = sell all
  assert.ok(rest.ok, rest.message);
  assert.equal((await rows()).filter((t) => t.status === "open").length, 0);
});

test("Sell everything closes every position; a missing price leaves only that coin unsold", async () => {
  for (const c of COINS) assert.ok((await buy(c, 100, px[c])).ok);
  const partial = await sellEverything(sql, { BTC: q(80000), ETH: null, XRP: q(1.4) }, cfg, px);
  assert.equal(partial.ok, false); assert.deepEqual(partial.sold.sort(), ["BTC", "XRP"]);
  assert.deepEqual((await rows()).filter((t) => t.status === "open").map((t) => t.symbol), ["ETH"]);
  const rest = await sellEverything(sql, { BTC: q(80000), ETH: q(2000), XRP: q(1.4) }, cfg, px);
  assert.ok(rest.ok, rest.message); assert.deepEqual(rest.sold, ["ETH"]);
  const none = await sellEverything(sql, { BTC: q(80000), ETH: q(2000), XRP: q(1.4) }, cfg, px);
  assert.equal(none.ok, false); assert.match(none.message, /don't hold anything/);
  const closed = (await rows()).filter((t) => t.status === "closed");
  near(await cash(), 1000 + closed.reduce((a, t) => a + t.pnl_usd, 0));          // every dollar is accounted for
});

test("sell -> rebuy -> sell: realised profit is kept, average cost restarts, running total is right", async () => {
  const before = (await rows()).filter((t) => t.status === "closed").reduce((a, t) => a + t.pnl_usd, 0);   // profit from the earlier tests stays untouched
  const btcBefore = (await rows()).filter((t) => t.symbol === "BTC" && t.status === "closed").reduce((a, t) => a + t.pnl_usd, 0);   // BTC result from the earlier test
  const cash0 = await cash();
  const spend = Math.min(200, cash0 - 1);
  assert.ok((await buy("BTC", spend, 80000)).ok);
  assert.ok((await sell("BTC", 1, 88000)).ok);                                   // +10%
  const trip1 = roundTrip(spend, 80000, 88000);
  assert.ok(trip1.pnl > 0);
  let s = summarize(await rows(), px, cfg, { BTC: q(88000), ETH: null, XRP: null });
  const btc1 = s.per.find((c) => c.coin === "BTC");
  near(btc1.realized, btcBefore + trip1.pnl); assert.equal(btc1.qty, 0); assert.equal(btc1.avgCost, null);

  assert.ok((await buy("BTC", spend, 90000)).ok);                                // REBUY higher
  s = summarize(await rows(), { ...px, BTC: 91000 }, cfg, { BTC: q(91000), ETH: null, XRP: null });
  const btc2 = s.per.find((c) => c.coin === "BTC");
  near(btc2.realized, btcBefore + trip1.pnl);                                    // earlier profit is still there after the rebuy
  near(btc2.openCost, spend);                                                    // only the new purchase is open
  near(btc2.avgCost, spend / (spend / 1.004 / fill(90000, "buy")));              // average cost = the NEW price (incl. fee), not the old one
  near(btc2.unrealized, btc2.qty * 91000 - spend);
  assert.ok(btc2.ifSoldNet != null && btc2.ifSoldNet < btc2.unrealized);        // selling costs come off the "if sold now" number

  assert.ok((await sell("BTC", 1, 85000)).ok);                                   // sell the rebuy at a loss
  const trip2 = roundTrip(spend, 90000, 85000);
  assert.ok(trip2.pnl < 0);
  s = summarize(await rows(), px, cfg, { BTC: null, ETH: null, XRP: null });
  const btc3 = s.per.find((c) => c.coin === "BTC");
  near(btc3.realized, btcBefore + trip1.pnl + trip2.pnl);                        // profit + loss add up
  near(s.realized, before + trip1.pnl + trip2.pnl);
  const mine = s.sales.filter((x) => x.symbol === "BTC");
  near(mine[mine.length - 1].running - mine[mine.length - 2].running, trip2.pnl);   // running total moves by exactly the last sale's P/L
  near(s.sales[s.sales.length - 1].running, s.realized);
  near((await cash()) - cash0, trip1.pnl + trip2.pnl);                            // and the cash balance agrees
});

test("profit in one coin never leaks into another", async () => {
  const s = summarize(await rows(), px, cfg, { BTC: null, ETH: null, XRP: null });
  for (const c of s.per) near(c.realized, (await rows()).filter((t) => t.symbol === c.coin && t.status === "closed").reduce((a, t) => a + t.pnl_usd, 0));
});

test.after(async () => { await sql.end(); });
