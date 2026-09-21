"""Real-time sudden-event system. PAPER TRADING ONLY: it produces a recommendation (BUY / HOLD / REDUCE / SELL), it never
places an order anywhere.

Trigger -> investigate -> update the signal -> say what to do now:
  * price:     a fast move (default >= 1% inside 5/15/30 minutes)
  * volume:    a volume spike (>= 3x normal with a price move, or >= 6x on its own)
  * orderbook: an extreme buy/sell imbalance together with a price move
  * news:      a major official/high-importance research event affecting the coin

The action is a transparent, rule-based overlay. It never edits or replaces the logged predictions (those are
immutable and keep being scored on their own). The rules are heuristics and UNPROVEN: live_signals stores the price at
each signal so their outcomes can be measured like everything else.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import coinbase
from .config import PRODUCTS, SYMBOLS
from .db import JsonList
from .detector import diagnose, find_trigger, headline, pct_move

SEVERITY = {"HOLD": 0, "BUY": 0, "REDUCE": 1, "SELL": 2}


# ---------------------------------------------------------------- pure decision logic
def standing_action(bull: float | None, cfg: dict) -> str:
    """The model's ordinary lean with no shock: BUY / SELL only when it clears the signal threshold, else HOLD."""
    if bull is None:
        return "HOLD"
    thr = cfg["signal_threshold_pct"] / 100
    return "BUY" if bull >= thr else "SELL" if 1 - bull >= thr else "HOLD"


def pick_catalyst(news: list[dict]) -> dict | None:
    """The strongest relevant news item, official sources weighted fully and everything else at half weight."""
    best, best_v = None, 0.0
    for n in news:
        w = 1.0 if (n.get("tier") or 4) <= 2 else 0.5
        v = abs(n["impact"]) * w
        if v > best_v:
            best, best_v = n, v
    return best if best and abs(best["impact"]) >= 10 else None


def decide(trig: dict | None, diag: dict | None, news: list[dict], base_p: float | None, cfg: dict) -> dict:
    """Combine what just happened into one score and one action. Every term is listed in `reasons`."""
    score, reasons = 0.0, []
    pct = trig["pct"] if trig else 0.0
    if trig and abs(pct) > 0.05:
        mag = min(abs(pct), 5.0) / 3.0
        if pct < 0:
            score -= mag
            reasons.append(f"Price fell {abs(pct):.1f}% in {trig['minutes']} min ({-mag:+.2f})")
        else:
            score += 0.5 * mag
            reasons.append(f"Price rose {pct:.1f}% in {trig['minutes']} min (counted at half weight: chasing a spike is risky) ({0.5 * mag:+.2f})")
        spike = trig.get("volume_spike") or 0
        if spike >= 3:
            v = -0.4 if pct < 0 else 0.2
            score += v
            reasons.append(f"Volume {spike:.1f}x normal ({v:+.2f})")
        imb = trig.get("imbalance")
        if imb is not None and abs(imb) >= 0.7:
            v = -0.3 if imb < 0 else 0.15
            score += v
            reasons.append(f"Order book is {'sell' if imb < 0 else 'buy'}-heavy ({imb:+.2f}) ({v:+.2f})")
        if diag and pct < 0:
            if diag["scope"] == "broad":
                score -= 0.4
                reasons.append("All coins are falling together: market-wide selling (-0.40)")
            elif diag["scope"] == "coin_specific":
                score -= 0.2
                reasons.append("Only this coin is falling: coin-specific pressure (-0.20)")
    elif trig and (trig.get("volume_spike") or 0) >= 6:
        reasons.append(f"Volume {trig['volume_spike']:.1f}x normal with no clear price direction yet: watch closely (0.00)")

    cat = pick_catalyst(news)
    hard_exit = False
    if cat:
        w = 1.0 if (cat.get("tier") or 4) <= 2 else 0.5
        v = max(-2.0, min(2.0, cat["impact"] / 50)) * w
        score += v
        reasons.append(f"{'Official' if w == 1 else 'News'} catalyst \"{cat['title'][:80]}\" (impact {cat['impact']:+d}, importance {cat['importance']}) ({v:+.2f})")
        hard_exit = (cat.get("tier") or 4) <= 2 and cat["importance"] >= 85 and cat["impact"] <= -60
    if base_p is not None:
        v = (base_p - 0.5) * 4
        score += v
        reasons.append(f"Model read now: {base_p:.0%} chance of a higher price ({v:+.2f})")

    negative_news = bool(cat and cat["impact"] < 0)
    if hard_exit or score <= -2.0:
        action = "SELL"
    elif score <= -0.8:
        action = "REDUCE"
    elif score >= 1.2 and not negative_news and (base_p is None or base_p >= 0.48):
        action = "BUY"
    else:
        action = "HOLD"
    urgency = "high" if (abs(score) >= 2.0 or hard_exit or abs(pct) >= 3) else "medium" if (abs(score) >= 0.8 or trig or cat) else "low"
    if hard_exit:
        reasons.insert(0, "Severe negative official news: exit rule triggered")
    return {"score": round(score, 3), "action": action, "urgency": urgency, "reasons": reasons, "catalyst": cat}


