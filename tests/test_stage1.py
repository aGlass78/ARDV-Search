"""Stage 1 tests: critical correctness fixes.

Written before the fixes were applied. Against the original code these
tests fail (or error); afterwards they must all pass:

1. Links discovered on a page are queued even when that page itself
   cannot be indexed.
2. normalize_url never raises (malformed ports etc.), lowercases the
   host, strips scheme-default ports, and does not merge distinct URLs.
3. Every HTTP request is paced by the configured per-host delay, even
   when the previous response was a failure, non-HTML, or blocked.
4. --delay is validated explicitly by the CLI instead of clamped.
5. Redirect targets are not fetched or indexed twice.
"""
import unittest
from unittest import mock

from helpers import ServerTestCase, main, page, run_cli


class NormalizeUrlTests(unittest.TestCase):
    """normalize_url must never raise and must canonicalize safely."""

    def test_malformed_port_returns_none(self):
        # parsed.port raises ValueError on ":80x" - this must be contained.
        self.assertIsNone(main.normalize_url("http://example.com:80x/"))

    def test_out_of_range_port_returns_none(self):
        self.assertIsNone(main.normalize_url("http://example.com:99999/"))

    def test_non_standard_port_allowed(self):
        # Any valid port is crawlable; restricting ports is an
        # explicit policy (ALLOWED_PORTS), not the default.
        self.assertEqual(main.normalize_url("http://example.com:8080/a"),
                         "http://example.com:8080/a")

    def test_default_http_port_stripped(self):
        self.assertEqual(main.normalize_url("http://example.com:80/a"),
                         "http://example.com/a")

    def test_default_https_port_stripped(self):
        self.assertEqual(main.normalize_url("https://example.com:443/a"),
                         "https://example.com/a")

    def test_port_kept_when_not_scheme_default(self):
        # 80 is an allowed port on https but is NOT the https default:
        # https://x:80 and https://x are distinct resources, do not merge.
        self.assertEqual(main.normalize_url("https://example.com:80/a"),
                         "https://example.com:80/a")

    def test_scheme_and_host_lowercased(self):
        self.assertEqual(main.normalize_url("HTTP://EXAMPLE.COM/Path"),
                         "http://example.com/Path")

    def test_empty_path_becomes_slash(self):
        self.assertEqual(main.normalize_url("http://example.com"),
                         "http://example.com/")

    def test_fragment_removed(self):
        self.assertEqual(main.normalize_url("http://example.com/a#frag"),
                         "http://example.com/a")

    def test_query_preserved(self):
        self.assertEqual(main.normalize_url("http://example.com/a?x=1"),
                         "http://example.com/a?x=1")

    def test_path_params_preserved(self):
        self.assertEqual(main.normalize_url("http://example.com/p;a=b"),
                         "http://example.com/p;a=b")

    def test_userinfo_rejected(self):
        self.assertIsNone(main.normalize_url("http://user:pass@example.com/"))

    def test_non_http_scheme_rejected(self):
        self.assertIsNone(main.normalize_url("ftp://example.com/"))

    def test_empty_url_returns_none(self):
        self.assertIsNone(main.normalize_url(""))
        self.assertIsNone(main.normalize_url(None))

    def test_ipv6_brackets_preserved(self):
        self.assertEqual(main.normalize_url("http://[::1]/x"), "http://[::1]/x")

    def test_ipv6_default_port_stripped(self):
        self.assertEqual(main.normalize_url("http://[::1]:80/x"), "http://[::1]/x")


