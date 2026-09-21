import numpy as np
import psycopg2
import psycopg2.extensions
import psycopg2.extras

from .config import DEFAULT_SETTINGS, database_url

psycopg2.extras.register_uuid()
# numpy scalars (from pandas) must reach Postgres as plain numbers
psycopg2.extensions.register_adapter(np.float64, lambda x: psycopg2.extensions.AsIs(repr(float(x)) if np.isfinite(x) else "'NaN'::float"))
psycopg2.extensions.register_adapter(np.int64, lambda x: psycopg2.extensions.AsIs(int(x)))
psycopg2.extensions.register_adapter(np.bool_, lambda x: psycopg2.extensions.AsIs("true" if x else "false"))


def _adapt(v):
    return psycopg2.extras.Json(v) if isinstance(v, (dict, list)) and not _is_pg_array(v) else v


def _is_pg_array(v) -> bool:
    # Python lists of plain strings (e.g. coins) are sent as Postgres arrays; wrap dicts/nested lists as JSON.
    return isinstance(v, list) and all(isinstance(x, str) for x in v) and not getattr(v, "_json", False)


class JsonList(list):
    """Marks a list that must be stored as jsonb rather than a Postgres array."""
    _json = True


class DB:
    def __init__(self, url: str | None = None):
        self.conn = psycopg2.connect(url or database_url())
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute("set time zone 'UTC'")

    def all(self, sql, params=None) -> list[dict]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    def one(self, sql, params=None) -> dict | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def run(self, sql, params=None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def insert(self, table: str, row: dict, returning: str = "id", on_conflict: str = ""):
        cols = list(row)
        vals = [_adapt(v) for v in row.values()]
        sql = (f"insert into {table} ({','.join(cols)}) values ({','.join(['%s'] * len(cols))}) "
               f"{on_conflict} returning {returning}")
        r = self.one(sql, vals)
        return r[returning] if r else None

    def update(self, table: str, row_id, fields: dict, key: str = "id") -> None:
        sets = ",".join(f"{c}=%s" for c in fields)
        self.run(f"update {table} set {sets} where {key}=%s", [_adapt(v) for v in fields.values()] + [row_id])

    def bulk(self, sql: str, rows: list[tuple]) -> None:
        """sql must contain a single 'values %s' placeholder."""
        if rows:
            with self.conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows, page_size=1000)

    def settings(self) -> dict:
        cfg = dict(DEFAULT_SETTINGS)
        for r in self.all("select key, value from settings"):
            cfg[r["key"]] = r["value"]
        return cfg
