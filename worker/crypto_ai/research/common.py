"""Event classification, impact scoring and duplicate grouping shared by every research source.

Credibility tiers (1 = most trusted):
  1 official government/regulator   2 official company/project   3 major financial news
  4 crypto publication              5 prediction market           6 social/unverified (never ingested)
"""
import re
from datetime import datetime, timedelta, timezone

from .. import events as lex

TIERS = {
    1: ("official_government", 95), 2: ("official_project", 85), 3: ("major_financial_news", 70),
    4: ("crypto_publication", 55), 5: ("prediction_market", 45), 6: ("social_unverified", 20),
}
FAMILY = {1: "primary", 2: "primary", 3: "press", 4: "press", 5: "market", 6: "social"}

CATEGORIES = ["regulation", "legislation", "fed", "interest_rates", "inflation", "ETF", "lawsuit", "enforcement",
              "exchange", "hack", "whale_activity", "partnership", "token_or_network_update", "macro", "adoption", "other"]

# first match wins
CATEGORY_RULES = [
    ("hack", r"\b(hack\w*|exploit\w*|stolen|drained|security breach|rug ?pull)\b"),
    ("ETF", r"\b(etfs?|exchange[- ]traded (fund|product)s?)\b"),
    ("lawsuit", r"\b(lawsuit|sues?|sued|complaint|court|judge|ruling|appeal\w*|litigation|plaintiff)\b"),
    ("enforcement", r"\b(enforcement|charg(es|ed)|fraud|penalt(y|ies)|fined?|cease-and-desist|wells notice|subpoena|investigation|settle(s|d|ment)?)\b"),
    ("interest_rates", r"\b(interest rates?|rate (cut|hike|decision)s?|federal funds|fomc|discount rate|basis points?|yields?)\b"),
    ("inflation", r"\b(inflation|cpi|pce|consumer price)\b"),
    ("fed", r"\b(federal reserve|fed|powell|federal open market|monetary policy|beige book)\b"),
    ("legislation", r"\b(bill|act|senate|house of representatives|congress\w*|legislat\w*|hearing|committee|markup|amendment)\b"),
    ("regulation", r"\b(regulat\w*|rulemaking|guidance|no-action|framework|compliance|licen[cs]e\w*|stablecoins?|proposed rule|final rule)\b"),
    ("exchange", r"\b(exchange|listing|delist\w*|binance|coinbase|kraken|okx|custody|trading halt)\b"),
    ("whale_activity", r"\b(whales?|large transfers?|wallet moves?|on-chain)\b"),
    ("partnership", r"\b(partner\w*|collaborat\w*|integrat\w*)\b"),
    ("token_or_network_update", r"\b(upgrade|hard fork|mainnet|release[sd]?|version|eip-\d+|protocol|validators?|rippled|geth|amendment)\b"),
    ("adoption", r"\b(adopt\w*|reserve|treasury|accepts?|payments?|institutional|bank)\b"),
    ("macro", r"\b(unemployment|payrolls?|jobs report|gdp|recession|labor market|tariffs?|jobless)\b"),
]

CATEGORY_BUMP = {"hack": 15, "ETF": 15, "lawsuit": 12, "enforcement": 12, "interest_rates": 12, "fed": 10, "inflation": 10,
                 "legislation": 10, "regulation": 10, "exchange": 6, "macro": 6, "adoption": 6, "partnership": 3,
                 "token_or_network_update": 3, "whale_activity": 3, "other": 0}
# typical size of a crypto-market move for that kind of event (before importance/sentiment scaling)
CATEGORY_MAG = {"hack": 55, "ETF": 60, "lawsuit": 55, "enforcement": 50, "interest_rates": 40, "fed": 35, "inflation": 35,
                "legislation": 45, "regulation": 45, "exchange": 40, "macro": 30, "adoption": 35, "partnership": 25,
                "token_or_network_update": 25, "whale_activity": 25, "other": 10}
# how strongly each coin reacts to each category (BTC, ETH, XRP)
COIN_SENS = {
    "ETF": (1.0, 0.9, 0.7), "lawsuit": (0.6, 0.7, 1.4), "enforcement": (0.7, 0.8, 1.3), "regulation": (0.8, 0.9, 1.2),
    "legislation": (0.8, 0.9, 1.2), "interest_rates": (1.0, 0.9, 0.8), "fed": (1.0, 0.9, 0.8), "inflation": (1.0, 0.9, 0.8),
    "macro": (1.0, 0.9, 0.8), "hack": (0.7, 1.0, 0.8), "token_or_network_update": (0.4, 1.0, 1.0),
}
CRED_CAP = {1: 100, 2: 85, 3: 75, 4: 65, 5: 60, 6: 30}
MIN_IMPORTANCE = 30


