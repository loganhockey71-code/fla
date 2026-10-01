"""GeckoTerminal public API (CoinGecko's DEX data product). Free, no key (verified live 2026-09). Trending pools per
network, complementing DexScreener with a second, independent DEX-data source."""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://api.geckoterminal.com/api/v2"
NETWORKS = ["eth"]   # native BTC/XRP have no meaningful EVM DEX pools; ETH mainnet covers WETH/wrapped-asset pools


def fetch_trending_pools(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    rows = []
    for net in NETWORKS:
        j = http_get(f"{BASE}/networks/{net}/trending_pools")
        for p in j.get("data", []):
            a = p.get("attributes", {})
            rows.append({"network": net, "ts": now, "pool_id": p.get("id"), "name": a.get("name"),
                        "base_token_price_usd": a.get("base_token_price_usd"), "quote_token_price_usd": a.get("quote_token_price_usd"),
                        "volume_24h_usd": (a.get("volume_usd") or {}).get("h24"), "reserve_usd": a.get("reserve_in_usd"), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("geckoterminal_trending_pools", "dex_data", "GeckoTerminal public API", ["ETH"], fetch_trending_pools, poll_every_s=600,
           notes="ETH mainnet trending pools only; no native BTC/XRP DEX presence to track this way."),
]
