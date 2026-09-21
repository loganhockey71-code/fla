"""Runs the manual-trading engine tests (web/tests/manual.test.mjs) against a throwaway REAL Postgres.

    python worker/tests/run_manual_tests.py

Needs `pip install pgserver` (a self-contained Postgres). It builds a temporary database from the three schema files,
runs the Node tests (including two-clicks-at-once concurrency), then deletes everything. Never touches Supabase.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parents[2]
srv = pgserver.get_server(tempfile.mkdtemp(prefix="pgs_"), cleanup_mode="delete")
uri = srv.get_uri()
if uri.startswith("postgres://"):
    uri = "postgresql://" + uri[len("postgres://"):]
for f in ("schema.sql", "schema_research.sql", "schema_manual.sql"):
    srv.psql((ROOT / "supabase" / f).read_text(encoding="utf-8").replace("create extension if not exists pgcrypto;", ""))
env = dict(os.environ, TEST_DATABASE_URL=uri + ("&" if "?" in uri else "?") + "sslmode=disable")
r = subprocess.run(["node", "--test", "tests/manual.test.mjs"], cwd=ROOT / "web", env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
out = r.stdout + r.stderr
print("\n".join(l for l in out.splitlines() if l.startswith(("✔", "✖", "ℹ tests", "ℹ pass", "ℹ fail", "skipped")) or "expected" in l or "AssertionError" in l))
sys.exit(r.returncode)
