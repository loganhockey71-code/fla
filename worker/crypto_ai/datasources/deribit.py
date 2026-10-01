"""Deribit public API. Free, no key (verified live 2026-09). BTC and ETH only - Deribit does not list XRP
derivatives, so XRP has no futures-basis source here (see DATA_SOURCES.md for what would be needed).

Funding rate for the perpetual, and the index price (a good proxy for computing spot-futures basis once paired
with Coinbase's spot mid from candles.py / micro_collect.py at research time - not computed here, just collected).
"""
from datetime import datetime, timedelta

import pandas as pd

from .base import Adapter, http_get

BASE = "https://www.deribit.com/api/v2/public"
PERP = {"BTC": "BTC-PERPETUAL", "ETH": "ETH-PERPETUAL"}
INDEX = {"BTC": "btc_usd", "ETH": "eth_usd"}


def fetch_funding_and_index(now: datetime, symbols=("BTC", "ETH")) -> pd.DataFrame:
    rows = []
    for s in symbols:
        if s not in PERP:
            continue
        try:
            fr = http_get(f"{BASE}/get_funding_rate_value", {
                "instrument_name": PERP[s], "start_timestamp": int((now - timedelta(hours=1)).timestamp() * 1000), "end_timestamp": int(now.timestamp() * 1000)})["result"]
            idx = http_get(f"{BASE}/get_index_price", {"index_name": INDEX[s]})["result"]
            rows.append({"symbol": s, "instrument": PERP[s], "ts": now, "funding_rate_1h": float(fr), "index_price": float(idx["index_price"]), "collected_at": now})
        except Exception:
            continue
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("deribit_funding_index", "futures_basis", "Deribit public API", ["BTC", "ETH"], fetch_funding_and_index, poll_every_s=300,
           notes="BTC/ETH only (no XRP derivatives on Deribit). Pair index_price with a spot mid at research time for basis."),
]