class DelayValidationTests(unittest.TestCase):
    """--delay has no politiness floor: zero is fine, negative is not."""

    def test_zero_delay_accepted(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli(["crawl", "http://localhost/x",
                                   "--delay", "0"])
            self.assertIsNone(code)
            crawl.assert_called_once()

    def test_fractional_delay_accepted(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli(["crawl", "http://localhost/x",
                                   "--delay", "0.5"])
            self.assertIsNone(code)
            crawl.assert_called_once()

    def test_negative_delay_rejected(self):
        with mock.patch.object(main, "crawl") as crawl:
            code, _, _ = run_cli(["crawl", "http://localhost/x",
                                   "--delay", "-1"])
            self.assertEqual(code, 2)
            crawl.assert_not_called()

    def test_default_is_fast(self):
        # The old 2.0s floor is gone; the default must be faster.
        self.assertLess(main.DEFAULT_DELAY, 1.0)


class PacingTests(ServerTestCase):
    """Every HTTP request must be spaced by the configured delay.

    404s, non-HTML responses, and empty pages must not let the crawler
    exceed its configured per-host rate (the old code only slept after
    successfully indexing a page).
    """

    def test_pacing_survives_404_binary_and_empty_pages(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body",
                            ["/missing", "/binaryish", "/empty", "/good1"]))
        s.add("/missing", "gone", status=404)
        s.add("/binaryish", "not html", content_type="application/pdf")
        s.add("/empty", page(None, "", ["/good2"]))  # fetched, not indexable
        s.add("/good1", page("Good", "good body"))
        s.add("/good2", page("Good2", "good2 body"))

        self.crawl(s.url("/seed"), max_pages=10, delay=0.15)

        paths = s.paths()
        # 7 requests: robots.txt + 6 pages (all of them get fetched).
        self.assertEqual(len(paths), 7)
        gaps = s.gaps()
        self.assertGreaterEqual(min(gaps), 0.10,
                                f"requests bunched too closely: {gaps}")
        # /missing (404), /binaryish (non-HTML) and /empty (nothing to
        # index) are fetched but not indexed; the rest are.
        self.assertEqual(self.indexed_paths(), ["/good1", "/good2", "/seed"])


class RobotsBlockingTests(ServerTestCase):
    def test_disallowed_path_is_never_fetched(self):
        s = self.server
        s.add_robots("User-agent: *\nDisallow: /blocked\n")
        s.add("/seed", page("Seed", "seed body", ["/blocked", "/good1"]))
        s.add("/good1", page("Good", "good body"))

        self.crawl(s.url("/seed"), max_pages=10, delay=0.05)

        self.assertNotIn("/blocked", s.paths())
        self.assertEqual(self.indexed_paths(), ["/good1", "/seed"])


class LostLinksTests(ServerTestCase):
    def test_links_survive_when_page_is_not_indexable(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        # No title, no visible text: the page itself cannot be indexed,
        # but it links onward and those links must still be crawled.
        s.add("/empty", page(None, "", ["/deep"]))
        s.add("/deep", page("Deep", "deep body"))

        self.crawl(s.url("/empty"), max_pages=10, delay=0.05)

        self.assertIn("/deep", s.paths())
        self.assertEqual(self.indexed_paths(), ["/deep"])


class RedirectTests(ServerTestCase):
    def test_redirect_target_not_fetched_twice(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/dupa", "/dupb"]))
        s.add("/dupa", redirect=s.url("/dupb"), status=302)
        s.add("/dupb", page("Dup", "dup body"))

        # threads=1 keeps the single-fetch guarantee deterministic:
        # with parallel workers /dupb's direct fetch may race /dupa's
        # redirect hop (still indexed exactly once either way).
        self.crawl(s.url("/seed"), max_pages=10, delay=0.05, threads=1)

        self.assertEqual(s.paths().count("/dupb"), 1,
                         f"/dupb must be fetched once, got {s.paths()}")
        self.assertEqual(self.indexed_paths(), ["/dupb", "/seed"])

    def test_seed_redirect_indexes_final_url(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/start", redirect=s.url("/real"), status=302)
        s.add("/real", page("Real", "real body"))

        self.crawl(s.url("/start"), max_pages=10, delay=0.05, threads=1)

        self.assertEqual(self.indexed_paths(), ["/real"])
        self.assertEqual(s.paths().count("/real"), 1)


if __name__ == "__main__":
    unittest.main()
