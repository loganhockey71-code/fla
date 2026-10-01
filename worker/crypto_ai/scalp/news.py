"""News context for the scalper: what the freshest research events say about ONE coin right now, and what that means for a long or a short.

The research layer (crypto_ai/research) already classifies every item: which coins it touches, bullish / bearish / neutral, how
important, how credible the source is, and which independent source FAMILIES (primary = regulators/projects, press, prediction
markets) reported it. This module turns that into a decision input:

  * each event's signed impact (-100..+100 for this coin) is weighted by source credibility x confidence x novelty x freshness
    (a headline's weight halves every `scalp_news_half_life_min`);
  * bullish and bearish weight are summed separately, and the independent source families on the winning side are counted:
    several independent families agreeing = higher confidence, five outlets rewriting one press release = still one;
  * if BOTH sides carry real weight and are close, the sources CONFLICT: no trade;
  * strong news against the trade blocks it, weak opposition halves it, agreeing news (more so with 2+ independent families)
    allows a somewhat larger position (still inside every risk cap), important official news with no clear direction halves it.

Point-in-time: only events WE had detected by `now` count (`detected_at <= now`); stories we merely noticed hours late are ignored,
exactly like the research features. There is no historical news for a backtest, so the backtest runs with no news (documented in
the report): the news layer is judged on live signals, which record the context they were taken in.
"""
from datetime import timedelta

IMPACT_COL = {"BTC": "btc_impact_score", "ETH": "eth_impact_score", "XRP": "xrp_impact_score"}
TIER_FAMILY = {1: "primary", 2: "primary", 3: "press", 4: "press", 5: "market"}
MAX_SIDE = 150.0                 # one side's summed weight is capped so a flood of headlines cannot look infinitely sure
CONFLICT_RATIO = 0.6             # the weaker side is at least this share of the stronger one
NEUTRAL_CTX = {"bias": "none", "net": 0.0, "bull": 0.0, "bear": 0.0, "confirmations": 0, "n_events": 0, "top": [], "conflict": False}


def load_events(db, symbol: str, now, window_min: float) -> list[dict]:
    """Recent research events touching `symbol`, as the pure functions below expect. Read-only."""
    col = IMPACT_COL[symbol]
    since = now - timedelta(minutes=window_min)
    rows = db.all(f"""select id, title, source, origin_tier, importance_score, {col} as impact, sentiment, event_category, source_credibility_score,
                             confidence_score, novelty_score, independent_confirmations, detected_at, published_at, details
                      from research_events
                      where %s = any(affected_coins) and kind in ('news','legislation','macro') and detected_at > %s and detected_at <= %s
                        and (kind <> 'news' or coalesce(published_at, detected_at) > %s)
                        and not (kind = 'legislation' and details->>'change' = 'new')
                      order by detected_at desc""", [symbol, since, now, now - timedelta(hours=6)])
    return rows


def context(events: list[dict], now, cfg: dict) -> dict:
    """Weighted, de-duplicated read of the news for one coin. Pure."""
    window, hl = float(cfg["scalp_news_window_min"]), float(cfg["scalp_news_half_life_min"])
    bull = bear = 0.0
    fam_bull: set = set()
    fam_bear: set = set()
    unclear_important = False
    used = []
    for e in events or []:
        det = e.get("detected_at")
        if det is None:
            continue
        age = (now - det).total_seconds() / 60.0
        if age < 0 or age > window:                                  # not yet detected at `now`, or too old to matter for a scalp
            continue
        impact = float(e.get("impact") or 0.0)
        w = ((e.get("source_credibility_score") or 50) / 100.0) * ((e.get("confidence_score") or 50) / 100.0) \
            * ((e.get("novelty_score") if e.get("novelty_score") is not None else 100) / 100.0) * 0.5 ** (age / max(hl, 1e-9))
        tier = int(e.get("origin_tier") or 4)
        fams = set((e.get("details") or {}).get("families") or []) or {TIER_FAMILY.get(tier, "press")}
        if impact > 0:
            bull += impact * w
            fam_bull |= fams
        elif impact < 0:
            bear += -impact * w
            fam_bear |= fams
        elif (e.get("importance_score") or 0) >= 60 and tier <= 2 and age <= 30:
            unclear_important = True                                 # a major official item with no readable direction = event risk
        used.append({"title": str(e.get("title", ""))[:110], "impact": impact, "weight": round(w, 3), "age_min": round(age, 1), "tier": tier})
    bull, bear = min(bull, MAX_SIDE), min(bear, MAX_SIDE)
    net = bull - bear
    cmin = float(cfg["scalp_news_conflict_min"])
    conflict = bull >= cmin and bear >= cmin and min(bull, bear) / max(bull, bear) >= CONFLICT_RATIO
    if conflict:
        bias = "conflict"
    elif net >= cfg["scalp_news_min_score"]:
        bias = "bullish"
    elif net <= -cfg["scalp_news_min_score"]:
        bias = "bearish"
    elif unclear_important:
        bias = "unclear"
    else:
        bias = "none"
    conf = len(fam_bull) if net > 0 else len(fam_bear) if net < 0 else max(len(fam_bull), len(fam_bear))
    top = sorted(used, key=lambda u: -abs(u["impact"]) * u["weight"])[:3]
    return {"bias": bias, "net": round(net, 2), "bull": round(bull, 2), "bear": round(bear, 2), "confirmations": int(conf),
            "n_events": len(used), "top": top, "conflict": bool(conflict)}


def effect(ctx: dict | None, direction: int, cfg: dict) -> dict:
    """What `ctx` means for a trade in `direction` (+1 long / -1 short): {'allow', 'size_mult', 'reason', 'alignment'}."""
    ok = {"allow": True, "size_mult": 1.0, "reason": "ok", "alignment": "none"}
    if not cfg.get("scalp_news_enabled", True) or not ctx:
        return ok
    bias = ctx["bias"]
    if bias == "conflict":
        return {"allow": False, "size_mult": 0.0, "reason": "news_conflict", "alignment": "conflict"}
    if bias == "unclear":
        return {"allow": True, "size_mult": 0.5, "reason": "news_unclear", "alignment": "unclear"}
    if bias == "none":
        return ok
    aligned = (bias == "bullish") == (direction > 0)
    if aligned:
        mult = 1.25 if ctx["confirmations"] >= 2 else 1.1
        return {"allow": True, "size_mult": mult, "reason": "news_supports", "alignment": "supports"}
    if abs(ctx["net"]) >= cfg["scalp_news_block_score"]:
        return {"allow": False, "size_mult": 0.0, "reason": "news_opposes", "alignment": "opposes"}
    return {"allow": True, "size_mult": 0.5, "reason": "news_caution", "alignment": "opposes"}


def effects(ctx: dict | None, cfg: dict) -> dict:
    """{+1: effect for a long, -1: effect for a short} - the form entry_decision takes in ctx['news']."""
    return {1: effect(ctx, 1, cfg), -1: effect(ctx, -1, cfg)}
