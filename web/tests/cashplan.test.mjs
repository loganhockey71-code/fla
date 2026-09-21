// node --test tests/cashplan.test.mjs   (pure logic, no database)
import test from "node:test";
import assert from "node:assert/strict";
import { buildPlan, scoreCoin, summarizeOption } from "../lib/cashplan.ts";

const coin = (c, action, bull, extra = {}) => ({ coin: c, action, confidence: Math.max(bull, 1 - bull), bull, sudden: false, urgency: null, change24h: 0, news: [], heldValue: 0, ...extra });
const input = (coins, extra = {}) => ({ soldCoins: ["BTC"], amount: 500, cash: 500, totalValue: 1000, coins, ...extra });
const total = (o) => o.keepCash + o.buys.reduce((a, b) => a + b.usd, 0);

test("the example: sold BTC, ETH is the strongest BUY, BTC neutral, XRP bearish", () => {
  const p = buildPlan(input([coin("BTC", "HOLD", 0.5), coin("ETH", "BUY", 0.81), coin("XRP", "SELL", 0.29)]));
  assert.deepEqual(p.recommended.buys.map((b) => b.coin), ["ETH"]);
  assert.ok(p.recommended.buys[0].usd > 0 && p.recommended.keepCash > 0, "invests part and keeps the rest");
  assert.match(p.reason, /ETH has the strongest BUY signal at 81% confidence/);
  assert.match(p.reason, /BTC is neutral/); assert.match(p.reason, /XRP is bearish/);
  assert.equal(p.safer.keepCash, 500); assert.equal(p.safer.buys.length, 0);              // safer option: keep everything
  assert.ok(p.aggressive.buys.reduce((a, b) => a + b.usd, 0) > p.recommended.buys[0].usd);   // more aggressive really is bigger
  assert.match(summarizeOption(p.recommended), /Keep \$\d+(\.\d+)? cash · Invest \$\d+(\.\d+)? into ETH/);
});

test("weak or neutral signals: keep the cash, say why, and flag that there is no edge", () => {
  const p = buildPlan(input([coin("BTC", "HOLD", 0.5), coin("ETH", "HOLD", 0.51), coin("XRP", "HOLD", 0.5)]));
  assert.equal(p.recommended.buys.length, 0); assert.equal(p.recommended.keepCash, 500); assert.equal(p.recommended.title, "Keep as cash");
  assert.match(p.reason, /No coin has a strong enough BUY/); assert.ok(p.caution && /no real edge/.test(p.caution));
  assert.match(p.aggressive.note, /not a recommendation/);                                   // a bet against the signals is labelled as one
});

test("a bearish coin is never bought in any option", () => {
  for (const acts of [["SELL", "HOLD", "HOLD"], ["REDUCE", "BUY", "SELL"], ["SELL", "SELL", "SELL"]]) {
    const p = buildPlan(input(["BTC", "ETH", "XRP"].map((c, i) => coin(c, acts[i], acts[i] === "BUY" ? 0.7 : acts[i] === "HOLD" ? 0.5 : 0.3))));
    for (const o of [p.recommended, p.safer, p.aggressive]) for (const b of o.buys) assert.notEqual(acts[["BTC", "ETH", "XRP"].indexOf(b.coin)], "SELL");
    for (const b of p.recommended.buys) assert.notEqual(acts[["BTC", "ETH", "XRP"].indexOf(b.coin)], "REDUCE");
  }
});

test("every option adds up to exactly the money that became available, and never more", () => {
  for (const amount of [12, 87.5, 500, 2500]) for (const bulls of [[0.5, 0.5, 0.5], [0.8, 0.75, 0.3], [0.9, 0.5, 0.5], [0.3, 0.3, 0.3]]) {
    const acts = bulls.map((b) => (b > 0.55 ? "BUY" : b < 0.45 ? "SELL" : "HOLD"));
    const p = buildPlan(input(["BTC", "ETH", "XRP"].map((c, i) => coin(c, acts[i], bulls[i])), { amount, cash: amount + 100, totalValue: 5000 }));
    for (const o of [p.recommended, p.safer, p.aggressive]) {
      assert.ok(Math.abs(total(o) - amount) < 0.011, `${o.key}: ${total(o)} vs ${amount}`);
      assert.ok(o.buys.every((b) => b.usd >= 5), "no dust orders");
    }
  }
});

