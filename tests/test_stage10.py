"""Stage 10 tests: four bug fixes.

1. Every redirect destination is robots-checked (and scope-checked)
   BEFORE the hop is followed: disallowed or unverifiable robots stop
   the chain without fetching the target; same-origin chains never
   refetch robots.txt; concurrent chains never deadlock.
2. The robots cache is keyed by origin - scheme + hostname +
   effective port - so http and https policies for one host are
   cached independently.
3. Any valid port is crawlable by default (restricting ports is an
   explicit ALLOWED_PORTS policy); malformed URLs stay rejected.
4. Error counters are split: timeouts, network errors, parse errors,
   and database errors are counted separately ("errors" is the total).
"""
import sqlite3
import threading
import time
import unittest
from unittest import mock
from urllib.robotparser import RobotFileParser

import requests

from helpers import ServerTestCase, main, page


PERMISSIVE = RobotFileParser()
PERMISSIVE.parse(["User-agent: *", "Allow: /"])


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def close(self):
        pass


class FakeSession:
    """Serves different robots.txt policies per URL, offline."""

    def __init__(self, policies):
        self.policies = policies    # robots.txt URL -> response text
        self.fetched = []

    def get(self, url, timeout=None):
        self.fetched.append(url)
        text = self.policies.get(url)
        if text is None:
            return FakeResponse(404)
        return FakeResponse(200, text)


class OriginKeyTests(unittest.TestCase):
    def test_scheme_distinguishes_origins(self):
        self.assertNotEqual(
            main.origin_of("http://example.com/x"),
            main.origin_of("https://example.com/x"))

    def test_port_distinguishes_origins(self):
        self.assertNotEqual(
            main.origin_of("http://example.com/x"),
            main.origin_of("http://example.com:8080/x"))

    def test_default_ports_fold_into_scheme(self):
        self.assertEqual(
            main.origin_of("http://example.com/x"),
            main.origin_of("http://example.com:80/x"))
        self.assertEqual(
            main.origin_of("https://example.com/x"),
            main.origin_of("https://example.com:443/x"))

    def test_host_casing_normalized(self):
        self.assertEqual(
            main.origin_of("HTTP://EXAMPLE.COM/x"),
            main.origin_of("http://example.com/x"))

    def test_http_and_https_policies_cached_independently(self):
        session = FakeSession({
            "http://example.com/robots.txt":
                "User-agent: *\nDisallow: /private\n",
            "https://example.com/robots.txt":
                "User-agent: *\nAllow: /\n",
        })
        cache = main.RobotsCache()

        http_rules = cache.rules_for(session, "http://example.com/page")
        https_rules = cache.rules_for(session, "https://example.com/page")

        self.assertFalse(
            http_rules.can_fetch(main.USER_AGENT,
                                 "http://example.com/private"))
        self.assertTrue(
            https_rules.can_fetch(main.USER_AGENT,
                                  "https://example.com/private"))

        # Independent entries: repeated lookups hit each cache slot
        # exactly once, with only one fetch per scheme.
        cache.rules_for(session, "http://example.com/other")
        cache.rules_for(session, "https://example.com/other")
        self.assertEqual(
            session.fetched,
            ["http://example.com/robots.txt",
             "https://example.com/robots.txt"])

    def test_default_port_shares_one_cache_entry(self):
        session = FakeSession({
            "http://example.com/robots.txt": "User-agent: *\nAllow: /\n",
        })
        cache = main.RobotsCache()

        cache.rules_for(session, "http://example.com/a")
        cache.rules_for(session, "http://example.com:80/b")

        self.assertEqual(session.fetched,
                         ["http://example.com/robots.txt"])


