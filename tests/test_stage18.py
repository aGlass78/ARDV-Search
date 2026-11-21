"""Stage 18: JSON search API.

Covers main.search_json() (the programmatic, Flask-independent search
entry point) and the Flask endpoints in app.py. The engine's existing
search semantics (exact/stem/AND->OR fallback, --db selection) carry
over unchanged; these tests verify the JSON envelope, graceful error
handling, and the API surface.
"""
import unittest

import main
from app import create_app
from helpers import IsolatedDbTestCase, ServerTestCase, page


def _seed_pages(testcase):
    testcase.insert_page("http://example.com/python", "Python",
                         "A programming language", "python body words")
    testcase.insert_page("http://example.com/cats", "Cats",
                         "", "many cats here and a cat")
    testcase.insert_page("http://example.com/cats2", "Cats 2",
                         "", "cats everywhere")
    testcase.insert_page("http://example.com/cats3", "Cats 3",
                         "", "cats cats cats")
    testcase.insert_page("http://example.com/google", "Google bomb",
                         "", "google bombing explained")


class SearchJsonTests(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        _seed_pages(self)

    def test_schema_and_values(self):
        payload = main.search_json("python")
        self.assertEqual(
            set(payload), {"query", "results", "total_results", "search_time"})
        self.assertEqual(payload["query"], "python")
        self.assertIsInstance(payload["total_results"], int)
        self.assertIsInstance(payload["search_time"], float)
        self.assertGreaterEqual(payload["search_time"], 0.0)

        self.assertEqual(len(payload["results"]), 1)
        result = payload["results"][0]
        self.assertEqual(set(result), {"title", "url", "snippet"})
        self.assertEqual(result["url"], "http://example.com/python")
        self.assertEqual(result["title"], "Python")
        # description is preferred over the FTS5 excerpt
        self.assertEqual(result["snippet"], "A programming language")

    def test_snippet_falls_back_to_excerpt(self):
        payload = main.search_json("cat")
        cat = next(
            r for r in payload["results"] if r["url"].endswith("/cats"))
        # no description -> the FTS5 excerpt (highlighted terms) is used
        self.assertIn("cats", cat["snippet"])

    def test_total_results_ignores_limit(self):
        payload = main.search_json("cats", limit=2)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["total_results"], 3)

    def test_exact_and_stem_flags_flow_through(self):
        # stem="google bomb" finds "google bombing"
        payload = main.search_json("google bomb", stem=True)
        self.assertGreaterEqual(payload["total_results"], 1)
        # exact "google bomb" still matches "google bomb" title
        exact = main.search_json("google bomb", exact=True)
        self.assertGreaterEqual(exact["total_results"], 1)

    def test_empty_query_returns_empty_payload(self):
        for query in ("", "   ", "!@#$"):
            payload = main.search_json(query)
            self.assertEqual(payload["query"], query)
            self.assertEqual(payload["results"], [])
            self.assertEqual(payload["total_results"], 0)
            self.assertGreaterEqual(payload["search_time"], 0.0)

    def test_limit_below_one_returns_empty_payload(self):
        payload = main.search_json("python", limit=0)
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["total_results"], 0)

    def test_db_path_parameter_selects_database(self):
        other = self.tmp_path / "other.db"
        conn = main.get_database(other)
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://other.example/", "Other", "", "other words"))
            conn.commit()
        finally:
            conn.close()

        payload = main.search_json("other", db_path=other)
        self.assertEqual(payload["total_results"], 1)
        self.assertEqual(payload["results"][0]["url"],
                         "http://other.example/")

        # the default (seeded) database is unaffected
        default = main.search_json("other")
        self.assertEqual(default["total_results"], 0)

    def test_missing_database_raises_database_not_found(self):
        with self.assertRaises(main.DatabaseNotFoundError):
            main.search_json("python",
                             db_path=self.tmp_path / "nope.db")


