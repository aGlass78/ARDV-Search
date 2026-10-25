"""Stage 8 tests: URL-prefix scope rules + new User-Agent.

1. The scope system is prefix-based. A rule 'host' allows the host
   and its subdomains anywhere; a rule 'host/path' limits the host to
   that path subtree with '/'-boundary matching: the user's exact
   requirement is that 'example.com/page' allows example.com/page/quote
   but NOT example.com/page2.
2. The CLI flag is --allow (with --allowed still accepted); entries
   are 'host' or 'host/path', optionally with an http(s):// scheme.
3. The crawler identifies itself as "ModelSearchEngine (educational
   crawler; contact: crawler.propeller706@passinbox.com)".
"""
import unittest
from unittest import mock

from helpers import ServerTestCase, main, page, run_cli


class ParseScopeEntryTests(unittest.TestCase):
    def test_bare_host(self):
        self.assertEqual(
            main.parse_scope_entry("Example.COM."),
            ("example.com", ""))

    def test_host_with_path(self):
        self.assertEqual(
            main.parse_scope_entry("example.com/page"),
            ("example.com", "/page"))

    def test_scheme_tolerated(self):
        self.assertEqual(
            main.parse_scope_entry("https://example.com/page"),
            ("example.com", "/page"))

    def test_trailing_slash_normalizes_away(self):
        self.assertEqual(
            main.parse_scope_entry("example.com/page/"),
            ("example.com", "/page"))
        self.assertEqual(
            main.parse_scope_entry("example.com/"),
            ("example.com", ""))

    def test_unusable_entries_rejected(self):
        for bad in ("", "  ", "x.com:8080", "ftp://x.com", "a b"):
            self.assertIsNone(main.parse_scope_entry(bad), bad)


class PathPrefixScopeTests(unittest.TestCase):
    """The user's exact requirement, at the rule-matching level."""

    SCOPE = main.get_scope(
        "http://elsewhere.org/", ["example.com/page"])

    def allowed(self, url):
        return main.is_allowed_url(url, self.SCOPE)

    def test_page_quote_is_in_scope(self):
        self.assertTrue(self.allowed("http://example.com/page/quote"))

    def test_page_itself_is_in_scope(self):
        self.assertTrue(self.allowed("http://example.com/page"))

    def test_page2_is_out_of_scope(self):
        # The prefix must end at a '/' boundary: '/page' never matches
        # '/page2' or '/pages'.
        self.assertFalse(self.allowed("http://example.com/page2"))
        self.assertFalse(self.allowed("http://example.com/pages"))

    def test_site_root_is_out_of_scope(self):
        self.assertFalse(self.allowed("http://example.com/"))
        self.assertFalse(self.allowed("http://example.com/other"))

    def test_subdomains_share_the_path_limit(self):
        self.assertTrue(self.allowed("http://sub.example.com/page/x"))
        self.assertFalse(self.allowed("http://sub.example.com/other"))

    def test_seed_host_stays_fully_allowed(self):
        self.assertTrue(self.allowed("http://elsewhere.org/any/path"))

    def test_bare_host_entry_allows_all_paths(self):
        scope = main.get_scope("http://a.com/", ["example.com"])
        self.assertTrue(
            main.is_allowed_url("http://example.com/anything", scope))
        self.assertTrue(
            main.is_allowed_url("http://example.com/page2", scope))


class PathPrefixCrawlTests(ServerTestCase):
    def test_path_scoped_allow_limits_the_crawl(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        # The seed is 127.0.0.1 (host-wide); "localhost" is a different
        # hostname reachable only through the path-scoped allow entry:
        # localhost/page/* is allowed, localhost/page2 is not. Scope
        # matches by hostname, so the entry carries no port.
        s.add("/seed", page("Seed", "seed text",
                            [f"http://localhost:{s.port}/page/quote",
                             f"http://localhost:{s.port}/page2",
                             "/local"]))
        s.add("/page/quote", page("Quote", "quote page"))
        s.add("/page2", page("Page2", "must never be crawled"))
        s.add("/local", page("Local", "local page"))

        stats = self.crawl(s.url("/seed"), max_pages=20, delay=0.02,
                           allowed=["localhost/page"])

        # /page/quote crawled and indexed... (sorted: different
        # hostnames come back in SQLite's URL order, not path order)
        self.assertEqual(
            sorted(self.indexed_paths()),
            ["/local", "/page/quote", "/seed"])
        # ...while /page2 was never even requested.
        self.assertNotIn("/page2", s.paths())
        self.assertEqual(stats["out_of_scope"], 0)  # dropped at discovery


class UserAgentTests(unittest.TestCase):
    def test_user_agent_constant(self):
        self.assertEqual(
            main.USER_AGENT,
            "ModelSearchEngine (educational crawler; "
            "contact: crawler.propeller706@passinbox.com)")

    def test_user_agent_sent_by_default_session(self):
        # get_session() headers carry the new agent for workers.
        session = main.get_session()
        try:
            self.assertEqual(
                session.headers.get("User-Agent"), main.USER_AGENT)
        finally:
            session.close()


class UserAgentCrawlTests(ServerTestCase):
    def test_server_sees_the_new_user_agent(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))

        self.crawl(s.url("/seed"), max_pages=5, delay=0.02)

        self.assertGreater(len(s.user_agents), 0)
        for agent in s.user_agents:
            self.assertEqual(agent, main.USER_AGENT)


class AllowCliTests(unittest.TestCase):
    def test_path_entries_pass_through(self):
        with mock.patch.object(main, "crawl", return_value=None) as crawl:
            code, _, _ = run_cli([
                "crawl", "http://example.com/",
                "--allow", "example.com/page",
                "--allow", "https://other.org/area",
            ])
            self.assertIsNone(code)
            self.assertEqual(
                crawl.call_args.kwargs.get("allowed"),
                ["example.com/page", "https://other.org/area"])

    def test_invalid_entry_rejected(self):
        with mock.patch.object(main, "crawl") as crawl:
            code, _, _ = run_cli([
                "crawl", "http://example.com/",
                "--allow", "example.com:8080/page",
            ])
            self.assertEqual(code, 2)
            crawl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
