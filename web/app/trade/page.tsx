import { getSettings, safe, sql } from "@/lib/db";
import { ACTION_HELP, latestPredictions, latestSignals } from "@/lib/data";
import { COINS, Coin, cashOf, fillPrice } from "@/lib/manual";
import { liveQuote } from "@/lib/quotes";
import { pct, pctPts, price, signedUsd, tone, usd, when } from "@/lib/format";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";
import OrderForms from "@/components/OrderForms";

export const dynamic = "force-dynamic";

type Advice = { predictions?: { horizon_h: number; signal: string; confidence: number }[] };

export default async function Trade() {
  const { data, error } = await safe(async () => {
    const db = sql();
    const cfg = await getSettings();
    const [quotes, preds, trades, stored, signals] = await Promise.all([
      Promise.all(COINS.map((c) => liveQuote(c))),
      latestPredictions(),
      db`select * from paper_trades where account = 'manual' order by id desc limit 200`,
      db`select distinct on (symbol) symbol, price from market_data order by symbol, ts desc`,
      latestSignals(),
    ]);
    return { cfg, quotes, preds, trades, stored, signals };
  });
  if (error || !data) return <><h1>Manual trading</h1><SetupError error={error ?? "unknown"} /></>;
  const { cfg, quotes, preds, trades, stored, signals } = data;

  const book = trades.filter((t) => t.status === "open" || t.status === "closed");
  const cash = cashOf(cfg.starting_balance, book as never);
  const px = (c: Coin, i: number) => quotes[i]?.mid ?? (stored.find((r) => r.symbol === c)?.price as number) ?? 0;
  const open = trades.filter((t) => t.status === "open");
  const positions = open.reduce((a, t) => a + (t.quantity as number) * px(t.symbol as Coin, COINS.indexOf(t.symbol as Coin)), 0);
  const total = cash + positions;
  const realised = trades.filter((t) => t.status === "closed").reduce((a, t) => a + (t.pnl_usd as number), 0);

  return (
    <>
      <h1>Manual paper trading</h1>
      <p className="sub">Buy and sell any time with fake money at live Coinbase prices (with the same {cfg.trading_fee_pct}% fee, {cfg.slippage_pct}% slippage and real spread as the AI). This is your own account: it is completely separate from the AI's test results.</p>

      <div className="grid g4">
        <Stat label="Cash" value={usd(cash)} sub={`started with ${usd(cfg.starting_balance, 0)}`} />
        <Stat label="In positions" value={usd(positions)} />
        <Stat label="Total value" value={usd(total)} />
        <Stat label="Total P/L" cls={tone(total - cfg.starting_balance)} value={signedUsd(total - cfg.starting_balance)}
          sub={<span className={tone(total - cfg.starting_balance)}>{pctPts(((total - cfg.starting_balance) / cfg.starting_balance) * 100)} return · realised {signedUsd(realised)}</span>} />
      </div>

      <div className="note bt" style={{ marginTop: 16 }}>
        <b>The AI advises; you decide.</b> The AI's call is shown on each coin. Nothing stops you trading against it, and each trade saves what the AI said at that moment.
        Its advice is <b>unproven</b>: there is no evidence yet that it works, and right now it mostly says HOLD because it has almost no edge.
      </div>

      <div className="grid g3" style={{ marginTop: 14 }}>
        {COINS.map((c, i) => {
          const q = quotes[i];
          const p24 = preds.find((p) => p.symbol === c && p.horizon_h === 24), p48 = preds.find((p) => p.symbol === c && p.horizon_h === 48);
          const lots = open.filter((t) => t.symbol === c);
          const qty = lots.reduce((a, t) => a + (t.quantity as number), 0);
          const cost = lots.reduce((a, t) => a + (t.amount_invested as number) + (t.fee_entry as number), 0);
          const mid = px(c, i);
          const ifSold = q && qty > 0 ? qty * fillPrice(q.mid, "sell", q.spreadPct, cfg.slippage_pct, cfg.use_real_spread) * (1 - cfg.trading_fee_pct / 100) - cost : null;
          const age = p24 ? (Date.now() - new Date(p24.created_at as Date).getTime()) / 3_600_000 : null;
          return (
            <div className="card" key={c}>
              <div className="name" style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <b style={{ fontSize: 18 }}>{c}</b><span className="muted">{q ? "live" : "stored price"}</span>
              </div>
              <div className="px" style={{ fontSize: 24, fontWeight: 700, margin: "4px 0 10px" }}>{price(mid)}</div>

              <div className="advice">
                <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>AI advice (unproven)</div>
                {p24 ? (
                  <>
                    <div style={{ fontSize: 20, margin: "4px 0" }}><Pill kind={p24.signal}>{p24.signal}</Pill> <b>{Math.round(p24.confidence * 100)}%</b> <span className="muted" style={{ fontSize: 12 }}>24h</span>
                      {p48 && <> &nbsp;<Pill kind={p48.signal}>{p48.signal}</Pill> <span className="muted" style={{ fontSize: 12 }}>48h</span></>}</div>
                    <div className="why">{p24.signal === "HOLD" ? `No clear edge: ${pct(p24.bullish_prob)} up / ${pct(p24.bearish_prob)} down.` : p24.signal === "BUY" ? `Model leans up (${pct(p24.bullish_prob)}).` : `Model leans down (${pct(p24.bearish_prob)}).`}
                      {age != null && age > 8 && <span className="warn"> Advice is {age.toFixed(0)} h old.</span>}</div>
                  </>
                ) : <div className="why">No prediction yet.</div>}
              </div>

              {signals[c] && (() => {
                const g = signals[c];
                const live = new Date(g.expires_at).getTime() > Date.now();
                const sudden = g.trigger_kind !== "scheduled";
                return (
                  <div className="advice" style={{ borderColor: sudden && live && g.urgency !== "low" ? "var(--warn)" : undefined, opacity: live ? 1 : 0.55 }}>
                    <div className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".04em" }}>Right now {sudden ? "(sudden event)" : ""} · {when(g.created_at)}</div>
                    <div style={{ fontSize: 20, margin: "4px 0" }}><Pill kind={g.action}>{g.action}</Pill> <span className="muted" style={{ fontSize: 12 }}>{ACTION_HELP[g.action]}</span></div>
                    <div className="why">{g.cause && sudden ? `Likely cause: ${g.cause}. ` : ""}{(g.reasons as string[]).slice(0, 2).join("; ")}{!live ? " (expired)" : ""}</div>
                    {live && qty > 0 && (g.action === "REDUCE" || g.action === "SELL") && <div className="why warn">You hold {c}: use {g.action === "SELL" ? "Sell all" : "Sell 50%"} below to follow this.</div>}
                  </div>);
              })()}

              <div className="row"><span>You hold</span><span>{qty > 0 ? `${qty.toPrecision(5)} ${c}` : "nothing"}</span></div>
              {qty > 0 && <div className="row"><span>Cost / value now</span><span>{usd(cost)} / {usd(qty * mid)}</span></div>}
              {ifSold != null && <div className="row"><span title="after selling costs">P/L if sold now</span><span className={tone(ifSold)}>{signedUsd(ifSold)}</span></div>}

              <OrderForms coin={c} cash={cash} holding={qty} live={!!q} />
            </div>
          );
        })}
      </div>

      <h2>Your manual trades</h2>
      {!trades.length ? <Empty>No manual trades yet. Use the Buy and Sell buttons above; it's all fake money.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Opened</th><th>Coin</th><th>Status</th><th className="num">Market px</th><th className="num">Fill px</th><th className="num">Invested</th><th className="num">Qty</th><th className="num">Fees</th><th className="num">Exit px</th><th className="num">P/L $</th><th className="num">P/L %</th><th>AI said at the time</th></tr></thead>
          <tbody>{trades.map((t) => {
            const adv = (t.ai_advice as Advice | null)?.predictions ?? [];
            const a24 = adv.find((p) => p.horizon_h === 24);
            const agree = a24 && (a24.signal === "BUY" ? "with the AI" : a24.signal === "HOLD" ? "AI said hold" : "against the AI");
            return (
              <tr key={t.id}>
                <td>{when(t.opened_at)}</td><td><b>{t.symbol}</b></td>
                <td>{t.status === "open" ? "open" : `closed${t.closed_at ? " " + when(t.closed_at) : ""}`}</td>
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
