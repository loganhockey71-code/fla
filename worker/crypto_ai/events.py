"""Keyword lexicon + first-pass scoring (coins, sentiment, importance) used by the research layer.
Transparent rules, not a prediction. Source lists live in research/registry.py."""
import re

COIN_PATTERNS = {
    "BTC": r"\b(bitcoin|btc|spot bitcoin|satoshi)\b",
    "ETH": r"\b(ethereum|ether|eth|vitalik|erc-20|eip-\d+)\b",
    "XRP": r"\b(xrp|ripple|xrpl|rippled|garlinghouse)\b",
}
BROAD = r"\b(crypto(currenc(y|ies))?|digital assets?|stablecoins?|blockchain|bitcoin|defi|virtual currenc(y|ies))\b"
MACRO = r"\b(fomc|federal open market committee|interest rates?|rate (cut|hike|decision)|inflation|monetary policy|cpi|federal funds)\b"

POSITIVE = r"\b(approv\w*|etf inflows?|dismiss\w*|rebound\w*|recover\w*|adopt\w*|launch\w*|partnership|upgrade|rate cut|lower(s|ed)? rates?|win|wins|victory|settle\w*|record|surge\w*|rall(y|ies)|green light|bullish|integrat\w*)\b"
NEGATIVE = r"\b(lawsuit|sues?|sued|charg(es|ed)|fraud|ban(s|ned)?|hack\w*|exploit\w*|crackdown|enforcement|reject\w*|den(y|ies|ied)|hike\w*|raises? rates?|setbacks?|defeat\w*|sell-?off|stall\w*|blocked|fails?|failed|slump\w*|tumble\w*|penalt(y|ies)|fines?|fined|shut ?down|insolven\w*|bankrupt\w*|outage|plunge\w*|crash\w*|bearish|delist\w*|warning|investigation|subpoena)\b"
HIGH_IMPACT = r"\b(etf|sec|cftc|lawsuit|enforcement|ban|approval|approves?|fomc|rate (cut|hike|decision)|hack|exploit|legislation|bill|stablecoin|clarity act|market structure|settlement|delist\w*|bankrupt\w*)\b"

NOISE = r"(most-viewed|newsletter|podcast|weekly roundup|webinar)"
BASE = {"official": 45, "project": 35, "media": 20}
CAP = {"official": 100, "project": 80, "media": 65}       # media can never outrank a primary source
CONF_BASE = {"official": 80, "project": 70, "media": 50}


def classify(title: str, summary: str, tier: str) -> dict:
    text = f"{title}. {summary or ''}".lower()
    coins = [c for c, pat in COIN_PATTERNS.items() if re.search(pat, text)]
    broad = bool(re.search(BROAD, text))
    macro = bool(re.search(MACRO, text)) and tier == "official"
    if not coins and (broad or macro):
        coins = ["BTC", "ETH", "XRP"]
    pos, neg = len(re.findall(POSITIVE, text)), len(re.findall(NEGATIVE, text))
    sentiment = "positive" if pos > neg else "negative" if neg > pos else "neutral"
    impact = len(re.findall(HIGH_IMPACT, text))
    importance = 0
    if coins:
        importance = BASE[tier] + min(impact, 3) * 9 + (10 if len(coins) < 3 else 0) + min(abs(pos - neg), 2) * 4
        if tier == "official" and not (broad or macro or len(coins) < 3):
            importance = min(importance, 25)        # official but unrelated to crypto/macro
        importance = min(importance, CAP[tier])
    if re.search(NOISE, text):
        importance = min(importance, 30)         # generic lists / newsletters are not events
    conf = CONF_BASE[tier] + (10 if abs(pos - neg) >= 2 else 0) - (15 if sentiment == "neutral" else 0) + (5 if len(coins) < 3 else -5)
    return {"coins": coins, "sentiment": sentiment, "importance": int(importance),
            "confidence": int(max(10, min(conf, 95)))}
