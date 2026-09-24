// Accuracy/performance tracking for the AI autopilot's own trades on the manual paper account. Pure, no I/O.
// "Closed" = a sale the autopilot itself made (exit_reason = 'autopilot_sell'); manual sells of an autopilot-bought
// coin aren't counted here since that's the user's own decision, not the AI's.
export type AutoTradeRow = {
  status: string; exit_reason: string | null; pnl_usd: number | null; pnl_pct: number | null;
  ai_advice: { source?: string } | null;
};
export type AutoStats = {
  closed: number; wins: number; winRate: number | null; avgPnlPct: number | null; realized: number; openPositions: number;
};

export function summarizeAutopilot(rows: AutoTradeRow[]): AutoStats {
  const closedRows = rows.filter((r) => r.exit_reason === "autopilot_sell");
  const wins = closedRows.filter((r) => (r.pnl_usd ?? 0) > 0).length;
  const realized = closedRows.reduce((a, r) => a + (r.pnl_usd ?? 0), 0);
  const avgPnlPct = closedRows.length ? closedRows.reduce((a, r) => a + (r.pnl_pct ?? 0), 0) / closedRows.length : null;
  const openPositions = rows.filter((r) => r.status === "open" && r.ai_advice?.source === "autopilot").length;
  return { closed: closedRows.length, wins, winRate: closedRows.length ? wins / closedRows.length : null, avgPnlPct, realized, openPositions };
}
