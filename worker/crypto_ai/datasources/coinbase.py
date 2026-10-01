"""Coinbase Exchange, wrapped as Adapters for the datasources registry/dashboard (the actual HTTP calls reuse the
project's existing, already-tested `crypto_ai.coinbase` module - this file adds no new network code, just makes
Coinbase show up alongside the other sources with the same run()/manifest/dashboard behaviour). Free, no key.
`ws-collect` (crypto_ai/scalp/micro_collect.py) already covers Coinbase's real-time ticker/trades/depth at far
higher resolution than these periodic snapshots; these adapters exist for dashboard visibility and for anyone who
only wants the lighter-weight periodic pull instead of running the always-on WS collector.
"""
from datetime import datetime, timedelta

import pandas as pd

from .. import coinbase as cb
from ..config import PRODUCTS
from .base import Adapter

SYMS = ("BTC", "ETH", "XRP")


def fetch_candles_1m(now: datetime, symbols=SYMS) -> pd.DataFrame:
    rows = []
    for s in symbols:
        df = cb.closed_only(cb.candles(PRODUCTS[s], 60, now - timedelta(minutes=6), now), 60, now)
        for ts, r in df.iterrows():
            rows.append({"symbol": s, "ts": ts, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume, "collected_at": now})
    return pd.DataFrame(rows)


def fetch_ticker(now: datetime, symbols=SYMS) -> pd.DataFrame:
    rows = []
    for s in symbols:
        t = cb.ticker(PRODUCTS[s])
        rows.append({"symbol": s, "ts": now, "price": t["price"], "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread_pct"], "volume_24h": t["volume_24h"], "collected_at": now})
    return pd.DataFrame(rows)


def fetch_order_book(now: datetime, symbols=SYMS) -> pd.DataFrame:
    from .okx import fetch_order_book as _shape_ref  # noqa: F401  (documents the shared {symbol, ts, bids, asks} shape across venues)
    rows = []
    for s in symbols:
        imb = cb.order_book_imbalance(PRODUCTS[s])
        rows.append({"symbol": s, "ts": now, "imbalance_50bp": imb, "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("coinbase_candles_1m", "ohlcv", "Coinbase Exchange public API", list(SYMS), fetch_candles_1m, poll_every_s=60,
           notes="Reuses crypto_ai.coinbase; the primary spot venue for the whole project."),
    Adapter("coinbase_ticker", "bid_ask", "Coinbase Exchange public API", list(SYMS), fetch_ticker, poll_every_s=15),
    Adapter("coinbase_order_book_imbalance", "order_books_l2", "Coinbase Exchange public API", list(SYMS), fetch_order_book, poll_every_s=30,
           notes="Full real-time depth/ticks are better covered by `ws-collect` (crypto_ai/scalp/micro_collect.py); this is a light periodic summary."),
]
