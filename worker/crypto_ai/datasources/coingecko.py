"""CoinGecko public API. `/search/trending` worked anonymously when verified live (2026-09); `/coins/markets` was
403'd from this sandbox's shared IP (CoinGecko's free tier is increasingly bot-protected and often needs their free
"Demo" key). Both work the same way once `COINGECKO_API_KEY` (a free Demo key from
https://www.coingecko.com/en/api/pricing - no payment method required) is set and sent as `x-cg-demo-api-key`; both
still WORK WITHOUT ONE, best-effort, since CoinGecko's docs say anonymous access remains supported at a lower,
unpublished rate limit - if your IP isn't blocked the way this sandbox's was, no key is needed at all.

Complements crypto_ai/coingecko.py (which only fetches simple reference prices for the live worker) with market
cap/volume/rank, trending-coins, and historical market-chart data for research.
"""
import os
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://api.coingecko.com/api/v3"
COIN_ID = {"BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple"}


def _headers() -> dict:
    key = os.environ.get("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def fetch_markets(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    ids = ",".join(COIN_ID[s] for s in symbols)
    j = http_get(f"{BASE}/coins/markets", {"vs_currency": "usd", "ids": ids}, headers=_headers())
    inv = {v: k for k, v in COIN_ID.items()}
    rows = [{"symbol": inv[c["id"]], "ts": now, "price": c["current_price"], "market_cap": c["market_cap"],
            "market_cap_rank": c["market_cap_rank"], "volume_24h": c["total_volume"], "change_24h_pct": c["price_change_percentage_24h"],
            "collected_at": now} for c in j if c["id"] in inv]
    return pd.DataFrame(rows)


def fetch_trending(now: datetime) -> pd.DataFrame:
    j = http_get(f"{BASE}/search/trending", headers=_headers())
    rows = [{"ts": now, "rank": i, "coin_id": c["item"]["id"], "symbol_native": c["item"]["symbol"], "market_cap_rank": c["item"].get("market_cap_rank"), "collected_at": now}
           for i, c in enumerate(j.get("coins", []))]
    return pd.DataFrame(rows)


def fetch_market_chart(now: datetime, symbols=("BTC", "ETH", "XRP"), days: int = 1) -> pd.DataFrame:
    """Historical price/market-cap/volume points - CoinGecko backfills this for free (days=1 -> ~5min granularity)."""
    rows = []
    for s in symbols:
        j = http_get(f"{BASE}/coins/{COIN_ID[s]}/market_chart", {"vs_currency": "usd", "days": str(days)}, headers=_headers())
        prices = dict(j.get("prices", []))
        vols = dict(j.get("total_volumes", []))
        mcaps = dict(j.get("market_caps", []))
        for t in prices:
            rows.append({"symbol": s, "ts": pd.to_datetime(int(t), unit="ms", utc=True), "price": prices[t], "volume": vols.get(t), "market_cap": mcaps.get(t), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("coingecko_markets", "market_data", "CoinGecko public API", ["BTC", "ETH", "XRP"], fetch_markets, poll_every_s=120,
           notes="Anonymous access may be 403'd depending on your IP; set COINGECKO_API_KEY (free Demo key) if so."),
    Adapter("coingecko_trending", "market_data", "CoinGecko public API", ["BTC", "ETH", "XRP"], fetch_trending, poll_every_s=600),
    Adapter("coingecko_market_chart", "ohlcv", "CoinGecko public API", ["BTC", "ETH", "XRP"], fetch_market_chart, poll_every_s=3600,
           notes="One-time-ish historical backfill (days=1 by default); increase `days` for a deeper one-off pull."),
]
