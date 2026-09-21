"""Runs every database-level test against a throwaway REAL Postgres (never Supabase).

    python worker/tests/run_scratch_tests.py

Needs `pip install pgserver` (a self-contained Postgres). Builds a temporary database from all four schema files,
runs the Node manual-trading tests and the Python end-to-end tests (fresh database for each), then deletes everything.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ("schema.sql", "schema_research.sql", "schema_manual.sql", "schema_learning.sql", "schema_cashplan.sql")


def fresh_db():
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="pgs_"), cleanup_mode="delete")
    uri = srv.get_uri()
    if uri.startswith("postgres://"):
        uri = "postgresql://" + uri[len("postgres://"):]
    for f in SCHEMAS:
        srv.psql((ROOT / "supabase" / f).read_text(encoding="utf-8").replace("create extension if not exists pgcrypto;", ""))
    return srv, uri + ("&" if "?" in uri else "?") + "sslmode=disable"


def run(cmd, cwd, url, keep=("✔", "✖", "ℹ tests", "ℹ pass", "ℹ fail", "passed", "failed", "skipped", "FAILED", "Error", "assert")):
    r = subprocess.run(cmd, cwd=cwd, env=dict(os.environ, TEST_DATABASE_URL=url), capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = r.stdout + r.stderr
    print("\n".join(l for l in out.splitlines() if any(k in l for k in keep) and "NOTICE" not in l)[-3500:])
    return r.returncode


if __name__ == "__main__":
    rc = 0
    for name in ("tests/manual.test.mjs", "tests/manual_pnl.test.mjs", "tests/cashplan_db.test.mjs"):
        srv, url = fresh_db()
        print(f"== {name} (Node) ==")
        rc |= run(["node", "--test", name], ROOT / "web", url)
    for name in ("tests/test_e2e.py", "tests/test_learning_e2e.py", "tests/test_research.py"):
        srv, url = fresh_db()
        print(f"== {name} ==")
        rc |= run([sys.executable, "-W", "ignore", "-m", "pytest", "-q", "-x", name], ROOT / "worker", url)
    sys.exit(rc)
