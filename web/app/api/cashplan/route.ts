import { NextRequest, NextResponse } from "next/server";
import { getSettings, sql } from "@/lib/db";
import { COINS, Coin, Quote } from "@/lib/manual";
import { liveQuote } from "@/lib/quotes";
import { blockCrossSite } from "@/lib/guard";
import { dismissPlan, executeChoice, Choice } from "@/lib/cashplan_db";

export const dynamic = "force-dynamic";

// PAPER TRADING ONLY. A cash plan does nothing until this endpoint receives an explicit choice from the user.
export async function POST(req: NextRequest) {
  const blocked = blockCrossSite(req);
  if (blocked) return blocked;
  let body: { id?: number; choice?: string };
  try { body = await req.json(); } catch { return NextResponse.json({ ok: false, message: "Bad request." }, { status: 400 }); }
  const id = Number(body.id);
  if (!Number.isInteger(id) || id <= 0) return NextResponse.json({ ok: false, message: "Bad plan id." }, { status: 400 });
  const db = sql();
  try {
    if (body.choice === "dismiss") {
      const r = await dismissPlan(db, id);
      return NextResponse.json(r, { status: r.ok ? 200 : 409 });
    }
    if (body.choice !== "recommended" && body.choice !== "safer" && body.choice !== "aggressive")
      return NextResponse.json({ ok: false, message: "Choose one of the options." }, { status: 400 });
    const cfg = await getSettings();
    const quotes = await Promise.all(COINS.map((c) => liveQuote(c)));
    const byCoin = Object.fromEntries(COINS.map((c, i) => [c, quotes[i]])) as Record<Coin, Quote | null>;
    const stored = await db`select distinct on (symbol) symbol, price from market_data order by symbol, ts desc`;
    const prices = Object.fromEntries(COINS.map((c, i) => [c, quotes[i]?.mid ?? (stored.find((r) => r.symbol === c)?.price as number) ?? 0])) as Record<Coin, number>;
    const res = await executeChoice(db, id, body.choice as Choice, byCoin, cfg, prices);
    return NextResponse.json(res, { status: res.ok ? 200 : 422 });
  } catch (e) {
    return NextResponse.json({ ok: false, message: `Something went wrong, nothing was changed (${e instanceof Error ? e.message.split("\n")[0].slice(0, 100) : "error"}).` }, { status: 500 });
  }
}
