"""Local (or, via DATALAKE_ROOT, any writable path - see below) Parquet data lake. Supabase stores only small
metadata (crypto_ai.scalp trades/features/labels/manifest rows) - large raw datasets from these adapters do NOT go
into Postgres. Layout: <root>/<category>/<source>/<symbol_or_all>/<YYYY-MM-DD>.parquet, one file per adapter per
day, appended-to (read-merge-write) within a day so a source polled every few minutes doesn't create thousands of
tiny files.

DATALAKE_ROOT can point anywhere writable: local disk by default, or an S3/GCS/R2 path if `fsspec` plus that
service's optional package (`s3fs`, `gcsfs`, ...) is installed - pandas' `to_parquet`/`read_parquet` already
understand such URLs through fsspec. Nothing else in this module needs to change for that; it is a deployment
choice, not a code change.
"""
import os
from datetime import datetime, timezone

import pandas as pd

DEFAULT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", ".cache", "datalake")


def root(root: str | None = None) -> str:
    return root or os.environ.get("DATALAKE_ROOT") or DEFAULT_ROOT


def _path(category: str, source: str, now: datetime, root_: str) -> str:
    day = now.strftime("%Y-%m-%d")
    return f"{root_}/{category}/{source}/{day}.parquet"


def write(df: pd.DataFrame, category: str, source: str, now: datetime | None = None, root: str | None = None) -> str:
    """Append `df` to today's partition for (category, source), de-duplicating on every column shared with what's
    already there (so re-running an adapter, or overlapping polls, never doubles rows)."""
    now = now or datetime.now(timezone.utc)
    root_ = globals()["root"](root)
    path = _path(category, source, now, root_)
    is_local = "://" not in path
    if is_local:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = None
    try:
        existing = pd.read_parquet(path)
    except (FileNotFoundError, OSError):
        existing = None
    out = pd.concat([existing, df], ignore_index=True) if existing is not None and len(existing) else df
    dedupe_cols = [c for c in out.columns if out[c].apply(lambda v: isinstance(v, (list, dict))).sum() == 0]
    out = out.drop_duplicates(subset=dedupe_cols or None, keep="last")
    out.to_parquet(path, index=False)
    return path


def read(category: str, source: str, start: datetime, end: datetime, root: str | None = None) -> pd.DataFrame:
    """Everything written for (category, source) between two dates (inclusive of any day the range touches)."""
    root_ = globals()["root"](root)
    days = pd.date_range(start.date(), end.date(), freq="D")
    parts = []
    for d in days:
        p = f"{root_}/{category}/{source}/{d:%Y-%m-%d}.parquet"
        try:
            parts.append(pd.read_parquet(p))
        except (FileNotFoundError, OSError):
            continue
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ---------------------------------------------------------------- Supabase manifest (metadata only - see schema_datalake.sql)
def record_manifest(db, category: str, adapter_name: str, source: str, path: str | None, rows: int, now: datetime, error: str | None = None) -> None:
    """One row per (adapter, day), upserted on every run - `error` (if any) and `path`/`rows` always reflect the
    MOST RECENT run, so the dashboard's 'errors' column shows the current state, not just the first failure."""
    db.run("""insert into datalake_manifest (category, adapter, source, day, path, rows, collected_at, last_error)
              values (%s,%s,%s,%s,%s,%s,%s,%s)
              on conflict (category, adapter, day) do update set rows = excluded.rows,
                path = coalesce(excluded.path, datalake_manifest.path), collected_at = excluded.collected_at, last_error = excluded.last_error""",
          [category, adapter_name, source, now.date(), path, rows, now, error])


def run_due(adapters: list, now: datetime | None = None, lake_root: str | None = None, db=None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {a.name: a.run(now, lake_root, db) for a in adapters if a.due(now)}
