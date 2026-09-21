"""Polymarket + Kalshi public, READ-ONLY market data. No accounts, no wallets, no orders, no trading endpoints are called.
Implied probabilities become research features / events only. They never decide BUY or SELL."""
import json
import re
from datetime import datetime, timedelta, timezone

from .. import events as lex
from ..http import get_json
from .common import category_of, record_event, similarity

POLY = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

QUERIES = ["bitcoin", "ethereum", "xrp", "crypto", "fed rate cut", "fed interest rate", "cpi inflation", "recession",
           "sec crypto etf", "stablecoin", "crypto bill senate", "clarity act", "market structure"]
RELEVANT = (r"\b(bitcoin|btc|ethereum|xrp|ripple|crypto\w*|stablecoin|etf|sec|cftc|fed|fomc|rate (cut|hike)s?|interest rates?|"
            r"inflation|cpi|recession|clarity act|market structure|digital assets?|genius act|unemployment|payrolls?)\b")
EXCLUDE = r"(\b(above|below|between)\b.*\bon\b)|up or down|\bhigh or low\b"          # daily price ladders are noise here
POS = r"\b(cut|cuts|approve\w*|pass\w*|sign\w*|adopt\w*|launch\w*|approval|all-time high|ath|reach|hit)\b"
NEG = r"\b(hike|hikes|recession|ban|sue\w*|lawsuit|hack\w*|crash\w*|dip|below|fall|default|shutdown|crackdown|reject\w*|delay\w*)\b"
MAX_TRACKED = 60
MOVE_PTS = 0.08


def direction(text: str) -> int:
    t = text.lower()
    if re.search(r"\b(no|not|won't|fail to|without)\b", t):        # "Will there be no rate cuts?" flips meaning
        t = re.sub(POS, "NEGATED", t) + " hike"
    p, n = bool(re.search(POS, t)), bool(re.search(NEG, t))
    return 1 if p and not n else -1 if n and not p else 0


