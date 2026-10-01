import { safe, sql, getSettings } from "@/lib/db";
import { ago, pctPts, price, signedUsd, tone, usd, when } from "@/lib/format";
import { Empty, Pill, SetupError, Stat } from "@/components/Ui";
import { heartbeatTone, leanOf, readIsFresh, signalStats, type Read, type SignalRow } from "@/lib/scalp";

const COINS = ["BTC", "ETH", "XRP"];
type Row = Record<string, unknown>;
const num = (x: unknown) => (x == null ? null : Number(x));

export default async function ScalperView() {
  const { data, error } = await safe(async () => {
    const db = sql();
    const cfg = await getSettings();
    const [agg, bySym, byRegime, todayR, open, recent, lessons, states, sigRows, sigRecent] = await Promise.all([
      db`select count(*)::int n, count(*) filter (where net_pnl_usd > 0)::int wins, coalesce(sum(net_pnl_usd),0)::float net,
                coalesce(sum(net_pnl_usd) filter (where net_pnl_usd > 0),0)::float gross_win, coalesce(-sum(net_pnl_usd) filter (where net_pnl_usd <= 0),0)::float gross_loss,
                avg(net_pnl_usd) filter (where net_pnl_usd > 0)::float avg_win, avg(net_pnl_usd) filter (where net_pnl_usd <= 0)::float avg_loss,
                avg(duration_min)::float dur, coalesce(sum(fees_usd),0)::float fees, coalesce(sum(slippage_usd),0)::float slip, coalesce(sum(gross_pnl_usd),0)::float gross,
                avg(mfe_pct)::float mfe, avg(mae_pct)::float mae, min(opened_at) first_at
         from scalp_trades where status = 'closed'`,
      db`select symbol, count(*)::int n, coalesce(sum(net_pnl_usd),0)::float net, count(*) filter (where net_pnl_usd > 0)::int wins from scalp_trades where status='closed' group by symbol`,
      db`select regime, count(*)::int n, coalesce(sum(net_pnl_usd),0)::float net, count(*) filter (where net_pnl_usd > 0)::int wins from scalp_trades where status='closed' group by regime order by regime`,
      db`select count(*)::int n from scalp_trades where opened_at >= date_trunc('day', now())`,
      db`select * from scalp_trades where status = 'open' order by opened_at`,
      db`select * from scalp_trades where status = 'closed' order by closed_at desc limit 60`,
      db`select l.lesson, l.verdict, l.created_at from scalp_lessons l order by l.id desc limit 12`,
      db`select key, value, updated_at from scalp_state`,
      db`select status, reason, outcome, profitable, fwd_net_pct::float fwd_net_pct from scalp_signals where decision_ts > now() - interval '14 days'`,
      db`select s.*, t.net_pnl_usd::float trade_net from scalp_signals s left join scalp_trades t on t.id = s.trade_id order by s.decision_ts desc limit 40`,
    ]);
    const mids = await db`select distinct on (symbol) symbol, price from market_data order by symbol, ts desc`;
    return { cfg, agg: agg[0] as Row, bySym, byRegime, today: (todayR[0] as Row).n as number, open, recent, lessons, states, mids, sigRows: sigRows as unknown as SignalRow[], sigRecent };
  });
  if (error || !data) return <><h2 className="viewtitle">Scalper</h2><SetupError error={error ?? "unknown"} /><div className="note">Run <code>supabase/schema_scalp.sql</code> in the Supabase SQL editor to create the scalper tables.</div></>;
  const { cfg, agg, bySym, byRegime, today, open, recent, lessons, states, mids, sigRows, sigRecent } = data;

  const st = (k: string) => states.find((s) => s.key === k)?.value as Row | undefined;
  const status = st("status") as { at?: string; enabled?: boolean; note?: string; threshold?: number | null; rejected?: Record<string, number>; reads?: Record<string, Read>;
    signals?: { raised?: number; confirmed?: number; failed?: Record<string, number>; expired?: number; blocked?: Record<string, number> }; error?: string | null } | undefined;
  const beat = st("heartbeat") as { at?: string } | undefined;
  const retrain = (states.find((s) => s.key === "retrain_log")?.value ?? []) as { at: string; decision: string; reason?: string }[];
  const insights = (states.find((s) => s.key === "insights")?.value ?? []) as string[];
  const blocked = (states.find((s) => s.key === "blocked_setups")?.value ?? []) as [string, number, string, number, number][];
  const policy = (st("exit_policy") as { policy?: Record<string, Row>; at?: string } | undefined);
  const mid = (c: string) => num(mids.find((m) => m.symbol === c)?.price);

  const n = Number(agg.n), net = Number(agg.net);
  const pf = Number(agg.gross_loss) > 0 ? Number(agg.gross_win) / Number(agg.gross_loss) : null;
  const days = agg.first_at ? Math.max(1, (Date.now() - new Date(agg.first_at as string).getTime()) / 86_400_000) : 1;
  const unreal = open.reduce((a, t) => {
    const m = mid(t.symbol as string);
    return m ? a + (t.direction === "long" ? 1 : -1) * ((m / (t.entry_mid as number)) - 1) * (t.notional as number) : a;
  }, 0);
  const equity = cfg.starting_balance + net + unreal;
  const hb = heartbeatTone(beat?.at);
  const sstats = signalStats(sigRows);
  const reads = status?.reads ?? {};
  const pct = (x: number | null) => (x == null ? "—" : `${(x * 100).toFixed(0)}%`);

  return (
    <>
      <h2 className="viewtitle">Short-term scalper</h2>
      <p className="sub">An autonomous paper trader. Each minute it predicts whether a trade over the next few minutes has a positive expected edge <b>after</b> {cfg.trading_fee_pct}% fee + {cfg.slippage_pct}% slippage per side. A prediction is only a <b>signal</b>: it trades only if the <b>next candle confirms</b> it, and every signal (traded or not) is later graded right or wrong and studied. News, liquidity, spread, your loss limits and a drawdown halt all sit in front of every trade. Adaptive stops, targets and trailing stops; longs and simulated shorts; fake money only. It does not force trades: on a quiet or costly day the right number is few or none.</p>
      {status?.error && <div className="note err"><b>The entry side of the last pass failed</b> ({status.error}). Open trades are still being managed; no new trades are opened until this is fixed.</div>}
      <div className="grid g4">
        <Stat label="Status" value={<Pill kind={cfg.autopilot_enabled ? "BUY" : "HOLD"}>{cfg.autopilot_enabled ? "ON" : "OFF"}</Pill>} sub={status?.note ?? "no pass recorded yet"} />
        <Stat label="Last worker pass" value={beat?.at ? ago(beat.at) : "never"} cls={hb.tone === "error" ? "neg" : ""} sub={hb.tone === "error" ? "worker looks stalled - open positions are only checked when it runs" : "heartbeat"} />
        <Stat label="Trades today" value={today} sub={`${open.length} open now`} />
        <Stat label="Edge threshold" value={status?.threshold != null ? `${status.threshold}%` : "none"} sub="min predicted net edge (validated out-of-sample)" />
      </div>
      <div className="grid g4" style={{ marginTop: 10 }}>
        <Stat label="Account value" value={usd(equity)} sub={`started at ${usd(cfg.starting_balance, 0)}`} />
        <Stat label="Net P&L (after costs)" cls={tone(net)} value={signedUsd(net)} sub={`gross ${signedUsd(Number(agg.gross))} - fees ${usd(Number(agg.fees))} - slippage ${usd(Number(agg.slip))}`} />
        <Stat label="Win rate" value={n ? `${((Number(agg.wins) / n) * 100).toFixed(1)}%` : "—"} sub={`${n} closed trades · ${(n / days).toFixed(1)}/day`} />
        <Stat label="Profit factor" value={pf != null ? pf.toFixed(2) : "—"} sub={`avg win ${num(agg.avg_win) != null ? signedUsd(num(agg.avg_win)) : "—"} · avg loss ${num(agg.avg_loss) != null ? signedUsd(num(agg.avg_loss)) : "—"}`} />
      </div>
      <div className="grid g4" style={{ marginTop: 10 }}>
        <Stat label="Avg duration" value={num(agg.dur) != null ? `${num(agg.dur)!.toFixed(1)} min` : "—"} />
        <Stat label="Avg MFE" cls="pos" value={num(agg.mfe) != null ? pctPts(num(agg.mfe)) : "—"} sub="best it went in our favour" />
        <Stat label="Avg MAE" cls="neg" value={num(agg.mae) != null ? pctPts(num(agg.mae)) : "—"} sub="worst it went against us" />
        <Stat label="Costs" value={usd(Number(agg.fees) + Number(agg.slip))} sub="fees + slippage paid so far" />
      </div>

      <h2>Live read, right now</h2>
      <div className="grid g3">
        {COINS.map((c) => {
          const r = reads[c];
          const fresh = !!r && readIsFresh(r.at);
          const l = leanOf(fresh ? r : null, cfg.scalp_allow_shorts);
          return (
            <div className="card" key={c}>
              <b>{c}</b> {fresh && r ? <Pill kind={l.lean === "long" ? "BUY" : l.lean === "short" ? "SELL" : "HOLD"}>{l.lean === "none" ? "no edge" : l.lean}</Pill> : <span className="muted">no fresh read</span>}
              <div className="why" style={{ marginTop: 6 }}>{fresh ? l.text : "The worker has not produced a live read in the last few minutes (it needs fresh candles and an active model)."}</div>
              {fresh && r && <div className="why muted">News: {r.news === "none" ? "nothing directional" : `${r.news} (${r.news_net >= 0 ? "+" : ""}${r.news_net})`} · price {price(r.price)}</div>}
            </div>
          );
        })}
      </div>

      <h2>Predictions and next-candle confirmation</h2>
      <div className="grid g4">
        <Stat label="Predictions (14 days)" value={sstats.total} sub={`${sstats.pending} waiting · ${sstats.expired} expired`} />
        <Stat label="Moved the predicted way" value={pct(sstats.all.right)} sub={`${sstats.all.n} graded · ${pct(sstats.all.paid)} would have paid after costs`} />
        <Stat label="Confirmed by the next candle" value={sstats.confirmed.n} sub={`right ${pct(sstats.confirmed.right)} · mean ${sstats.confirmed.meanNet != null ? pctPts(sstats.confirmed.meanNet, 3) : "—"} net`} />
        <Stat label="Refused by the next candle" value={sstats.failed.n} sub={`right ${pct(sstats.failed.right)} · mean ${sstats.failed.meanNet != null ? pctPts(sstats.failed.meanNet, 3) : "—"} net`} />
      </div>
      <div className="note" style={{ marginTop: 8 }}>{sstats.verdict}{Object.keys(sstats.reasons).length > 0 && <> Refused / blocked because: {Object.entries(sstats.reasons).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k.replace(/_/g, " ")} ×${v}`).join(", ")}.</>}</div>
      {!sigRecent.length ? <Empty>No predictions yet. A prediction is recorded whenever the model's predicted net edge clears its threshold, whether or not it ends up trading.</Empty> : <div className="scroll"><table>
        <thead><tr><th>Predicted (UTC)</th><th>Coin</th><th>Side</th><th className="num">Pred. edge</th><th>What happened</th><th>News</th><th>Graded</th><th>Why it was right or wrong</th></tr></thead>
        <tbody>{sigRecent.map((g) => {
          const nb = ((g.news as Row)?.bias as string) ?? "none";
          return (
            <tr key={g.id as number}><td>{when(g.decision_ts as Date)}</td><td><b>{g.symbol as string}</b></td><td>{g.direction as string}</td><td className="num">{pctPts(num(g.pred_edge_pct), 3)}</td>
              <td><Pill kind={g.status === "confirmed" ? "BUY" : g.status === "pending" ? "HOLD" : "SELL"}>{g.status as string}</Pill>{g.reason && g.reason !== "confirmed" ? <div className="why">{(g.reason as string).replace(/_/g, " ")}</div> : null}
                {g.trade_net != null && <div className={`why ${tone(num(g.trade_net))}`}>traded: {signedUsd(num(g.trade_net))}</div>}</td>
              <td className="muted">{nb === "none" ? "—" : nb}</td>
              <td>{g.outcome ? <Pill kind={g.outcome === "right" ? "BUY" : g.outcome === "wrong" ? "SELL" : "HOLD"}>{g.outcome as string}</Pill> : <span className="muted">not yet</span>}{g.fwd_gross_pct != null && <div className="why">{pctPts(num(g.fwd_gross_pct))} gross</div>}</td>
              <td style={{ maxWidth: 460 }}>{(g.lesson as string) ?? <span className="muted">graded after the {cfg.scalp_hold_bars}-minute window closes</span>}</td></tr>
          );
        })}</tbody></table></div>}

      <h2>By coin and by market regime</h2>
      <div className="grid g3">
        {COINS.map((c) => { const r = bySym.find((x) => x.symbol === c); return <Stat key={c} label={`${c} — ${r?.n ?? 0} trades`} cls={tone(num(r?.net))} value={signedUsd(num(r?.net) ?? 0)} sub={r?.n ? `${(((r.wins as number) / (r.n as number)) * 100).toFixed(0)}% win` : undefined} />; })}
      </div>
      {byRegime.length > 0 && <div className="scroll" style={{ marginTop: 8 }}><table><thead><tr><th>Regime</th><th className="num">Trades</th><th className="num">Win rate</th><th className="num">Net P&L</th></tr></thead>
        <tbody>{byRegime.map((r) => <tr key={r.regime as string}><td>{r.regime as string}</td><td className="num">{r.n as number}</td><td className="num">{(((r.wins as number) / (r.n as number)) * 100).toFixed(0)}%</td><td className={`num ${tone(r.net as number)}`}>{signedUsd(r.net as number)}</td></tr>)}</tbody></table></div>}

      <h2>What it has learned</h2>
      {insights.length ? <ul>{insights.map((t, i) => <li key={i}>{t}</li>)}</ul> : <Empty>No lessons yet — they appear after the first completed trades (learning runs 05:00–23:59 local time).</Empty>}
      {blocked.length > 0 && <div className="note">Blocked for negative expectancy after costs: {blocked.map((b) => `${b[0]} ${b[1] > 0 ? "long" : "short"} ${b[2]} (${b[3].toFixed(3)}% over ${b[4]})`).join("; ")}.</div>}
      {policy?.policy && Object.keys(policy.policy).length > 0 && <div className="note">Learned exit multipliers (walk-forward, applied to new trades): {Object.entries(policy.policy).map(([k, v]) => `${k}: stop ${Number(v.stop_mult).toFixed(1)}× target ${Number(v.tp_mult).toFixed(1)}× trail ${Number(v.trail_mult).toFixed(1)}×`).join(" · ")}</div>}
      {retrain.length > 0 && <p className="muted" style={{ fontSize: 12 }}>Model: last check {ago(retrain[retrain.length - 1].at)} — {retrain[retrain.length - 1].decision}: {retrain[retrain.length - 1].reason}</p>}
      {lessons.length > 0 && <div className="scroll"><table><thead><tr><th>When</th><th>Verdict</th><th>Why the trade worked or failed</th></tr></thead>
        <tbody>{lessons.map((l, i) => <tr key={i}><td>{when(l.created_at as Date)}</td><td><Pill kind={l.verdict === "win" ? "BUY" : "SELL"}>{l.verdict as string}</Pill></td><td>{l.lesson as string}</td></tr>)}</tbody></table></div>}

      <h2>Open positions</h2>
      {!open.length ? <Empty>Nothing open.</Empty> : <div className="scroll"><table>
        <thead><tr><th>Opened</th><th>Coin</th><th>Side</th><th className="num">Entry</th><th className="num">Stop</th><th className="num">Target</th><th>Trail</th><th className="num">Live</th><th className="num">MFE</th><th className="num">MAE</th><th>Setup</th></tr></thead>
        <tbody>{open.map((t) => <tr key={t.id as number}><td>{when(t.opened_at as Date)}</td><td><b>{t.symbol as string}</b></td><td><Pill kind={t.direction === "long" ? "BUY" : "SELL"}>{t.direction as string}</Pill></td>
          <td className="num">{price(t.entry_price as number)}</td><td className="num">{price(t.stop_px as number)}</td><td className="num">{price(t.take_profit_px as number)}</td><td>{t.trail_active ? "armed" : "—"}</td>
          <td className="num">{price(mid(t.symbol as string))}</td><td className="num pos">{pctPts(num(t.mfe_pct))}</td><td className="num neg">{pctPts(num(t.mae_pct))}</td><td className="muted">{t.setup as string} · {t.regime as string}</td></tr>)}</tbody></table></div>}

      <h2>Recent completed trades</h2>
      {!recent.length ? <Empty>No completed trades yet. The scalper only trades when the model's predicted net edge clears costs and every risk gate.</Empty> : <div className="scroll"><table>
        <thead><tr><th>Closed</th><th>Coin</th><th>Side</th><th className="num">Min</th><th className="num">Entry</th><th className="num">Exit</th><th className="num">Stop</th><th className="num">Target</th><th>Exit reason</th>
          <th className="num">MFE</th><th className="num">MAE</th><th className="num">Fees</th><th className="num">Slip</th><th className="num">Gross</th><th className="num">Net</th><th>Setup / regime</th></tr></thead>
        <tbody>{recent.map((t) => <tr key={t.id as number}><td>{when(t.closed_at as Date)}</td><td><b>{t.symbol as string}</b></td><td>{t.direction as string}</td><td className="num">{num(t.duration_min)?.toFixed(1)}</td>
          <td className="num">{price(t.entry_price as number)}</td><td className="num">{price(t.exit_price as number)}</td><td className="num muted">{price(t.initial_stop_px as number)}</td><td className="num muted">{price(t.take_profit_px as number)}</td>
          <td>{(t.exit_reason as string)?.replace(/_/g, " ")}{t.trail_active ? " (trailed)" : ""}</td><td className="num pos">{pctPts(num(t.mfe_pct))}</td><td className="num neg">{pctPts(num(t.mae_pct))}</td>
          <td className="num">{usd(num(t.fees_usd))}</td><td className="num">{usd(num(t.slippage_usd))}</td><td className={`num ${tone(num(t.gross_pnl_usd))}`}>{signedUsd(num(t.gross_pnl_usd))}</td>
          <td className={`num ${tone(num(t.net_pnl_usd))}`}><b>{signedUsd(num(t.net_pnl_usd))}</b></td><td className="muted">{t.setup as string} · {t.regime as string}</td></tr>)}</tbody></table></div>}
    </>
  );
}
