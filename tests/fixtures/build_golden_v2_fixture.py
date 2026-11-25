#!/usr/bin/env python3
"""Phase 3 v2 golden fixture builder.

Builds tests/fixtures/golden_v2.db from the SAME corpus as the legacy
v1 fixture (golden_v1.db), but through the v2 schema: ordinary `pages`
table + external-content `pages_fts` + sync triggers. The Phase 3
parity suite replays golden_v1_expected.json against this v2 database
using the rewritten search implementation.

The corpus is imported from build_golden_fixture.py (by file path, so
this script does not depend on the tests package layout) - there is a
single source of truth for the corpus content.

Deterministic: same corpus, byte-stable after a WAL checkpoint
(TRUNCATE) and removal of -wal/-shm side files.
"""
import hashlib
import importlib.util
import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import main  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent
DB_PATH = FIXTURES / "golden_v2.db"

_CORPUS_MODULE_NAME = "build_golden_fixture"


def _load_corpus():
    """Import CORPUS from the v1 builder without relying on package layout."""
    path = FIXTURES / "build_golden_fixture.py"
    spec = importlib.util.spec_from_file_location(_CORPUS_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CORPUS


def build_database(corpus=None):
    """Create a fresh v2 DB and return its sha256."""
    corpus = _load_corpus() if corpus is None else corpus

    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        side = pathlib.Path(str(DB_PATH) + suffix)
        if side.exists():
            side.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        main.create_schema_v2(conn)
        conn.commit()  # caller-owned commit (fresh init)
        conn.executemany(
            "INSERT INTO pages (url, title, description, body) "
            "VALUES (?, ?, ?, ?)",
            corpus,
        )
        conn.commit()
    finally:
        conn.close()

    # Flush WAL into the main .db so the file hash is stable and complete.
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        side = pathlib.Path(str(DB_PATH) + suffix)
        if side.exists():
            side.unlink()

    return hashlib.sha256(DB_PATH.read_bytes()).hexdigest()


if __name__ == "__main__":
    digest = build_database()
    print(f"CREATED v2 golden fixture: {DB_PATH.name} "
          f"(sha256 {digest[:12]}…)")