def category_of(text: str) -> str:
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, text, re.I):
            return cat
    return "other"


def classify(title: str, summary: str, tier: int, hint: str | None = None) -> dict | None:
    """Returns None when the item is not relevant to BTC/ETH/XRP."""
    lex_tier = {1: "official", 2: "project"}.get(tier, "media")
    base = lex.classify(title, summary, lex_tier)
    if not base["coins"]:
        return None
    text = f"{title}. {summary or ''}"
    cat = category_of(title)
    if cat == "other":
        cat = category_of(summary or "")
    if hint and cat != "hack":
        cat = hint                                   # e.g. release notes from a protocol repo are network updates
    named = [c for c, pat in lex.COIN_PATTERNS.items() if re.search(pat, text.lower())]
    hits = len(re.findall(lex.HIGH_IMPACT, text.lower()))
    importance = (lex.BASE[lex_tier] + 0 + CATEGORY_BUMP[cat] + min(hits, 3) * 6 + (8 if 0 < len(named) < 3 else 0)
                  + min(abs(len(re.findall(lex.POSITIVE, text.lower())) - len(re.findall(lex.NEGATIVE, text.lower()))), 2) * 3)
    if tier == 1 and not named and cat in ("other", "enforcement", "lawsuit", "regulation") and not re.search(lex.BROAD, text.lower()):
        importance = min(importance, 25)                       # an official item that isn't about crypto
    if re.search(lex.NOISE, text.lower()):
        importance = min(importance, 30)
    importance = int(min(importance, CRED_CAP[tier]))
    return {"category": cat, "coins": base["coins"], "named": named, "sentiment": base["sentiment"], "importance": importance}


def impacts(category: str, sentiment: str, importance: int, coins: list[str], named: list[str]) -> dict[str, int]:
    """Expected move per coin, -100..+100. Neutral events carry no direction, hence zero."""
    sign = {"positive": 1, "negative": -1}.get(sentiment, 0)
    sens = COIN_SENS.get(category, (1.0, 1.0, 1.0))
    out = {}
    for coin, s in zip(("BTC", "ETH", "XRP"), sens):
        if coin not in coins:
            out[coin] = 0
            continue
        focus = 1.0 if (coin in named or not named) else 0.5      # event about another coin bleeds over at half weight
        out[coin] = int(max(-100, min(100, round(sign * CATEGORY_MAG[category] * (importance / 100) * s * focus * 1.6))))
    return out


# ---------------------------------------------------------------- duplicate detection
_STOP = set("the a an and or of to in on for with by at from as is are was were be has have will its it this that new says said "
            "after over about into amid report reports update announces announced news".split())


def tokens(title: str) -> set[str]:
    words = [w for w in re.findall(r"[a-z0-9$%.]+", title.lower()) if len(w) > 2 and w not in _STOP]
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return max(inter / len(ta | tb), 0.9 * inter / min(len(ta), len(tb)))


DUP_THRESHOLD = 0.6


def independent_confirmations(families: set[str]) -> int:
    """Copies inside one family (five outlets rewriting one press release) count once."""
    return max(1, len(families & {"primary", "press", "market"}))


def confidence(cred: int, sentiment: str, confirmations: int) -> int:
    return int(min(95, round(0.7 * cred + (10 if sentiment != "neutral" else 0) + 5 * (confirmations - 1) + 5)))


# ---------------------------------------------------------------- persistence
def _now():
    return datetime.now(timezone.utc)