test("two strong coins are split; the amount invested is bounded", () => {
  const p = buildPlan(input([coin("BTC", "BUY", 0.78), coin("ETH", "BUY", 0.8), coin("XRP", "HOLD", 0.5)], { totalValue: 5000 }));
  assert.equal(p.recommended.buys.length, 2); assert.equal(p.recommended.title, "Split between coins");
  assert.ok(p.recommended.buys.reduce((a, b) => a + b.usd, 0) <= 500 * 0.6 + 0.01);
});

test("concentration cap: one coin never grows past 40% of the whole portfolio", () => {
  const nearlyFull = buildPlan(input([coin("BTC", "HOLD", 0.5), coin("ETH", "BUY", 0.85, { heldValue: 399 }), coin("XRP", "HOLD", 0.5)]));
  assert.equal(nearlyFull.recommended.buys.length, 0);                                       // only $1 of room: below the minimum order
  const some = buildPlan(input([coin("BTC", "HOLD", 0.5), coin("ETH", "BUY", 0.85, { heldValue: 300 }), coin("XRP", "HOLD", 0.5)]));
  assert.ok(some.recommended.buys[0].usd <= 100 + 1e-9);                                     // 40% of 1000 minus 300 already held
});

test("a stressed market halves the size of the recommendation", () => {
  const calm = buildPlan(input([coin("BTC", "HOLD", 0.5), coin("ETH", "BUY", 0.85), coin("XRP", "HOLD", 0.5)]));
  const stressed = buildPlan(input([coin("BTC", "SELL", 0.3, { change24h: -0.06 }), coin("ETH", "BUY", 0.85), coin("XRP", "REDUCE", 0.4, { change24h: -0.06 })]));
  const spend = (p) => p.recommended.buys.reduce((a, b) => a + b.usd, 0);
  assert.ok(spend(stressed) < spend(calm) && spend(stressed) > 0);
  assert.match(stressed.reason, /stressed/);
});

test("official positive news can lift a neutral coin, official negative news can sink one", () => {
  const up = buildPlan(input([coin("BTC", "HOLD", 0.5, { news: [{ title: "SEC approves ETF", impact: 60, tier: 1 }] }), coin("ETH", "HOLD", 0.5), coin("XRP", "HOLD", 0.5)]));
  assert.deepEqual(up.recommended.buys.map((b) => b.coin), ["BTC"]);
  const down = scoreCoin(coin("XRP", "BUY", 0.6, { news: [{ title: "SEC sues Ripple", impact: -75, tier: 1 }] }), false).score;
  const clean = scoreCoin(coin("XRP", "BUY", 0.6), false).score;
  assert.ok(down < clean - 0.4);
  const media = scoreCoin(coin("XRP", "HOLD", 0.5, { news: [{ title: "rumour", impact: 60, tier: 4 }] }), false).score;
  assert.ok(media < 0.5 * 0.6 + 1e-9 && media > 0);                                          // media counts half as much as an official source
});

test("nothing to invest means empty options; requested amount is capped by available cash", () => {
  const none = buildPlan(input([coin("BTC", "BUY", 0.9), coin("ETH", "BUY", 0.9), coin("XRP", "BUY", 0.9)], { amount: 0 }));
  assert.equal(none.recommended.buys.length, 0); assert.equal(none.safer.keepCash, 0);
  const capped = buildPlan(input([coin("ETH", "BUY", 0.9), coin("BTC", "HOLD", 0.5), coin("XRP", "HOLD", 0.5)], { amount: 900, cash: 120 }));
  assert.equal(capped.amount, 120); assert.ok(Math.abs(total(capped.recommended) - 120) < 0.011);
});

test("the aggressive option is a real 60/40 split between the top two coins, never a token amount", () => {
  const p = buildPlan(input([coin("BTC", "HOLD", 0.5), coin("ETH", "BUY", 0.81), coin("XRP", "SELL", 0.29)], { totalValue: 5000, cash: 2000, amount: 500 }));
  assert.equal(p.aggressive.buys.length, 2);
  const [a, b] = p.aggressive.buys;
  assert.deepEqual([a.coin, b.coin], ["ETH", "BTC"]);
  assert.ok(Math.abs(a.usd / (a.usd + b.usd) - 0.6) < 0.01, `${a.usd} / ${b.usd}`);
  assert.ok(b.usd > 50);
});
