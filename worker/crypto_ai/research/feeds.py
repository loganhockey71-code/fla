"""RSS/Atom pollers (SEC, Federal Reserve, CFTC, XRPL, Ethereum, news). Free public feeds only."""
import re
from datetime import datetime, timedelta, timezone

import feedparser

from ..http import get_conditional
from .common import record_event

MAX_AGE_DAYS = 14        # ignore old backlog; feeds like blog.ethereum.org expose hundreds of past posts
MAX_ENTRIES = 60
HINTS = {"xrpl_rippled": "token_or_network_update", "eth_geth": "token_or_network_update"}


def poll_rss(db, src: dict) -> dict:
    status, body, etag, lm = get_conditional(src["url"], src.get("etag"), src.get("last_modified"))
    db.run("update source_registry set etag=%s, last_modified=%s where key=%s", [etag, lm, src["key"]])
    if status == 304:
        return {"status": "not_modified", "new": 0, "dup": 0}
    feed = feedparser.parse(body)
    new = dup = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    for e in feed.entries[:MAX_ENTRIES]:
        link, title = e.get("link"), re.sub(r"\s+", " ", (e.get("title") or "")).strip()
        if not link or not title:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else datetime.now(timezone.utc)
        if published < cutoff:
            continue
        summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("summary", "") or ""))[:1000].strip()
        res = record_event(db, src, {"external_id": e.get("id") or link, "title": title, "url": link, "published_at": published,
                                     "summary": summary, "raw": f"{src['name']} | {link}", "kind": "news",
                                     "category_hint": HINTS.get(src["key"])})
        new += res == "new"
        dup += res == "duplicate"
    return {"status": "ok", "new": new, "dup": dup}
