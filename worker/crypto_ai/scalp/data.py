"""1-minute candle access: Coinbase (public, no key) with a local cache for research runs, and the `candles` table
(granularity=60) for the live worker so every pass only fetches what it has not seen."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import coinbase
from ..config import PRODUCTS, SYMBOLS
from .features import LOOKBACK_BARS

GRAN = 60
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", ".cache")
ONE_MIN = pd.Timedelta(minutes=1)


def fill_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Coinbase omits minutes with no trades. Rebuild a continuous 1m index: a missing minute is flat at the previous
    close with zero volume, so rolling windows keep meaning 'N minutes'."""
    if df.empty:
        return df
    full = pd.date_range(df.index[0], df.index[-1], freq="1min", tz="UTC", name="ts")
    df = df.reindex(full)
    df["close"] = df["close"].ffill()
    for k in ("open", "high", "low"):
        df[k] = df[k].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0.0)
    return df


def fetch_cached(symbol: str, days: int, end: datetime | None = None, use_cache: bool = True) -> pd.DataFrame:
    """Closed 1m candles for the last `days` days ending at `end` (default now), extended incrementally in a local CSV cache."""
    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"candles_1m_{symbol}.csv.gz")
    cached = pd.DataFrame()
    if use_cache and os.path.exists(path):
        cached = pd.read_csv(path, index_col=0, parse_dates=True)
        cached.index = pd.DatetimeIndex(cached.index, tz="UTC") if cached.index.tz is None else cached.index
        cached.index.name = "ts"
    parts = [cached] if len(cached) else []
    have_from = cached.index[0] if len(cached) else None
    have_to = cached.index[-1] + ONE_MIN if len(cached) else None
    if have_from is None or have_from > pd.Timestamp(start) + pd.Timedelta(hours=1):
        parts.append(coinbase.candles(PRODUCTS[symbol], GRAN, start, have_from.to_pydatetime() if have_from is not None else end))
    if have_to is None or have_to < pd.Timestamp(end) - pd.Timedelta(minutes=2):
        parts.append(coinbase.candles(PRODUCTS[symbol], GRAN, (have_to.to_pydatetime() if have_to is not None else start), end))
    df = pd.concat([p for p in parts if len(p)]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = coinbase.closed_only(df, GRAN, end)
    if use_cache:
        df.to_csv(path, compression="gzip")
    return fill_gaps(df[df.index >= pd.Timestamp(start)])


def load_frames(days: int, end: datetime | None = None, symbols: list[str] | None = None, log=print) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols or SYMBOLS:
        log(f"[data] {s}: loading {days}d of 1m candles ...")
        out[s] = fetch_cached(s, days, end)
        log(f"       {len(out[s]):,} bars  {out[s].index[0]:%Y-%m-%d %H:%M} -> {out[s].index[-1]:%Y-%m-%d %H:%M} UTC")
    return out


# ---------------------------------------------------------------- live worker (database)
def save_candles(db, symbol: str, df: pd.DataFrame) -> None:
    rows = [(symbol, GRAN, ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume) for ts, r in df.iterrows()]
    db.bulk("insert into candles (symbol, granularity, ts, open, high, low, close, volume) values %s "
            "on conflict (symbol, granularity, ts) do nothing", rows)


def sync_live(db, symbol: str, now: datetime, bars: int = LOOKBACK_BARS) -> pd.DataFrame:
    """Fetch only the 1m candles missing from the database (all of them on first run), store them, and return the last
    `bars` closed candles as a gap-free frame. A worker that was late or down catches up here without losing a minute."""
    last = db.one("select max(ts) t from candles where symbol=%s and granularity=%s", [symbol, GRAN])["t"]
    want_from = now - timedelta(minutes=bars + 5)
    start = max(want_from, last + timedelta(minutes=1)) if last is not None else want_from
    if now - start > timedelta(seconds=90):
        try:
            fresh = coinbase.closed_only(coinbase.candles(PRODUCTS[symbol], GRAN, start, now), GRAN, now)
            if len(fresh):
                save_candles(db, symbol, fresh)
        except Exception as e:      # a Coinbase blip must not abort the pass: open trades still have to be managed from what is stored
            print(f"[scalp] {symbol}: candle fetch failed ({type(e).__name__}); using the candles already stored", file=sys.stderr)
    rows = db.all("select ts, open, high, low, close, volume from candles where symbol=%s and granularity=%s and ts >= %s order by ts",
                  [symbol, GRAN, now - timedelta(minutes=bars + 5)])
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows).set_index("ts")
    df.index = pd.DatetimeIndex(df.index)
    return coinbase.closed_only(fill_gaps(df.astype(float)), GRAN, now).iloc[-bars:]


def load_db_history(db, days: int, now: datetime | None = None) -> dict[str, pd.DataFrame]:
    """All stored 1m candles for the `days` days before `now` (used by the live retrain / exit-policy learning)."""
    out = {}
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    for s in SYMBOLS:
        rows = db.all("select ts, open, high, low, close, volume from candles where symbol=%s and granularity=%s and ts >= %s order by ts",
                      [s, GRAN, since])
        if rows:
            df = pd.DataFrame(rows).set_index("ts")
            df.index = pd.DatetimeIndex(df.index)
            out[s] = fill_gaps(df.astype(float))
    return out
