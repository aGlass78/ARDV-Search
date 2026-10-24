"""Stage 4 tests: general-purpose scope + no hardcoded delay floor.

1. The static wiktionary.org allowlist is gone. Scope derives from
   the seed URL: its hostname plus any deeper subdomains. Seeding
   quotes.toscrape.com must never wander to books.toscrape.com or to
   the parent toscrape.com - lifting the domain restriction must not
   turn the crawler loose on the entire internet.
2. The 2-second politeness floor is removed: --delay 0 is valid, the
   default is fast, and negative delays are still rejected. robots.txt
   Crawl-delay and Retry-After continue to apply (covered by stage 2).
"""
import unittest
from unittest import mock

from helpers import ServerTestCase, main, page, run_cli


class ScopeDerivationTests(unittest.TestCase):
    QUOTES_SEED = "http://quotes.toscrape.com/"

    def allowed(self, url):
        return main.is_allowed_host(
            url, main.get_allowed_hosts(self.QUOTES_SEED))

    def test_seed_host_in_scope(self):
        self.assertTrue(self.allowed("http://quotes.toscrape.com/page/1/"))

    def test_deeper_subdomains_in_scope(self):
        self.assertTrue(self.allowed("http://en.quotes.toscrape.com/x"))

    def test_sibling_subdomain_out_of_scope(self):
        # The headline requirement: a quotes.toscrape.com seed must
        # never wander to books.toscrape.com.
        self.assertFalse(self.allowed("http://books.toscrape.com/"))

    def test_parent_domain_out_of_scope(self):
        self.assertFalse(self.allowed("http://toscrape.com/"))

    def test_lookalike_hosts_out_of_scope(self):
        self.assertFalse(self.allowed("http://quotes.toscrape.com.evil.com/"))
        self.assertFalse(self.allowed("http://notquotes.toscrape.com/"))
        self.assertFalse(self.allowed("http://toscrape.org/"))

    def test_uppercase_hosts_normalized(self):
        self.assertTrue(self.allowed("HTTP://QUOTES.TOSCRAPE.COM/page"))

    def test_port_is_not_part_of_scope(self):
        # A seed on a non-standard port still scopes by hostname only.
        scope = main.get_allowed_hosts("http://quotes.toscrape.com:8443/")
        self.assertTrue(main.is_allowed_host("http://quotes.toscrape.com/", scope))

    def test_invalid_seed_has_no_scope(self):
        self.assertEqual(main.get_allowed_hosts("not a url"), set())

    def test_bare_domain_seed_allows_all_subdomains(self):
        # Seeding the bare domain is the documented way to allow
        # every subdomain at once.
        scope = main.get_allowed_hosts("http://toscrape.com/")
        self.assertTrue(main.is_allowed_host("http://toscrape.com/", scope))
        self.assertTrue(main.is_allowed_host("http://quotes.toscrape.com/", scope))
        self.assertTrue(main.is_allowed_host("http://books.toscrape.com/", scope))
        self.assertFalse(main.is_allowed_host("http://toscrape.org/", scope))


class CrawlScopeTests(ServerTestCase):
    def test_links_to_other_hosts_are_ignored(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        # The seed is 127.0.0.1; "localhost" is a *different* hostname
        # that the same server would happily answer - the crawler must
        # still refuse to follow it.
        s.add("/seed", page("Seed", "seed text",
                            [f"http://localhost:{s.port}/other", "/same"]))
        s.add("/other", page("Other", "must never be crawled"))
        s.add("/same", page("Same", "same host page"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.05)

        self.assertNotIn("/other", s.paths())  # never even requested
        self.assertEqual(self.indexed_paths(), ["/same", "/seed"])
        self.assertEqual(stats["links_discovered"], 1)


class ZeroDelayCrawlTests(ServerTestCase):
    def test_zero_delay_crawl_works(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", ["/p1", "/p2", "/p3"]))
        for p in ("/p1", "/p2", "/p3"):
            s.add(p, page(p, f"{p} body"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0)

        self.assertEqual(self.indexed_paths(),
                         ["/p1", "/p2", "/p3", "/seed"])
        self.assertEqual(stats["errors"], 0)
        self.assertLess(stats["elapsed_seconds"], 5.0)

    def test_negative_delay_rejected_by_crawl(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))

        result = self.crawl(s.url("/seed"), max_pages=5, delay=-1)

        self.assertIsNone(result)
        self.assertIn("negative", self.stdout)


class CliDefaultTests(unittest.TestCase):
    def test_delay_defaults_to_fast_value(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli(["crawl", "http://localhost/x"])
            self.assertIsNone(code)
            self.assertEqual(crawl.call_args.kwargs.get("delay"),
                             main.DEFAULT_DELAY)
        self.assertEqual(main.DEFAULT_DELAY, 0.0)


if __name__ == "__main__":
    unittest.main()
