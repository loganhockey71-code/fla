// Saving cash plans and carrying one out AFTER the user confirms. PAPER TRADING ONLY.
// Nothing here runs on its own: creating a plan only stores a recommendation, and money moves only in executeChoice.
import type postgres from "postgres";
import { buildPlan } from "./cashplan.ts";
import type { CoinView, Plan } from "./cashplan.ts";
import { cashOf, executeOrder } from "./manual.ts";
import type { Cfg, Coin, Quote } from "./manual.ts";

export type Choice = "recommended" | "safer" | "aggressive";

/** Analyse the money a sale just freed up and store the recommendation (older unanswered plans are retired). */
export async function makePlan(sql: postgres.Sql, cfg: Cfg, soldCoins: Coin[], proceeds: number, coins: CoinView[]): Promise<{ id: number; plan: Plan }> {
  const trades = await sql`select * from paper_trades where account = 'manual' and status in ('open','closed')`;
  const cash = cashOf(cfg.starting_balance, trades as never);
  const totalValue = cash + coins.reduce((a, c) => a + c.heldValue, 0);
  const plan = buildPlan({ soldCoins, amount: proceeds, cash, totalValue, coins });
  await sql`update cash_plans set status = 'dismissed', chosen = 'superseded', decided_at = now() where status = 'pending'`;
  const [row] = await sql`insert into cash_plans (sold_coins, amount, cash_after, total_value, plan)
                          values (${soldCoins}, ${plan.amount}, ${cash}, ${totalValue}, ${sql.json(plan as never)}) returning id`;
  return { id: row.id as number, plan };
}

export async function pendingPlan(sql: postgres.Sql, maxAgeHours = 24) {
  const [r] = await sql`select * from cash_plans where status = 'pending' and created_at > now() - make_interval(hours => ${maxAgeHours}) order by id desc limit 1`;
  return r ?? null;
}

/** The plan you just decided on, so the page can show what happened for a couple of minutes. */
export async function recentDecision(sql: postgres.Sql, seconds = 120) {
  const [r] = await sql`select * from cash_plans where status in ('confirmed','dismissed') and chosen <> 'superseded' and decided_at > now() - make_interval(secs => ${seconds}) order by id desc limit 1`;
  return r ?? null;
}

export async function dismissPlan(sql: postgres.Sql, id: number): Promise<{ ok: boolean; message: string }> {
  const r = await sql`update cash_plans set status = 'dismissed', chosen = 'dismissed', decided_at = now() where id = ${id} and status = 'pending' returning id`;
  return r.length ? { ok: true, message: "Okay. The money stays as cash." } : { ok: false, message: "That plan was already handled." };
}

/**
 * The user pressed Confirm on one option. Claims the plan first (so a double click can't run it twice), then buys each
 * allocation with the normal paper-trading engine (real live price, fees, slippage, never more than the cash you have).
 */
export async function executeChoice(sql: postgres.Sql, id: number, choice: Choice, quotes: Record<Coin, Quote | null>, cfg: Cfg, prices: Record<Coin, number>) {
  const claimed = await sql`update cash_plans set status = 'executing' where id = ${id} and status = 'pending' returning *`;
  if (!claimed.length) return { ok: false, message: "That plan was already handled, so nothing was bought.", bought: [] as { coin: Coin; usd: number }[] };
  const plan = claimed[0].plan as Plan;
  const option = plan[choice];
  if (!option) { await sql`update cash_plans set status = 'pending' where id = ${id}`; return { ok: false, message: "Unknown option.", bought: [] }; }

  const done: { coin: Coin; usd: number; message: string }[] = [];
  const errors: string[] = [];
  for (const a of option.buys) {
    const q = quotes[a.coin];
    if (!q) { errors.push(`${a.coin}: no live price, skipped.`); continue; }
    const r = await executeOrder(sql, { side: "buy", coin: a.coin, amountUsd: a.usd }, q, cfg, prices, { source: "cash_plan", plan_id: id, option: choice });
    if (r.ok) done.push({ coin: a.coin, usd: a.usd, message: r.message }); else errors.push(`${a.coin}: ${r.message}`);
  }
  if (option.buys.length && !done.length) {                                        // nothing happened: let the user try again
    await sql`update cash_plans set status = 'pending' where id = ${id}`;
    return { ok: false, message: `Nothing was bought. ${errors.join(" ")}`, bought: [] };
  }
  await sql`update cash_plans set status = 'confirmed', chosen = ${choice}, decided_at = now(), executed = ${sql.json({ bought: done, errors } as never)} where id = ${id}`;
  const msg = done.length ? `Bought ${done.map((d) => `$${d.usd.toFixed(2)} of ${d.coin}`).join(" and ")}. ${option.keepCash > 0.004 ? `Kept $${option.keepCash.toFixed(2)} as cash.` : ""} ${errors.join(" ")}`.trim()
                          : `Kept all $${option.keepCash.toFixed(2)} as cash.`;
  return { ok: true, message: msg, bought: done.map((d) => ({ coin: d.coin, usd: d.usd })) };
}
