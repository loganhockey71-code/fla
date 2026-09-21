import { safe, sql, getSettings } from "@/lib/db";
import { pct, when } from "@/lib/format";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";


const CLASS: Record<string, string> = {
  event_shock_missed: "an event after the call pointed the way price moved", macro_release_in_window: "a big macro release landed in the window",
  market_led_move: "the wider crypto market moved together with the coin", volume_shock: "a volume spike drove the move", volatility_regime_shift: "volatility jumped",
  event_misread: "a bullish/bearish event existed, price went the other way", analogs_disagreed: "similar past situations went the other way",
  misleading_drivers: "the top model drivers pointed the wrong way", prediction_market_shift: "a prediction market repriced first",
  hold_missed_move: "HOLD missed a big move", unexplained: "no measurable cause in our data",
};
const STATUS = { confirmed: "bad", candidate: "warn", insufficient: "muted", noise: "muted" } as const;
const DECISION = { promoted: "good", rejected: "warn", insufficient_data: "muted" } as const;

export default async function LearningView() {
  const { data, error } = await safe(async () => {
    const db = sql();
    const cfg = await getSettings();
    const [c] = await db`select count(*)::int scored, count(*) filter (where r.signal_correct = false)::int wrong from prediction_results r join predictions p on p.id = r.prediction_id where p.variant = 'market'`;
    const [pmN] = await db`select count(*)::int n from post_mortems`;
    const [classes, recent, patterns, models, challenges] = await Promise.all([
      db`select error_class, count(*)::int n from post_mortems group by 1 order by 2 desc`,
      db`select m.*, p.created_at as made_at from post_mortems m join predictions p on p.id = m.prediction_id order by m.id desc limit 25`,
      db`select * from learned_patterns order by case status when 'confirmed' then 0 when 'candidate' then 1 when 'noise' then 2 else 3 end, p_value nulls last, key`,
      db`select m.symbol, m.variant, m.version, m.trained_at,
           (select count(*)::int from predictions p join prediction_results r on r.prediction_id = p.id where p.symbol = m.symbol and p.variant = m.variant and p.created_at > m.trained_at) new_scored
         from model_versions m where m.is_active order by m.variant, m.symbol`,
      db`select * from model_challenges order by id desc limit 15`,
    ]);
    return { cfg, c, pmN, classes, recent, patterns, models, challenges };
  });
  if (error || !data) return <><h2 className="viewtitle">Learning</h2><SetupError error={error ?? "unknown"} /></>;
  const { cfg, c, pmN, classes, recent, patterns, models, challenges } = data;
  const shown = patterns.filter((p) => p.status !== "insufficient");

  return (
    <>
      <h2 className="viewtitle">Self-learning</h2>
      <p className="sub">Every wrong prediction gets a post-mortem. Patterns are only trusted after many examples. A model is only replaced after it beats the current one on data neither has seen. <b>One mistake never changes anything.</b></p>

      <div className="grid g4">
        <Stat label="Scored predictions (market model)" value={c.scored} />
        <Stat label="Wrong" value={c.wrong} sub={c.scored ? `${pct(c.wrong / c.scored)} of scored (HOLD counts as wrong if price left the band)` : undefined} />
        <Stat label="Post-mortems written" value={pmN.n} sub="append-only, never edited" />
        <Stat label="Models replaced so far" value={challenges.filter((x) => x.decision === "promoted").length} sub={`${challenges.length} challenges recorded`} />
      </div>

      <h2>When are we wrong? (main explanation per mistake)</h2>
      {!classes.length ? <Empty>No wrong predictions have been scored yet. Predictions resolve 24h/48h after they are made.</Empty> : (
        <div className="scroll"><table><thead><tr><th>Explanation</th><th className="num">Mistakes</th><th className="num">Share</th></tr></thead>
          <tbody>{classes.map((k) => <tr key={k.error_class}><td>{CLASS[k.error_class] ?? k.error_class}</td><td className="num">{k.n}</td><td className="num">{pct(k.n / pmN.n)}</td></tr>)}</tbody></table></div>
      )}

      <h2>Patterns (statistics over many predictions)</h2>
      <div className="note">A situation only becomes a pattern with at least <b>{cfg.pattern_min_examples}</b> examples on each side and a statistically significant excess of errors, corrected for testing many patterns at once. Patterns are <b>suggestions for you to review</b>. Nothing is applied automatically.</div>
      {!shown.length ? <Empty>No pattern has enough evidence yet ({patterns.length} situations are being tracked). This fills in as predictions are scored.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Situation</th><th>Scope</th><th>Status</th><th className="num">Error rate with</th><th className="num">without</th><th className="num">Lift</th><th className="num">p-value</th><th className="num">Examples</th><th>Suggestion</th></tr></thead>
          <tbody>{shown.map((p) => (
            <tr key={p.key}><td>{p.description}</td><td>{p.scope} · {p.horizon_h ? `${p.horizon_h}h` : "24h+48h"}</td><td><Pill kind={STATUS[p.status as keyof typeof STATUS]}>{p.status}</Pill></td>
              <td className="num">{pct(p.error_rate_with)}</td><td className="num">{pct(p.error_rate_without)}</td><td className="num">{p.lift ? `${Number(p.lift).toFixed(2)}x` : "—"}</td>
              <td className="num">{p.p_value != null ? Number(p.p_value).toExponential(1) : "—"}</td><td className="num">{p.n_with_tag}</td><td>{p.suggestion ?? <span className="muted">—</span>}</td></tr>))}</tbody></table></div>
      )}

      <h2>Retraining status</h2>
      <p className="muted" style={{ marginTop: -4 }}>A retrain is attempted only after {cfg.retrain_min_days} days AND {cfg.retrain_min_new_scored} newly scored predictions, then judged on the most recent {cfg.retrain_holdout_days} days that the challenger never trained on.</p>
      <div className="scroll"><table>
        <thead><tr><th>Model</th><th>Active version</th><th className="num">Trained</th><th className="num">Days ({cfg.retrain_min_days} needed)</th><th className="num">New scored ({cfg.retrain_min_new_scored} needed)</th><th>Retrain</th></tr></thead>
        <tbody>{models.map((m) => {
          const days = (Date.now() - new Date(m.trained_at as Date).getTime()) / 86_400_000;
          const ready = days >= cfg.retrain_min_days && m.new_scored >= cfg.retrain_min_new_scored;
          return (
            <tr key={m.symbol + m.variant}><td><b>{m.symbol}</b> {m.variant}</td><td className="muted">{m.version}</td><td className="num">{when(m.trained_at)}</td>
              <td className="num">{days.toFixed(0)}</td><td className="num">{m.new_scored}</td><td>{ready ? <Pill kind="warn">eligible for a challenge</Pill> : <span className="muted">not yet: keeps current model</span>}</td></tr>);
        })}</tbody></table></div>

      <h2>Champion vs challenger history</h2>
      {!challenges.length ? <Empty>No retrain has been attempted yet. That is expected until the thresholds above are met.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>When</th><th>Model</th><th>Decision</th><th>Champion → challenger</th><th>Reason (judged on unseen data)</th></tr></thead>
          <tbody>{challenges.map((x) => (
            <tr key={x.id}><td>{when(x.created_at)}</td><td><b>{x.symbol}</b> {x.variant}</td><td><Pill kind={DECISION[x.decision as keyof typeof DECISION] ?? "muted"}>{x.decision.replace("_", " ")}</Pill></td>
              <td className="muted">{x.champion_version} → {x.challenger_version ?? "—"}</td><td>{x.reason}</td></tr>))}</tbody></table></div>
      )}

      <h2>Recent post-mortems</h2>
      {!recent.length ? <Empty>None yet.</Empty> : (
        <div className="grid" style={{ gap: 10 }}>{recent.map((m) => {
          const sim = (m.findings as { similar?: { k: number; up_rate: number; mean_return: number } | null }).similar;
          return (
            <div className="card" key={m.id}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
                <span><b>{m.symbol}</b> {m.horizon_h}h <Pill kind={m.signal}>{m.signal}</Pill> <span className="muted">{m.variant} · made {when(m.made_at)}</span></span>
                <span className="neg">{Number(m.actual_return_pct) >= 0 ? "+" : ""}{Number(m.actual_return_pct).toFixed(2)}%</span></div>
              <div className="why" style={{ fontSize: 13, color: "var(--text)" }}>{m.summary}</div>
              <div className="why">{(m.tags as string[]).map((t) => t.replace(/_/g, " ")).join(" · ")}{sim ? ` · ${sim.k} similar past situations: price rose ${pct(sim.up_rate, 0)} of the time (avg ${(sim.mean_return * 100).toFixed(2)}%)` : ""}</div>
            </div>);
        })}</div>
      )}
    </>
  );
}
