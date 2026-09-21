import { notFound } from "next/navigation";
import { safe, sql } from "@/lib/db";
import { latestPrices } from "@/lib/data";
import { price, pct, pctPts, when } from "@/lib/format";
import { Empty, Pill, SetupError } from "@/components/Ui";
import { TimeChart } from "@/components/Charts";

export const dynamic = "force-dynamic";

export default async function CoinPage({ params }: { params: { coin: string } }) {
  const sym = params.coin.toUpperCase();
  if (!["BTC", "ETH", "XRP"].includes(sym)) notFound();

  const { data, error } = await safe(async () => {
    const db = sql();
    const [px, preds, recent, evs, candles] = await Promise.all([
      latestPrices(),
      db`select distinct on (horizon_h) * from predictions where symbol=${sym} and variant='market' order by horizon_h, created_at desc`,
      db`select p.*, r.actual_price, r.actual_return_pct, r.signal_correct from predictions p left join prediction_results r on r.prediction_id=p.id
         where p.symbol=${sym} and p.variant='market' order by p.created_at desc limit 12`,
      db`select id, coalesce(published_at, detected_at) as occurred_at, title, source, sentiment, importance_score as importance from research_events where ${sym} = any(affected_coins) order by detected_at desc limit 8`,
      db`select ts, close from candles where symbol=${sym} and granularity=900 order by ts desc limit 384`,
    ]);
    return { px: px[sym], preds, recent, evs, candles: candles.reverse() };
  });
  if (error || !data) return <><h1>{sym}</h1><SetupError error={error ?? "unknown"} /></>;
  const { px, preds, recent, evs, candles } = data;

  return (
    <>
      <h1>{sym} <span className="muted" style={{ fontWeight: 400 }}>{price(px?.price)}</span></h1>
      <p className="sub">{px ? `Live price ${when(px.ts)} · spread ${px.spread_pct?.toFixed(3) ?? "—"}%` : "No market data collected yet."}</p>

      <div className="card"><h3>Last 4 days (15-minute candles)</h3>
        <TimeChart data={candles.map((c) => ({ t: (c.ts as Date).toISOString(), v: c.close as number }))} />
      </div>

      <h2>Current predictions</h2>
      <div className="grid g2">
        {[24, 48].map((h) => {
          const p = preds.find((x) => x.horizon_h === h);
          if (!p) return <Empty key={h}>No {h}h prediction yet.</Empty>;
          return (
            <div className="card" key={h}>
              <h3>Next {h} hours · made {when(p.created_at)}</h3>
              <div style={{ fontSize: 22, marginBottom: 8 }}><Pill kind={p.signal}>{p.signal}</Pill> <b>{Math.round(p.confidence * 100)}%</b> confidence</div>
              <div className="row"><span>Bullish / bearish</span><span><span className="pos">{pct(p.bullish_prob)}</span> / <span className="neg">{pct(p.bearish_prob)}</span></span></div>
              <div className="row"><span>Expected range (~68%)</span><span>{price(p.range_low)} – {price(p.range_high)}</span></div>
              <div className="row"><span>Price at prediction</span><span>{price(p.price_at_prediction)}</span></div>
              <div className="row"><span>Resolves</span><span>{when(p.target_time)}</span></div>
              <div className="row"><span>Model</span><span className="muted">{p.model_version}</span></div>
              <div className="why">{p.explanation}</div>
            </div>
          );
        })}
      </div>

      <h2>Recent {sym} predictions</h2>
      <div className="scroll"><table>
        <thead><tr><th>Made</th><th>Horizon</th><th>Signal</th><th className="num">Conf.</th><th className="num">Price</th><th className="num">Actual</th><th className="num">Move</th><th>Correct?</th></tr></thead>
        <tbody>{recent.map((r) => (
          <tr key={r.id}>
            <td>{when(r.created_at)}</td><td>{r.horizon_h}h</td><td><Pill kind={r.signal}>{r.signal}</Pill></td>
            <td className="num">{Math.round(r.confidence * 100)}%</td><td className="num">{price(r.price_at_prediction)}</td>
            <td className="num">{r.actual_price ? price(r.actual_price) : "pending"}</td>
            <td className={`num ${r.actual_return_pct > 0 ? "pos" : "neg"}`}>{r.actual_return_pct != null ? pctPts(r.actual_return_pct) : ""}</td>
            <td>{r.signal_correct == null ? "" : r.signal_correct ? <span className="pos">✓</span> : <span className="neg">✗</span>}</td>
          </tr>))}
        </tbody></table></div>

      <h2>Events affecting {sym}</h2>
      {!evs.length ? <Empty>No events yet.</Empty> : (
        <div className="scroll"><table><tbody>{evs.map((e) => (
          <tr key={e.id}><td>{when(e.occurred_at)}</td><td>{e.title}<div className="why">{e.source}</div></td>
            <td><Pill kind={e.sentiment}>{e.sentiment}</Pill></td><td className="num">imp {e.importance}</td></tr>))}</tbody></table></div>
      )}
    </>
  );
}
