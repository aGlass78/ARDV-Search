"""Stage 2 tests: crawl reliability and performance.

Written before implementation; the behaviors under test:

1. robots.txt Crawl-delay directives are respected (config stays
   conservative) and failed robots.txt fetches are not cached forever.
2. HTTP 429/503 responses honor Retry-After; responses larger than a
   configurable limit are skipped; malformed metadata never crashes.
3. The database path is anchored next to main.py, not the CWD, and
   existing data is preserved.
4. crawl() returns accurate stats including elapsed time.
5. SQLite runs in WAL mode (readers work during writes) with the
   durability level unchanged (synchronous=FULL).
"""
import os
import pathlib
import requests
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import (IsolatedDbTestCase, ServerTestCase, TestServer, main,
                     page, run_cli)


class RobotsFailureRetryTests(ServerTestCase):
    def test_failed_robots_fetch_is_retried_after_ttl(self):
        s = self.server
        s.robots_failures = 1  # first robots.txt hit returns 503
        s.add_robots("User-agent: *\nAllow: /\n")

        session = requests.Session()
        cache = main.RobotsCache()

        with mock.patch.object(main, "ROBOTS_RETRY_SECONDS", 0):
            # First lookup fails: the host is paused, not excluded.
            self.assertIsNone(
                cache.rules_for(session, s.url("/x")))
            # TTL of 0: the next lookup refetches and succeeds.
            rules = cache.rules_for(session, s.url("/x"))
            self.assertIsNotNone(rules)

        self.assertEqual(s.paths().count("/robots.txt"), 2)
        session.close()

    def test_failed_robots_fetch_held_within_retry_window(self):
        s = self.server
        s.robots_failures = 1
        s.add_robots("User-agent: *\nAllow: /\n")

        session = requests.Session()
        cache = main.RobotsCache()

        with mock.patch.object(main, "ROBOTS_RETRY_SECONDS", 60):
            self.assertIsNone(
                cache.rules_for(session, s.url("/x")))
            # Inside the window the failure stands: no immediate refetch.
            self.assertIsNone(
                cache.rules_for(session, s.url("/x")))

        self.assertEqual(s.paths().count("/robots.txt"), 1)
        session.close()

    def test_in_run_retry_recovers_within_a_crawl(self):
        # Host A's seed links to two pages on host B, whose first
        # robots.txt fetch fails. The first B URL is skipped; the
        # second must trigger a robots retry and be indexed.
        # threads=1 keeps the failure/retry sequence deterministic.
        a = self.server
        b = TestServer()
        b.start()
        self.addCleanup(b.stop)

        b.robots_failures = 1
        b.add_robots("User-agent: *\nAllow: /\n")
        b.add("/good", page("Good", "good body"))
        b.add("/good2", page("Good2", "good2 body"))
        a.add_robots("User-agent: *\nAllow: /\n")
        a.add("/seed", page("Seed", "seed body",
                            [b.url("/good"), b.url("/good2")]))

        with mock.patch.object(main, "ROBOTS_RETRY_SECONDS", 0):
            stats = self.crawl(a.url("/seed"), max_pages=10, delay=0.05,
                               threads=1)

        conn = sqlite3.connect(self.db_path)
        try:
            urls = [row[0] for row in
                    conn.execute("SELECT url FROM pages").fetchall()]
        finally:
            conn.close()

        self.assertIn(a.url("/seed"), urls)
        self.assertIn(b.url("/good2"), urls)
        self.assertNotIn(b.url("/good"), urls)  # skipped, not retried
        self.assertEqual(b.paths().count("/robots.txt"), 2)
        self.assertEqual(stats["robots_unavailable"], 1)


class CrawlDelayTests(ServerTestCase):
    def test_robots_crawl_delay_overrides_configured_delay(self):
        # Note: Python's robotparser only understands integer Crawl-delay
        # values (fractional ones are dropped), as are real-world ones.
        s = self.server
        s.add_robots("User-agent: *\nCrawl-delay: 1\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/p1", "/p2"]))
        for p in ("/p1", "/p2"):
            s.add(p, page(p, f"{p} body"))

        # Configured delay is tiny; robots.txt Crawl-delay must win.
        self.crawl(s.url("/seed"), max_pages=20, delay=0.05)

        gaps = s.gaps()
        self.assertGreaterEqual(min(gaps), 0.9,
                                f"Crawl-delay not honored, gaps: {gaps}")
        self.assertEqual(self.indexed_paths(), ["/p1", "/p2", "/seed"])


