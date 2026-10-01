"""Free on-chain metrics. Free, no key.

blockchain.info Charts API: BTC-only (it's a Bitcoin block explorer). Confirmed transactions/day, mempool size and
hash rate are daily/near-daily series - a coarse, low-frequency on-chain context feature, not a short-term signal.

Etherscan (ETH): the free tier needs a (free, no cost) API key - same pattern as FRED_API_KEY/CONGRESS_API_KEY
already used in this project (`ETHERSCAN_API_KEY` env var). Without a key this adapter is skipped, not broken.
"""
import os
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

CHARTS = {"n_transactions": "n-transactions", "hash_rate": "hash-rate", "mempool_size": "mempool-size"}


def fetch_btc_charts(now: datetime, symbols=("BTC",)) -> pd.DataFrame:
    rows = []
    for metric, chart in CHARTS.items():
        j = http_get(f"https://api.blockchain.info/charts/{chart}", {"timespan": "5days", "format": "json"})
        for v in j.get("values", []):
            rows.append({"symbol": "BTC", "metric": metric, "ts": pd.to_datetime(v["x"], unit="s", utc=True), "value": v["y"], "collected_at": now})
    return pd.DataFrame(rows)


def fetch_eth_supply(now: datetime, symbols=("ETH",)) -> pd.DataFrame:
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        return pd.DataFrame()
    j = http_get("https://api.etherscan.io/v2/api", {"chainid": "1", "module": "stats", "action": "ethsupply2", "apikey": key})
    if j.get("status") != "1":
        return pd.DataFrame()
    r = j["result"]
    return pd.DataFrame([{"symbol": "ETH", "ts": now, "eth_supply": float(r.get("EthSupply", 0)) / 1e18, "collected_at": now}])


ADAPTERS = [
    Adapter("blockchain_info_btc_charts", "onchain_metrics", "blockchain.info Charts API", ["BTC"], fetch_btc_charts, poll_every_s=21600,
           notes="Daily-resolution BTC on-chain series; coarse context, not a short-term feature."),
    Adapter("etherscan_eth_supply", "onchain_metrics", "Etherscan API (free tier, needs ETHERSCAN_API_KEY)", ["ETH"], fetch_eth_supply, poll_every_s=21600,
           notes="Skipped (not broken) if ETHERSCAN_API_KEY is unset. Whale-transaction scanning needs per-block scanning - not implemented yet, see DATA_SOURCES.md."),
]
