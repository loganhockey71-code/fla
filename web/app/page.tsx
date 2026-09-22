import Link from "next/link";
import { getSettings, safe, sql } from "@/lib/db";
import { Coin } from "@/lib/manual";
import { coinSnapshots, heldValues, latestNews, manualPortfolio } from "@/lib/views";
import { yourMove } from "@/lib/advice";
import { pendingPlan } from "@/lib/cashplan_db";
import { ago, price, pctPts, signedUsd, tone, usd } from "@/lib/format";
import { Empty, Pill, SetupError } from "@/components/Ui";
import { Card, CoinIcon, Spark } from "@/components/Visuals";

export const dynamic = "force-dynamic";
const NAME: Record<string, string> = { BTC: "Bitcoin", ETH: "Ethereum", XRP: "XRP" };
const BAR: Record<string, string> = { BUY: "#2ee59d", HOLD: "#5b8def", REDUCE: "#f5b942", SELL: "#ff5c7a" };
const IMPACT = { positive: "Positive", negative: "Negative", neutral: "Neutral" } as const;

export default async function Dashboard() {
  const { data, error } = await safe(async () => {
    const db = sql();
    const cfg = await getSettings();
    const snaps = await coinSnapshots(db);
    const mids = Object.fromEntries(snaps.map((s) => [s.coin, s.price ?? 0])) as Record<Coin, number>;
    const [news, port, plan, held] = await Promise.all([latestNews(db, 5), manualPortfolio(db, cfg.starting_balance, mids), pendingPlan(db), heldValues(db, mids)]);
    return { cfg, snaps, news, port, plan, held };
  });
  if (error || !data) return <><h1>Dashboard</h1><SetupError error={error ?? "unknown"} /></>;
  const { cfg, snaps, news, port, plan, held } = data;

  return (
    <>
      {plan && (
        <div className="alert warn" style={{ marginTop: 18 }}>
          <b>You have a cash plan waiting.</b> You sold ${Number(plan.amount).toFixed(2)} of {(plan.sold_coins as string[]).join(" and ")}. <Link href="/trades">See what the AI recommends doing with it →</Link>
        </div>
      )}

      <section className="coinrow" aria-label="Prices">
        {snaps.map((s) => (
          <div className="card coincard" key={s.coin}>
            <CoinIcon coin={s.coin} size={52} />
            <div className="cc-main">
              <div className="cc-name"><b>{NAME[s.coin]}</b><span className="muted">{s.coin}</span></div>
              <div className="cc-price">{price(s.price)}
                <span className={`chg ${s.change24h != null && s.change24h < 0 ? "neg" : "pos"}`}>{s.change24h != null ? pctPts(s.change24h * 100) : "—"}</span><span className="muted small"> 24h</span></div>
            </div>
            <Spark points={s.spark} up={(s.change24h ?? 0) >= 0} />
          </div>
        ))}
      </section>

      <div className="dash-grid">
        <Card title="AI Trading Signals" action={<Link href="/signals" className="more">View analysis →</Link>}>
          <p className="muted small" style={{ margin: "-4px 0 10px" }}>Based on market data, news and technical analysis. Confidence near 50% means the AI sees no clear edge. Its advice is unproven.</p>
          <div className="scroll flat"><table className="signals">
            <thead><tr><th>Coin</th><th title="the AI's signal, translated for what you hold right now">Your move</th><th>Confidence</th><th>Reason</th></tr></thead>
            <tbody>{snaps.map((s) => {
              const move = yourMove(s.coin, s.action, held[s.coin] ?? 0, port.cash);
              return (
              <tr key={s.coin}>
                <td><span className="rowcoin"><CoinIcon coin={s.coin} size={34} /><span><b>{s.coin}</b><br /><span className="muted small">{NAME[s.coin]}</span></span></span></td>
                <td><Pill kind={move.kind}>{move.label}</Pill><div className="muted small" style={{ marginTop: 3 }}>{move.text}{s.sudden ? " (sudden event)" : ""}</div></td>
                <td style={{ minWidth: 150 }}>
                  {s.confidence != null ? (
                    <><b>{Math.round(s.confidence * 100)}%</b><div className="bar"><i style={{ width: `${Math.round(s.confidence * 100)}%`, background: BAR[s.action] }} /></div></>
                  ) : <span className="muted">—</span>}
                </td>
                <td className="reason">{s.reason}</td>
              </tr>);
            })}</tbody></table></div>
        </Card>

        <Card title="Latest News & Market Impact" action={<Link href="/news" className="more">View all news →</Link>}>
          {!news.length ? <Empty>No important news yet.</Empty> : (
            <div className="scroll flat"><table className="newslist">
              <thead><tr><th>Time</th><th>Headline</th><th>Coins</th><th>Impact</th></tr></thead>
              <tbody>{news.map((n) => (
                <tr key={n.id as number}>
                  <td className="muted nowrap">{ago(n.at as Date)}</td>
                  <td className="headline">{n.source_url ? <a href={n.source_url as string} target="_blank" rel="noreferrer">{n.title as string}</a> : (n.title as string)}</td>
                  <td className="nowrap">{(n.affected_coins as string[]).length >= 3 ? "All coins" : (n.affected_coins as string[]).join(" ")}</td>
                  <td><Pill kind={n.sentiment as string}>{IMPACT[n.sentiment as keyof typeof IMPACT]}</Pill></td>
                </tr>))}</tbody></table></div>
          )}
        </Card>
      </div>

      <div className="dash-grid">
        <Card title="Paper Trading Portfolio" action={<Link href="/trades" className="more">Trade →</Link>}>
          <div className="portfolio">
            <div><div className="muted small">Total value</div><div className="big">{usd(port.total)}</div></div>
            <div><div className="muted small">Cash</div><div className="big2">{usd(port.cash)}</div></div>
            <div><div className="muted small">Profit / loss</div><div className={`big2 ${tone(port.pnl)}`}>{signedUsd(port.pnl)}</div>
              <div className={`small ${tone(port.pnl)}`}>{pctPts((port.pnl / cfg.starting_balance) * 100)} since you started</div></div>
          </div>
          <p className="muted small" style={{ marginBottom: 0 }}>Fake money only. Started with {usd(cfg.starting_balance, 0)}. Real Coinbase prices, with fees and slippage.</p>
        </Card>

        <Card title="Recent Trades" action={<Link href="/trades" className="more">View all trades →</Link>}>
          {!port.events.length ? <Empty>No trades yet. Open <Link href="/trades">Trades</Link> to buy or sell with fake money.</Empty> : (
            <div className="scroll flat"><table>
              <thead><tr><th>Time</th><th>Coin</th><th>Side</th><th className="num">Amount</th><th className="num">Price</th><th className="num">P/L</th></tr></thead>
              <tbody>{port.events.slice(0, 5).map((e, i) => (
                <tr key={i}><td className="muted nowrap">{ago(e.at)}</td><td><b>{e.coin}</b></td><td className={e.side === "BUY" ? "pos" : "neg"}><b>{e.side}</b></td>
                  <td className="num">{e.qty.toPrecision(4)}</td><td className="num">{price(e.price)}</td>
                  <td className={`num ${tone(e.pnl)}`}>{e.pnl != null ? signedUsd(e.pnl) : "—"}</td></tr>))}</tbody></table></div>
          )}
        </Card>
      </div>
    </>
  );
}
