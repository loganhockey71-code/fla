import Link from "next/link";
import { safe, sql } from "@/lib/db";
import { pct, pctPts, price, when } from "@/lib/format";
import { Empty, Pill, SetupError } from "@/components/Ui";


export default async function PredictionsView({ searchParams }: { searchParams: { coin?: string; h?: string; v?: string } }) {
  const coin = ["BTC", "ETH", "XRP"].includes(searchParams.coin ?? "") ? searchParams.coin! : "";
  const v = searchParams.v === "research" ? "research" : "market";
  const h = searchParams.h === "24" || searchParams.h === "48" ? Number(searchParams.h) : 0;
  const { data, error } = await safe(() => sql()`
    select p.*, r.actual_price, r.actual_return_pct, r.signal_correct, r.directional_correct, r.in_range
    from predictions p left join prediction_results r on r.prediction_id = p.id
    where p.variant = ${v} and (${coin} = '' or p.symbol = ${coin}) and (${h} = 0 or p.horizon_h = ${h})
    order by p.created_at desc limit 300`);
  if (error || !data) return <><h2 className="viewtitle">Predictions</h2><SetupError error={error ?? "unknown"} /></>;
  const link = (c: string, hh: number, vv = v) => `/signals?${new URLSearchParams({ tab: "predictions", ...(c ? { coin: c } : {}), ...(hh ? { h: String(hh) } : {}), v: vv })}`;

  return (
    <>
      <h2 className="viewtitle">Prediction history</h2>
      <p className="sub">Append-only: the database rejects any UPDATE or DELETE on predictions. Each row keeps the exact model version and the feature values it saw.</p>
      <div className="tabs">
        {["", "BTC", "ETH", "XRP"].map((c) => <Link key={c} href={link(c, h)} className={coin === c ? "on" : ""}>{c || "All coins"}</Link>)}
        <span style={{ width: 12 }} />
        {["market", "research"].map((x) => <Link key={x} href={link(coin, h, x)} className={v === x ? "on" : ""}>{x === "market" ? "Market-only model" : "Market + research (shadow)"}</Link>)}
        <span style={{ width: 12 }} />
        {[0, 24, 48].map((x) => <Link key={x} href={link(coin, x)} className={h === x ? "on" : ""}>{x ? `${x}h` : "Both horizons"}</Link>)}
      </div>
      {!data.length ? <Empty>No predictions yet.</Empty> : (
        <div className="scroll"><table>
          <thead><tr><th>Made (UTC)</th><th>Coin</th><th>Hz</th><th>Signal</th><th className="num">Bull / Bear</th><th className="num">Conf.</th><th className="num">Price</th><th className="num">Expected range</th><th>Model</th><th className="num">Actual</th><th className="num">Move</th><th>Result</th></tr></thead>
          <tbody>{data.map((p) => (
            <tr key={p.id} title={p.explanation}>
              <td>{when(p.created_at)}</td><td><b>{p.symbol}</b></td><td>{p.horizon_h}h</td><td><Pill kind={p.signal}>{p.signal}</Pill></td>
              <td className="num">{pct(p.bullish_prob)} / {pct(p.bearish_prob)}</td><td className="num">{Math.round(p.confidence * 100)}%</td>
              <td className="num">{price(p.price_at_prediction)}</td><td className="num">{price(p.range_low)} – {price(p.range_high)}</td>
              <td className="muted">{p.model_version}</td>
              <td className="num">{p.actual_price ? price(p.actual_price) : <span className="muted">resolves {when(p.target_time)}</span>}</td>
              <td className={`num ${p.actual_return_pct > 0 ? "pos" : "neg"}`}>{p.actual_return_pct != null ? pctPts(p.actual_return_pct) : ""}</td>
              <td>{p.signal_correct == null ? "" : p.signal_correct ? <span className="pos">correct</span> : <span className="neg">wrong</span>}
                {p.in_range === false && <span className="muted"> · out of range</span>}</td>
            </tr>))}</tbody></table></div>
      )}
      <p className="muted" style={{ marginTop: 14 }}>Correct = BUY and price rose, SELL and price fell, or HOLD and price stayed inside the hold band. Hover a row for the model's explanation.</p>
    </>
  );
}
