// Run: node --experimental-strip-types --test tests/advice.test.mjs   (Node 22+)
import test from "node:test";
import assert from "node:assert/strict";
import { suggestBuyUsd, yourMove } from "../lib/advice.ts";

const CFG = { normal_position_pct: 10, high_conf_position_pct: 20, high_confidence_threshold: 75 };

test("suggestBuyUsd sizes normally vs high-confidence, capped by cash, rounded to $5", () => {
  assert.equal(suggestBuyUsd(0.6, 1000, 1000, CFG), 100);   // 10% of $1000
  assert.equal(suggestBuyUsd(0.8, 1000, 1000, CFG), 200);   // high confidence -> 20%
  assert.equal(suggestBuyUsd(0.6, 1000, 40, CFG), 40);      // capped by cash, rounds to $40
  assert.equal(suggestBuyUsd(0.6, 1000, 2, CFG), 0);        // below minimum -> no suggestion
});

test("BUY with none held and cash suggests a dollar amount, not a bare HOLD", () => {
  const m = yourMove("BTC", "BUY", 0, 1000, { confidence: 0.6, totalValue: 1000, cfg: CFG });
  assert.equal(m.label, "BUY");
  assert.match(m.text, /Invest \$100 in BTC and hold/);
});

test("HOLD with none of the coin is WAIT, never HOLD", () => {
  const m = yourMove("BTC", "HOLD", 0, 500);
  assert.equal(m.label, "WAIT");
  assert.match(m.text, /Stay in cash/);
});
test("HOLD while holding is HOLD", () => assert.equal(yourMove("BTC", "HOLD", 80, 500).label, "HOLD"));
test("BUY with none and cash is BUY", () => assert.equal(yourMove("BTC", "BUY", 0, 500).label, "BUY"));
test("BUY with no cash cannot say BUY", () => assert.equal(yourMove("BTC", "BUY", 0, 2).label, "WAIT"));
test("BUY while holding is ADD", () => assert.equal(yourMove("BTC", "BUY", 50, 500).label, "ADD"));
test("SELL/REDUCE with none held is STAY OUT, not a sell", () => {
  assert.equal(yourMove("ETH", "SELL", 0, 500).label, "STAY OUT");
  assert.equal(yourMove("ETH", "REDUCE", 0, 500).label, "STAY OUT");
});
test("SELL/REDUCE while holding stay sells", () => {
  assert.equal(yourMove("ETH", "SELL", 30, 0).label, "SELL");
  assert.equal(yourMove("ETH", "REDUCE", 30, 0).label, "REDUCE");
});
