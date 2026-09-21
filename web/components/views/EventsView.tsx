import Link from "next/link";
import { safe, sql } from "@/lib/db";
import { when } from "@/lib/format";
import { Empty, Pill, SetupError } from "@/components/Ui";


const CATS = ["regulation", "legislation", "fed", "interest_rates", "inflation", "ETF", "lawsuit", "enforcement", "exchange", "hack",
  "whale_activity", "partnership", "token_or_network_update", "macro", "adoption", "other"];
const TIER: Record<number, string> = { 1: "government", 2: "project", 3: "financial news", 4: "crypto media", 5: "prediction market", 6: "social" };
type Q = { coin?: string; cat?: string; min?: string; kind?: string; view?: string };

const impact = (n: number) => <span className={n > 0 ? "pos" : n < 0 ? "neg" : "muted"}>{n > 0 ? "+" : ""}{n}</span>;

export default async function EventsView({ searchParams }: { searchParams: Q }) {
  const coin = ["BTC", "ETH", "XRP"].includes(searchParams.coin ?? "") ? searchParams.coin! : "";
  const cat = CATS.includes(searchParams.cat ?? "") ? searchParams.cat! : "";
  const min = Math.max(0, Math.min(100, Number(searchParams.min) || 0));
  const kind = ["news", "legislation", "macro", "prediction_market"].includes(searchParams.kind ?? "") ? searchParams.kind! : "";
  const moves = searchParams.view === "moves";

  const { data, error } = await safe(async () => {
    const db = sql();
    if (moves) return { moves: await db`select * from events where kind='sudden_move' order by occurred_at desc limit 100`, rows: [], sources: [] };
    const [rows, sources] = await Promise.all([
      db`select * from research_events
         where (${coin} = '' or ${coin} = any(affected_coins)) and (${cat} = '' or event_category = ${cat})
           and importance_score >= ${min} and (${kind} = '' or kind = ${kind})
         order by coalesce(published_at, detected_at) desc, importance_score desc limit 150`,
      db`select key, name, tier, tier_label, credibility_score, poll_interval_s, last_polled_at, last_status, last_error, items_last_run from source_registry order by tier, key`,
    ]);
    return { moves: [], rows, sources };
  });
  if (error || !data) return <><h2 className="viewtitle">Events / Research</h2><SetupError error={error ?? "unknown"} /></>;
  const href = (o: Partial<Q>) => {
    const p = new URLSearchParams(Object.entries({ coin, cat, min: min ? String(min) : "", kind, ...o }).filter(([, v]) => v) as [string, string][]);
    return `/news${p.size ? "?" + p : ""}`;
  };

  return (
    <>
      <h2 className="viewtitle">Events / Research</h2>
      <p className="sub">Official sources, project announcements, news, Congress.gov, FRED and prediction markets, scored for BTC/ETH/XRP. Copies of one story are grouped into a single event; prediction markets are a feature, never a trade signal.</p>

      <div className="tabs">
        <Link href={href({ coin: "" })} className={!moves && !coin ? "on" : ""}>All coins</Link>
        {["BTC", "ETH", "XRP"].map((c) => <Link key={c} href={href({ coin: c })} className={!moves && coin === c ? "on" : ""}>{c}</Link>)}
        <span style={{ width: 12 }} />
        {[0, 40, 60, 75].map((m) => <Link key={m} href={href({ min: m ? String(m) : "" })} className={!moves && min === m ? "on" : ""}>{m ? `importance ≥ ${m}` : "any importance"}</Link>)}
        <span style={{ width: 12 }} />
        <Link href="/news?view=moves" className={moves ? "on" : ""}>Sudden moves</Link>
      </div>
      {!moves && (
        <div className="tabs">
          <Link href={href({ cat: "" })} className={!cat ? "on" : ""}>all categories</Link>
          {CATS.map((c) => <Link key={c} href={href({ cat: c })} className={cat === c ? "on" : ""}>{c.replace(/_/g, " ")}</Link>)}
        </div>
      )}
      {!moves && (
        <div className="tabs">
          <Link href={href({ kind: "" })} className={!kind ? "on" : ""}>all sources</Link>
          {[["news", "news & official"], ["legislation", "Congress"], ["macro", "FRED macro"], ["prediction_market", "prediction markets"]].map(([k, l]) =>
            <Link key={k} href={href({ kind: k })} className={kind === k ? "on" : ""}>{l}</Link>)}
        </div>
      )}

      {moves ? (
        !data.moves.length ? <Empty>No sudden moves detected yet.</Empty> : (
          <div className="scroll"><table><thead><tr><th>When</th><th>Move</th><th>Coin</th><th className="num">Confidence</th></tr></thead>
            <tbody>{data.moves.map((e) => (
              <tr key={e.id}><td>{when(e.occurred_at)}</td><td><b>{e.title}</b><div className="why">{e.explanation}</div></td><td>{(e.coins as string[]).join(", ")}</td><td className="num">{e.confidence}%</td></tr>))}</tbody></table></div>)
      ) : !data.rows.length ? <Empty>No events match. The scheduled research job fills this in — run <code>python -m crypto_ai.cli research --force</code> to poll every source now.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Published</th><th>Event</th><th>Source</th><th>Category</th><th>Impact</th><th className="num">Importance</th><th className="num">Confidence</th><th className="num">P(event)</th><th className="num" title="expected move, -100…+100">BTC</th><th className="num">ETH</th><th className="num">XRP</th></tr></thead>
          <tbody>{data.rows.map((e) => (
            <tr key={e.id}>
              <td>{when(e.published_at ?? e.detected_at)}</td>
              <td style={{ maxWidth: 480 }}>
                {e.source_url ? <a href={e.source_url} target="_blank" rel="noreferrer">{e.title}</a> : e.title}
                <div className="why">{(e.affected_coins as string[]).join(" · ")}{e.duplicate_count > 0 && ` · ${e.duplicate_count} copies folded in (${e.independent_confirmations} independent confirmation${e.independent_confirmations > 1 ? "s" : ""})`}
                  {` · novelty ${e.novelty_score}`}</div>
              </td>
              <td>{e.source}<div><Pill kind={e.origin_tier <= 2 ? "official" : e.origin_tier === 5 ? "warn" : "media"}>{TIER[e.origin_tier ?? 4]} · {e.source_credibility_score}</Pill></div></td>
              <td>{String(e.event_category).replace(/_/g, " ")}</td>
              <td><Pill kind={e.sentiment}>{e.sentiment}</Pill></td>
              <td className="num">{e.importance_score}</td><td className="num">{e.confidence_score}%</td>
              <td className="num">{e.event_probability != null ? `${Math.round(e.event_probability * 100)}%` : "—"}<div className="why">{e.event_probability_source}</div></td>
              <td className="num">{impact(e.btc_impact_score)}</td><td className="num">{impact(e.eth_impact_score)}</td><td className="num">{impact(e.xrp_impact_score)}</td>
            </tr>))}</tbody></table></div>
      )}

      {!moves && data.sources.length > 0 && (
        <>
          <h2>Source health</h2>
          <div className="scroll"><table>
            <thead><tr><th>Source</th><th>Trust tier</th><th className="num">Every</th><th>Last poll</th><th>Status</th></tr></thead>
            <tbody>{data.sources.map((s) => (
              <tr key={s.key}><td>{s.name}</td><td>{s.tier}. {String(s.tier_label).replace(/_/g, " ")} ({s.credibility_score})</td>
                <td className="num">{s.poll_interval_s >= 3600 ? `${s.poll_interval_s / 3600}h` : `${s.poll_interval_s / 60}m`}</td><td>{s.last_polled_at ? when(s.last_polled_at) : "never"}</td>
                <td>{s.last_status === "error" ? <span className="neg" title={s.last_error}>error: {s.last_error}</span> : s.last_status ? <span className="pos">{s.last_status}</span> : <span className="muted">—</span>}</td></tr>))}</tbody></table></div>
          <p className="muted" style={{ marginTop: 8 }}>Trust tiers: 1 official government · 2 official project · 3 major financial news · 4 crypto media · 5 prediction market · 6 social/unverified (never ingested). Impact = expected move −100…+100, rule-based, not a forecast.</p>
        </>
      )}
    </>
  );
}
