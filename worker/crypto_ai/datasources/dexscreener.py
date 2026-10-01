"""DexScreener public API. Free, no key (verified live 2026-09). DEX pairs, liquidity, volume, and price changes for
BTC/ETH/XRP-related tokens across chains (native BTC has no DEX presence; this mainly covers wrapped/bridged
representations and ETH/XRP's own DEX activity)."""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://api.dexscreener.com/latest/dex"
QUERY = {"BTC": "WBTC", "ETH": "WETH", "XRP": "XRP"}


def fetch_pairs(now: datetime, symbols=("BTC", "ETH", "XRP"), top_n: int = 5) -> pd.DataFrame:
    rows = []
    for s in symbols:
        j = http_get(f"{BASE}/search", {"q": QUERY[s]})
        for p in (j.get("pairs") or [])[:top_n]:
            rows.append({"symbol": s, "ts": now, "chain": p.get("chainId"), "dex": p.get("dexId"), "pair_address": p.get("pairAddress"),
                        "base_token": (p.get("baseToken") or {}).get("symbol"), "quote_token": (p.get("quoteToken") or {}).get("symbol"),
                        "price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
                        "liquidity_usd": (p.get("liquidity") or {}).get("usd"), "volume_24h_usd": (p.get("volume") or {}).get("h24"),
                        "price_change_24h_pct": (p.get("priceChange") or {}).get("h24"), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("dexscreener_pairs", "dex_data", "DexScreener public API", ["BTC", "ETH", "XRP"], fetch_pairs, poll_every_s=300,
           notes="Native BTC has no DEX presence; covers wrapped/bridged tokens. Top-N pairs by search relevance, not a full market scan."),
]