def explain(symbol: str, res: dict, trig: dict | None, diag: dict | None) -> str:
    what = {"BUY": "BUY (small)", "HOLD": "HOLD", "REDUCE": "REDUCE (sell about half)", "SELL": "SELL (exit)"}[res["action"]]
    head = f"{what} {symbol} now."
    if trig and diag:
        head += " " + headline(symbol, trig, diag).replace(f"{symbol} ", "", 1)
    elif diag:
        head += f" Likely cause: {diag['cause']}."
    return head + " Why: " + "; ".join(res["reasons"][:5]) + "."


# ---------------------------------------------------------------- data + persistence
def _news_for(db, symbol: str, since: datetime) -> list[dict]:
    col = {"BTC": "btc_impact_score", "ETH": "eth_impact_score", "XRP": "xrp_impact_score"}[symbol]
    rows = db.all(f"""select id, title, source, origin_tier, importance_score, {col} as impact, event_category, sentiment from research_events
                      where %s = any(affected_coins) and kind in ('news','legislation','macro') and detected_at > %s
                        and (kind <> 'news' or coalesce(published_at, detected_at) > %s)          -- old stories we merely noticed late are not catalysts
                        and not (kind = 'legislation' and details->>'change' = 'new') order by detected_at desc""", [symbol, since, since])
    return [{"id": r["id"], "title": r["title"], "source": r["source"], "tier": r["origin_tier"], "importance": r["importance_score"],
             "impact": r["impact"], "category": r["event_category"]} for r in rows]


def news_triggers(db, cfg: dict, now: datetime) -> dict[str, dict]:
    """Major fresh research events per coin, not yet signalled."""
    out = {}
    for r in db.all("""select id, title, origin_tier, importance_score, affected_coins from research_events
                       where detected_at > %s and importance_score >= %s and (origin_tier <= 2 or importance_score >= 75)
                         and kind in ('news','legislation','macro') and not (kind = 'legislation' and details->>'change' = 'new')
                         and (kind <> 'news' or coalesce(published_at, detected_at) > %s) order by importance_score desc""",
                    [now - timedelta(minutes=20), cfg["news_trigger_importance"], now - timedelta(hours=6)]):
        for c in r["affected_coins"]:
            if c in SYMBOLS and c not in out and not db.one("select 1 x from live_signals where symbol=%s and news_event_id=%s and trigger_kind='news'", [c, r["id"]]):
                out[c] = r
    return out


def _fresh_model_read(db, symbols: list[str], now: datetime) -> dict[str, float | None]:
    """Model's probability RIGHT NOW (24h horizon, market model). Not stored as an official prediction."""
    from .features import compute_features
    from .model import predict_probability
    from .predictor import live_frames
    out = {s: None for s in symbols}
    try:
        frames = live_frames(now)
    except Exception:
        return out
    for s in symbols:
        m = db.one("select * from model_versions where symbol=%s and variant='market' and is_active", [s])
        if not m:
            continue
        try:
            feats = compute_features(frames, s)
            out[s] = predict_probability(m, 24, feats.iloc[-1])[0]
        except Exception:
            pass
    return out


def _cooled_down(db, cfg, symbol: str, kind_group: tuple, res: dict, trig: dict | None, now: datetime) -> tuple[bool, str | None]:
    """(allowed to emit, previous action). Inside the cooldown only a clearly bigger shock is re-issued."""
    last = db.one("select * from live_signals where symbol=%s and trigger_kind = any(%s) order by created_at desc limit 1", [symbol, list(kind_group)])
    prev = db.one("select action from live_signals where symbol=%s order by created_at desc limit 1", [symbol])
    if not last or now - last["created_at"] > timedelta(minutes=cfg["signal_cooldown_min"]):
        return True, prev["action"] if prev else None
    escalated = abs(res["score"]) >= abs(last["score"] or 0) + 0.7 or SEVERITY[res["action"]] > SEVERITY[last["action"]]
    return escalated, prev["action"] if prev else None


