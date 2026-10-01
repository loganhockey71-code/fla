"""OKX public market-data API. Free, no key, no auth for every endpoint used here (verified live 2026-09). Not
Binance/Bybit: both returned geo-restriction errors from this environment (Binance: "restricted location" per its
own Terms; Bybit: a CloudFront country block) - using them would risk exactly the ToS violation the brief warned
against, so they are catalogued but not integrated (see DATA_SOURCES.md).

Covers funding rate, open interest, liquidations, order book (L2 top-of-book snapshot) and recent trades (with
aggressor side) for BTC/ETH/XRP perpetual swaps and spot pairs.
"""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://www.okx.com/api/v5"
SWAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "XRP": "XRP-USDT-SWAP"}
FAMILY = {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "XRP": "XRP-USDT"}
SPOT = {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "XRP": "XRP-USDT"}


def _rows(symbols, fn) -> pd.DataFrame:
    out = []
    for s in symbols:
        try:
            out.append(fn(s))
        except Exception:
            continue
    return pd.DataFrame([r for r in out if r is not None])


def fetch_funding_rate(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    def one(s):
        j = http_get(f"{BASE}/public/funding-rate", {"instId": SWAP[s]})["data"][0]
        return {"symbol": s, "inst_id": j["instId"], "ts": pd.to_datetime(int(j["fundingTime"]), unit="ms", utc=True),
                "funding_rate": float(j["fundingRate"]), "collected_at": now}
    return _rows(symbols, one)


def fetch_open_interest(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    def one(s):
        j = http_get(f"{BASE}/public/open-interest", {"instId": SWAP[s]})["data"][0]
        return {"symbol": s, "inst_id": j["instId"], "ts": pd.to_datetime(int(j["ts"]), unit="ms", utc=True),
                "oi_contracts": float(j["oi"]), "oi_ccy": float(j["oiCcy"]), "oi_usd": float(j["oiUsd"]), "collected_at": now}
    return _rows(symbols, one)


def fetch_liquidations(now: datetime, symbols=("BTC", "ETH", "XRP")) -> pd.DataFrame:
    def one(s):
        j = http_get(f"{BASE}/public/liquidation-orders", {"instType": "SWAP", "instFamily": FAMILY[s], "state": "filled", "limit": "100"})["data"]
        if not j or not j[0].get("details"):
            return None
        rows = [{"symbol": s, "inst_id": j[0]["instId"], "ts": pd.to_datetime(int(d["ts"]), unit="ms", utc=True),
                 "side": d["side"], "pos_side": d["posSide"], "bankrupt_price": float(d["bkPx"]), "size": float(d["sz"]), "collected_at": now}
                for d in j[0]["details"]]
        return rows
    parts = []
    for s in symbols:
        try:
            r = one(s)
            if r:
                parts.extend(r)
        except Exception:
            continue
    return pd.DataFrame(parts)


def fetch_order_book(now: datetime, symbols=("BTC", "ETH", "XRP"), depth: int = 20) -> pd.DataFrame:
    def one(s):
        j = http_get(f"{BASE}/market/books", {"instId": SPOT[s], "sz": str(depth)})["data"][0]
        return {"symbol": s, "inst_id": SPOT[s], "ts": pd.to_datetime(int(j["ts"]), unit="ms", utc=True),
                "bids": [[b[0], b[1]] for b in j["bids"]], "asks": [[a[0], a[1]] for a in j["asks"]], "collected_at": now}
    return _rows(symbols, one)


def fetch_trades(now: datetime, symbols=("BTC", "ETH", "XRP"), limit: int = 100) -> pd.DataFrame:
    def one(s):
        j = http_get(f"{BASE}/market/trades", {"instId": SPOT[s], "limit": str(limit)})["data"]
        return [{"symbol": s, "inst_id": SPOT[s], "trade_id": t["tradeId"], "ts": pd.to_datetime(int(t["ts"]), unit="ms", utc=True),
                 "price": float(t["px"]), "size": float(t["sz"]), "aggressor": t["side"], "collected_at": now} for t in j]
    parts = []
    for s in symbols:
        try:
            parts.extend(one(s))
        except Exception:
            continue
    return pd.DataFrame(parts)


ADAPTERS = [
    Adapter("okx_funding_rate", "funding_rates", "OKX public API", ["BTC", "ETH", "XRP"], fetch_funding_rate, poll_every_s=300,
           notes="Perpetual swap funding rate, updated ~every 8h but polled more often to catch the printed value promptly."),
    Adapter("okx_open_interest", "open_interest", "OKX public API", ["BTC", "ETH", "XRP"], fetch_open_interest, poll_every_s=300),
    Adapter("okx_liquidations", "liquidations", "OKX public API", ["BTC", "ETH", "XRP"], fetch_liquidations, poll_every_s=120,
           notes="Recent filled liquidation orders (snapshot, not a full historical archive - poll often to avoid gaps)."),
    Adapter("okx_order_book", "order_books_l2", "OKX public API", ["BTC", "ETH", "XRP"], fetch_order_book, poll_every_s=30,
           notes="Top-N aggregated book snapshot (a second, independent venue's depth alongside Coinbase's)."),
    Adapter("okx_trades", "tick_trades", "OKX public API", ["BTC", "ETH", "XRP"], fetch_trades, poll_every_s=30),
]