def num(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def coins_for(text: str) -> list[str]:
    named = [c for c, pat in lex.COIN_PATTERNS.items() if re.search(pat, text.lower())]
    return named or ["BTC", "ETH", "XRP"]


def _relevant(text: str) -> bool:
    return bool(re.search(RELEVANT, text.lower())) and not re.search(EXCLUDE, text.lower())


# ---------------------------------------------------------------- Polymarket
def fetch_polymarket() -> list[dict]:
    seen, out = set(), []
    for q in QUERIES:
        j = get_json(f"{POLY}/public-search", {"q": q, "limit_per_type": 8}, ttl=240)
        for ev in j.get("events") or []:
            if ev.get("closed") or not ev.get("active", True):
                continue
            for m in ev.get("markets") or []:
                mid = str(m.get("id") or m.get("conditionId"))
                if mid in seen or m.get("closed"):
                    continue
                try:
                    outcomes, prices = json.loads(m.get("outcomes") or "[]"), json.loads(m.get("outcomePrices") or "[]")
                except ValueError:
                    continue
                if len(outcomes) != 2 or len(prices) != 2 or "yes" not in [o.lower() for o in outcomes]:
                    continue
                prob = float(prices[[o.lower() for o in outcomes].index("yes")])
                title = m.get("question") or ev.get("title") or ""
                if not _relevant(title) or not (0.01 < prob < 0.99):
                    continue
                seen.add(mid)
                out.append({"platform": "polymarket", "market_id": mid, "title": title, "yes_prob": prob,
                            "liquidity": num(m, "liquidityNum", "liquidity"), "volume": num(m, "volumeNum", "volume"),
                            "volume_24h": num(m, "volume24hr"), "end_date": m.get("endDate"),
                            "url": f"https://polymarket.com/event/{ev.get('slug', '')}"})
    return out


# ---------------------------------------------------------------- Kalshi
def kalshi_prob(m: dict) -> float | None:
    bid, ask = num(m, "yes_bid_dollars"), num(m, "yes_ask_dollars")
    if bid and ask:
        return (bid + ask) / 2
    last = num(m, "last_price_dollars")
    if last:
        return last
    c_bid, c_ask, c_last = num(m, "yes_bid"), num(m, "yes_ask"), num(m, "last_price")     # legacy cent fields
    if c_bid and c_ask:
        return (c_bid + c_ask) / 200
    return c_last / 100 if c_last else None


def fetch_kalshi() -> list[dict]:
    out, cursor = [], None
    for _ in range(8):                                    # ~1,600 open events max per poll
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        j = get_json(f"{KALSHI}/events", params, ttl=240)
        for ev in j.get("events", []):
            ev_title = ev.get("title", "")
            if not _relevant(ev_title + " " + (ev.get("category") or "")):
                continue
            for m in ev.get("markets", []):
                prob = kalshi_prob(m)
                if prob is None or not (0.01 < prob < 0.99):
                    continue
                sub = m.get("yes_sub_title") or m.get("subtitle") or ""
                title = f"{ev_title}: {sub}".strip(": ")
                if re.search(EXCLUDE, title.lower()):
                    continue
                out.append({"platform": "kalshi", "market_id": m["ticker"], "title": title, "yes_prob": prob,
                            "liquidity": num(m, "liquidity_dollars", "liquidity"),
                            "volume": num(m, "volume_fp", "volume", "volume_dollars"),
                            "volume_24h": num(m, "volume_24h_fp", "volume_24h"),
                            "end_date": m.get("close_time"), "url": f"https://kalshi.com/markets/{(ev.get('series_ticker') or '').lower()}"})
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


# ---------------------------------------------------------------- shared: snapshots + move events
def _activity(m: dict) -> float:
    return (m.get("volume_24h") or 0) * 3 + (m.get("volume") or 0) * 0.1 + (m.get("liquidity") or 0)


def store_and_detect(db, src: dict, markets: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    markets = sorted(markets, key=_activity, reverse=True)[:MAX_TRACKED]
    snaps = events = 0
    for m in markets:
        text = m["title"]
        last = db.one("select yes_prob, captured_at from prediction_market_snapshots where platform=%s and market_id=%s "
                      "order by captured_at desc limit 1", [m["platform"], m["market_id"]])
        if last and abs(last["yes_prob"] - m["yes_prob"]) < 0.005 and now - last["captured_at"] < timedelta(minutes=30):
            continue                                           # unchanged: don't bloat the table
        db.insert("prediction_market_snapshots", {
            "platform": m["platform"], "market_id": m["market_id"], "title": text[:400], "category": category_of(text),
            "coins": coins_for(text), "direction": direction(text), "yes_prob": m["yes_prob"], "liquidity": m["liquidity"],
            "volume": m["volume"], "volume_24h": m["volume_24h"], "end_date": m["end_date"], "url": m["url"], "captured_at": now})
        snaps += 1
        # compare with ~24h ago (or the oldest snapshot if we have >1h of history)
        base = db.one("select yes_prob, captured_at from prediction_market_snapshots where platform=%s and market_id=%s "
                      "and captured_at <= %s order by captured_at desc limit 1", [m["platform"], m["market_id"], now - timedelta(hours=24)])
        if base is None:
            base = db.one("select yes_prob, captured_at from prediction_market_snapshots where platform=%s and market_id=%s "
                          "and captured_at <= %s order by captured_at asc limit 1", [m["platform"], m["market_id"], now - timedelta(hours=1)])
        if base is None or abs(m["yes_prob"] - base["yes_prob"]) < MOVE_PTS:
            continue
        if _emit_move(db, src, m, base["yes_prob"], now):
            events += 1
    return {"status": "ok", "new": events, "snapshots": snaps, "seen": len(markets)}


def _emit_move(db, src, m, p0, now) -> bool:
    text, p1 = m["title"], m["yes_prob"]
    d = direction(text)
    move = p1 - p0
    sentiment = "neutral" if d == 0 else ("positive" if d * move > 0 else "negative")
    # is the same question priced on the other platform? blend and record both.
    other_p, other_platform = None, "kalshi" if m["platform"] == "polymarket" else "polymarket"
    for r in db.all("select title, yes_prob from prediction_market_snapshots where platform=%s and captured_at > %s order by captured_at desc limit 300",
                    [other_platform, now - timedelta(hours=24)]):
        if similarity(text, r["title"]) >= 0.55:
            other_p = r["yes_prob"]
            break
    blended = (p1 + other_p) / 2 if other_p is not None else p1
    source = f"{m['platform']}+{other_platform}" if other_p is not None else m["platform"]
    vol = m.get("volume_24h") or m.get("volume") or 0
    importance = int(min(70, 28 + abs(move) * 100 + (8 if vol > 100_000 else 4 if vol > 10_000 else 0)))
    arrow = f"{p0:.0%} → {p1:.0%}"
    ev = {"category": category_of(text), "coins": coins_for(text), "named": [c for c, pat in lex.COIN_PATTERNS.items() if re.search(pat, text.lower())],
          "sentiment": sentiment, "importance": importance}
    bucket = now.strftime("%Y%m%d") + str(now.hour // 6)
    res = record_event(db, src, {
        "external_id": f"{m['platform']}:{m['market_id']}:{bucket}", "title": f"Prediction market moved {arrow}: {text[:220]}", "url": m["url"],
        "published_at": now, "summary": f"{m['platform'].title()} implied probability {arrow} in ~24h. Volume/liquidity: {m.get('volume_24h') or m.get('volume') or 'n/a'}"
                                        f" / {m.get('liquidity') or 'n/a'}. Market prices are one input, not a signal.",
        "raw": f"{m['platform']} {m['market_id']}", "kind": "prediction_market", "no_dedupe": True, "preclassified": ev,
        "event_probability": blended, "event_probability_source": source,
        "details": {"platform": m["platform"], "prob_before": p0, "prob_now": p1, "other_platform": other_platform,
                    "other_platform_prob": other_p, "volume": m.get("volume"), "liquidity": m.get("liquidity"), "direction": d}})
    return res == "new"


def poll_polymarket(db, src):
    return store_and_detect(db, src, fetch_polymarket())


def poll_kalshi(db, src):
    return store_and_detect(db, src, fetch_kalshi())
