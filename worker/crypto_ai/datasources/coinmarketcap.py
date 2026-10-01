"""CoinMarketCap API. Requires a free key (`COINMARKETCAP_API_KEY` - Basic plan, $0, signup at
https://coinmarketcap.com/api/ - no payment method required for the free tier, ~333 credits/day). Skipped (not
broken) when the key is absent, same pattern as Etherscan/CoinGecko's optional key."""
import os
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://pro-api.coinmarketcap.com/v1"
SYMBOLS = "BTC,ETH,XRP"


def fetch_quotes(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    key = os.environ.get("COINMARKETCAP_API_KEY")
    if not key:
        return pd.DataFrame()
    j = http_get(f"{BASE}/cryptocurrency/quotes/latest", {"symbol": ",".join(symbols), "convert": "USD"}, headers={"X-CMC_PRO_API_KEY": key})
    rows = []
    for s in symbols:
        d = j.get("data", {}).get(s)
        if not d:
            continue
        q = d["quote"]["USD"]
        rows.append({"symbol": s, "ts": now, "price": q["price"], "market_cap": q["market_cap"], "volume_24h": q["volume_24h"],
                    "percent_change_24h": q["percent_change_24h"], "cmc_rank": d.get("cmc_rank"), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("coinmarketcap_quotes", "market_data", "CoinMarketCap API (free tier, needs COINMARKETCAP_API_KEY)", ["BTC", "ETH", "XRP"], fetch_quotes, poll_every_s=300),
]