def record_event(db, src: dict, item: dict) -> str | None:
    """src: source_registry row. item: {external_id,title,url,published_at,summary,raw,kind, + optional overrides}.
    Returns 'new' | 'duplicate' | None (already known / irrelevant)."""
    if db.one("select 1 x from research_events where source_key=%s and external_id=%s", [src["key"], item["external_id"]]):
        return None
    if item.get("url") and db.one("select 1 x from event_duplicates where url=%s", [item["url"]]):
        return None
    tier = src["tier"]
    cls = classify(item["title"], item.get("summary") or "", tier, item.get("category_hint")) if not item.get("preclassified") else item["preclassified"]
    if cls is None or cls["importance"] < MIN_IMPORTANCE:
        return None

    since = _now() - timedelta(hours=72)
    best, best_sim = None, 0.0
    rows = [] if item.get("no_dedupe") else db.all(
        "select id, title, origin_tier, source_key, details, affected_coins from research_events where detected_at > %s "
        "and kind = %s", [since, item.get("kind", "news")])
    for row in rows:
        sim = similarity(item["title"], row["title"])
        if sim > best_sim:
            best, best_sim = row, sim

    if best and best_sim >= DUP_THRESHOLD:
        return _fold_duplicate(db, src, item, best, best_sim, cls)

    imp = impacts(cls["category"], cls["sentiment"], cls["importance"], cls["coins"], cls["named"])
    cred = src["credibility_score"]
    prob, prob_src = item.get("event_probability"), item.get("event_probability_source")
    if prob is None:
        prob, prob_src = (1.0, "published_fact") if tier <= 2 else (0.9, "reported_by_press")
    novelty = int(max(20, round(100 * (1 - best_sim)))) if best else 100
    db.insert("research_events", {
        "kind": item.get("kind", "news"), "title": item["title"][:400], "source": src["name"], "source_key": src["key"],
        "source_url": item.get("url"), "external_id": item["external_id"], "published_at": item.get("published_at"),
        "detected_at": _now(), "event_category": cls["category"], "affected_coins": cls["coins"],
        "sentiment": cls["sentiment"], "importance_score": cls["importance"], "source_credibility_score": cred,
        "novelty_score": novelty, "confidence_score": confidence(cred, cls["sentiment"], 1),
        "btc_impact_score": imp["BTC"], "eth_impact_score": imp["ETH"], "xrp_impact_score": imp["XRP"],
        "event_probability": prob, "event_probability_source": prob_src,
        "summary": (item.get("summary") or item["title"])[:600], "raw_text_or_reference": (item.get("raw") or item.get("url") or "")[:2000],
        "fingerprint": " ".join(sorted(tokens(item["title"])))[:300], "origin_tier": tier,
        "details": {**item.get("details", {}), "families": [FAMILY[tier]]},
    })
    return "new"


def _fold_duplicate(db, src, item, canon, sim, cls) -> str:
    """Attach a copy to the canonical event. Promote the copy to origin if it is a more credible source."""
    tier = src["tier"]
    families = set((canon["details"] or {}).get("families", [FAMILY.get(canon["origin_tier"] or 4, "press")]))
    fam = FAMILY[tier]
    independent = fam not in families
    families.add(fam)
    n_conf = independent_confirmations(families)
    db.insert("event_duplicates", {"event_id": canon["id"], "source_key": src["key"], "source_name": src["name"], "url": item.get("url"),
                                   "title": item["title"][:400], "published_at": item.get("published_at"), "similarity": round(sim, 3),
                                   "counts_as_independent": independent},
              on_conflict="on conflict (event_id, url) do nothing")
    cur = db.one("select * from research_events where id=%s", [canon["id"]])
    fields = {"duplicate_count": cur["duplicate_count"] + 1, "independent_confirmations": n_conf, "updated_at": _now(),
              "details": {**(cur["details"] or {}), "families": sorted(families)}}
    origin_tier = cur["origin_tier"] or 4
    if tier < origin_tier and item.get("kind", "news") == cur["kind"]:            # more credible origin found: promote
        db.insert("event_duplicates", {"event_id": canon["id"], "source_key": cur["source_key"], "source_name": cur["source"],
                                       "url": cur["source_url"], "title": cur["title"], "published_at": cur["published_at"],
                                       "similarity": 1.0, "counts_as_independent": False},
                  on_conflict="on conflict (event_id, url) do nothing")
        imp = impacts(cur["event_category"], cur["sentiment"], min(cur["importance_score"] + 10, CRED_CAP[tier]), cur["affected_coins"], [])
        fields.update({"source": src["name"], "source_key": src["key"], "source_url": item.get("url"),
                       "external_id": item["external_id"], "origin_tier": tier, "source_credibility_score": src["credibility_score"],
                       "importance_score": min(cur["importance_score"] + 10, CRED_CAP[tier]),
                       "event_probability": 1.0 if tier <= 2 else cur["event_probability"],
                       "event_probability_source": "published_fact" if tier <= 2 else cur["event_probability_source"]})
    fields["confidence_score"] = confidence(fields.get("source_credibility_score", cur["source_credibility_score"]), cur["sentiment"], n_conf)
    db.update("research_events", canon["id"], fields)
    return "duplicate"
