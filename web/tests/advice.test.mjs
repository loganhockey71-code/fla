// Run: node --experimental-strip-types --test tests/advice.test.mjs   (Node 22+)
import test from "node:test";
import assert from "node:assert/strict";
import { yourMove } from "../lib/advice.ts";

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
