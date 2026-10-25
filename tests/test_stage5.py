"""Stage 5 tests: the six review fixes on the multithreaded engine.

1. --allowed: extra hostnames join the seed-derived scope.
2. robots.txt is fetched OUTSIDE the cache lock, so workers never
   serialize on the robots check (parallel origins overlap).
3. Pacing is keyed by hostname, not hostname:port - one machine on
   several ports shares a single rate limit.
4. Retry-After is a temporary pause (not a sticky floor) plus exactly
   one automatic retry of the rate-limited URL.
5. Redirect scope is checked BEFORE each hop is followed; loops and
   over-long chains are abandoned, never followed forever.
6. Database writes are batched (DB_COMMIT_BATCH) with a final flush.
"""
import sqlite3
import threading
import time
import unittest
from unittest import mock

import requests

from helpers import ServerTestCase, TestServer, main, page, run_cli


class AllowedHostTests(unittest.TestCase):
    def test_extra_hosts_join_seed_scope(self):
        scope = main.get_scope(
            "http://quotes.toscrape.com/",
            ["books.toscrape.com", "EXAMPLE.org."]
        )
        self.assertTrue(
            main.is_allowed_url("http://quotes.toscrape.com/x", scope))
        self.assertTrue(
            main.is_allowed_url("http://books.toscrape.com/x", scope))
        self.assertTrue(
            main.is_allowed_url("http://example.org/y", scope))

    def test_invalid_extras_are_skipped_leniently(self):
        # Ports, empty strings, and whitespace are unusable; paths
        # are valid now (host/path scoping).
        scope = main.get_scope(
            "http://a.com/", ["", "x:8080", "  ", "good.org", "b.org/area"]
        )
        self.assertTrue(
            main.is_allowed_url("http://good.org/z", scope))
        self.assertTrue(
            main.is_allowed_url("http://b.org/area/z", scope))
        self.assertFalse(
            main.is_allowed_url("http://b.org/other", scope))

    def test_cli_allowed_is_repeatable(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli([
                "crawl", "http://quotes.toscrape.com/",
                "--allowed", "books.toscrape.com",
                "--allowed", "blog.toscrape.com",
            ])
            self.assertIsNone(code)
            self.assertEqual(
                crawl.call_args.kwargs.get("allowed"),
                ["books.toscrape.com", "blog.toscrape.com"]
            )

    def test_cli_allow_spelling_also_works(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli([
                "crawl", "http://quotes.toscrape.com/",
                "--allow", "books.toscrape.com",
                "--allow", "example.com/page",
            ])
            self.assertIsNone(code)
            self.assertEqual(
                crawl.call_args.kwargs.get("allowed"),
                ["books.toscrape.com", "example.com/page"]
            )

    def test_cli_allowed_rejects_unusable_entries(self):
        for bad in ("x.com:8080", "", "ftp://x.com", "  "):
            with mock.patch.object(main, "crawl") as crawl:
                code, _, _ = run_cli([
                    "crawl", "http://quotes.toscrape.com/",
                    "--allowed", bad,
                ])
                self.assertEqual(code, 2, f"expected rejection for {bad!r}")
                crawl.assert_not_called()


class AllowedCrawlTests(ServerTestCase):
    def test_extra_allowed_host_is_crawled(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        # The seed host is 127.0.0.1; "localhost" is a different
        # hostname that is out of scope unless explicitly allowed.
        s.add("/seed", page("Seed", "seed text",
                            [f"http://localhost:{s.port}/other", "/same"]))
        s.add("/other", page("Other", "other body"))
        s.add("/same", page("Same", "same body"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           allowed=["localhost"])

        # Sort paths so the assertion is independent of the order in
        # which SQLite returns different hostnames/ports.
        self.assertEqual(sorted(self.indexed_paths()),
                         ["/other", "/same", "/seed"])
        self.assertIn("/other", s.paths())


class RobotsConcurrencyTests(ServerTestCase):
    def test_parallel_origins_do_not_serialize(self):
        # Two origins whose robots.txt each take 0.3s. If the robots
        # fetch were performed while holding a lock, the checks would
        # take >= 0.6s serialized; unlocked fetching overlaps them.
        s = self.server
        s.slow["/robots.txt"] = 0.3
        s.add_robots("User-agent: *\nAllow: /\n")

        robots = main.RobotsCache()
        results = [None, None]
        barrier = threading.Barrier(2)

        def worker(index, session):
            barrier.wait()
            results[index] = robots.rules_for(session, s.url("/seed"))

        sessions = []
        for _ in range(2):
            sess = requests.Session()
            sess.headers.update({"User-Agent": main.USER_AGENT})
            sessions.append(sess)

        threads = [
            threading.Thread(target=worker, args=(i, sessions[i]))
            for i in range(2)
        ]
        started = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.55,
                        f"robots checks serialized: {elapsed:.2f}s")
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        for sess in sessions:
            sess.close()

    def test_same_origin_race_is_safe(self):
        s = self.server
        s.slow["/robots.txt"] = 0.1
        s.add_robots("User-agent: *\nAllow: /\n")

        robots = main.RobotsCache()
        results = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def worker():
            sess = requests.Session()
            sess.headers.update({"User-Agent": main.USER_AGENT})
            barrier.wait()
            rules = robots.rules_for(sess, s.url("/x"))
            with lock:
                results.append(rules is not None)
            sess.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results, [True] * 8)
        # Bounded duplicate fetches on a cold cache are acceptable;
        # every worker must still end up with valid rules.
        self.assertLessEqual(s.paths().count("/robots.txt"), 8)


class SharedHostPacingTests(ServerTestCase):
    def test_same_hostname_different_ports_share_pacing(self):
        # Two servers, same hostname (127.0.0.1), different ports. If
        # pacing were keyed on host:port (netloc), alternating ports
        # would evade the rate limit entirely.
        a = self.server
        b = TestServer()
        b.start()
        self.addCleanup(b.stop)

        for srv in (a, b):
            srv.add_robots("User-agent: *\nAllow: /\n")
        a.add("/seed", page("Seed", "seed body",
                            [a.url("/p1"), b.url("/p2")]))
        a.add("/p1", page("P1", "one"))
        b.add("/p2", page("P2", "two"))

        self.crawl(a.url("/seed"), max_pages=10, delay=0.2, threads=1)

        merged = sorted(a.requests + b.requests)
        gaps = [x[0] - t[0] for t, x in zip(merged, merged[1:])]
        # Generous tolerance for Windows timer granularity.
        self.assertGreaterEqual(min(gaps), 0.15,
                                f"ports got separate pacing, gaps: {gaps}")
        self.assertEqual(sorted(self.indexed_paths()),
                         ["/p1", "/p2", "/seed"])


class RedirectScopeTests(ServerTestCase):
    def test_out_of_scope_redirect_target_never_fetched(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/escape"]))
        s.add("/escape", redirect=f"http://localhost:{s.port}/landing",
              status=302)
        s.add("/landing", page("Landing", "never crawled"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           threads=1)

        self.assertNotIn("/landing", s.paths())  # scope checked before hop
        self.assertNotIn("/landing", self.indexed_paths())
        self.assertEqual(stats["out_of_scope"], 1)
        self.assertEqual(self.indexed_paths(), ["/seed"])

    def test_multi_hop_chain_followed_within_scope(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/r1"]))
        s.add("/r1", redirect=s.url("/r2"), status=301)
        s.add("/r2", redirect=s.url("/r3"), status=302)
        s.add("/r3", page("Real", "real body"))

        self.crawl(s.url("/seed"), max_pages=10, delay=0.02, threads=1)

        self.assertEqual(self.indexed_paths(), ["/r3", "/seed"])
        for hop in ("/r1", "/r2", "/r3"):
            self.assertIn(hop, s.paths())

    def test_redirect_hops_are_paced(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/r1"]))
        s.add("/r1", redirect=s.url("/r2"), status=302)
        s.add("/r2", page("Real", "real body"))

        self.crawl(s.url("/seed"), max_pages=10, delay=0.15, threads=1)

        paths = s.paths()
        gaps = s.gaps()
        hop_gap = gaps[paths.index("/r1")]
        self.assertGreaterEqual(hop_gap, 0.10,
                                f"redirect hop not paced: {hop_gap}")
    def test_redirect_loop_is_abandoned(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/loopa"]))
        s.add("/loopa", redirect=s.url("/loopb"), status=302)
        s.add("/loopb", redirect=s.url("/loopa"), status=302)

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           threads=1)

        # Abandoned after MAX_REDIRECTS hops, never an infinite loop.
        self.assertEqual(stats["out_of_scope"], 1)
        self.assertEqual(self.indexed_paths(), ["/seed"])
        self.assertLessEqual(len(s.paths()), 2 + main.MAX_REDIRECTS + 2)


class CommitCountingConn:
    """Wraps a sqlite3 connection and counts commit() calls."""

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        self.commits += 1
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class BatchCommitTests(ServerTestCase):
    def test_writes_are_batched_and_flushed(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body",
                            [f"/p{i}" for i in range(9)]))
        for i in range(9):
            s.add(f"/p{i}", page(f"P{i}", f"page {i}"))

        real = main.get_database()
        proxy = CommitCountingConn(real)

        with mock.patch.object(main, "DB_COMMIT_BATCH", 3), \
             mock.patch.object(main, "get_database", return_value=proxy):
            stats = self.crawl(s.url("/seed"), max_pages=20, delay=0.02,
                               threads=1)

        # 10 indexed pages, one commit per 3 pages plus the final
        # flush of the open batch: 4 commits instead of 10.
        self.assertEqual(proxy.commits, 4)
        self.assertEqual(stats["indexed"], 10)
        self.assertEqual(len(self.indexed_paths()), 10)

    def test_default_batch_size_is_sensible(self):
        self.assertGreaterEqual(main.DB_COMMIT_BATCH, 10)


if __name__ == "__main__":
    unittest.main()
