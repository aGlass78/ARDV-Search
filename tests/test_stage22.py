"""Phase 3: rewritten v2 search + golden-fixture parity.

The golden v1 expected payloads (tests/fixtures/golden_v1_expected.json)
are replayed against the v2 database (golden_v2.db) using the NEW
search implementation. Only ``search_time`` (wall clock) is excluded
from comparison; the fixture itself and its expected JSON are never
modified.

Additional focused tests cover the rewritten SQL (pages_fts + rowid
join), deterministic tie-breaking, and the preserved behaviors.
"""
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest

import main  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
GOLDEN_V2 = FIXTURES / "golden_v2.db"
EXPECTED = FIXTURES / "golden_v1_expected.json"


def comparable(payload):
    """Payload minus the inherently variable search_time wall-clock."""
    return {k: v for k, v in payload.items() if k != "search_time"}


def ensure_golden_v2():
    """Build golden_v2.db from the canonical corpus if it is missing."""
    if GOLDEN_V2.exists():
        return
    spec = importlib.util.spec_from_file_location(
        "build_golden_v2_fixture",
        FIXTURES / "build_golden_v2_fixture.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_database()


class GoldenV2ParityTests(unittest.TestCase):
    """Replay all 15 legacy golden cases against the rewritten v2 search."""

    @classmethod
    def setUpClass(cls):
        ensure_golden_v2()
        cls.expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        cls.cases = cls.expected["queries"]

    def test_parity_all_fifteen_cases(self):
        for key, case in self.cases.items():
            with self.subTest(case=key):
                kwargs = dict(case["kwargs"])
                payload = main.search_json(
                    db_path=str(GOLDEN_V2), **kwargs)
                got = comparable(payload)
                want = comparable(case["payload"])
                self.assertEqual(got, want)


class V2SearchRewriteTests(unittest.TestCase):
    """Focused tests for the rewritten SQL and edge behaviors."""

    @classmethod
    def setUpClass(cls):
        ensure_golden_v2()

    def _make_v2_db(self, corpus):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = pathlib.Path(tmp.name) / "t.db"
        conn = sqlite3.connect(str(db))
        try:
            main.create_schema_v2(conn)
            conn.commit()
            conn.executemany(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                corpus,
            )
            conn.commit()
        finally:
            conn.close()
        return db

    def test_fts_query_targets_pages_fts_with_rowid_join(self):
        conn = sqlite3.connect(str(GOLDEN_V2))
        try:
            rows = main._fts_query(conn, '"tie"*', 10)
        finally:
            conn.close()
        # (url, title, description, excerpt) - description = p.description,
        # excerpt = snippet(pages_fts, 2, ...) i.e. the body column.
        self.assertEqual(
            [row[0] for row in rows],
            ["http://example.com/tie-a", "http://example.com/tie-b"],
        )
        self.assertEqual(rows[0][2], "")  # no description on tie pages
        self.assertEqual(
            rows[0][3],
            "identical duplicate content appears in two pages",
        )

    def test_equal_bm25_scores_ordered_by_rowid(self):
        # Three identical-content docs inserted in C, A, B order => rowids
        # C=1, A=2, B=3. With equal scores the secondary ORDER BY rowid
        # must return C, A, B (not insertion order, not alphabetical).
        corpus = [
            ("http://x/c", "Same", "", "identical content here"),
            ("http://x/a", "Same", "", "identical content here"),
            ("http://x/b", "Same", "", "identical content here"),
        ]
        db = self._make_v2_db(corpus)
        conn = sqlite3.connect(str(db))
        try:
            rows = main._fts_query(conn, '"identical"*', 10)
        finally:
            conn.close()
        self.assertEqual([row[0] for row in rows],
                         ["http://x/c", "http://x/a", "http://x/b"])

    def test_total_results_independent_of_limit(self):
        limited = main.search_json("google", limit=1,
                                   db_path=str(GOLDEN_V2))
        full = main.search_json("google", db_path=str(GOLDEN_V2))
        self.assertEqual(len(limited["results"]), 1)
        self.assertEqual(limited["total_results"], 4)
        self.assertEqual(full["total_results"], limited["total_results"])

    def test_description_preferred_over_excerpt(self):
        payload = main.search_json("google bomb", db_path=str(GOLDEN_V2))
        self.assertEqual(payload["results"][0]["snippet"], "Inflected form")
        self.assertEqual(payload["results"][0]["title"], "Google Bombing")

    def test_body_excerpt_fallback(self):
        payload = main.search_json("cat", db_path=str(GOLDEN_V2))
        self.assertTrue(payload["results"][0]["snippet"].startswith("[Cats]"))

    def test_empty_blank_and_punctuation_only_return_no_results(self):
        for query in ("", "   ", "!@#$"):
            with self.subTest(query=query):
                payload = main.search_json(query, db_path=str(GOLDEN_V2))
                self.assertEqual(payload["results"], [])
                self.assertEqual(payload["total_results"], 0)

    def test_prefix_match_on_v2(self):
        payload = main.search_json("runner", db_path=str(GOLDEN_V2))
        self.assertEqual(payload["results"][0]["url"],
                         "http://example.com/stem")
        self.assertEqual(payload["results"][0]["snippet"], "Porters")

    def test_stemming_on_v2(self):
        payload = main.search_json("runner", stem=True,
                                   db_path=str(GOLDEN_V2))
        self.assertEqual(payload["results"][0]["url"],
                         "http://example.com/stem")

    def test_exact_phrase_on_v2(self):
        payload = main.search_json("google bomb", exact=True,
                                   db_path=str(GOLDEN_V2))
        self.assertEqual(
            [r["url"] for r in payload["results"]],
            ["http://example.com/phrase", "http://example.com/google-bomb"],
        )

    def test_and_or_fallback_on_v2(self):
        payload = main.search_json("zzznope python",
                                   db_path=str(GOLDEN_V2))
        self.assertEqual([r["url"] for r in payload["results"]],
                         ["http://example.com/python"])
        self.assertEqual(payload["total_results"], 1)


if __name__ == "__main__":
    unittest.main()
