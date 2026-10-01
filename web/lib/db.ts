import postgres from "postgres";
import { mergeSettings } from "./settings.ts";

const g = globalThis as unknown as { __sql?: ReturnType<typeof postgres> };

export function sql() {
  if (!process.env.DATABASE_URL) throw new Error("DATABASE_URL is not set");
  // Use Supabase's SESSION pooler (port 5432) for the dashboard: the transaction pooler (6543) stalls this driver under
  // concurrent queries. prepare:false stays on so either string at least connects.
  const local = /@(localhost|127\.0\.0\.1)[:/]/.test(process.env.DATABASE_URL);
  return (g.__sql ??= postgres(process.env.DATABASE_URL, { ssl: local ? false : "require", max: Number(process.env.DB_POOL_MAX ?? 3), prepare: false, idle_timeout: 20, connect_timeout: 15 }));
}

/** Run a page's data loading; on failure show a setup hint instead of crashing. */
export async function safe<T>(fn: () => Promise<T>): Promise<{ data?: T; error?: string }> {
  try {
    return { data: await fn() };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

export { asBool, EDITABLE, mergeSettings } from "./settings.ts";
export type { Settings } from "./settings.ts";

export async function getSettings() {
  return mergeSettings((await sql()`select key, value from settings`) as unknown as { key: string; value: unknown }[]);
}
