import { revalidatePath } from "next/cache";
import { getSettings, safe, sql } from "@/lib/db";
import { yourMove } from "@/lib/advice";
import { ACTION_HELP, latestPredictions, latestSignals } from "@/lib/data";
import { COINS, Coin, cashOf, summarize } from "@/lib/manual";
import { liveQuote } from "@/lib/quotes";
import { ago, pct, pctPts, price, signedUsd, tone, usd, when } from "@/lib/format";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";
import OrderForms, { QuickSell, SellEverything } from "@/components/OrderForms";
import CashPlanPanel from "@/components/CashPlanPanel";
import { pendingPlan, recentDecision } from "@/lib/cashplan_db";


async function setAutopilot(form: FormData) {
  "use server";
  await sql()`insert into settings (key, value, updated_at) values ('autopilot_enabled', ${form.get("on") === "1" ? "true" : "false"}::jsonb, now())
              on conflict (key) do update set value = excluded.value, updated_at = now()`;
  revalidatePath("/trades");
}

type Advice = { predictions?: { horizon_h: number; signal: string; confidence: number }[] };

export default async function MyTradingView() {
  const { data, error } = await safe(async () => {
    const db = sql();
    const cfg = await getSettings();
    const [quotes, preds, trades, stored, signals, plan, decided, autoRows, autoStatus] = await Promise.all([
      Promise.all(COINS.map((c) => liveQuote(c))),
      latestPredictions(),
      db`select * from paper_trades where account = 'manual' order by id desc limit 500`,
      db`select distinct on (symbol) symbol, price from market_data order by symbol, ts desc`,
      latestSignals(),
      pendingPlan(db),
      recentDecision(db),
      db`select id, symbol, status, opened_at, closed_at, exec_price, exit_price, quantity, amount_invested, pnl_usd, exit_reason, ai_advice from paper_trades
         where account = 'manual' and (ai_advice->>'source' = 'autopilot' or exit_reason = 'autopilot_sell') order by id desc limit 60`,
      db`select value from settings where key = 'autopilot_status'`,
    ]);
    return { cfg, quotes, preds, trades, stored, signals, plan, decided, autoRows, autoStatus };
  });
  if (error || !data) return <><h2 className="viewtitle">Manual trading</h2><SetupError error={error ?? "unknown"} /></>;
  const { cfg, quotes, preds, trades, stored, signals, plan, decided, autoRows, autoStatus } = data;

  const q = Object.fromEntries(COINS.map((c, i) => [c, quotes[i]])) as Record<Coin, (typeof quotes)[number]>;
  const mids = Object.fromEntries(COINS.map((c, i) => [c, quotes[i]?.mid ?? (stored.find((r) => r.symbol === c)?.price as number) ?? 0])) as Record<Coin, number>;
  const book = trades.filter((t) => t.status === "open" || t.status === "closed");
  const cash = cashOf(cfg.starting_balance, book as never);
  const pnl = summarize(trades as never, mids, cfg, q);
  const held = pnl.per.filter((c) => c.qty > 0);
  const positions = held.reduce((a, c) => a + c.value, 0);
  const total = cash + positions;
  const totalPnl = total - cfg.starting_balance;
  const allLive = COINS.every((c) => !!q[c]);
  const ifSoldAll = held.reduce((a, c) => a + (c.ifSoldNet ?? 0), 0);

  // ---- autopilot: what the AI did for you (buys are tagged in ai_advice, sells by exit_reason)
  const auto = autoStatus[0]?.value as { at?: string; enabled?: boolean; made?: number; note?: string } | undefined;
  const autoOn = !!cfg.autopilot_enabled;
  type AutoEvent = { at: Date; side: "BUY" | "SELL"; coin: string; px: number; usd: number; pnl: number | null; why: string };
  const autoEvents: AutoEvent[] = autoRows.flatMap((t): AutoEvent[] => {
    const adv = t.ai_advice as { source?: string; why?: string } | null;
    const out: AutoEvent[] = [];
    if (adv?.source === "autopilot") out.push({ at: t.opened_at as Date, side: "BUY", coin: t.symbol as string, px: t.exec_price as number, usd: t.amount_invested as number, pnl: null, why: adv.why ?? "" });
    if (t.exit_reason === "autopilot_sell") out.push({ at: t.closed_at as Date, side: "SELL", coin: t.symbol as string, px: t.exit_price as number, usd: (t.quantity as number) * (t.exit_price as number), pnl: t.pnl_usd as number, why: "" });
    return out;
  }).sort((a, b) => +new Date(b.at) - +new Date(a.at)).slice(0, 12);
  const autoRealized = autoRows.filter((t) => t.exit_reason === "autopilot_sell").reduce((a, t) => a + ((t.pnl_usd as number) ?? 0), 0);
  const staleH = auto?.at ? (Date.now() - new Date(auto.at).getTime()) / 3_600_000 : null;

  return (
    <>
      <h2 className="viewtitle">Manual paper trading</h2>
      <p className="sub">Buy and sell any time with fake money at live Coinbase prices (same {cfg.trading_fee_pct}% fee, {cfg.slippage_pct}% slippage and real spread as the AI). This is your own account, completely separate from the AI's test results. Everything happens on this page.</p>

      <div className="card" style={{ marginTop: 14 }} id="autopilot">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <div>
            <b style={{ fontSize: 16 }}>AI autopilot</b> <Pill kind={autoOn ? "BUY" : "HOLD"}>{autoOn ? "ON" : "OFF"}</Pill>
            <div className="muted" style={{ fontSize: 13, marginTop: 4, maxWidth: 640 }}>
              {autoOn ? "While you are away the AI trades this account for you (fake money): it buys when it leans up, sells when it leans down or a sudden event hits, and does nothing when it sees no edge."
                      : "Off: nothing is traded unless you press the buttons yourself."} It puts at most 10% (20% when very confident) into one buy, never more than 40% of the account in one coin, and never more than your cash. It follows the same unproven signals shown below, so treat the result as a test.
            </div>
          </div>
          <form action={setAutopilot}><input type="hidden" name="on" value={autoOn ? "0" : "1"} /><button className={autoOn ? "" : "primary"} type="submit">{autoOn ? "Turn autopilot off" : "Turn autopilot on"}</button></form>
        </div>
        {autoOn && (
          <div className="why" style={{ marginTop: 8 }}>
            {auto?.at ? <>Last check <b>{ago(auto.at)}</b>: {auto.note}</> : "It has not run yet. It runs each time the worker ticks (every 30 minutes on GitHub Actions, or about once a minute with the watch loop)."}
            {staleH != null && staleH > 3 && <span className="warn"> The worker has not checked in for {staleH.toFixed(0)} h, so nothing is being traded. Check the worker.</span>}
          </div>)}
        {autoEvents.length > 0 && (
          <div className="scroll" style={{ marginTop: 10 }}><table>
            <thead><tr><th>When</th><th>Autopilot did</th><th className="num">Price</th><th className="num">Amount</th><th className="num">Result</th><th>Why</th></tr></thead>
            <tbody>{autoEvents.map((e, i) => (
              <tr key={i}><td>{when(e.at)}</td><td><Pill kind={e.side}>{e.side}</Pill> <b>{e.coin}</b></td><td className="num">{price(e.px)}</td><td className="num">{usd(e.usd)}</td>
                <td className={`num ${tone(e.pnl)}`}>{e.pnl != null ? signedUsd(e.pnl) : "—"}</td><td className="muted">{e.why || "signal turned bearish"}</td></tr>))}</tbody></table>
            <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>Profit locked in by autopilot sales so far: <b className={tone(autoRealized)}>{signedUsd(autoRealized)}</b></div>
          </div>)}
        {autoOn && !autoEvents.length && auto?.at && <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>No autopilot trades yet. It only trades when the AI has a clear signal.</div>}
      </div>

      {!plan && decided && (() => {
        const ex = decided.executed as { bought?: { coin: string; usd: number }[] } | null;
        const bought = ex?.bought ?? [];
        const kept = decided.chosen === "dismissed" || !bought.length;
        return (
          <div className="cashplan done" role="status">
            <div className="cp-kicker">Cash plan</div>
            <div className="cp-big">{kept ? `Okay: the ${usd(decided.amount as number)} stays as cash.` : `Done: bought ${bought.map((b) => `${usd(b.usd)} of ${b.coin}`).join(" and ")}.`}</div>
            <div className="why">{decided.chosen === "dismissed" ? "You chose to keep it as cash." : `You confirmed the ${decided.chosen} option. Nothing else was bought.`}</div>
          </div>);
      })()}
      {plan && <CashPlanPanel id={plan.id as number} soldCoins={plan.sold_coins as string[]} plan={plan.plan as never} cash={cash} />}

      <div className="grid g4">
        <Stat label="Cash" value={usd(cash)} sub={`started with ${usd(cfg.starting_balance, 0)}`} />
        <Stat label="In positions (market value)" value={usd(positions)} />
        <Stat label="Total value" value={usd(total)} />
        <Stat label="Total profit / loss" cls={tone(totalPnl)} value={signedUsd(totalPnl)}
          sub={<span className={tone(totalPnl)}>{pctPts((totalPnl / cfg.starting_balance) * 100)} since you started</span>} />
      </div>
      <div className="grid g4" style={{ marginTop: 12 }}>
        <Stat label="Profit locked in (sold)" cls={tone(pnl.realized)} value={signedUsd(pnl.realized)} sub="realised, after all fees. It stays even if you rebuy." />
        <Stat label="Profit on what you hold" cls={tone(pnl.unrealized)} value={signedUsd(pnl.unrealized)} sub="unrealised, at today's market price" />
      </div>

      <h2 style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span>Your holdings</span>
        <SellEverything positions={held.length} live={allLive || held.every((c) => !!q[c.coin])} />
      </h2>
      {!held.length ? (
        <Empty><b>You don't hold anything yet.</b> Choose a coin below and press the green <b>Buy</b> button. Once you own something it appears here with <b>Sell</b> buttons, and on each coin card you can sell 25%, 50%, 75%, all, or a dollar amount.</Empty>
      ) : (
        <div className="scroll"><table>
          <thead><tr><th>Coin</th><th className="num">You hold</th><th className="num">Avg cost each</th><th className="num">You paid</th><th className="num">Worth now</th><th className="num" title="after the selling fee, spread and slippage">P/L if sold now</th><th>Sell</th></tr></thead>
          <tbody>{held.map((c) => (
            <tr key={c.coin}>
              <td><b>{c.coin}</b></td><td className="num">{c.qty.toPrecision(6)}</td><td className="num">{price(c.avgCost)}</td><td className="num">{usd(c.openCost)}</td><td className="num">{usd(c.value)}</td>
              <td className={`num ${tone(c.ifSoldNet)}`}>{c.ifSoldNet != null ? `${signedUsd(c.ifSoldNet)} (${pctPts((c.ifSoldNet / c.openCost) * 100)})` : "—"}</td>
              <td><QuickSell coin={c.coin} live={!!q[c.coin]} /></td>
            </tr>))}
            <tr><td colSpan={3}><b>Total</b></td><td className="num"><b>{usd(held.reduce((a, c) => a + c.openCost, 0))}</b></td><td className="num"><b>{usd(positions)}</b></td>
              <td className={`num ${tone(ifSoldAll)}`}><b>{signedUsd(ifSoldAll)}</b></td><td /></tr>
          </tbody></table></div>
      )}

      <div className="note bt" style={{ marginTop: 16 }}>
        <b>The AI advises; you decide.</b> Its call is shown on each coin below and each trade saves what it said at that moment. Its advice is <b>unproven</b>: there is no evidence yet that it works.
      </div>

      <div className="grid g3" style={{ marginTop: 14 }}>
        {COINS.map((c, i) => {
          const p24 = preds.find((p) => p.symbol === c && p.horizon_h === 24), p48 = preds.find((p) => p.symbol === c && p.horizon_h === 48);
          const h = pnl.per.find((x) => x.coin === c)!;
          const age = p24 ? (Date.now() - new Date(p24.created_at as Date).getTime()) / 3_600_000 : null;
          const g = signals[c];
          return (
            <div className="card" key={c}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}><b style={{ fontSize: 18 }}>{c}</b><span className="muted">{quotes[i] ? "live price" : "stored price"}</span></div>
              <div style={{ fontSize: 24, fontWeight: 700, margin: "4px 0 10px" }}>{price(mids[c])}</div>

              {(() => {
                const live = !!g && new Date(g.expires_at).getTime() > Date.now();
                const action = (live ? g.action : p24?.signal ?? "HOLD") as "BUY" | "HOLD" | "REDUCE" | "SELL";
                const confidence = live ? (g.model_bull_prob != null ? Math.max(g.model_bull_prob, 1 - g.model_bull_prob) : null) : p24?.confidence ?? null;
                const m = yourMove(c, action, h.value, cash, { confidence, totalValue: total, cfg });
                return (
                  <div className="advice" style={{ borderColor: "var(--accent)" }}>
                    <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>Your move (based on what you hold)</div>
                    <div style={{ fontSize: 20, margin: "4px 0" }}><Pill kind={m.kind}>{m.label}</Pill></div>
                    <div className="why">{m.text}</div>
                  </div>);
              })()}

              <div className="advice">
                <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>AI signal (unproven)</div>
                {p24 ? (
                  <>
                    <div style={{ fontSize: 20, margin: "4px 0" }}><Pill kind={p24.signal}>{p24.signal}</Pill> <b>{Math.round(p24.confidence * 100)}%</b> <span className="muted" style={{ fontSize: 12 }}>24h</span>
                      {p48 && <> &nbsp;<Pill kind={p48.signal}>{p48.signal}</Pill> <span className="muted" style={{ fontSize: 12 }}>48h</span></>}</div>
                    <div className="why">{p24.signal === "HOLD" ? `No clear edge: ${pct(p24.bullish_prob)} up / ${pct(p24.bearish_prob)} down.` : p24.signal === "BUY" ? `Model leans up (${pct(p24.bullish_prob)}).` : `Model leans down (${pct(p24.bearish_prob)}).`}
                      {age != null && age > 8 && <span className="warn"> Advice is {age.toFixed(0)} h old.</span>}</div>
                  </>
                ) : <div className="why">No prediction yet.</div>}
              </div>

              {g && (() => {
                const live = new Date(g.expires_at).getTime() > Date.now();
                const sudden = g.trigger_kind !== "scheduled";
                return (
                  <div className="advice" style={{ borderColor: sudden && live && g.urgency !== "low" ? "var(--warn)" : undefined, opacity: live ? 1 : 0.55 }}>
                    <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>Right now {sudden ? "(sudden event)" : ""} · {when(g.created_at)}</div>
                    <div style={{ fontSize: 20, margin: "4px 0" }}><Pill kind={g.action}>{g.action}</Pill> <span className="muted" style={{ fontSize: 12 }}>{ACTION_HELP[g.action]}</span></div>
                    <div className="why">{g.cause && sudden ? `Likely cause: ${g.cause}. ` : ""}{(g.reasons as string[]).slice(0, 2).join("; ")}{!live ? " (expired)" : ""}</div>
                    {live && h.qty > 0 && (g.action === "REDUCE" || g.action === "SELL") && <div className="why warn">You hold {c}: use {g.action === "SELL" ? "Sell all" : "Sell 50%"} below to follow this.</div>}
                  </div>);
              })()}

              <div className="row"><span>You hold</span><span>{h.qty > 0 ? `${h.qty.toPrecision(5)} ${c}` : "nothing"}</span></div>
              {h.qty > 0 && <div className="row"><span>Avg cost each</span><span>{price(h.avgCost)}</span></div>}
              {h.qty > 0 && <div className="row"><span title="after selling costs">P/L if sold now</span><span className={tone(h.ifSoldNet)}>{h.ifSoldNet != null ? signedUsd(h.ifSoldNet) : "—"}</span></div>}
              <div className="row"><span>Profit already locked in</span><span className={tone(h.realized)}>{signedUsd(h.realized)}</span></div>

              <OrderForms coin={c} cash={cash} holding={h.qty} heldValue={h.value} live={!!quotes[i]} />
            </div>
          );
        })}
      </div>

      <h2>Profit &amp; loss by coin</h2>
      <p className="muted" style={{ marginTop: -4 }}>When you sell, that profit or loss is <b>locked in</b> and stays in the totals. When you buy the same coin again, your <b>average cost starts fresh</b> from the new purchase, so the two results never get mixed up.</p>
      <div className="scroll"><table>
        <thead><tr><th>Coin</th><th className="num">Total bought</th><th className="num">Total sold (received)</th><th className="num">Profit locked in</th><th className="num">Still holding (cost)</th><th className="num">Unrealised now</th></tr></thead>
        <tbody>{pnl.per.map((c) => (
          <tr key={c.coin}><td><b>{c.coin}</b></td><td className="num">{usd(c.bought)}</td><td className="num">{usd(c.soldProceeds)}</td><td className={`num ${tone(c.realized)}`}>{signedUsd(c.realized)}</td>
            <td className="num">{c.qty > 0 ? usd(c.openCost) : "—"}</td><td className={`num ${tone(c.unrealized)}`}>{c.qty > 0 ? signedUsd(c.unrealized) : "—"}</td></tr>))}
          <tr><td><b>All coins</b></td><td className="num"><b>{usd(pnl.per.reduce((a, c) => a + c.bought, 0))}</b></td><td className="num"><b>{usd(pnl.per.reduce((a, c) => a + c.soldProceeds, 0))}</b></td>
            <td className={`num ${tone(pnl.realized)}`}><b>{signedUsd(pnl.realized)}</b></td><td className="num"><b>{usd(pnl.per.reduce((a, c) => a + c.openCost, 0))}</b></td><td className={`num ${tone(pnl.unrealized)}`}><b>{signedUsd(pnl.unrealized)}</b></td></tr>
        </tbody></table></div>

      <h2>Completed sales</h2>
      {!pnl.sales.length ? <Empty>No sales yet. Each time you sell, the profit or loss for that sale is listed here with a running total.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Sold</th><th>Coin</th><th className="num">Amount</th><th className="num">Bought at</th><th className="num">Sold at</th><th className="num">Profit / loss</th><th className="num">%</th><th className="num">Running total</th></tr></thead>
          <tbody>{[...pnl.sales].reverse().map((t) => (
            <tr key={t.id}><td>{when(t.closed_at)}</td><td><b>{t.symbol}</b></td><td className="num">{Number((t as never as { quantity: number }).quantity).toPrecision(5)}</td>
              <td className="num">{price((t as never as { exec_price: number }).exec_price)}</td><td className="num">{price(t.exit_price)}</td>
              <td className={`num ${tone(t.pnl_usd)}`}>{signedUsd(t.pnl_usd)}</td><td className={`num ${tone((t as never as { pnl_pct: number }).pnl_pct)}`}>{pctPts((t as never as { pnl_pct: number }).pnl_pct)}</td>
              <td className={`num ${tone(t.running)}`}><b>{signedUsd(t.running)}</b></td></tr>))}</tbody></table></div>
      )}

      <h2>Every manual trade</h2>
      {!trades.length ? <Empty>No manual trades yet. Use the Buy buttons above; it's all fake money.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Opened</th><th>Coin</th><th>Status</th><th className="num">Market px</th><th className="num">Fill px</th><th className="num">Invested</th><th className="num">Qty</th><th className="num">Fees</th><th className="num">Exit px</th><th className="num">P/L $</th><th className="num">P/L %</th><th>AI said at the time</th></tr></thead>
          <tbody>{trades.map((t) => {
            const adv = (t.ai_advice as Advice | null)?.predictions ?? [];
            const a24 = adv.find((p) => p.horizon_h === 24);
            const agree = a24 && (a24.signal === "BUY" ? "with the AI" : a24.signal === "HOLD" ? "AI said hold" : "against the AI");
            return (
              <tr key={t.id}>
                <td>{when(t.opened_at)}</td><td><b>{t.symbol}</b></td>
                <td>{t.status === "open" ? "open" : `sold ${t.closed_at ? when(t.closed_at) : ""}`}</td>
                <td className="num">{price(t.market_price)}</td><td className="num">{price(t.exec_price)}</td><td className="num">{usd(t.amount_invested)}</td>
                <td className="num">{Number(t.quantity).toPrecision(5)}</td><td className="num">{usd((t.fee_entry as number) + ((t.fee_exit as number) ?? 0))}</td>
                <td className="num">{price(t.exit_price)}</td>
                <td className={`num ${tone(t.pnl_usd)}`}>{t.pnl_usd != null ? signedUsd(t.pnl_usd) : "—"}</td><td className={`num ${tone(t.pnl_pct)}`}>{t.pnl_pct != null ? pctPts(t.pnl_pct) : "—"}</td>
                <td>{a24 ? <><Pill kind={a24.signal}>{a24.signal}</Pill> <span className="muted">{Math.round(a24.confidence * 100)}% · bought {agree}</span></> : <span className="muted">—</span>}</td>
              </tr>);
          })}</tbody></table></div>
      )}
    </>
  );
}
