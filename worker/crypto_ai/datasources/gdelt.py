"""GDELT Project Doc API. Free, no key. Its own published rate limit is one request per 5 seconds (poll_every_s
enforces far more headroom than that). Note: this sandbox's shared outbound IP was already being told to slow down
when this was verified live (2026-09) - GDELT is a heavily-shared public resource, so a busy IP (shared hosting,
CI runners, etc.) can see this even at a compliant request rate; it is expected to work normally from a typical
home/cloud-VM IP. Complements the project's existing official-source RSS feeds (research/feeds.py) with broader
global news-volume/tone signal for "bitcoin"/"ethereum"/"XRP" search terms.
"""
from datetime import datetime

import pandas as pd

from .base import Adapter, http_get

BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
QUERY = {"BTC": "bitcoin", "ETH": "ethereum", "XRP": "XRP ripple"}


def fetch_articles(now: datetime, symbols=("BTC", "ETH", "XRP"), timespan: str = "1h", maxrecords: int = 25) -> pd.DataFrame:
    rows = []
    for s in symbols:
        try:
            # Shorter timeout/fewer retries than the default: a busy shared IP can see GDELT stall on every attempt
            # (observed live from this sandbox), and this adapter should fail fast rather than spend minutes per poll.
            j = http_get(BASE, {"query": QUERY[s], "mode": "artlist", "maxrecords": str(maxrecords), "format": "json", "timespan": timespan}, retries=2, timeout=8)
        except Exception:
            continue   # GDELT sometimes returns a plain-text rate-limit message instead of JSON, or times out - skip this symbol, don't crash the batch
        if not isinstance(j, dict):
            continue
        for a in j.get("articles", []):
            rows.append({"symbol": s, "ts": pd.to_datetime(a.get("seendate"), format="%Y%m%dT%H%M%SZ", utc=True, errors="coerce"),
                        "title": a.get("title"), "url": a.get("url"), "domain": a.get("domain"), "language": a.get("language"),
                        "source_country": a.get("sourcecountry"), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("gdelt_articles", "news", "GDELT Project Doc API", ["BTC", "ETH", "XRP"], fetch_articles, poll_every_s=900,
           notes="Global news volume, not sentiment-scored here; respects GDELT's own 1-req/5s limit via poll_every_s."),
]
