"""DefiLlama public API. Free, no key, generous rate limits, open data (verified live 2026-09; see
https://defillama.com/docs/api for the current terms - attribution appreciated, no login/key needed for these
read-only endpoints). Not per-coin (BTC/ETH/XRP aren't DeFi protocols) - this is macro-liquidity context: total
stablecoin supply (a proxy for dry powder / risk appetite) and total value locked across DeFi.
"""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get


def fetch_stablecoins(now: datetime) -> pd.DataFrame:
    j = http_get("https://stablecoins.llama.fi/stablecoincharts/all", {"stablecoin": "1"})
    df = pd.DataFrame(j)
    df["ts"] = pd.to_datetime(df["date"].astype(int), unit="s", utc=True)
    df["total_circulating_usd"] = df["totalCirculatingUSD"].apply(lambda d: d.get("peggedUSD") if isinstance(d, dict) else None)
    df["collected_at"] = now
    return df[["ts", "total_circulating_usd", "collected_at"]].dropna()


def fetch_tvl(now: datetime) -> pd.DataFrame:
    j = http_get("https://api.llama.fi/v2/historicalChainTvl")
    df = pd.DataFrame(j)
    df["ts"] = pd.to_datetime(df["date"].astype(int), unit="s", utc=True)
    df["collected_at"] = now
    return df.rename(columns={"tvl": "total_tvl_usd"})[["ts", "total_tvl_usd", "collected_at"]]


ADAPTERS = [
    Adapter("defillama_stablecoins", "stablecoin_flows", "DefiLlama public API", ["BTC", "ETH", "XRP"], fetch_stablecoins, poll_every_s=21600,
           notes="Market-wide total, not per-coin - a macro liquidity/risk-appetite feature, not a direct BTC/ETH/XRP signal."),
    Adapter("defillama_tvl", "defi_tvl", "DefiLlama public API", ["BTC", "ETH", "XRP"], fetch_tvl, poll_every_s=21600),
]
