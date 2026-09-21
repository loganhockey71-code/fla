import { safe, sql, getSettings } from "@/lib/db";
import { COINS, perfMetrics, scoredResults } from "@/lib/data";
import { pct, pctPts, signedUsd, tone, when } from "@/lib/format";
import { MILESTONES, Scored, binomialP, compareVariants, verdict } from "@/lib/stats";
import { Empty, Pill, SetupError } from "@/components/Ui";
import { AccuracyBars, TimeChart } from "@/components/Charts";

export const dynamic = "force-dynamic";
const DAY = 86400_000;

type R = { directional_correct: boolean; high_confidence: boolean; target_time: Date; created_at: Date };

function cumulative(rows: R[]) {
  let hits = 0;
  return rows.map((r, i) => {
    if (r.directional_correct) hits++;
    return { t: new Date(r.target_time).toISOString(), v: (hits / (i + 1)) * 100, n: i + 1 };
  });
}

export default async function Performance() {
  const { data, error } = await safe(async () => {
    const cfg = await getSettings();
    const db = sql();
    const [perf, results, equity, closed, models] = await Promise.all([
      perfMetrics(), scoredResults(),
      db`select ts, total_value from portfolio order by ts`,
      db`select closed_at, pnl_usd from paper_trades where status='closed' and account='ai' order by closed_at`,
      db`select symbol, version, trained_at, backtest_metrics from model_versions where is_active order by symbol`,
    ]);
    return { cfg, perf, allResults: results as unknown as (R & Scored)[], equity, closed, models };
  });
  if (error || !data) return <><h1>Performance</h1><SetupError error={error ?? "unknown"} /></>;
  const { cfg, perf, allResults, equity, closed, models } = data;
  const results = allResults.filter((r) => r.variant === "market");   // headline numbers = the model that trades

  // ---- 30/60/90/180-day checkpoints: everything scored within the first N days of live predictions
  const start = results.length ? Math.min(...results.map((r) => +new Date(r.created_at))) : null;
  const first = equity.length ? +new Date(equity[0].ts) : start;
  const elapsedDays = first ? (Date.now() - first) / DAY : 0;
  const milestones = MILESTONES.map((d) => {
    if (!first || elapsedDays < d) return { d, reached: false as const };
    const sub = results.filter((r) => +new Date(r.target_time) <= first + d * DAY);
    const hits = sub.filter((r) => r.directional_correct).length;
    const eq = [...equity].reverse().find((e) => +new Date(e.ts) <= first + d * DAY);
    return { d, reached: true as const, n: sub.length, hits, v: verdict(sub.length, hits, 0.5), p: binomialP(hits, sub.length), value: eq?.total_value as number | undefined };
  });

  // ---- chart series
  const equitySeries = equity.map((e) => ({ t: new Date(e.ts).toISOString(), v: e.total_value as number }));
  let cum = 0;
  const cumPnl = closed.map((t) => { cum += t.pnl_usd as number; return { t: new Date(t.closed_at).toISOString(), v: cum }; });
  const series = COINS.flatMap((c) => [24, 48].map((h) => {
    const m = perf.find(c, h, "all");
    return { name: `${c} ${h}h`, acc: (m?.directional_accuracy ?? null) as number | null, n: (m?.total_predictions ?? 0) as number };
  }));
  const overall = cumulative(results);
  const hcOnly = cumulative(results.filter((r) => r.high_confidence));

  const cols = ["Total", "Correct", "Directional", "Win rate", "Avg win", "Avg loss", "Profit factor", "Max drawdown", "Best", "Worst"];
  const row = (label: string, m: ReturnType<typeof perf.find>, paper = false) => m && (
    <tr key={label}>
      <td><b>{label}</b></td><td className="num">{m.total_predictions}</td><td className="num">{m.correct_predictions}</td>
      <td className="num">{pct(m.directional_accuracy)}</td><td className="num">{pct(m.win_rate)}</td>
      <td className="num pos">{m.avg_win == null ? "—" : pctPts(m.avg_win)}</td><td className="num neg">{m.avg_loss == null ? "—" : pctPts(m.avg_loss)}</td>
      <td className="num">{m.profit_factor == null ? "—" : m.profit_factor.toFixed(2)}</td>
      <td className="num">{m.max_drawdown == null ? "—" : `${m.max_drawdown.toFixed(2)}${paper ? "%" : " pts"}`}</td>
      <td className="num">{m.best_trade == null ? "—" : paper ? signedUsd(m.best_trade) : pctPts(m.best_trade)}</td>
      <td className="num">{m.worst_trade == null ? "—" : paper ? signedUsd(m.worst_trade) : pctPts(m.worst_trade)}</td>
    </tr>);
  const head = (first: string) => <thead><tr><th>{first}</th>{cols.map((c) => <th key={c} className="num">{c}</th>)}</tr></thead>;

  return (
    <>
      <h1>Performance</h1>
      <p className="sub">LIVE results only, unless a section says BACKTEST. {results.length} scored predictions{first ? ` over ${elapsedDays.toFixed(1)} days` : ""}.</p>

      <h2>Do we have an edge? Checkpoints</h2>
      <div className="scroll"><table>
        <thead><tr><th>Checkpoint</th><th>Verdict</th><th className="num">Scored</th><th className="num">Directional accuracy</th><th className="num">p-value vs coin flip</th><th className="num">Portfolio value</th><th>Reading</th></tr></thead>
        <tbody>{milestones.map((m) => m.reached ? (
          <tr key={m.d}><td><b>{m.d} days</b></td><td><Pill kind={m.v.tone}>{m.v.label}</Pill></td><td className="num">{m.n}</td>
            <td className="num">{pct(m.n ? m.hits / m.n : null)}</td><td className="num">{m.p.toFixed(3)}</td><td className="num">{m.value ? `$${m.value.toLocaleString("en-US", { maximumFractionDigits: 0 })}` : "—"}</td><td className="muted">{m.v.text}</td></tr>
        ) : (
          <tr key={m.d}><td><b>{m.d} days</b></td><td colSpan={6} className="muted">Not reached yet — day {elapsedDays.toFixed(1)} of {m.d}.</td></tr>
        ))}</tbody></table></div>
      <p className="muted" style={{ marginTop: 8 }}>A low p-value only says the accuracy is unlikely to be luck. Money is a separate test: check the paper P/L below, which pays fees, spread and slippage.</p>

      <h2>Fake portfolio</h2>
      <div className="grid g2">
        <div className="card"><h3>Portfolio value over time</h3><TimeChart kind="area" data={equitySeries} refY={cfg.starting_balance} unit="" color="#6ea8ff" /></div>
        <div className="card"><h3>Cumulative realised P/L ($)</h3><TimeChart data={cumPnl} refY={0} color="#2ecc8f" /></div>
      </div>

      <h2>Prediction accuracy</h2>
      <div className="grid g2">
        <div className="card"><h3>Directional accuracy by coin &amp; horizon</h3><AccuracyBars data={series} /></div>
        <div className="card"><h3>Accuracy over time (cumulative, all predictions)</h3><TimeChart data={overall} refY={50} unit="%" domain={[30, 70]} color="#6ea8ff" /></div>
      </div>
      <div className="card" style={{ marginTop: 14 }}><h3>High-confidence (≥ {cfg.high_confidence_threshold}%) accuracy over time</h3>
        {hcOnly.length ? <TimeChart data={hcOnly} refY={50} unit="%" domain={[0, 100]} color="#b18cff" /> :
          <Empty>No prediction has reached {cfg.high_confidence_threshold}% confidence yet. That is expected: the model only claims what its out-of-sample record supports.</Empty>}
      </div>

      <h2>Accuracy tables (live)</h2>
      {!perf.rows.length ? <Empty>Metrics appear after the first predictions are scored (24h after the first prediction).</Empty> : (
        <>
          <div className="scroll"><table>{head("Series")}<tbody>
            {row("All predictions", perf.find("ALL", null, "all"))}
            {COINS.flatMap((c) => [24, 48].map((h) => row(`${c} ${h}h`, perf.find(c, h, "all"))))}
          </tbody></table></div>
          <h2>By signal type</h2>
          <div className="scroll"><table>{head("Slice")}<tbody>
            {row("BUY", perf.find("ALL", null, "BUY"))}{row("HOLD", perf.find("ALL", null, "HOLD"))}{row("SELL", perf.find("ALL", null, "SELL"))}
            {row(`High confidence ≥ ${cfg.high_confidence_threshold}%`, perf.find("ALL", null, "high_conf"))}
          </tbody></table></div>
          <p className="muted" style={{ marginTop: 8 }}>Win rate / avg win / avg loss / drawdown here are per-signal % moves, gross of costs (SELL counted as if it profits from a fall). For HOLD they are blank because HOLD takes no position.</p>
          <h2>Paper account (net of costs)</h2>
          <div className="scroll"><table>{head("Scope")}<tbody>
            {row("All coins", perf.find("ALL", null, "paper"), true)}
            {COINS.map((c) => row(c, perf.find(c, null, "paper"), true))}
          </tbody></table></div>
        </>
      )}

      <h2>Does research data help? Market-only vs market + research</h2>
      {(() => {
        const rows: [string, (r: Scored) => boolean][] = [["All", () => true], ["BTC", (r) => r.symbol === "BTC"], ["ETH", (r) => r.symbol === "ETH"], ["XRP", (r) => r.symbol === "XRP"], ["24h", (r) => r.horizon_h === 24], ["48h", (r) => r.horizon_h === 48]];
        const all = compareVariants(allResults);
        const tone = all.label === "Research helps" ? "good" : all.label === "Research hurts" ? "bad" : all.label === "Too early" ? "muted" : "warn";
        return (
          <>
            <div className="note" style={{ borderLeftColor: "var(--accent)" }}><b>{all.label}.</b> {all.text} <Pill kind={tone}>{all.label}</Pill></div>
            <div className="scroll"><table>
              <thead><tr><th>Slice</th><th className="num">Paired</th><th className="num">Market-only dir. acc.</th><th className="num">+ research dir. acc.</th><th className="num">Market-only signal acc.</th><th className="num">+ research signal acc.</th>
                <th className="num">Right only w/ market</th><th className="num">Right only w/ research</th><th className="num">p-value</th><th className="num" title="mean BUY move minus 1% round-trip cost">Market BUY net</th><th className="num">Research BUY net</th></tr></thead>
              <tbody>{rows.map(([label, f]) => {
                const c = compareVariants(allResults.filter(f));
                return (
                  <tr key={label}><td><b>{label}</b></td><td className="num">{c.pairs}</td><td className="num">{pct(c.market.dir)}</td><td className="num">{pct(c.research.dir)}</td>
                    <td className="num">{pct(c.market.sig)}</td><td className="num">{pct(c.research.sig)}</td><td className="num">{c.onlyM}</td><td className="num">{c.onlyR}</td><td className="num">{c.pairs ? c.p.toFixed(2) : "—"}</td>
                    <td className="num">{c.market.netBuy == null ? "—" : `${pctPts(c.market.netBuy)} (${c.market.buys})`}</td><td className="num">{c.research.netBuy == null ? "—" : `${pctPts(c.research.netBuy)} (${c.research.buys})`}</td></tr>);
              })}</tbody></table></div>
            <p className="muted" style={{ marginTop: 8 }}>Both models predict at the same instant from the same candles; the research model also sees FRED macro data and detected events/prediction-market moves (saved verbatim with every prediction). Only the market-only model paper-trades. Event features are unknown before the research layer started collecting, so expect the research model to look the same as the market model for a while.</p>
          </>
        );
      })()}

      <h2>BACKTEST — not live results</h2>
      <div className="note bt"><b>Read this before trusting any number below.</b> These come from walk-forward testing on historical candles when each model was trained. They are kept apart from live results, ignore the spread, and a model tuned on history usually looks better than it does live. Only the live sections above count as evidence.</div>
      {!models.length ? <Empty>No trained models yet.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Model</th><th>Hz</th><th className="num">OOS samples</th><th className="num">Direction acc.</th><th className="num">Always-up baseline</th><th className="num">Mean fold AUC</th><th className="num">Sim. trades</th><th className="num">Sim. return (net)</th><th className="num">Buy &amp; hold</th></tr></thead>
          <tbody>{models.flatMap((m) => ["24", "48"].map((h) => {
            const b = (m.backtest_metrics as Record<string, any>)[h];
            if (!b) return null;
            return (
              <tr key={m.symbol + h}>
                <td><b>{m.symbol}</b> <span className="muted">{m.version}</span></td><td>{h}h</td><td className="num">{b.oos_samples}</td>
                <td className="num">{pct(b.directional_accuracy)}</td><td className="num">{pct(b.always_up_accuracy)}</td>
                <td className="num">{b.auc.toFixed(3)}{b.no_skill ? " (no skill)" : ""}</td><td className="num">{b.strategy.trades}</td>
                <td className={`num ${tone(b.strategy.compounded_return_pct)}`}>{pctPts(b.strategy.compounded_return_pct)}</td>
                <td className="num">{pctPts(b.buy_and_hold_return_pct, 1)}</td>
              </tr>);
          }))}</tbody></table></div>
      )}
      <p className="muted" style={{ marginTop: 8 }}>AUC 0.50 = coin flip. Anything under ~0.55 is very weak once fees are paid.{models[0] ? ` Latest training: ${when(models[0].trained_at)}.` : ""}</p>
    </>
  );
}
