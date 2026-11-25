#!/usr/bin/env python3
"""Phase 0 golden fixture builder (legacy FTS5 v1 schema, current search impl).

Regenerates tests/fixtures/golden_v1.db and golden_v1_expected.json from the
CURRENT (v1) codebase. First run writes the baseline; later runs verify the
fixture is stable (byte-identical DB, identical expected payloads). The Phase 3
parity suite replays golden_v1_expected.json against the v2 implementation.

The fixture is intentionally deterministic: a fixed corpus, no randomness, and
all expected payloads recorded by executing the real search path (search_json).
"""
import hashlib
import json
import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import main  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent
DB_PATH = FIXTURES / "golden_v1.db"
EXPECTED_PATH = FIXTURES / "golden_v1_expected.json"

# url, title, description, body
CORPUS = [
    ("http://example.com/python", "Python", "A programming language",
     "Python is a high-level interpreted programming language used for web "
     "development, data science, and automation. Python emphasizes readability."),
    ("http://example.com/cats", "Cats", "",
     "Cats are small carnivorous mammals often kept as pets. Cats like to "
     "sleep and hunt. cats cats."),
    ("http://example.com/google-bomb", "", "",
     "Google bombing is a search manipulation technique. google bomb google bombs."),
    ("http://example.com/google-bombing", "Google Bombing", "Inflected form",
     "google bombing refers to coordinated link campaigns."),
    ("http://example.com/phrase", "Phrase Test", "test",
     "google bomb controversy discusses the phenomenon."),
    ("http://example.com/interrupted", "", "",
     "google controversial bomb is out of order."),
    ("http://example.com/punct", "Punctuation!", "Has punctuation.",
     "Hello, world! This is a punctuation sentence; test it."),
    ("http://example.com/tie-a", "Tie", "",
     "identical duplicate content appears in two pages"),
    ("http://example.com/tie-b", "Tie", "",
     "identical duplicate content appears in two pages"),
    ("http://example.com/stem", "Stemming", "Porters",
     "running runner runs ran. The runner runs daily. run."),
]

# Each entry: key (identifies the case) + kwargs for main.search_json.
# Covers: default/prefix/description-preference, excerpt-fallback snippet,
# total_results ignoring limit, AND multi-term, OR fallback, stemming,
# exact phrase, punctuation, empty/blank/invalid queries, BM25 tie-breaking.
QUERIES = [
    {"key": "single_default",          "kwargs": {"query": "python"}},
    {"key": "prefix_fallback_snippet", "kwargs": {"query": "cat"}},
    {"key": "count_limit",             "kwargs": {"query": "cats", "limit": 2}},
    {"key": "and_multiterm",           "kwargs": {"query": "google bomb"}},
    {"key": "stem",                    "kwargs": {"query": "google bomb", "stem": True}},
    {"key": "exact_phrase",            "kwargs": {"query": "google bomb", "exact": True}},
    {"key": "or_fallback",             "kwargs": {"query": "zzznope python"}},
    {"key": "punct_word",              "kwargs": {"query": "punctuation"}},
    {"key": "punct_only",              "kwargs": {"query": "!@#$"}},
    {"key": "empty",                   "kwargs": {"query": ""}},
    {"key": "blank",                   "kwargs": {"query": "   "}},
    {"key": "tie_break",               "kwargs": {"query": "tie"}},
    {"key": "prefix_stem_off",         "kwargs": {"query": "runner"}},
    {"key": "prefix_stem_on",          "kwargs": {"query": "runner", "stem": True}},
    {"key": "multiword_and",           "kwargs": {"query": "hello world"}},
]


def build_database():
    """Create a fresh legacy v1 FTS5 DB and return its sha256."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        side = pathlib.Path(str(DB_PATH) + suffix)
        if side.exists():
            side.unlink()

    conn = main.get_database(DB_PATH)  # legacy schema (current code)
    try:
        conn.executemany(
            "INSERT INTO pages (url, title, description, body) "
            "VALUES (?, ?, ?, ?)",
            CORPUS,
        )
        conn.commit()
    finally:
        conn.close()

    # Flush WAL into the main .db so the file hash is stable and complete.
    conn = sqlite3.connect(DB_PATH)
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


def record_payloads():
    """Execute every query through the current implementation."""
    payloads = {}
    for entry in QUERIES:
        key = entry["key"]
        kwargs = dict(entry["kwargs"])
        payload = main.search_json(db_path=str(DB_PATH), **kwargs)
        # Type assertions harden the fixture contract.
        assert isinstance(payload["total_results"], int), key
        assert isinstance(payload["search_time"], float), key
        for result in payload["results"]:
            assert set(result) == {"title", "url", "snippet"}, key
        payloads[key] = {"kwargs": kwargs, "payload": payload}
    return payloads


def comparable(payload):
    """Payload minus the inherently variable search_time wall-clock."""
    return {k: v for k, v in payload.items() if k != "search_time"}


def run():
    db_hash = build_database()
    payloads = record_payloads()

    doc = {
        "meta": {
            "db_sha256": db_hash,
            "corpus_pages": len(CORPUS),
            "sqlite_version": sqlite3.sqlite_version,
            "python_version": sys.version.split()[0],
        },
        "queries": payloads,
    }

    if EXPECTED_PATH.exists():
        existing = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
        db_ok = existing["meta"]["db_sha256"] == db_hash
        q_ok = (
            set(payloads) == set(existing["queries"])
            and all(
                comparable(payloads[key]["payload"])
                == comparable(existing["queries"][key]["payload"])
                for key in payloads
            )
        )
        if db_ok and q_ok:
            print(f"VERIFIED: fixture stable ({len(payloads)} queries, "
                  f"db sha256 {db_hash[:12]}…)")
            return 0
        print("MISMATCH: fixture changed against recorded golden baseline.")
        if not db_ok:
            print(f"  db sha256: recorded {existing['meta']['db_sha256'][:12]}… "
                  f"vs new {db_hash[:12]}…")
        if not q_ok:
            for key in payloads:
                if comparable(payloads[key]["payload"]) != comparable(
                        existing["queries"].get(key, {}).get("payload", {})):
                    print(f"  query differs: {key}")
        print("  golden files were NOT overwritten.")
        return 1

    EXPECTED_PATH.write_text(
        json.dumps(doc, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"CREATED golden fixture: {EXPECTED_PATH.name} "
          f"({len(payloads)} queries, db {db_hash[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