class ApiTests(IsolatedDbTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.client = self.app.test_client()
        _seed_pages(self)

    def test_search_endpoint(self):
        resp = self.client.get("/api/search?q=python")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["query"], "python")
        self.assertEqual(data["results"][0]["title"], "Python")
        self.assertEqual(data["total_results"], 1)

    def test_search_endpoint_limit(self):
        resp = self.client.get("/api/search?q=cats&limit=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["total_results"], 3)

    def test_empty_query_returns_empty_results(self):
        resp = self.client.get("/api/search?q=")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["total_results"], 0)

    def test_invalid_limit_returns_400(self):
        resp = self.client.get("/api/search?q=python&limit=abc")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "limit must be an integer")

    def test_zero_and_negative_limit_return_400(self):
        for bad in ("0", "-3"):
            resp = self.client.get(f"/api/search?q=python&limit={bad}")
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.get_json()["error"],
                             "limit must be at least 1")

    def test_exact_and_stem_are_mutually_exclusive(self):
        resp = self.client.get("/api/search?q=python&exact=1&stem=1")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "exact and stem are mutually exclusive")

    def test_limit_omitted_uses_engine_default(self):
        # No limit param -> default 10; still returns all seeded matches.
        resp = self.client.get("/api/search?q=cats")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["total_results"], 3)

    def test_missing_database_returns_404(self):
        missing = str(self.tmp_path / "nope.db")
        resp = self.client.get("/api/search?q=python&db=" + missing)
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertEqual(data["error"], "database not found")
        self.assertEqual(data["database"], missing)

    def test_db_flag_selects_database(self):
        other = self.tmp_path / "other.db"
        conn = main.get_database(other)
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://other.example/", "Other", "", "other words"))
            conn.commit()
        finally:
            conn.close()

        resp = self.client.get(
            "/api/search?q=other&db=" + str(other))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total_results"], 1)

        # without the db flag, the default database has no "other"
        resp = self.client.get("/api/search?q=other")
        self.assertEqual(resp.get_json()["total_results"], 0)

    def test_stem_and_exact_query_params(self):
        resp = self.client.get("/api/search?q=google+bomb&stem=1")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.get_json()["total_results"], 1)

        resp = self.client.get("/api/search?q=google+bomb&exact=1")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.get_json()["total_results"], 1)

    def test_status_endpoint(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["engine"], "ModelSearchEngine")
        self.assertEqual(data["database"], str(main.DB_PATH))
        self.assertTrue(data["database_exists"])

    def test_root_serves_frontend_shell(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ARDV Search", resp.get_data(as_text=True))


class CrawlApiTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.client = self.app.test_client()

    def _start(self, payload):
        resp = self.client.post("/api/crawl", json=payload)
        self.assertEqual(resp.status_code, 202)
        data = resp.get_json()
        self.assertEqual(data["status"], "running")
        return data["crawl_id"]

    def _wait_for_finish(self, crawl_id, timeout=20.0):
        import time as _time
        deadline = _time.time() + timeout
        last = None
        while _time.time() < deadline:
            resp = self.client.get(f"/api/crawl/status/{crawl_id}")
            self.assertEqual(resp.status_code, 200)
            last = resp.get_json()
            if last["status"] in ("completed", "failed"):
                return last
            _time.sleep(0.05)
        self.fail(f"crawl {crawl_id} did not finish in {timeout}s")

    def test_crawl_endpoint_indexes_pages(self):
        self.server.add("/seed", page("Seed", "hello world", links=["/page"]))
        self.server.add("/page", page("Page", "more words"))

        crawl_id = self._start({
            "start_url": self.server.url("/seed"),
            "threads": 1,
        })
        final = self._wait_for_finish(crawl_id)

        self.assertEqual(final["status"], "completed")
        self.assertIsNone(final["error"])
        stats = final["stats"]
        self.assertGreaterEqual(stats["indexed"], 2)
        self.assertIn("db_path", stats)
        self.assertIn("elapsed_seconds", stats)
        self.assertIn("/seed", self.indexed_paths())
        self.assertIn("/page", self.indexed_paths())

    def test_crawl_max_pages_respected(self):
        self.server.add("/seed", page("Seed", "hi", links=["/a", "/b"]))
        self.server.add("/a", page("A", "a"))
        self.server.add("/b", page("B", "b"))

        crawl_id = self._start({
            "start_url": self.server.url("/seed"),
            "max_pages": 2,
            "threads": 1,
        })
        final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["stats"]["indexed"], 2)

    def test_crawl_db_param_selects_database(self):
        self.server.add("/seed", page("Seed", "hello"))

        other = self.tmp_path / "crawl.db"
        crawl_id = self._start({
            "start_url": self.server.url("/seed"),
            "db": str(other),
            "threads": 1,
        })
        final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["stats"]["db_path"], str(other))

        conn = main.get_database(other)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 1)

    def test_crawl_requires_url(self):
        resp = self.client.post("/api/crawl", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "start_url must be a non-empty string")

        resp = self.client.post("/api/crawl", json={"start_url": "   "})
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post("/api/crawl", json={"start_url": 42})
        self.assertEqual(resp.status_code, 400)

    def test_crawl_rejects_malformed_url(self):
        resp = self.client.post("/api/crawl", json={"url": "not a url"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid URL", resp.get_json()["error"])

    def test_crawl_invalid_sitemap(self):
        resp = self.client.post("/api/crawl", json={
            "url": self.server.url("/seed"),
            "sitemap": "banana",
        })
        self.assertEqual(resp.status_code, 400)

    def test_crawl_invalid_allow_entry(self):
        for bad in (["host:88"], ["a b"], [123], "example.com"):
            resp = self.client.post("/api/crawl", json={
                "url": self.server.url("/seed"),
                "allow": bad,
            })
            self.assertEqual(resp.status_code, 400, bad)

    def test_crawl_invalid_numeric_options(self):
        for field in ("max_pages", "max_bytes", "threads"):
            resp = self.client.post("/api/crawl", json={
                "url": self.server.url("/seed"),
                field: 0,
            })
            self.assertEqual(resp.status_code, 400, field)
            resp = self.client.post("/api/crawl", json={
                "url": self.server.url("/seed"),
                field: "lots",
            })
            self.assertEqual(resp.status_code, 400, field)

        resp = self.client.post("/api/crawl", json={
            "url": self.server.url("/seed"),
            "delay": -1,
        })
        self.assertEqual(resp.status_code, 400)

    def test_crawl_unknown_parameter_rejected(self):
        resp = self.client.post("/api/crawl", json={
            "url": self.server.url("/seed"),
            "bogus": 1,
        })
        self.assertEqual(resp.status_code, 400)

    def test_crawl_non_object_body_rejected(self):
        resp = self.client.post("/api/crawl", json=[1, 2, 3])
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post("/api/crawl", data="not json",
                                content_type="text/plain")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
