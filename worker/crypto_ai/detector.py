"""Sudden-move detector + automatic 'why did it move?' investigation."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import coinbase
from .config import PRODUCTS, SYMBOLS

WINDOWS_MIN = [5, 15, 30]
OTHER_MOVE_MIN_PCT = 0.3        # another coin "also moved" if it went the same way by at least this much
OB_EXTREME = 0.7


def pct_move(closes: pd.Series, minutes: int) -> float:
    return (closes.iloc[-1] / closes.iloc[-1 - minutes] - 1) * 100


def find_trigger(closes: pd.Series, vols: pd.Series, imbalance: float | None, cfg: dict) -> dict | None:
    """Pure: does the last hour of 1-minute candles contain something unusual?"""
    if len(closes) < 65:
        return None
    thr = cfg["sudden_move_pct"]
    move = None
    for w in WINDOWS_MIN:
        m = pct_move(closes, w)
        if abs(m) >= thr:
            move = {"window": w, "pct": m}
            break
    recent = vols.iloc[-5:].sum()
    baseline = vols.iloc[-65:-5].sum() / 12
    spike = recent / baseline if baseline > 0 else 0.0
    reasons = []
    if move:
        reasons.append("price")
    if spike >= cfg["volume_spike_x"] and abs(pct_move(closes, 5)) >= 0.3:
        reasons.append("volume")
    if imbalance is not None and abs(imbalance) >= OB_EXTREME and abs(pct_move(closes, 5)) >= 0.3:
        reasons.append("orderbook")
    if not reasons:
        return None
    w = move["window"] if move else 15
    seg = closes.iloc[-1 - w:]
    # start of the move = the opposite extreme inside the window (so "in 12 minutes" is the real duration)
    start_i = seg.idxmax() if seg.iloc[-1] < seg.iloc[0] else seg.idxmin()
    minutes = max(int((seg.index[-1] - start_i).total_seconds() // 60), 1)
    pct = (seg.iloc[-1] / seg.loc[start_i] - 1) * 100
    return {"pct": float(pct), "minutes": minutes, "window": w, "volume_spike": float(spike),
            "imbalance": imbalance, "reasons": reasons}


def diagnose(symbol: str, trig: dict, other_moves: dict[str, float], buy_pressure: float | None,
             news: list[dict]) -> dict:
    """Pure: rank candidate causes and pick the most likely one with a confidence."""
    direction = 1 if trig["pct"] > 0 else -1
    same = [c for c, m in other_moves.items() if m * direction >= OTHER_MOVE_MIN_PCT]
    scope = "broad" if len(same) == len(other_moves) else "partial" if same else "coin_specific"
    verb = "buying" if direction > 0 else "selling"
    flow = ""
    if buy_pressure is not None and ((direction < 0 and buy_pressure < 0.4) or (direction > 0 and buy_pressure > 0.6)):
        flow = f" + {'increased' if trig['volume_spike'] >= 2 else 'heavy'} {symbol} {verb[:-3]} volume"
    candidates = []
    official = [n for n in news if n["source_tier"] == "official" and n["importance"] >= 40]
    other_news = [n for n in news if n["source_tier"] != "official" and n["importance"] >= 40]
    if official:
        n = max(official, key=lambda x: x["importance"])
        candidates.append((60 + n["importance"] * 0.3 + (8 if scope != "coin_specific" else 0),
                           f"official announcement: \"{n['title'][:90]}\" ({n['source']})", n))
    if other_news:
        n = max(other_news, key=lambda x: x["importance"])
        candidates.append((45 + n["importance"] * 0.25, f"news: \"{n['title'][:90]}\" ({n['source']})", n))
    if scope == "broad":
        s = 55 + (10 if trig["volume_spike"] >= 2 else 0) + (5 if flow else 0)
        candidates.append((s, f"broad crypto {verb}{flow}", None))
    elif scope == "partial":
        candidates.append((48 + (8 if trig["volume_spike"] >= 2 else 0),
                           f"{'/'.join(same)}-led market {verb}, spilling into {symbol}{flow}", None))
    else:
        s = 45 + (12 if trig["volume_spike"] >= 3 else 0) + (5 if flow else 0)
        candidates.append((s, f"{symbol}-specific {verb}{flow} (other coins did not follow)", None))
    score, cause, ref = max(candidates, key=lambda c: c[0])
    if score < 42 and not ref:
        cause, score = "no clear cause found", 30
    return {"scope": scope, "cause": cause, "confidence": int(min(score, 90)), "news_ref": ref,
            "coins_also_moved": same}


def headline(symbol: str, trig: dict, d: dict) -> str:
    verb = "dropped" if trig["pct"] < 0 else "rose"
    return (f"{symbol} {verb} {abs(trig['pct']):.1f}% in {trig['minutes']} minutes. "
            f"Most likely cause: {d['cause']}. Confidence: {d['confidence']}%.")


def run_detector(db, cfg: dict, snaps: dict | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=95)
    frames = {s: coinbase.closed_only(coinbase.candles(PRODUCTS[s], 60, start, now), 60, now) for s in SYMBOLS}
    found = []
    for s in SYMBOLS:
        fr = frames[s]
        if len(fr) < 65:
            continue
        snap = (snaps or {}).get(s, {})
        trig = find_trigger(fr["close"], fr["volume"], snap.get("ob_imbalance"), cfg)
        if not trig:
            continue
        if db.one("select 1 as x from events where kind='sudden_move' and %s = any(coins) and occurred_at > %s",
                  [s, now - timedelta(minutes=30)]):
            continue
        others = {o: pct_move(frames[o]["close"], trig["window"]) for o in SYMBOLS if o != s and len(frames[o]) > trig["window"]}
        news = db.all("""select title, source, case when origin_tier <= 2 then 'official' else 'media' end as source_tier,
                                importance_score as importance from research_events
                         where kind in ('news','legislation') and %s = any(affected_coins)
                           and not (kind = 'legislation' and details->>'change' = 'new')   -- first sighting of an old bill is not a catalyst
                           and coalesce(published_at, detected_at) > %s""", [s, now - timedelta(hours=6)])
        d = diagnose(s, trig, others, snap.get("buy_pressure"), news)
        text = headline(s, trig, d)
        db.insert("events", {
            "occurred_at": now, "kind": "sudden_move", "title": text.split(". Most likely")[0], "source": "Sudden-move detector",
            "source_tier": None, "coins": [s], "sentiment": "positive" if trig["pct"] > 0 else "negative",
            "importance": int(min(100, 50 + abs(trig["pct"]) * 10)), "confidence": d["confidence"],
            "explanation": text,
            "details": {"trigger": trig, "others_move_pct": others, "scope": d["scope"], "buy_pressure": snap.get("buy_pressure"),
                        "coins_also_moved": d["coins_also_moved"], "news_considered": len(news),
                        "linked_news": d["news_ref"]["title"] if d["news_ref"] else None},
        })
        found.append({"symbol": s, "text": text})
    return found
