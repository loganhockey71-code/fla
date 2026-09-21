import Link from "next/link";
import { safe, getSettings } from "@/lib/db";
import { COINS, latestPredictions, latestPrices, perfMetrics, portfolioSummary, scoredResults } from "@/lib/data";
import { pct, price, pctPts, signedUsd, tone, usd, when } from "@/lib/format";
import { verdict } from "@/lib/stats";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";

export const dynamic = "force-dynamic";

export default async function Dashboard() {
  const { data, error } = await safe(async () => {
    const cfg = await getSettings();
    const [prices, preds, perf, port, results] = await Promise.all([
      latestPrices(), latestPredictions(), perfMetrics(), portfolioSummary(cfg.starting_balance), scoredResults(),
    ]);
    return { cfg, prices, preds, perf, port, results };
  });
  if (error || !data) return <><h1>Dashboard</h1><SetupError error={error ?? "unknown"} /></>;
  const { cfg, prices, preds, perf, port, results } = data;

  const all = perf.find("ALL", null, "all");
  const hc = perf.find("ALL", null, "high_conf");
  const mine = results.filter((r) => r.variant === "market");
  const n = mine.length;
  const hits = mine.filter((r) => r.directional_correct).length;
  const v = verdict(n, hits, 0.5);

  return (
    <>
      <h1>Dashboard</h1>
      <p className="sub">Would following this AI with real prices have made money? Live paper results only — backtests are on the Performance page.</p>

      <div className="grid g4">
        <Stat label="Fake portfolio value" value={usd(port.current)} sub={`started at ${usd(port.starting, 0)}`} />
        <Stat label="Total P/L" cls={tone(port.pnl)} value={signedUsd(port.pnl)} sub={<span className={tone(port.ret)}>{pctPts(port.ret * 100)} return</span>} />
        <Stat label="Directional accuracy (all predictions)" value={pct(all?.directional_accuracy)}
          sub={all ? `${all.correct_predictions}/${all.total_predictions} signals correct incl. HOLD` : "no scored predictions yet"} />
        <Stat label="High-confidence accuracy (≥ 75%)" value={hc && hc.total_predictions > 0 ? pct(hc.directional_accuracy) : "—"}
          sub={`n = ${hc?.total_predictions ?? 0}${hc && hc.total_predictions < 30 ? " — too few to trust" : ""}`} />
      </div>

      <div className={`note ${v.tone === "good" ? "" : ""}`} style={{ borderLeftColor: v.tone === "good" ? "var(--pos)" : v.tone === "bad" ? "var(--neg)" : "var(--warn)" }}>
        <b>Verdict so far: {v.label}.</b> {v.text}
      </div>

      <h2>Coins</h2>
      <div className="grid g3">
        {COINS.map((c) => {
          const px = prices[c];
          return (
            <Link key={c} href={`/${c.toLowerCase()}`} className="card coin">
              <div className="name"><b>{c}</b><span className="muted">{px ? when(px.ts) : ""}</span></div>
              <div className="px">{price(px?.price)}</div>
              {[24, 48].map((h) => {
                const p = preds.find((x) => x.symbol === c && x.horizon_h === h);
                return (
                  <div className="row" key={h}>
                    <span>{h}h</span>
                    {p ? <span><Pill kind={p.signal}>{p.signal}</Pill> <b>{Math.round(p.confidence * 100)}%</b></span> : <span className="muted">no prediction yet</span>}
                  </div>
                );
              })}
              <div className="row"><span className="muted">Paper P/L</span><span className={tone(port.byCoin[c].pnl)}>{signedUsd(port.byCoin[c].pnl)}</span></div>
            </Link>
          );
        })}
      </div>
      {!preds.length && <Empty>No predictions yet. Run <code>python -m crypto_ai.cli train</code> once, then <code>python -m crypto_ai.cli tick</code> (or let the scheduled job do it).</Empty>}
      <p className="muted" style={{ marginTop: 18 }}>
        Percent next to BUY/HOLD/SELL = the model's confidence (its calibrated probability for the side it picked). Trades use {cfg.trading_fee_pct}% fee and {cfg.slippage_pct}% slippage plus the live spread.
      </p>
    </>
  );
}
