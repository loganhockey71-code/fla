import { safe, sql, getSettings } from "@/lib/db";
import { COINS, portfolioSummary } from "@/lib/data";
import { pctPts, price, signedUsd, tone, usd, when } from "@/lib/format";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";


export default async function AiTradesView() {
  const { data, error } = await safe(async () => {
    const cfg = await getSettings();
    const [port, trades] = await Promise.all([
      portfolioSummary(cfg.starting_balance),
      sql()`select * from paper_trades where account = 'ai' order by opened_at desc limit 300`,
    ]);
    return { port, trades };
  });
  if (error || !data) return <><h2 className="viewtitle">Paper Trades</h2><SetupError error={error ?? "unknown"} /></>;
  const { port, trades } = data;

  return (
    <>
      <h2 className="viewtitle">Paper trades</h2>
      <p className="sub">Fake $ at real prices. Long-only spot, no leverage, BTC/ETH/XRP only. Fills cross the live spread, add slippage, and pay the fee both ways.</p>
      <div className="grid g4">
        <Stat label="Starting balance" value={usd(port.starting)} />
        <Stat label="Current balance" value={usd(port.current)} sub={port.cash != null ? `cash ${usd(port.cash, 0)} · open positions ${usd(port.positions, 0)}` : undefined} />
        <Stat label="Total P/L" cls={tone(port.pnl)} value={signedUsd(port.pnl)} />
        <Stat label="Return" cls={tone(port.ret)} value={pctPts(port.ret * 100)} />
      </div>
      <h2>Realised profit by coin</h2>
      <div className="grid g3">
        {COINS.map((c) => <Stat key={c} label={`${c} — ${port.byCoin[c].n} closed trades`} cls={tone(port.byCoin[c].pnl)} value={signedUsd(port.byCoin[c].pnl)} />)}
      </div>

      <h2>Every simulated trade</h2>
      {!trades.length ? <Empty>No trades yet. A trade happens only when a BUY (or a SELL closing an open long) is issued — HOLD never trades.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Opened</th><th>Coin</th><th>Signal</th><th>Status</th><th className="num">Market px</th><th className="num">Fill px</th><th className="num">Invested</th><th className="num">Qty</th><th className="num">Fees</th><th className="num">Slip / spread</th><th className="num">Exit px</th><th className="num">P/L $</th><th className="num">P/L %</th><th className="num">Balance after</th></tr></thead>
          <tbody>{trades.map((t) => (
            <tr key={t.id}>
              <td>{when(t.opened_at)}</td><td><b>{t.symbol}</b> <span className="muted">{t.horizon_h}h</span></td><td><Pill kind={t.signal}>{t.signal}</Pill></td>
              <td>{t.status === "skipped" ? <span className="muted" title={t.skip_reason}>skipped: {t.skip_reason}</span> : t.status === "open" ? <>open · exits {when(t.planned_exit_at)}</> : `closed (${t.exit_reason})`}</td>
              <td className="num">{price(t.market_price)}</td><td className="num">{price(t.exec_price)}</td><td className="num">{t.amount_invested != null ? usd(t.amount_invested) : "—"}</td>
              <td className="num">{t.quantity != null ? Number(t.quantity).toPrecision(5) : "—"}</td>
              <td className="num">{t.fee_entry != null ? usd((t.fee_entry as number) + ((t.fee_exit as number) ?? 0)) : "—"}</td>
              <td className="num">{t.slippage_entry_pct != null ? `${t.slippage_entry_pct}% / ${(t.spread_entry_pct as number).toFixed(3)}%` : "—"}</td>
              <td className="num">{price(t.exit_price)}</td>
              <td className={`num ${tone(t.pnl_usd)}`}>{t.pnl_usd != null ? signedUsd(t.pnl_usd) : "—"}</td>
              <td className={`num ${tone(t.pnl_pct)}`}>{t.pnl_pct != null ? pctPts(t.pnl_pct) : "—"}</td>
              <td className="num">{t.balance_after != null ? usd(t.balance_after) : t.balance_after_open != null ? usd(t.balance_after_open) : "—"}</td>
            </tr>))}</tbody></table></div>
      )}
    </>
  );
}