class RetryAfterTests(ServerTestCase):
    """429/503 handling: pause honored, pause expires, one retry.

    threads=1 keeps the URL order deterministic so the gaps can be
    asserted (with parallel workers the /rate and /good fetches race).
    """

    def test_retry_after_seconds_honored_then_expires(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/rate", "/good"]))
        s.add("/rate", "slow down", status=429,
              headers={"Retry-After": "1"})
        s.add("/good", page("Good", "good body"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.05,
                           threads=1)

        paths = s.paths()
        gaps = s.gaps()
        i = paths.index("/rate")
        # The request right after the 429 waits out the Retry-After.
        self.assertGreaterEqual(gaps[i], 0.9,
                                f"Retry-After ignored, gap: {gaps[i]}")
        # /rate is requeued for exactly one retry; after it is 429ed
        # again it is dropped, and the next wait is back to the
        # base delay (the pause expired, no permanent floor).
        self.assertEqual(paths.count("/rate"), 2)
        j = paths.index("/rate", i + 1)
        self.assertLessEqual(gaps[j - 1], 0.5,
                             f"Retry-After became a sticky floor: {gaps}")
        self.assertEqual(self.indexed_paths(), ["/good", "/seed"])
        self.assertEqual(stats["rate_limited"], 2)
        self.assertEqual(stats["rate_limited_retries"], 1)

    def test_rate_limited_url_recovered_by_retry(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/flaky"]))
        s.add("/flaky", page("Flaky", "flaky body"),
              fail={"times": 1, "status": 429,
                    "headers": {"Retry-After": "1"}})

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.05,
                           threads=1)

        # First hit is rate limited; the single automatic retry lands.
        self.assertEqual(self.indexed_paths(), ["/flaky", "/seed"])
        self.assertEqual(s.paths().count("/flaky"), 2)
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["rate_limited_retries"], 1)

    def test_malformed_retry_after_ignored(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/rate", "/good"]))
        s.add("/rate", "slow down", status=429,
              headers={"Retry-After": "soon"})
        s.add("/good", page("Good", "good body"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.05,
                           threads=1)

        self.assertEqual(self.indexed_paths(), ["/good", "/seed"])
        self.assertEqual(s.paths().count("/rate"), 1)  # no retry without Retry-After
        self.assertEqual(stats["rate_limited"], 1)
        self.assertEqual(stats["rate_limited_retries"], 0)

    def test_past_http_date_retry_after_does_not_stall(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/rate", "/good"]))
        s.add("/rate", "slow down", status=429,
              headers={"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"})
        s.add("/good", page("Good", "good body"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.05,
                           threads=1)

        self.assertEqual(self.indexed_paths(), ["/good", "/seed"])
        # The date is in the past: zero extra pause, base delay held
        # (tolerance covers Windows timer granularity under load).
        self.assertGreaterEqual(min(s.gaps()), 0.02)
        self.assertEqual(stats["rate_limited"], 2)  # original + zero-delay retry
        self.assertEqual(stats["rate_limited_retries"], 1)


class MaxBytesTests(ServerTestCase):
    def crawl_two(self, route_kwargs, max_bytes):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/big"]))
        s.add("/big", page("Big", "A" * 5000), **route_kwargs)
        return self.crawl(s.url("/seed"), max_pages=10, delay=0.05,
                          max_bytes=max_bytes)

    def test_oversized_body_skipped(self):
        stats = self.crawl_two({}, max_bytes=1000)
        self.assertEqual(self.indexed_paths(), ["/seed"])
        self.assertEqual(stats["too_large"], 1)

    def test_lying_content_length_skipped(self):
        stats = self.crawl_two({"headers": {"Content-Length": "999999999"}},
                               max_bytes=1000)
        self.assertEqual(self.indexed_paths(), ["/seed"])
        self.assertEqual(stats["too_large"], 1)

    def test_garbage_content_length_still_indexed(self):
        stats = self.crawl_two({"headers": {"Content-Length": "abc"}},
                               max_bytes=1_000_000)
        self.assertEqual(self.indexed_paths(), ["/big", "/seed"])
        self.assertEqual(stats["too_large"], 0)

    def test_missing_content_length_still_indexed(self):
        stats = self.crawl_two({"no_content_length": True},
                               max_bytes=1_000_000)
        self.assertEqual(self.indexed_paths(), ["/big", "/seed"])
        self.assertEqual(stats["too_large"], 0)


class MaxBytesCliTests(unittest.TestCase):
    def test_flag_is_passed_to_crawl(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli(["crawl", "http://127.0.0.1/x",
                                   "--max-bytes", "1000"])
            self.assertIsNone(code)
            self.assertEqual(crawl.call_args.kwargs.get("max_bytes"), 1000)

    def test_non_positive_max_bytes_rejected(self):
        with mock.patch.object(main, "crawl") as crawl:
            code, _, _ = run_cli(["crawl", "http://127.0.0.1/x",
                                   "--max-bytes", "0"])
            self.assertEqual(code, 2)
            crawl.assert_not_called()


class DatabasePathTests(unittest.TestCase):
    def test_db_path_is_next_to_script(self):
        p = pathlib.Path(main.DB_PATH)
        self.assertTrue(p.is_absolute())
        self.assertEqual(p.parent, pathlib.Path(main.__file__).resolve().parent)
        self.assertEqual(p.name, "result.db")

    def test_database_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as elsewhere:
            db = pathlib.Path(home) / "x.db"
            with mock.patch.object(main, "DB_PATH", db):
                cwd = os.getcwd()
                os.chdir(elsewhere)
                try:
                    conn = main.get_database()
                    conn.execute("INSERT INTO pages (url, title, description, body)"
                                 " VALUES ('http://x/', 't', 'd', 'b')")
                    conn.commit()
                    conn.close()
                finally:
                    os.chdir(cwd)
            self.assertTrue(db.exists(), "DB must live next to main.py, not in CWD")
            self.assertEqual(list(pathlib.Path(elsewhere).glob("*.db")), [])

    def test_existing_database_preserved(self):
        with tempfile.TemporaryDirectory() as home:
            db = pathlib.Path(home) / "keep.db"
            with mock.patch.object(main, "DB_PATH", db):
                conn = main.get_database()
                conn.execute("INSERT INTO pages (url, title, description, body)"
                             " VALUES ('http://x/', 't', 'd', 'b')")
                conn.commit()
                conn.close()

                conn = main.get_database()  # must not drop or recreate
                count = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
                conn.close()
        self.assertEqual(count, 1)


class CrawlStatsTests(ServerTestCase):
    def test_counters_match_reality(self):
        s = self.server
        s.add_robots("User-agent: *\nDisallow: /blocked\n")
        s.add("/seed", page("Seed", "seed body",
                            ["/good1", "/good2", "/blocked", "/missing",
                             "/binaryish", "/empty", "/dupa", "/dupb", "/out"]))
        s.add("/good1", page("Good1", "one"))
        s.add("/good2", page("Good2", "two"))
        s.add("/missing", "gone", status=404)
        s.add("/binaryish", "not html", content_type="application/pdf")
        s.add("/empty", page(None, "", ["/deep"]))  # fetched, not indexable
        s.add("/dupa", redirect=s.url("/dupb"), status=302)
        s.add("/dupb", page("Dup", "dup body"))
        s.add("/deep", page("Deep", "deep body"))
        # /out redirects to a host outside the allowed domains.
        s.add("/out", redirect=f"http://localhost:{s.port}/landing", status=302)
        s.add("/landing", page("Landing", "never indexed"))

        stats = self.crawl(s.url("/seed"), max_pages=50, delay=0.03,
                          threads=1)

        self.assertEqual(self.indexed_paths(),
                         ["/deep", "/dupb", "/good1", "/good2", "/seed"])
        self.assertEqual(stats["indexed"], 5)
        self.assertEqual(stats["links_discovered"], 10)  # 9 from seed, /deep from /empty
        self.assertEqual(stats["requests"], 9)
        self.assertEqual(stats["http_errors"], 1)
        self.assertEqual(stats["robots_blocked"], 1)
        self.assertEqual(stats["non_html"], 1)
        self.assertEqual(stats["not_indexable"], 1)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["out_of_scope"], 1)
        self.assertEqual(stats["too_large"], 0)
        self.assertEqual(stats["rate_limited"], 0)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["robots_unavailable"], 0)
        self.assertGreater(stats["elapsed_seconds"], 0)

        # Redirect targets fetched exactly once; blocked page untouched.
        self.assertEqual(s.paths().count("/dupb"), 1)
        # Scope is checked BEFORE following a redirect, so the
        # out-of-scope landing page is never even requested.
        self.assertNotIn("/landing", s.paths())
        self.assertNotIn("/blocked", s.paths())


class SqliteTests(unittest.TestCase):
    def _tmp_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name) / "t.db"

    def test_wal_mode_enabled(self):
        with mock.patch.object(main, "DB_PATH", self._tmp_db()):
            conn = main.get_database()
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
        self.assertEqual(mode, "wal")

    def test_durability_level_unchanged(self):
        # Stage 2 must NOT trade durability for speed: synchronous stays FULL.
        with mock.patch.object(main, "DB_PATH", self._tmp_db()):
            conn = main.get_database()
            level = conn.execute("PRAGMA synchronous").fetchone()[0]
            conn.close()
        self.assertEqual(level, 2)  # 2 = FULL

    def test_reader_works_during_open_write_transaction(self):
        # The whole point of WAL: searches can run while a crawl writes.
        db = self._tmp_db()
        with mock.patch.object(main, "DB_PATH", db):
            writer = main.get_database()
            writer.execute("INSERT INTO pages (url, title, description, body)"
                           " VALUES ('http://x/1', 't', 'd', 'b')")  # no commit

            reader = sqlite3.connect(db)
            try:
                count = reader.execute("SELECT count(*) FROM pages").fetchone()[0]
            finally:
                reader.close()
                writer.rollback()
                writer.close()
        self.assertEqual(count, 0)

    def test_committed_data_survives_reopen(self):
        with mock.patch.object(main, "DB_PATH", self._tmp_db()):
            conn = main.get_database()
            for i in range(5):
                conn.execute("INSERT INTO pages (url, title, description, body)"
                             " VALUES (?, 't', 'd', 'b')",
                             (f"http://x/{i}",))
            conn.commit()
            conn.close()

            conn = sqlite3.connect(main.DB_PATH)
            count = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
            conn.close()
        self.assertEqual(count, 5)


if __name__ == "__main__":
    unittest.main()
