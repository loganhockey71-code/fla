"""The adapter contract every data source implements. A new source = a new module with a module-level `ADAPTERS`
list of `Adapter` instances; `crypto_ai.cli collect-datasources` and `datalake.run_due` discover and run them
without any other code changing.

An Adapter's `fetch(now)` does the HTTP call(s) and returns a plain pandas DataFrame in the source's own natural
shape (documented in DATA_SOURCES.md, not enforced here - normalising across sources happens at research time, one
category at a time, once there's enough collected to be worth it). `run()` is the thin, shared wrapper: fetch, then
hand the frame to `datalake.write` (Parquet, partitioned) plus one manifest row - so every adapter gets the same
storage behaviour for free and only needs to implement `fetch`.
"""
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd
import requests

DEFAULT_TIMEOUT = 15
RETRY_STATUSES = {429, 500, 502, 503, 504}


def http_get(url: str, params: dict | None = None, retries: int = 3, timeout: int = DEFAULT_TIMEOUT, headers: dict | None = None) -> dict | list:
    """Shared, polite GET: capped retries with backoff on 429/5xx, a descriptive User-Agent, and it raises on
    anything else so a broken source fails loudly instead of silently returning nothing. `headers` (e.g. an
    optional free API key) are merged on top of the default User-Agent, never logged."""
    last = None
    hdrs = {"User-Agent": "crypto-ai-lab-research/1.0 (free/no-key public endpoint)", **(headers or {})}
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=hdrs)
            if r.status_code in RETRY_STATUSES and i < retries - 1:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def http_post_json(url: str, body: dict, retries: int = 3, timeout: int = DEFAULT_TIMEOUT, headers: dict | None = None) -> dict:
    """Same retry/backoff/UA contract as http_get, for JSON-RPC style sources (e.g. a public rippled node) that
    take their request as a POSTed JSON body instead of query params."""
    last = None
    hdrs = {"User-Agent": "crypto-ai-lab-research/1.0 (free/no-key public endpoint)", "Content-Type": "application/json", **(headers or {})}
    for i in range(retries):
        try:
            r = requests.post(url, json=body, timeout=timeout, headers=hdrs)
            if r.status_code in RETRY_STATUSES and i < retries - 1:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last


@dataclass
class Adapter:
    name: str                       # unique id, e.g. "okx_funding_rate"
    category: str                   # one of the DATA_SOURCES.md categories, e.g. "funding_rates"
    source: str                     # human name of the source, e.g. "OKX public API"
    symbols: list[str]              # our symbols this covers, e.g. ["BTC", "ETH", "XRP"]
    fetch: Callable[[datetime], pd.DataFrame]
    poll_every_s: int = 300         # how often `run_due` considers this adapter due
    license_note: str = "Public market-data API; no key required; see DATA_SOURCES.md for terms."
    notes: str = ""
    last_run: dict = field(default_factory=dict)   # in-memory only; per-process due-checking

    def due(self, now: datetime) -> bool:
        last = self.last_run.get("at")
        return last is None or (now - last).total_seconds() >= self.poll_every_s

    def run(self, now: datetime | None = None, lake_root: str | None = None, db=None) -> dict:
        """Fetch once, write to the data lake (Parquet), and (if `db` is given) upsert one manifest row. Never
        raises past this point for a scheduled/batch run - the caller sees {'ok': False, 'error': ...} instead, so
        one broken source can't take down a `collect-datasources` pass over all of them."""
        from . import datalake
        now = now or datetime.now(timezone.utc)
        self.last_run["at"] = now
        try:
            df = self.fetch(now)
        except Exception as e:
            traceback.print_exc()
            if db is not None:
                datalake.record_manifest(db, self.category, self.name, self.source, None, 0, now, error=str(e)[:500])
            return {"ok": False, "adapter": self.name, "error": str(e)}
        if df is None or df.empty:
            if db is not None:
                datalake.record_manifest(db, self.category, self.name, self.source, None, 0, now)
            return {"ok": True, "adapter": self.name, "rows": 0}
        path = datalake.write(df, category=self.category, source=self.name, now=now, root=lake_root)
        if db is not None:
            datalake.record_manifest(db, self.category, self.name, self.source, path, len(df), now)
        return {"ok": True, "adapter": self.name, "rows": len(df), "path": path}
