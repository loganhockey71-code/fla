"""Keeps the microstructure tables bounded: rolls raw book_ticks/trade_ticks older than their retention window into
`micro_bars_1s` (one row per symbol per second that had activity), then deletes the raw rows that were compacted.
depth_snapshots has its own, longer retention and is simply trimmed (it's already small and coarse).

`build_1s_bars` is pure (DataFrames in, DataFrame out) so the aggregation is unit tested without a database.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd

DEFAULT_RAW_HOURS = 48
DEFAULT_DEPTH_DAYS = 7


def build_1s_bars(quotes: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """quotes: columns [symbol, ts, mid, spread_pct]. trades: columns [symbol, ts, price, size, aggressor].
    Returns one row per (symbol, second) that had a quote or a trade - never a dense per-second grid."""
    parts = []
    symbols = set(quotes["symbol"]) | set(trades["symbol"]) if len(quotes) or len(trades) else set()
    for sym in symbols:
        q = quotes[quotes.symbol == sym].sort_values("ts") if len(quotes) else quotes
        t = trades[trades.symbol == sym].sort_values("ts") if len(trades) else trades
        qsec = q["ts"].dt.floor("s") if len(q) else pd.Series([], dtype="datetime64[ns, UTC]")
        tsec = t["ts"].dt.floor("s") if len(t) else pd.Series([], dtype="datetime64[ns, UTC]")
        secs = sorted(set(qsec) | set(tsec))
        if not secs:
            continue
        qg = q.groupby(qsec) if len(q) else None
        tg = t.groupby(tsec) if len(t) else None
        rows = []
        for sec in secs:
            qrow = qg.get_group(sec) if qg is not None and sec in qg.groups else None
            trow = tg.get_group(sec) if tg is not None and sec in tg.groups else None
            mid = qrow["mid"] if qrow is not None else pd.Series([], dtype=float)
            buy = trow[trow.aggressor == "buy"]["size"].sum() if trow is not None else 0.0
            sell = trow[trow.aggressor == "sell"]["size"].sum() if trow is not None else 0.0
            vwap = (trow["price"] * trow["size"]).sum() / trow["size"].sum() if trow is not None and trow["size"].sum() > 0 else None
            rows.append({
                "symbol": sym, "ts": sec,
                "mid_open": float(mid.iloc[0]) if len(mid) else None, "mid_high": float(mid.max()) if len(mid) else None,
                "mid_low": float(mid.min()) if len(mid) else None, "mid_close": float(mid.iloc[-1]) if len(mid) else None,
                "spread_avg": float(qrow["spread_pct"].mean()) if qrow is not None else None,
                "spread_max": float(qrow["spread_pct"].max()) if qrow is not None else None,
                "n_quotes": int(len(qrow)) if qrow is not None else 0, "n_trades": int(len(trow)) if trow is not None else 0,
                "buy_volume": float(buy), "sell_volume": float(sell), "vwap": float(vwap) if vwap is not None else None,
            })
        parts.append(pd.DataFrame(rows))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["symbol", "ts", "mid_open", "mid_high", "mid_low", "mid_close",
                                                                                    "spread_avg", "spread_max", "n_quotes", "n_trades", "buy_volume", "sell_volume", "vwap"])


# ---------------------------------------------------------------- database wrapper
def compact_due(db, cfg: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    raw_cut = now - timedelta(hours=cfg.get("micro_raw_retention_hours", DEFAULT_RAW_HOURS))
    depth_cut = now - timedelta(days=cfg.get("micro_depth_retention_days", DEFAULT_DEPTH_DAYS))
    qrows = db.all("select symbol, ts, mid, spread_pct from book_ticks where ts < %s", [raw_cut])
    trows = db.all("select symbol, ts, price, size, aggressor from trade_ticks where ts < %s", [raw_cut])
    n_bars = 0
    if qrows or trows:
        q = pd.DataFrame(qrows) if qrows else pd.DataFrame(columns=["symbol", "ts", "mid", "spread_pct"])
        t = pd.DataFrame(trows) if trows else pd.DataFrame(columns=["symbol", "ts", "price", "size", "aggressor"])
        for df in (q, t):
            if len(df):
                df["ts"] = pd.to_datetime(df["ts"], utc=True)
        bars = build_1s_bars(q, t)
        if len(bars):
            db.bulk("""insert into micro_bars_1s (symbol, ts, mid_open, mid_high, mid_low, mid_close, spread_avg, spread_max, n_quotes, n_trades, buy_volume, sell_volume, vwap)
                       values %s on conflict (symbol, ts) do update set mid_open=excluded.mid_open, mid_high=excluded.mid_high, mid_low=excluded.mid_low,
                       mid_close=excluded.mid_close, spread_avg=excluded.spread_avg, spread_max=excluded.spread_max, n_quotes=excluded.n_quotes,
                       n_trades=excluded.n_trades, buy_volume=excluded.buy_volume, sell_volume=excluded.sell_volume, vwap=excluded.vwap""",
                    [tuple(None if pd.isna(v) else v for v in r) for r in bars[["symbol", "ts", "mid_open", "mid_high", "mid_low", "mid_close",
                     "spread_avg", "spread_max", "n_quotes", "n_trades", "buy_volume", "sell_volume", "vwap"]].itertuples(index=False)])
            n_bars = len(bars)
        db.run("delete from book_ticks where ts < %s", [raw_cut])
        db.run("delete from trade_ticks where ts < %s", [raw_cut])
    n_depth = db.one("select count(*) n from depth_snapshots where ts < %s", [depth_cut])["n"]
    db.run("delete from depth_snapshots where ts < %s", [depth_cut])
    return {"bars_written": n_bars, "quote_rows_compacted": len(qrows), "trade_rows_compacted": len(trows), "depth_rows_purged": n_depth}
