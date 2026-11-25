"""M3.3: Flask endpoint regression across the four database states.

Search and crawl endpoints are exercised through the real application
routes (create_app + test client) against missing / v1 / v2 / invalid
databases, verifying that open_database semantics surface end-to-end:

  - search: missing -> 404 (non-creating), v1 -> auto-migrate then
    results, v2 -> results, empty/corrupt -> 500;
  - crawl: missing -> fresh v2 created and crawl completes, v1 ->
    migrate then crawl (legacy rows retained), corrupt -> job fails
    cleanly and the file is untouched.

No fixture databases are used; everything is real temporary SQLite DBs.
"""

import pathlib
import sqlite3
import tempfile
import time
import unittest

import main
from app import create_app
from helpers import TestServer, page


def _seed_v1(db_path, rows):
    """Create a legacy v1 FTS5 database at db_path with the given rows."""
    db_path = pathlib.Path(db_path)
    conn = main.get_database(db_path)
    try:
        for url, title, description, body in rows:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                (url, title, description, body))
        conn.commit()
    finally:
        conn.close()


def _seed_v2(db_path, rows):
    """Create a v2 database at db_path with the given rows."""
    db_path = pathlib.Path(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        main.create_schema_v2(conn)
        conn.commit()
        for url, title, description, body in rows:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                (url, title, description, body))
        conn.commit()
    finally:
        conn.close()


def _detect_kind(db_path):
    probe = None
    try:
        probe = main._connect_readonly(db_path)
        return main.detect_schema(probe)["kind"]
    finally:
        if probe is not None:
            probe.close()


class SearchEndpointDbStatesTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.app = create_app()
        self.client = self.app.test_client()

    def _get(self, q, db):
        return self.client.get(
            "/api/search", query_string={"q": q, "db": str(db)})

    def test_missing_db_search_is_404_and_non_creating(self):
        p = self.tmp_path / "missing.db"
        self.assertFalse(p.exists())
        r = self._get("anything", p)
        self.assertEqual(r.status_code, 404)
        payload = r.get_json()
        self.assertEqual(payload["error"], "database not found")
        self.assertFalse(p.exists())

    def test_v1_db_search_migrates_and_returns_results(self):
        p = self.tmp_path / "legacy.db"
        _seed_v1(p, [("http://a.example/", "A", "", "legacy keyword")])
        self.assertEqual(_detect_kind(p), "v1")

        r = self._get("legacy", p)
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["total_results"], 1)
        self.assertEqual(_detect_kind(p), "v2")  # migrated in place

    def test_v2_db_search_returns_results(self):
        p = self.tmp_path / "v2.db"
        _seed_v2(p, [("http://b.example/", "B", "", "fresh keyword")])
        r = self._get("fresh", p)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["total_results"], 1)

    def test_empty_db_search_is_500(self):
        p = self.tmp_path / "empty.db"
        p.write_bytes(b"")
        r = self._get("anything", p)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"], "internal search error")

    def test_corrupt_db_search_is_500(self):
        p = self.tmp_path / "corrupt.db"
        p.write_bytes(b"not a sqlite database at all" * 8)
        r = self._get("anything", p)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.get_json()["error"], "internal search error")


class CrawlEndpointDbStatesTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.server = TestServer().start()
        self.addCleanup(self.server.stop)
        self.server.add_robots("User-agent: *\nAllow: /\n")
        self.server.add("/seed", page("Seed", "seed body"))
        self.app = create_app()
        self.client = self.app.test_client()

    def _start_crawl(self, db_path):
        r = self.client.post(
            "/api/crawl",
            json={
                "start_url": self.server.url("/seed"),
                "max_pages": 3,
                "threads": 1,
                "db": str(db_path),
            })
        self.assertEqual(r.status_code, 202)
        return r.get_json()["crawl_id"]

    def _wait_for_finish(self, crawl_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.client.get(f"/api/crawl/status/{crawl_id}")
            data = r.get_json()
            if data["status"] in ("completed", "failed", "stopped"):
                return data
            time.sleep(0.05)
        raise AssertionError("crawl job did not reach a terminal state")

    def test_missing_db_crawl_creates_fresh_v2(self):
        p = self.tmp_path / "fresh.db"
        self.assertFalse(p.exists())
        cid = self._start_crawl(p)
        data = self._wait_for_finish(cid)
        self.assertEqual(data["status"], "completed")
        self.assertGreaterEqual(data["stats"]["indexed"], 1)
        self.assertTrue(p.exists())
        self.assertEqual(_detect_kind(p), "v2")

    def test_v1_db_crawl_migrates_then_crawls(self):
        p = self.tmp_path / "legacy.db"
        _seed_v1(p, [("http://keep.example/", "Keep", "", "retained word")])
        self.assertEqual(_detect_kind(p), "v1")

        cid = self._start_crawl(p)
        data = self._wait_for_finish(cid)
        self.assertEqual(data["status"], "completed")

        self.assertEqual(_detect_kind(p), "v2")
        conn = sqlite3.connect(str(p))
        try:
            urls = {
                row[0] for row in
                conn.execute("SELECT url FROM pages").fetchall()}
        finally:
            conn.close()
        # The pre-existing legacy row survived the migration.
        self.assertIn("http://keep.example/", urls)
        self.assertTrue(any("127.0.0.1" in u for u in urls))

    def test_corrupt_db_crawl_job_fails(self):
        p = self.tmp_path / "corrupt.db"
        garbage = b"not a sqlite database at all" * 8
        p.write_bytes(garbage)
        cid = self._start_crawl(p)
        data = self._wait_for_finish(cid)
        self.assertEqual(data["status"], "failed")
        self.assertTrue(data["error"])
        # The corrupt file is untouched (never created/overwritten).
        self.assertEqual(p.read_bytes(), garbage)


if __name__ == "__main__":
    unittest.main()
