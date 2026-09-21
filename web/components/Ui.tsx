import { ReactNode } from "react";

export const Pill = ({ kind, children }: { kind: string; children: ReactNode }) => <span className={`pill ${kind}`}>{children}</span>;

export function Stat({ label, value, cls = "", sub }: { label: string; value: ReactNode; cls?: string; sub?: ReactNode }) {
  return (
    <div className="card stat">
      <div className={`v ${cls}`}>{value}</div>
      <div className="l">{label}</div>
      {sub && <div className="l">{sub}</div>}
    </div>
  );
}

export function SetupError({ error }: { error: string }) {
  return (
    <div className="note err">
      <b>Can't reach the database.</b> {error}
      <br />
      Check that <code>DATABASE_URL</code> is set (see <code>.env.example</code>) and that you ran <code>supabase/schema.sql</code> in the Supabase SQL editor.
    </div>
  );
}

export const Empty = ({ children }: { children: ReactNode }) => <div className="note">{children}</div>;
