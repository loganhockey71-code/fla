// "What should I do?" for one coin, given the AI's signal AND what you actually hold. Pure, no I/O. PAPER TRADING ONLY.
// A bare "HOLD" is meaningless when you own none of the coin, so the signal is translated into a move that fits your position.
import type { Action } from "./cashplan";

export const MIN_BUY_USD = 5;
export type YourMove = { label: "BUY" | "ADD" | "HOLD" | "WAIT" | "REDUCE" | "SELL" | "STAY OUT"; kind: Action; text: string };

export function yourMove(coin: string, action: Action, heldValue: number, cash: number): YourMove {
  const holds = heldValue >= 0.01;
  const canBuy = cash >= MIN_BUY_USD;
  switch (action) {
    case "BUY":
      if (!holds) return canBuy
        ? { label: "BUY", kind: "BUY", text: `Buy some ${coin}: the AI leans up and you hold none.` }
        : { label: "WAIT", kind: "HOLD", text: `The AI leans up on ${coin} but you have no cash to buy with.` };
      return { label: canBuy ? "ADD" : "HOLD", kind: "BUY", text: canBuy ? `Keep your ${coin}; the AI still leans up, so adding more is fine.` : `Keep your ${coin}; the AI still leans up (no cash to add).` };
    case "HOLD":
      return holds
        ? { label: "HOLD", kind: "HOLD", text: `Keep your ${coin}. No clear edge either way.` }
        : { label: "WAIT", kind: "HOLD", text: `Don't invest in ${coin} now: no clear edge. Stay in cash.` };
    case "REDUCE":
      return holds
        ? { label: "REDUCE", kind: "REDUCE", text: `Sell about half of your ${coin}.` }
        : { label: "STAY OUT", kind: "SELL", text: `Don't buy ${coin} now: the AI expects weakness. You hold none, so there is nothing to sell.` };
    case "SELL":
      return holds
        ? { label: "SELL", kind: "SELL", text: `Sell all of your ${coin}.` }
        : { label: "STAY OUT", kind: "SELL", text: `Don't buy ${coin} now: the AI expects a fall. You hold none, so there is nothing to sell.` };
  }
}
