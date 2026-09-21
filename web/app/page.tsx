import Link from "next/link";
import { safe, getSettings } from "@/lib/db";
import { ACTION_HELP, COINS, latestPredictions, latestPrices, latestSignals, perfMetrics, portfolioSummary, scoredResults } from "@/lib/data";
import { pct, price, pctPts, signedUsd, tone, usd, when } from "@/lib/format";
import { verdict } from "@/lib/stats";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";

export const dynamic = "force-dynamic";

export default async function Dashboard() {
  const { data, error } = await safe(async () => {
    const cfg = await getSettings();
    const [prices, preds, perf, port, results, signals] = await Promise.all([
      latestPrices(), latestPredictions(), perfMetrics(), portfolioSummary(cfg.starting_balance), scoredResults(), latestSignals(),
    ]);
    return { cfg, prices, preds, perf, port, results, signals };
  });
  if (error || !data) return <><h1>Dashboard</h1><SetupError error={error ?? "unknown"} /></>;
  const { cfg, prices, preds, perf, port, results, signals } = data;

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

      <h2>What to do now</h2>
      {COINS.some((c) => signals[c] && signals[c].trigger_kind !== "scheduled" && signals[c].urgency !== "low" && Date.now() - new Date(signals[c].created_at).getTime() < 3_600_000) && (
        <div className={`alert ${COINS.some((c) => signals[c]?.urgency === "high" && Date.now() - new Date(signals[c].created_at).getTime() < 3_600_000) ? "" : "warn"}`}>
          <b>Sudden event in the last hour.</b>{" "}
          {COINS.filter((c) => signals[c] && signals[c].trigger_kind !== "scheduled" && signals[c].urgency !== "low" && Date.now() - new Date(signals[c].created_at).getTime() < 3_600_000)
            .map((c) => `${c}: ${signals[c].action}`).join(" · ")}. Details on the <Link href="/signals">Signals</Link> page.
        </div>
      )}
      <div className="now">
        {COINS.map((c) => {
          const g = signals[c];
          const live = g && new Date(g.expires_at).getTime() > Date.now();
          return (
            <div className={`card ${g && !live ? "stale" : ""}`} key={c}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}><b>{c}</b>
                {g && <span className="muted" style={{ fontSize: 12 }}>{g.trigger_kind === "scheduled" ? "regular model read" : `sudden ${g.trigger_kind}`} · {when(g.created_at)}</span>}</div>
              {g ? (
                <>
                  <div className="act"><Pill kind={g.action}>{g.action}</Pill></div>
                  <div className="muted" style={{ fontSize: 12 }}>{ACTION_HELP[g.action]}{g.urgency !== "low" ? ` · urgency ${g.urgency}` : ""}{!live ? " · expired, waiting for the next update" : ""}</div>
                  <div className="why">{g.cause && g.trigger_kind !== "scheduled" ? `Likely cause: ${g.cause}. ` : ""}{(g.reasons as string[])[0]}</div>
                </>
              ) : <div className="why">No signal yet. The worker publishes one after each prediction run.</div>}
            </div>
          );
        })}
      </div>
      <p className="muted" style={{ marginTop: 8 }}>Rule-based and unproven: it reacts to sudden moves, volume spikes and major news, and never changes the logged predictions. Paper trading only.</p>

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
