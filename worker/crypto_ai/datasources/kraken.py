"""Kraken public REST API. Free, no key, no auth (verified live 2026-09). Covers OHLC, recent trades, bid/ask
spread history, and order-book depth for BTC/ETH/XRP."""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://api.kraken.com/0/public"
PAIR = {"BTC": "XBTUSD", "ETH": "ETHUSD", "XRP": "XRPUSD"}


def _result_key(j: dict, pair: str):
    """Kraken echoes back its own internal pair name (e.g. XXBTZUSD for XBTUSD) as the result dict's only key."""
    r = j.get("result", {})
    for k in r:
        if k != "last" and k != "since":
            return r[k]
    return None


def fetch_ohlc(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{BASE}/OHLC", {"pair": PAIR[s], "interval": "1"})
        if j.get("error"):
            continue
        for c in _result_key(j, PAIR[s]) or []:
            rows.append({"symbol": s, "ts": pd.to_datetime(int(c[0]), unit="s", utc=True), "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]), "vwap": float(c[5]), "volume": float(c[6]), "trades": int(c[7]), "collected_at": now})
    return pd.DataFrame(rows)


def fetch_trades(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{BASE}/Trades", {"pair": PAIR[s]})
        if j.get("error"):
            continue
        for t in _result_key(j, PAIR[s]) or []:
            rows.append({"symbol": s, "ts": pd.to_datetime(float(t[2]), unit="s", utc=True), "price": float(t[0]), "size": float(t[1]),
                        "aggressor": "buy" if t[3] == "b" else "sell", "order_type": t[4], "collected_at": now})
    return pd.DataFrame(rows)


def fetch_spread(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{BASE}/Spread", {"pair": PAIR[s]})
        if j.get("error"):
            continue
        for row in (_result_key(j, PAIR[s]) or [])[-20:]:
            bid, ask = float(row[1]), float(row[2])
            mid = (bid + ask) / 2
            rows.append({"symbol": s, "ts": pd.to_datetime(int(row[0]), unit="s", utc=True), "bid": bid, "ask": ask,
                        "spread_pct": (ask - bid) / mid * 100 if mid else None, "collected_at": now})
    return pd.DataFrame(rows)


def fetch_depth(now: datetime, symbols=("BTC", "ETH", "XRP"), count: int = 20) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{BASE}/Depth", {"pair": PAIR[s], "count": str(count)})
        if j.get("error"):
            continue
        d = _result_key(j, PAIR[s])
        if d:
            rows.append({"symbol": s, "ts": now, "bids": [[b[0], b[1]] for b in d["bids"]], "asks": [[a[0], a[1]] for a in d["asks"]], "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("kraken_ohlc", "ohlcv", "Kraken public API", ["BTC", "ETH", "XRP"], fetch_ohlc, poll_every_s=60),
    Adapter("kraken_trades", "tick_trades", "Kraken public API", ["BTC", "ETH", "XRP"], fetch_trades, poll_every_s=30),
    Adapter("kraken_spread", "bid_ask", "Kraken public API", ["BTC", "ETH", "XRP"], fetch_spread, poll_every_s=15),
    Adapter("kraken_depth", "order_books_l2", "Kraken public API", ["BTC", "ETH", "XRP"], fetch_depth, poll_every_s=30),
]