def run_watch_cycle(db, cfg: dict, snaps: dict | None = None) -> list[dict]:
    """One pass of the sudden-event system. Cheap when nothing is happening (3 candle requests)."""
    from .predictor import micro_snapshot
    now = datetime.now(timezone.utc)
    frames = {s: coinbase.closed_only(coinbase.candles(PRODUCTS[s], 60, now - timedelta(minutes=95), now), 60, now) for s in SYMBOLS}
    trig_by = {}
    for s in SYMBOLS:
        fr = frames[s]
        if len(fr) < 65:
            continue
        snap = (snaps or {}).get(s) or {}
        t = find_trigger(fr["close"], fr["volume"], snap.get("ob_imbalance"), cfg)
        if t:
            trig_by[s] = t
    news_by = news_triggers(db, cfg, now)
    coins = sorted(set(trig_by) | set(news_by))
    if not coins:
        return []
    if snaps is None or any(c not in snaps for c in coins):
        snaps = {**(snaps or {}), **{c: micro_snapshot(c) for c in coins if c not in (snaps or {})}}
    base = _fresh_model_read(db, coins, now)
    out = []
    for s in coins:
        trig = trig_by.get(s)
        snap = snaps.get(s) or {}
        price = snap.get("price") or float(frames[s]["close"].iloc[-1])
        news = _news_for(db, s, now - timedelta(hours=6))
        diag = None
        if trig:
            others = {o: pct_move(frames[o]["close"], trig["window"]) for o in SYMBOLS if o != s and len(frames[o]) > trig["window"]}
            diag_news = [{"title": n["title"], "source": n["source"], "source_tier": "official" if (n["tier"] or 4) <= 2 else "media", "importance": n["importance"]} for n in news]
            diag = diagnose(s, trig, others, snap.get("buy_pressure"), diag_news)
        res = decide(trig, diag, news, base.get(s), cfg)
        kind = "news" if (s in news_by and not trig) else ("price" if trig and "price" in trig["reasons"] else "volume" if trig and "volume" in trig["reasons"] else "orderbook" if trig else "news")
        group = ("news",) if kind == "news" else ("price", "volume", "orderbook")
        ok, prev = _cooled_down(db, cfg, s, group, res, trig, now)
        if not ok:
            continue
        event_id = None
        if trig and not db.one("select 1 x from events where kind='sudden_move' and %s = any(coins) and occurred_at > %s", [s, now - timedelta(minutes=cfg["signal_cooldown_min"])]):
            text = headline(s, trig, diag)
            event_id = db.insert("events", {"occurred_at": now, "kind": "sudden_move", "title": text.split(". Most likely")[0], "source": "Sudden-move detector", "source_tier": None,
                                            "coins": [s], "sentiment": "positive" if trig["pct"] > 0 else "negative", "importance": int(min(100, 50 + abs(trig["pct"]) * 10)),
                                            "confidence": diag["confidence"], "explanation": text,
                                            "details": {"trigger": trig, "others_move_pct": {}, "scope": diag["scope"], "buy_pressure": snap.get("buy_pressure"), "action": res["action"]}})
        db.insert("live_signals", {
            "symbol": s, "trigger_kind": kind, "action": res["action"], "urgency": res["urgency"], "price": price, "model_bull_prob": base.get(s), "score": res["score"],
            "move_pct": trig["pct"] if trig else None, "move_minutes": trig["minutes"] if trig else None, "volume_spike": trig["volume_spike"] if trig else None,
            "ob_imbalance": snap.get("ob_imbalance"), "scope": diag["scope"] if diag else None, "cause": (diag["cause"] if diag else (news_by[s]["title"] if s in news_by else None)),
            "cause_confidence": diag["confidence"] if diag else None, "reasons": JsonList(res["reasons"]), "explanation": explain(s, res, trig, diag),
            "news_event_id": news_by[s]["id"] if (kind == "news" and s in news_by) else None, "event_id": event_id, "previous_action": prev,
            "expires_at": now + timedelta(hours=2 if res["urgency"] != "low" else 1)})
        out.append({"symbol": s, "action": res["action"], "urgency": res["urgency"], "text": explain(s, res, trig, diag)})
    return out


def publish_standing(db, cfg: dict) -> int:
    """After each prediction run, record the model's ordinary lean as the current 'scheduled' signal for each coin."""
    n = 0
    for s in SYMBOLS:
        p = db.one("select id, bullish_prob, price_at_prediction, created_at from predictions where symbol=%s and horizon_h=24 and variant='market' order by created_at desc limit 1", [s])
        if not p:
            continue
        already = db.one("select 1 x from live_signals where symbol=%s and trigger_kind='scheduled' and expires_at=%s", [s, p["created_at"] + timedelta(hours=7)])   # this prediction's signal
        newer = db.one("select 1 x from live_signals where symbol=%s and created_at >= %s", [s, p["created_at"]])                                            # a sudden-event signal is fresher
        if already or newer:
            continue
        action = standing_action(p["bullish_prob"], cfg)
        prev = db.one("select action from live_signals where symbol=%s order by created_at desc limit 1", [s])
        db.insert("live_signals", {"symbol": s, "trigger_kind": "scheduled", "action": action, "urgency": "low", "price": p["price_at_prediction"], "model_bull_prob": p["bullish_prob"],
                                   "score": (p["bullish_prob"] - 0.5) * 4, "reasons": JsonList([f"No sudden event. Model lean: {p['bullish_prob']:.0%} up over 24h"]),
                                   "explanation": f"{action} {s}: no sudden event; ordinary model read is {p['bullish_prob']:.0%} up over 24h.", "previous_action": prev["action"] if prev else None,
                                   "expires_at": p["created_at"] + timedelta(hours=7)})
        n += 1
    return n