class RedirectRobotsTests(ServerTestCase):
    def _setup_chain(self):
        s = self.server
        s.add_robots("User-agent: *\nDisallow: /secret\n")
        s.add("/seed", page("Seed", "seed text", ["/redirect"]))
        s.add("/redirect", redirect=f"http://localhost:{s.port}/secret",
              status=302)
        s.add("/secret", page("Secret", "must never be crawled"))
        return s

    def test_disallowed_redirect_target_never_fetched(self):
        s = self._setup_chain()

        # localhost is a different origin: only the path... only the
        # robots rule stands between the redirect and /secret.
        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           allowed=["localhost"])

        self.assertEqual(stats["robots_blocked"], 1)
        self.assertIn("/redirect", s.paths())
        self.assertNotIn("/secret", s.paths())   # never fetched
        self.assertEqual(self.indexed_paths(), ["/seed"])
        # The blocked hop made a real request (the redirect itself);
        # two origins = two robots.txt fetches.
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(s.paths().count("/robots.txt"), 2)

    def test_unverifiable_robots_stops_the_chain(self):
        s = self._setup_chain()

        def rules_by_origin(_self, session, url, limiter=None):
            return PERMISSIVE if "127.0.0.1" in url else None

        with mock.patch.object(main.RobotsCache, "rules_for",
                               rules_by_origin):
            stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                               allowed=["localhost"])

        self.assertEqual(stats["robots_unavailable"], 1)
        self.assertNotIn("/secret", s.paths())
        self.assertEqual(self.indexed_paths(), ["/seed"])

    def test_same_origin_chain_fetches_robots_once(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed text", ["/r1"]))
        s.add("/r1", redirect=s.url("/r2"), status=302)
        s.add("/r2", page("Real", "real body"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                          threads=1)

        self.assertEqual(self.indexed_paths(), ["/r2", "/seed"])
        self.assertEqual(s.paths().count("/robots.txt"), 1)

    def test_concurrent_chains_do_not_deadlock(self):
        # Two workers follow redirect chains through one shared
        # RobotsCache and RateLimiter. No lock is ever held across
        # network I/O, so both must finish (a deadlock hangs the join).
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/ra", redirect=s.url("/rb"), status=302)
        s.add("/rb", page("RB", "rb body"))
        s.slow["/robots.txt"] = 0.05   # widen any lock race windows

        robots = main.RobotsCache()
        limiter = main.RateLimiter(0.01)
        scope = main.get_scope(s.url("/ra"))
        sessions = [requests.Session() for _ in range(4)]
        outcomes = []

        def worker(sess):
            try:
                outcomes.append(main.fetch_html(
                    sess, s.url("/ra"), 10 ** 6, scope, limiter,
                    robots)[2])
            except Exception as error:      # pragma: no cover
                outcomes.append(error)

        threads = [threading.Thread(target=worker, args=(sess,))
                   for sess in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        for sess in sessions:
            sess.close()

        self.assertEqual(outcomes, [s.url("/rb")] * 4)
        self.assertLessEqual(s.paths().count("/robots.txt"), 4)


class PortPolicyTests(unittest.TestCase):
    def test_any_valid_port_is_allowed_by_default(self):
        self.assertEqual(
            main.normalize_url("http://example.com:8080/a"),
            "http://example.com:8080/a")
        self.assertEqual(
            main.normalize_url("https://example.com:54321/a"),
            "https://example.com:54321/a")

    def test_malformed_ports_still_rejected(self):
        self.assertIsNone(main.normalize_url("http://example.com:80x/"))
        self.assertIsNone(main.normalize_url("http://example.com:99999/"))
        self.assertIsNone(main.normalize_url("ftp://example.com/"))
        self.assertIsNone(main.normalize_url("http://u:p@example.com/"))

    def test_explicit_policy_still_restricts(self):
        with mock.patch.object(main, "ALLOWED_PORTS", {80, 443}):
            self.assertIsNone(
                main.normalize_url("http://example.com:8080/"))
            self.assertEqual(
                main.normalize_url("http://example.com:80/a"),
                "http://example.com/a")


class SplitErrorCounterTests(ServerTestCase):
    def _crawl_one(self, **patches):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))
        return self.crawl(s.url("/seed"), max_pages=5, delay=0.02,
                          threads=1)

    def test_timeout_gets_its_own_counter(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/hang"]))
        s.slow["/hang"] = 1.0

        with mock.patch.object(main, "REQUEST_TIMEOUT", 0.2):
            stats = self.crawl(s.url("/seed"), max_pages=5, delay=0.02,
                               threads=1)

        self.assertEqual(stats["timeouts"], 1)
        self.assertEqual(stats["network_errors"], 0)
        self.assertEqual(stats["parse_errors"], 0)
        self.assertEqual(stats["db_errors"], 0)
        self.assertEqual(stats["errors"], 1)
        self.assertIn("timed out", self.stdout)

    def test_network_errors_counted_separately(self):
        class ExplodingSession:
            def get(self, *args, **kwargs):
                raise requests.ConnectionError("boom")

            def close(self):
                pass

        with mock.patch.object(main, "get_session",
                               return_value=ExplodingSession()), \
             mock.patch.object(main.RobotsCache, "rules_for",
                               lambda *a, **k: PERMISSIVE):
            stats = self._crawl_one()

        self.assertEqual(stats["network_errors"], 1)
        self.assertEqual(stats["timeouts"], 0)
        self.assertEqual(stats["errors"], 1)
        self.assertIn("Request failed", self.stdout)

    def test_parse_errors_counted_separately(self):
        def broken_soup(*args, **kwargs):
            raise RuntimeError("parser exploded")

        with mock.patch.object(main, "BeautifulSoup", broken_soup), \
             mock.patch.object(main.RobotsCache, "rules_for",
                               lambda *a, **k: PERMISSIVE):
            stats = self._crawl_one()

        self.assertEqual(stats["parse_errors"], 1)
        self.assertEqual(stats["network_errors"], 0)
        self.assertEqual(stats["errors"], 1)
        self.assertIn("Processing failed", self.stdout)

    def test_db_errors_counted_separately(self):
        class FailingConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk full")

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        with mock.patch.object(main, "get_database",
                               return_value=FailingConn()):
            stats = self._crawl_one()

        self.assertEqual(stats["db_errors"], 1)
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(stats["errors"], 1)
        self.assertIn("Database error", self.stdout)


if __name__ == "__main__":
    unittest.main()
