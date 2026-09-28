"""Loads candle history from the database (topped up from Coinbase) for learning jobs."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import coinbase
from ..config import BAR, PRODUCTS, SYMBOLS


def load_frames(db, days: int = 400, topup: bool = False) -> dict[str, pd.DataFrame]:
    """Closed 15-minute candles per coin. topup=True fetches whatever is missing since the newest stored candle."""
    now = datetime.now(timezone.utc)
    frames = {}
    for s in SYMBOLS:
        rows = db.all("select ts, open, high, low, close, volume from candles where symbol=%s and granularity=%s and ts > %s order by ts",
                      [s, BAR, now - timedelta(days=days)])
        df = pd.DataFrame(rows)
        if len(df):
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df = df.set_index("ts").astype(float)
        else:
            df = coinbase.to_frame([])
        if topup:
            start = (df.index[-1].to_pydatetime() if len(df) else now - timedelta(days=days)) - timedelta(hours=1)
            new = coinbase.closed_only(coinbase.candles(PRODUCTS[s], BAR, start, now), BAR, now)
            if len(df) and df.index[0] > pd.Timestamp(now - timedelta(days=days - 2)):     # stored history is shorter than asked: backfill the start
                new = pd.concat([coinbase.candles(PRODUCTS[s], BAR, now - timedelta(days=days), df.index[0].to_pydatetime()), new])
            if len(new):
                db.bulk("insert into candles (symbol, granularity, ts, open, high, low, close, volume) values %s on conflict (symbol, granularity, ts) do nothing",
                        [(s, BAR, ts.to_pydatetime(), r.open, r.high, r.low, r.close, r.volume) for ts, r in new.iterrows()])
                df = pd.concat([df, new]).loc[lambda d: ~d.index.duplicated(keep="last")].sort_index()
        frames[s] = coinbase.closed_only(df, BAR, now)
    return frames
