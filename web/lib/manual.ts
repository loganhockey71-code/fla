// Manual PAPER trading. Fake money only: nothing here talks to an exchange, a broker or a wallet.
// Kept dependency-free (types only) so it can be tested directly against a scratch database.
import type postgres from "postgres";

export type Coin = "BTC" | "ETH" | "XRP";
export const COINS: Coin[] = ["BTC", "ETH", "XRP"];
export const MIN_ORDER_USD = 5;
export const MAX_SPREAD_PCT = 2; // refuse to fill in a broken market

export type Cfg = { trading_fee_pct: number; slippage_pct: number; use_real_spread: boolean; starting_balance: number };
export type Quote = { mid: number; spreadPct: number };
export type Order = { side: "buy" | "sell"; coin: Coin; amountUsd?: number; fraction?: number };
export type Result = { ok: boolean; message: string };

/** Same fill model as the AI account (worker/crypto_ai/paper.py): cross half the real spread, then slippage against you. */
export function fillPrice(mid: number, side: "buy" | "sell", spreadPct: number, slipPct: number, useSpread: boolean): number {
  const half = useSpread ? spreadPct / 2 / 100 : 0;
  const slip = slipPct / 100;
  return side === "buy" ? mid * (1 + half + slip) : mid * (1 - half - slip);
}

type Trade = { status: string; amount_invested: number; fee_entry: number; quantity: number; exit_price: number | null; fee_exit: number | null };

export function cashOf(start: number, trades: Trade[]): number {
  let cash = start;
  for (const t of trades) {
    if (t.status === "open" || t.status === "closed") cash -= t.amount_invested + t.fee_entry;
    if (t.status === "closed") cash += t.quantity * (t.exit_price ?? 0) - (t.fee_exit ?? 0);
  }
  return cash;
}

const usd = (n: number) => `$${n.toFixed(2)}`;

/**
 * Executes one manual paper order inside a transaction (serialised by an advisory lock so two clicks can't overspend).
 * buy : amountUsd = total to spend INCLUDING the fee. Never more than your cash (no leverage).
 * sell: fraction in (0,1] of your holding in that coin.
 */
