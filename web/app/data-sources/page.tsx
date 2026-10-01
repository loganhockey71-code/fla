import { safe, sql } from "@/lib/db";
import { ago, when } from "@/lib/format";
import { Empty, Pill, SetupError } from "@/components/Ui";
import { DATA_SOURCES } from "@/lib/datasources";
import { FEATURES } from "@/lib/trainingFeatures";

export const dynamic = "force-dynamic";

export default async function DataSourcesPage() {
  const { data, error } = await safe(async () => {
    const db = sql();
    return db`select category, adapter, source, day, rows, collected_at, last_error from datalake_manifest order by adapter, day desc`.catch(() => []);
  });
  if (error) return <><h1>Data Sources</h1><SetupError error={error} /></>;
  const rows = data || [];

  type ManifestRow = { category: unknown; adapter: unknown; source: unknown; day: unknown; rows: unknown; collected_at: unknown; last_error: unknown };
  const byAdapter = new Map<string, ManifestRow[]>();
  for (const r of rows as ManifestRow[]) {
    const key = r.adapter as string;
    const list = byAdapter.get(key) ?? [];
    list.push(r);
    byAdapter.set(key, list);
  }

  return (
    <>
      <h1>Data Sources</h1>
      <p className="sub">
        Free-only data collection (see <code>worker/DATA_SOURCES.md</code>). Raw data lives in local/object-storage Parquet
        files, not this database — this page reads only the small per-adapter-per-day manifest rows. Run{" "}
        <code>python -m crypto_ai.cli collect-datasources</code> on the worker to populate this.
      </p>
      {!rows.length && <Empty>No manifest rows yet. Run <code>collect-datasources</code> at least once, and make sure <code>supabase/schema_datalake.sql</code> has been applied.</Empty>}
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Source</th><th>Category</th><th>Symbols</th><th>Key required</th><th>Last successful fetch</th><th className="num">Records (today)</th><th>Latest data timestamp</th><th>Status</th></tr>
          </thead>
          <tbody>
            {DATA_SOURCES.map((s) => {
              const history = byAdapter.get(s.name) ?? [];
              const latest = history[0];
              const lastOk = history.find((r) => !r.last_error);
              const status = !history.length ? "never run" : latest?.last_error ? "error" : "ok";
              return (
                <tr key={s.name}>
                  <td><b>{s.name}</b><div className="muted" style={{ fontSize: 12 }}>{s.source}</div></td>
                  <td>{s.category}</td>
                  <td>{s.symbols.join(", ")}</td>
                  <td>{s.keyRequired ? <span className="muted">{s.keyRequired}</span> : <Pill kind="BUY">none</Pill>}</td>
                  <td>{lastOk ? ago(lastOk.collected_at as Date) : "never"}</td>
                  <td className="num">{(latest?.rows as number) ?? "—"}</td>
                  <td>{lastOk ? when(lastOk.day as Date) : "—"}</td>
                  <td>
                    {status === "ok" && <Pill kind="BUY">ok</Pill>}
                    {status === "error" && <Pill kind="SELL">error</Pill>}
                    {status === "never run" && <Pill kind="HOLD">never run</Pill>}
                    {!!latest?.last_error && <div className="muted" style={{ fontSize: 12, maxWidth: 240 }}>{(latest!.last_error as string).slice(0, 140)}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <h2>What training actually uses</h2>
      <p className="sub">
        The live scalper model (<code>worker/crypto_ai/scalp/model.py</code>) trains ONLY on the features below, from
        Coinbase 1-minute candles collected by the worker&apos;s own <code>tick</code>/<code>ws-collect</code> loop. None of the
        newly-collected sources on this page feed the production model yet — per project policy, a new feature is only
        added to this list after it demonstrates a real, out-of-sample, walk-forward net-P&amp;L improvement (see
        <code> worker/EDGE_RESEARCH.md</code> and <code>worker/crypto_ai/scalp/research.py</code>). Adding a data source here is
        step one of a research pipeline, not an automatic model change.
      </p>
      <div className="scroll">
        <table>
          <thead><tr><th>Feature</th><th>Used by production model</th></tr></thead>
          <tbody>{FEATURES.map((f) => <tr key={f}><td><code>{f}</code></td><td><Pill kind="BUY">yes</Pill></td></tr>)}</tbody>
        </table>
      </div>
    </>
  );
}
