"""Free public Coinbase Exchange market-data endpoints (no API key, read-only)."""
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

BASE = "https://api.exchange.coinbase.com"
_S = requests.Session()
_S.headers["User-Agent"] = "crypto-ai-paper-lab/1.0"


def _get(path: str, params: dict | None = None, retries: int = 4):
    for i in range(retries):
        try:
            r = _S.get(BASE + path, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(1 + i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(0.5 * (i + 1))


def ticker(product: str) -> dict:
    j = _get(f"/products/{product}/ticker")
    bid, ask = float(j["bid"]), float(j["ask"])
    mid = (bid + ask) / 2
    return {
        "price": mid, "bid": bid, "ask": ask,
        "spread_pct": (ask - bid) / mid * 100,
        "volume_24h": float(j["volume"]),
    }


def order_book_imbalance(product: str, band_pct: float = 0.5) -> float | None:
    """(bid depth - ask depth) / total, within band_pct of mid. +1 = all bids."""
    j = _get(f"/products/{product}/book", {"level": 2})
    bids, asks = j["bids"], j["asks"]
    if not bids or not asks:
        return None
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2
    lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    b = sum(float(x[1]) for x in bids if float(x[0]) >= lo)
    a = sum(float(x[1]) for x in asks if float(x[0]) <= hi)
    return (b - a) / (b + a) if (a + b) > 0 else None


def buy_pressure(product: str, limit: int = 100) -> float | None:
    """Share of recent volume that was taker-BUY. Coinbase's `side` is the MAKER side,
    so a maker 'sell' hit by an aggressive buyer is a taker buy."""
    trades = _get(f"/products/{product}/trades", {"limit": limit})
    tot = sum(float(t["size"]) for t in trades)
    if tot <= 0:
        return None
    return sum(float(t["size"]) for t in trades if t["side"] == "sell") / tot


def _candles_page(product: str, gran: int, start: datetime, end: datetime) -> list:
    return _get(f"/products/{product}/candles", {
        "granularity": gran, "start": start.isoformat(), "end": end.isoformat()}) or []


def candles(product: str, gran: int, start: datetime, end: datetime | None = None) -> pd.DataFrame:
    """Candles between start and end (UTC). Index = candle OPEN time. May include the
    still-forming candle - callers must drop it with closed_only()."""
    end = end or datetime.now(timezone.utc)
    rows, cur = [], start
    step = timedelta(seconds=gran * 300)
    while cur < end:
        nxt = min(cur + step, end)
        rows += _candles_page(product, gran, cur, nxt)
        cur = nxt
        time.sleep(0.12)
    return to_frame(rows)


def to_frame(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="ts"))
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def closed_only(df: pd.DataFrame, gran: int, now: datetime | None = None) -> pd.DataFrame:
    """Drop the still-forming candle so nothing 'from the future' leaks into features."""
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    return df[df.index + pd.Timedelta(seconds=gran) <= now]


def price_at(product: str, when: datetime) -> float | None:
    """Real price at `when`: close of the last 1-minute candle that ENDED at or before `when`
    (so at most 59 s stale, and never uses data from after `when`)."""
    end = when.replace(second=0, microsecond=0)
    start = end - timedelta(minutes=1)
    df = to_frame(_candles_page(product, 60, start, end))
    df = df[df.index == pd.Timestamp(start)]
    return float(df["close"].iloc[0]) if len(df) else None


def price_at_safe(symbol: str, when: datetime) -> float | None:
    from .config import PRODUCTS
    try:
        return price_at(PRODUCTS[symbol], when)
    except Exception:
        return None