export async function executeOrder(sql: postgres.Sql, o: Order, quote: Quote, cfg: Cfg, prices: Record<Coin, number>, advice: unknown): Promise<Result> {
  if (!COINS.includes(o.coin)) return { ok: false, message: "Only BTC, ETH and XRP can be traded." };
  if (!(quote.mid > 0) || !Number.isFinite(quote.mid)) return { ok: false, message: "No valid live price right now. Nothing was traded." };
  if (quote.spreadPct > MAX_SPREAD_PCT) return { ok: false, message: `Spread is ${quote.spreadPct.toFixed(2)}%, too wide to fill fairly. Nothing was traded.` };
  const fee = cfg.trading_fee_pct / 100;
  const px = { ...prices, [o.coin]: quote.mid } as Record<Coin, number>;

  return sql.begin(async (tx) => {
    await tx`select pg_advisory_xact_lock(778899)`;
    const rows = await tx`select * from paper_trades where account = 'manual' and status in ('open','closed') order by id`;
    const cash = cashOf(cfg.starting_balance, rows as unknown as Trade[]);
    const openValue = (skipIds: Set<number>, extra = 0) =>
      rows.filter((r) => r.status === "open" && !skipIds.has(r.id as number)).reduce((a, r) => a + (r.quantity as number) * px[r.symbol as Coin], 0) + extra;

    // ------------------------------------------------------------------ BUY
    if (o.side === "buy") {
      let amount = Number(o.amountUsd);
      if (!Number.isFinite(amount) || amount < MIN_ORDER_USD) return { ok: false, message: `Minimum order is ${usd(MIN_ORDER_USD)}.` };
      if (amount > cash + 1e-9) {
        if (amount - cash > 0.01) return { ok: false, message: `Not enough cash: you have ${usd(cash)} and no leverage is allowed.` };
        amount = cash;                                                    // "Max" rounding
      }
      const notional = amount / (1 + fee);
      const exec = fillPrice(quote.mid, "buy", quote.spreadPct, cfg.slippage_pct, cfg.use_real_spread);
      const qty = notional / exec;
      const feeEntry = notional * fee;
      const after = cash - amount + openValue(new Set(), qty * quote.mid);
      await tx`insert into paper_trades (prediction_id, symbol, horizon_h, signal, status, opened_at, market_price, exec_price, amount_invested, quantity,
                 fee_entry, slippage_entry_pct, spread_entry_pct, planned_exit_at, balance_after_open, account, ai_advice)
               values (null, ${o.coin}, 0, 'BUY', 'open', now(), ${quote.mid}, ${exec}, ${notional}, ${qty}, ${feeEntry}, ${cfg.slippage_pct},
                 ${cfg.use_real_spread ? quote.spreadPct : 0}, null, ${after}, 'manual', ${tx.json(advice as never)})`;
      return { ok: true, message: `Bought ${qty.toPrecision(6)} ${o.coin} at ${usd(exec)} (market ${usd(quote.mid)}). Spent ${usd(amount)} incl. ${usd(feeEntry)} fee. Cash left ${usd(cash - amount)}.` };
    }

    // ------------------------------------------------------------------ SELL
    const f = Number(o.fraction);
    if (!(f > 0 && f <= 1)) return { ok: false, message: "Choose how much to sell." };
    const lots = rows.filter((r) => r.status === "open" && r.symbol === o.coin);
    if (!lots.length) return { ok: false, message: `You don't hold any ${o.coin} to sell (paper trading is spot only, no shorting).` };
    const exit = fillPrice(quote.mid, "sell", quote.spreadPct, cfg.slippage_pct, cfg.use_real_spread);
    const full = f > 0.9999;
    let proceeds = 0, pnlTotal = 0, qtySold = 0;
    const touched = new Set<number>();
    const sold: { lot: (typeof lots)[number]; part: number; gross: number; feeExit: number; pnl: number; pct: number; cost: number }[] = [];
    for (const lot of lots) {
      const part = full ? 1 : f;
      const q = (lot.quantity as number) * part;
      const cost = ((lot.amount_invested as number) + (lot.fee_entry as number)) * part;
      const gross = q * exit, feeExit = gross * fee, pnl = gross - feeExit - cost;
      sold.push({ lot, part, gross, feeExit, pnl, pct: (pnl / cost) * 100, cost });
      proceeds += gross - feeExit; pnlTotal += pnl; qtySold += q; touched.add(lot.id as number);
    }
    const remaining = full ? 0 : lots.reduce((a, l) => a + (l.quantity as number) * (1 - f) * quote.mid, 0);
    const after = cash + proceeds + openValue(touched, remaining);
    for (const s of sold) {
      const l = s.lot;
      if (full) {
        await tx`update paper_trades set status='closed', closed_at=now(), exit_market_price=${quote.mid}, exit_price=${exit}, fee_exit=${s.feeExit},
                 exit_reason='manual_sell', pnl_usd=${s.pnl}, pnl_pct=${s.pct}, balance_after=${after} where id=${l.id as number}`;
      } else {                                                                       // split: closed slice + a smaller open lot
        await tx`insert into paper_trades (prediction_id, symbol, horizon_h, signal, status, opened_at, market_price, exec_price, amount_invested, quantity, fee_entry,
                   slippage_entry_pct, spread_entry_pct, closed_at, exit_market_price, exit_price, fee_exit, exit_reason, pnl_usd, pnl_pct, balance_after, account, ai_advice)
                 values (null, ${l.symbol as string}, 0, 'BUY', 'closed', ${l.opened_at as Date}, ${l.market_price as number}, ${l.exec_price as number},
                   ${(l.amount_invested as number) * f}, ${(l.quantity as number) * f}, ${(l.fee_entry as number) * f}, ${l.slippage_entry_pct as number},
                   ${l.spread_entry_pct as number}, now(), ${quote.mid}, ${exit}, ${s.feeExit}, 'manual_sell', ${s.pnl}, ${s.pct}, ${after}, 'manual',
                   ${tx.json((l.ai_advice ?? null) as never)})`;
        await tx`update paper_trades set quantity=${(l.quantity as number) * (1 - f)}, amount_invested=${(l.amount_invested as number) * (1 - f)},
                 fee_entry=${(l.fee_entry as number) * (1 - f)}, balance_after_open=${after} where id=${l.id as number}`;
      }
    }
    return { ok: true, message: `Sold ${qtySold.toPrecision(6)} ${o.coin} at ${usd(exit)} (market ${usd(quote.mid)}). Received ${usd(proceeds)} after fees. ${pnlTotal >= 0 ? "Profit" : "Loss"} ${usd(Math.abs(pnlTotal))}.` };
  });
}
