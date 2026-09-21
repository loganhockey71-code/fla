import { safe, sql } from "@/lib/db";
import { ACTION_HELP, COINS } from "@/lib/data";
import { pctPts, price, when } from "@/lib/format";
import { Empty, Pill, SetupError } from "@/components/Ui";

export const dynamic = "force-dynamic";
const URG = { high: "bad", medium: "warn", low: "muted" } as const;
const KIND: Record<string, string> = { price: "price move", volume: "volume spike", orderbook: "order book", news: "major news", scheduled: "regular model read" };

export default async function Signals({ searchParams }: { searchParams: { coin?: string; all?: string } }) {
  const coin = COINS.includes((searchParams.coin ?? "") as never) ? searchParams.coin! : "";
  const onlyEvents = searchParams.all !== "1";
  const { data, error } = await safe(() => sql()`
    select * from live_signals where (${coin} = '' or symbol = ${coin}) and (${!onlyEvents} or trigger_kind <> 'scheduled')
    order by created_at desc limit 150`);
  if (error || !data) return <><h1>Signals</h1><SetupError error={error ?? "unknown"} /></>;

  return (
    <>
      <h1>Real-time signals</h1>
      <p className="sub">When a coin moves fast, volume spikes, or major news lands, the system investigates why, refreshes the model read, and says what to do now. Paper trading only.</p>
      <div className="grid g4" style={{ marginBottom: 12 }}>
        {Object.entries(ACTION_HELP).map(([a, t]) => <div className="card" key={a}><Pill kind={a}>{a}</Pill><div className="why" style={{ marginTop: 8 }}>{t}</div></div>)}
      </div>
      <div className="note">
        <b>How it decides.</b> Each shock is scored from: how far and fast the price moved, volume vs normal, order-book pressure, whether the whole market or just this coin moved, a fresh model read, and any major official or media news. Every term is listed in the row's "why".
        Falls count fully and rallies count half (chasing spikes is risky). Severe negative official news (importance 85+) triggers SELL on its own. These rules are <b>heuristics and unproven</b>; each signal stores its price so results can be measured later. The logged predictions are never changed by this system.
      </div>
      <div className="tabs">
        <a href={`/signals?${onlyEvents ? "" : "all=1"}`} className="on">{onlyEvents ? "Sudden events only" : "Including regular reads"}</a>
        <a href={`/signals?${onlyEvents ? "all=1" : ""}`}>{onlyEvents ? "Show regular model reads too" : "Show sudden events only"}</a>
      </div>
      {!data.length ? <Empty>No sudden events yet. That is normal in a calm market. The watcher checks every run (run <code>python -m crypto_ai.cli watch</code> for near-real-time).</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>When (UTC)</th><th>Coin</th><th>Trigger</th><th>Do now</th><th>Urgency</th><th className="num">Move</th><th className="num">Volume</th><th className="num">Model read</th><th>Why / likely cause</th></tr></thead>
          <tbody>{data.map((g) => (
            <tr key={g.id}>
              <td>{when(g.created_at)}</td><td><b>{g.symbol}</b><div className="why">{price(g.price)}</div></td><td>{KIND[g.trigger_kind]}</td>
              <td><Pill kind={g.action}>{g.action}</Pill>{g.previous_action && g.previous_action !== g.action && <div className="why">was {g.previous_action}</div>}</td>
              <td><Pill kind={URG[g.urgency as keyof typeof URG]}>{g.urgency}</Pill></td>
              <td className={`num ${g.move_pct < 0 ? "neg" : "pos"}`}>{g.move_pct != null ? `${pctPts(g.move_pct, 1)} / ${g.move_minutes}m` : "—"}</td>
              <td className="num">{g.volume_spike ? `${Number(g.volume_spike).toFixed(1)}x` : "—"}</td>
              <td className="num">{g.model_bull_prob != null ? `${Math.round(g.model_bull_prob * 100)}% up` : "—"}</td>
              <td style={{ maxWidth: 520 }}>{g.explanation}{g.cause_confidence != null && <div className="why">confidence in the explanation {g.cause_confidence}% · scope {g.scope ?? "n/a"}</div>}</td>
            </tr>))}</tbody></table></div>
      )}
    </>
  );
}
