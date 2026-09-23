// "What should I do?" for one coin, given the AI's signal AND what you actually hold. Pure, no I/O. PAPER TRADING ONLY.
// A bare "HOLD" is meaningless when you own none of the coin, so the signal is translated into a move that fits your position.
import type { Action } from "./cashplan";

export const MIN_BUY_USD = 5;
export type YourMove = { label: "BUY" | "ADD" | "HOLD" | "WAIT" | "REDUCE" | "SELL" | "STAY OUT"; kind: Action; text: string };
export type SizingCfg = { normal_position_pct: number; high_conf_position_pct: number; high_confidence_threshold: number };

/** Same sizing the AI's own account and autopilot use: 10% of the portfolio, 20% when high-confidence. Never more than cash. */
export function suggestBuyUsd(confidence: number | null, totalValue: number, cash: number, cfg: SizingCfg): number {
  const pct = confidence != null && confidence * 100 >= cfg.high_confidence_threshold ? cfg.high_conf_position_pct : cfg.normal_position_pct;
  const raw = Math.min(totalValue * pct / 100, cash);
  if (raw < MIN_BUY_USD) return 0;
  return Math.max(MIN_BUY_USD, Math.round(raw / 5) * 5);            // round to a "nice" $5 step
}

export function yourMove(coin: string, action: Action, heldValue: number, cash: number, sizing?: { confidence: number | null; totalValue: number; cfg: SizingCfg }): YourMove {
  const holds = heldValue >= 0.01;
  const canBuy = cash >= MIN_BUY_USD;
  switch (action) {
    case "BUY": {
      if (!holds) {
        if (!canBuy) return { label: "WAIT", kind: "HOLD", text: `The AI leans up on ${coin} but you have no cash to buy with.` };
        const amt = sizing ? suggestBuyUsd(sizing.confidence, sizing.totalValue, cash, sizing.cfg) : 0;
        return { label: "BUY", kind: "BUY", text: amt > 0 ? `Invest $${amt} in ${coin} and hold: the AI leans up and you hold none.` : `Buy some ${coin}: the AI leans up and you hold none.` };
      }
      return { label: canBuy ? "ADD" : "HOLD", kind: "BUY", text: canBuy ? `Keep your ${coin}; the AI still leans up, so adding more is fine.` : `Keep your ${coin}; the AI still leans up (no cash to add).` };
    }
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
