"""Free real-time microstructure collector for BTC/ETH/XRP. Research data only - it does not feed live trading.

Coinbase Exchange's public WebSocket feed (`ticker` + `matches` channels) needs no API key and gives real-time best
bid/ask and every trade print with its aggressor side. `level2`/`level3`/`full` (order-book depth) now require
authentication (confirmed 2026-09), so depth comes from the free REST snapshot instead, polled every few seconds -
that is a deliberate, documented trade-off, not an oversight.

Everything DB-facing is a thin wrapper around pure functions (`parse_ticker`, `parse_match`, `depth_from_book`,
`quote_changed`) so the parsing/throttling/depth-band logic is unit tested without a socket or a database.
"""
import asyncio
import json
import time
from datetime import datetime, timezone

from .. import coinbase
from ..config import PRODUCTS, SYMBOLS

WS_URL = "wss://ws-feed.exchange.coinbase.com"
DEPTH_BANDS_BP = [5, 25]        # basis points around mid
MIN_QUOTE_INTERVAL_S = 0.15     # per symbol: collapse a burst of quote flicker into at most ~6-7 writes/sec
DEPTH_POLL_S = 2.0
TRADE_FLUSH_S = 1.0


# ---------------------------------------------------------------- pure parsing / calculation
def parse_ticker(msg: dict) -> dict | None:
    """A `ticker` message -> a book_ticks row, or None if it lacks a usable two-sided quote."""
    try:
        bid, ask = float(msg["best_bid"]), float(msg["best_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return {
        "symbol": msg["product_id"].split("-")[0], "ts": msg["time"], "best_bid": bid, "best_ask": ask,
        "bid_size": float(msg["best_bid_size"]) if msg.get("best_bid_size") else None,
        "ask_size": float(msg["best_ask_size"]) if msg.get("best_ask_size") else None,
        "mid": mid, "spread_pct": (ask - bid) / mid * 100,
        "last_trade_price": float(msg["price"]) if msg.get("price") else None, "sequence": msg.get("sequence"),
    }


def parse_match(msg: dict) -> dict:
    """A `match`/`last_match` message -> a trade_ticks row. `side` is the MAKER side; the aggressor (taker) is the
    other one - same convention as coinbase.buy_pressure()."""
    return {"trade_id": int(msg["trade_id"]), "symbol": msg["product_id"].split("-")[0], "ts": msg["time"],
            "price": float(msg["price"]), "size": float(msg["size"]), "aggressor": "sell" if msg["side"] == "buy" else "buy"}


def quote_changed(prev: dict | None, cur: dict, min_interval_s: float, now: float) -> bool:
    """Whether this quote is worth writing: the touch (bid or ask) actually moved, or enough time passed since the
    last write that a same-price refresh is still worth a heartbeat row."""
    if prev is None:
        return True
    if (cur["best_bid"], cur["best_ask"]) != (prev["best_bid"], prev["best_ask"]):
        return True
    return now - prev["_written_at"] >= max(min_interval_s * 20, 5.0)


def depth_from_book(bids: list, asks: list, bands_bp: list[float]) -> dict:
    """Displayed depth and imbalance within each band (basis points of mid) of a level-2 book payload
    ([[price, size, ...], ...] per side, best price first). Pure, so it's tested on synthetic books."""
    if not bids or not asks:
        return {}
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    out = {"mid": mid, "spread_pct": (best_ask - best_bid) / mid * 100}
    for bp in bands_bp:
        band = bp / 10000
        lo, hi = mid * (1 - band), mid * (1 + band)
        b = sum(float(x[1]) for x in bids if float(x[0]) >= lo)
        a = sum(float(x[1]) for x in asks if float(x[0]) <= hi)
        out[f"bid_depth_{bp}bp"] = b
        out[f"ask_depth_{bp}bp"] = a
        out[f"imbalance_{bp}bp"] = (b - a) / (b + a) if (a + b) > 0 else None
    return out


# ---------------------------------------------------------------- database wrapper
def _insert_ticks(db, rows: list[dict]) -> None:
    if rows:
        db.bulk("insert into book_ticks (symbol, ts, best_bid, best_ask, bid_size, ask_size, mid, spread_pct, last_trade_price, sequence) values %s",
                [(r["symbol"], r["ts"], r["best_bid"], r["best_ask"], r["bid_size"], r["ask_size"], r["mid"], r["spread_pct"], r["last_trade_price"], r["sequence"]) for r in rows])


def _insert_trades(db, rows: list[dict]) -> None:
    if rows:
        db.bulk("insert into trade_ticks (trade_id, symbol, ts, price, size, aggressor) values %s on conflict (symbol, trade_id) do nothing",
                [(r["trade_id"], r["symbol"], r["ts"], r["price"], r["size"], r["aggressor"]) for r in rows])


def poll_depth(db, symbols=SYMBOLS) -> None:
    for s in symbols:
        try:
            j = coinbase._get(f"/products/{PRODUCTS[s]}/book", {"level": 2})
            d = depth_from_book(j["bids"], j["asks"], DEPTH_BANDS_BP)
            if d:
                db.insert("depth_snapshots", {"symbol": s, "ts": datetime.now(timezone.utc), "mid": d["mid"], "spread_pct": d["spread_pct"],
                                              "bid_depth_5bp": d["bid_depth_5bp"], "ask_depth_5bp": d["ask_depth_5bp"], "imbalance_5bp": d["imbalance_5bp"],
                                              "bid_depth_25bp": d["bid_depth_25bp"], "ask_depth_25bp": d["ask_depth_25bp"], "imbalance_25bp": d["imbalance_25bp"]})
        except Exception as e:
            print(f"[micro] depth poll {s}: {e}")


async def run(db, symbols=SYMBOLS, log=print, run_seconds: float | None = None) -> None:
    """Connect once and stream until `run_seconds` elapse (None = forever). Reconnects with backoff on any error -
    a dropped connection must never crash the collector."""
    import websockets

    products = [PRODUCTS[s] for s in symbols]
    started = time.monotonic()
    backoff = 2
    last_quote: dict[str, dict] = {}
    quote_buf, trade_buf = [], []
    last_trade_flush = last_depth_poll = time.monotonic()
    while run_seconds is None or time.monotonic() - started < run_seconds:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, max_size=2**20) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": products, "channels": ["ticker", "matches"]}))
                backoff = 2
                log(f"[micro] connected, subscribed to {products}")
                while run_seconds is None or time.monotonic() - started < run_seconds:
                    now = time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, TRADE_FLUSH_S))
                    except asyncio.TimeoutError:
                        raw = None
                    if raw is not None:
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "ticker":
                            row = parse_ticker(msg)
                            if row:
                                prev = last_quote.get(row["symbol"])
                                if quote_changed(prev, row, MIN_QUOTE_INTERVAL_S, now):
                                    quote_buf.append(row)
                                    last_quote[row["symbol"]] = {**row, "_written_at": now}
                        elif t in ("match", "last_match"):
                            trade_buf.append(parse_match(msg))
                        elif t == "error":
                            log(f"[micro] server error: {msg}")
                    if now - last_trade_flush >= TRADE_FLUSH_S:
                        _insert_ticks(db, quote_buf)
                        _insert_trades(db, trade_buf)
                        quote_buf, trade_buf = [], []
                        last_trade_flush = now
                    if now - last_depth_poll >= DEPTH_POLL_S:
                        poll_depth(db, symbols)
                        last_depth_poll = now
        except Exception as e:
            log(f"[micro] connection error: {e}; reconnecting in {backoff}s")
            _insert_ticks(db, quote_buf)
            _insert_trades(db, trade_buf)
            quote_buf, trade_buf = [], []
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    _insert_ticks(db, quote_buf)
    _insert_trades(db, trade_buf)
