"""Reddit's OFFICIAL API (OAuth2 "script" app, client_credentials grant) - not scraping. Free: register a free
"script" type app at https://www.reddit.com/prefs/apps to get `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`. Read-only,
free tier rate limit (documented: 100 queries/min per OAuth client as of Reddit's current developer terms - verify
current limits at https://support.reddithelp.com/hc/en-us/articles/16160319875092). Skipped (not broken) without
both env vars. Not live-tested here (needs the user's own registered app credentials); mock-tested against Reddit's
documented OAuth response shape.
"""
import os
import time

import pandas as pd
import requests

from .base import Adapter, http_get

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
SUBREDDITS = {"BTC": "Bitcoin", "ETH": "ethereum", "XRP": "Ripple"}
_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}


def _get_token() -> str | None:
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return None
    if _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["expires_at"]:
        return _TOKEN_CACHE["token"]
    r = requests.post(TOKEN_URL, auth=(cid, secret), data={"grant_type": "client_credentials"},
                      headers={"User-Agent": "crypto-ai-lab-research/1.0"}, timeout=15)
    r.raise_for_status()
    j = r.json()
    _TOKEN_CACHE.update(token=j["access_token"], expires_at=time.time() + j.get("expires_in", 3600) - 60)
    return _TOKEN_CACHE["token"]


def fetch_hot_posts(now, symbols=("BTC", "ETH", "XRP"), limit: int = 25) -> pd.DataFrame:
    token = _get_token()
    if not token:
        return pd.DataFrame()
    rows = []
    for s in symbols:
        j = http_get(f"{API_BASE}/r/{SUBREDDITS[s]}/hot", {"limit": str(limit)}, headers={"Authorization": f"bearer {token}", "User-Agent": "crypto-ai-lab-research/1.0"})
        for child in j.get("data", {}).get("children", []):
            d = child["data"]
            rows.append({"symbol": s, "subreddit": SUBREDDITS[s], "post_id": d["id"], "ts": pd.to_datetime(d["created_utc"], unit="s", utc=True),
                        "title": d["title"], "score": d["score"], "num_comments": d["num_comments"], "upvote_ratio": d.get("upvote_ratio"), "collected_at": now})
    return pd.DataFrame(rows)


ADAPTERS = [
    Adapter("reddit_hot_posts", "social_sentiment", "Reddit official API (needs REDDIT_CLIENT_ID/SECRET, free app)", ["BTC", "ETH", "XRP"], fetch_hot_posts, poll_every_s=600,
           notes="Post title/score/comment-count only, no text sentiment scoring here (see optional LLM layer for that)."),
]
