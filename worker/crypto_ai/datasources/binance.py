"""Binance public REST API. Free, no key for market data - BUT confirmed LIVE (2026-09, this environment) to return
"Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms".
That is Binance's own Terms of Service blocking this environment's region/IP class, not a bug here - per the brief's
"do not scrape ... or violate ... terms of service" instruction, this adapter is written correctly (and unit tested
with mocked HTTP) but MUST NOT be assumed to work until you verify live from YOUR OWN deployment's IP; `Adapter.run()`
already reports a clean `ok: False` with the restriction message instead of crashing if it's blocked for you too.

Covers 1m candles, recent trades, aggregate trades, and futures funding rate + open interest for BTC/ETH/XRP.
"""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

SPOT_BASE = "https://api.binance.com/api/v3"
FAPI_BASE = "https://fapi.binance.com/fapi/v1"
SPOT_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "XRP": "XRPUSDT"}
PERP_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "XRP": "XRPUSDT"}


def fetch_candles_1m(now: datetime, symbols=("BTC", "ETH", "XRP"), limit: int = 5) -> pd.DataFrame:
    rows = []
    for s in symbols:
        for c in http_get(f"{SPOT_BASE}/klines", {"symbol": SPOT_SYM[s], "interval": "1m", "limit": str(limit)}):
            rows.append({"symbol": s, "ts": pd.to_datetime(int(c[0]), unit="ms", utc=True), "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]), "trades": int(c[8]), "collected_at": now})
    return pd.DataFrame(rows)


def fetch_trades(now: datetime, symbols=("BTC", "ETH", "XRP"), limit: int = 100) -> pd.DataFrame:
    rows = []
    for s in symbols:
        for t in http_get(f"{SPOT_BASE}/trades", {"symbol": SPOT_SYM[s], "limit": str(limit)}):
            rows.append({"symbol": s, "trade_id": t["id"], "ts": pd.to_datetime(int(t["time"]), unit="ms", utc=True),
                        "price": float(t["price"]), "size": float(t["qty"]), "aggressor": "sell" if t["isBuyerMaker"] else "buy", "collected_at": now})
    return pd.DataFrame(rows)


def fetch_agg_trades(now: datetime, symbols=("BTC", "ETH", "XRP"), limit: int = 100) -> pd.DataFrame:
    """Aggregate trades: consecutive fills from one taker order at the same price are pre-merged by Binance."""
    rows = []
    for s in symbols:
        for t in http_get(f"{SPOT_BASE}/aggTrades", {"symbol": SPOT_SYM[s], "limit": str(limit)}):
            rows.append({"symbol": s, "agg_id": t["a"], "ts": pd.to_datetime(int(t["T"]), unit="ms", utc=True),
                        "price": float(t["p"]), "size": float(t["q"]), "aggressor": "sell" if t["m"] else "buy", "collected_at": now})
    return pd.DataFrame(rows)


def fetch_funding_rate(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{FAPI_BASE}/fundingRate", {"symbol": PERP_SYM[s], "limit": "1"})
        if j:
            rows.append({"symbol": s, "ts": pd.to_datetime(int(j[0]["fundingTime"]), unit="ms", utc=True), "funding_rate": float(j[0]["fundingRate"]), "collected_at": now})
    return pd.DataFrame(rows)


def fetch_open_interest(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{FAPI_BASE}/openInterest", {"symbol": PERP_SYM[s]})
        rows.append({"symbol": s, "ts": pd.to_datetime(int(j["time"]), unit="ms", utc=True), "open_interest": float(j["openInterest"]), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("binance_candles_1m", "ohlcv", "Binance public API", ["BTC", "ETH", "XRP"], fetch_candles_1m, poll_every_s=60,
           notes="Confirmed GEO-RESTRICTED from this environment under Binance's own Terms - verify from your own deployment before relying on it."),
    Adapter("binance_trades", "tick_trades", "Binance public API", ["BTC", "ETH", "XRP"], fetch_trades, poll_every_s=30, notes="See binance_candles_1m note."),
    Adapter("binance_agg_trades", "tick_trades", "Binance public API", ["BTC", "ETH", "XRP"], fetch_agg_trades, poll_every_s=30, notes="See binance_candles_1m note."),
    Adapter("binance_funding_rate", "funding_rates", "Binance public API", ["BTC", "ETH", "XRP"], fetch_funding_rate, poll_every_s=300, notes="See binance_candles_1m note."),
    Adapter("binance_open_interest", "open_interest", "Binance public API", ["BTC", "ETH", "XRP"], fetch_open_interest, poll_every_s=300, notes="See binance_candles_1m note."),
]
