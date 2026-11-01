"""Stage 13 tests: malformed-URL safety + robots trailing-slash semantics.

1. Malformed URLs must never crash the crawler. urlparse() raises
   ValueError for e.g. unmatched IPv6 brackets ("http://[::1"), and
   urljoin() can raise the same way on a malformed Location header.
   normalize_url() must fail closed (return None), and callers must
   treat an un-normalizable URL as out-of-scope rather than passing
   None into scope matching.

2. Robots permissions are checked against the exact URL that will be
   fetched (including any trailing slash), not the normalized form:
   /private and /private/ are distinct paths under robots.txt when the
   site uses exact ("$") rules, so stripping the slash before the
   robots check would evaluate the wrong path.
"""
import unittest

from helpers import ServerTestCase, main, page


class MalformedUrlTests(unittest.TestCase):
    def test_normalize_url_rejects_unmatched_ipv6_brackets(self):
        self.assertIsNone(main.normalize_url("http://[::1"))
        self.assertIsNone(main.normalize_url("http://["))
        self.assertIsNone(main.normalize_url("http://]"))

    def test_normalize_url_rejects_non_string_input(self):
        self.assertIsNone(main.normalize_url(None))
        self.assertIsNone(main.normalize_url(""))
        self.assertIsNone(main.normalize_url(42))

    def test_is_allowed_url_tolerates_none(self):
        scope = main.get_scope("https://example.com/")
        self.assertFalse(main.is_allowed_url(None, scope))
        self.assertFalse(main.is_allowed_url(None, ()))
        self.assertFalse(main.is_allowed_url("", scope))


class RedirectMalformedLocationTests(ServerTestCase):
    def test_malformed_redirect_target_is_skipped_safely(self):
        # The redirect Location is an unmatched-IPv6-bracket URL; the
        # chain must be abandoned (out_of_scope), not crash.
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed text", ["/bad"]))
        s.add("/bad", redirect="http://[::1", status=302)

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           threads=1)

        self.assertEqual(stats["out_of_scope"], 1)
        self.assertEqual(self.indexed_paths(), ["/seed"])
        self.assertIn("/bad", s.paths())        # blocking hop was fetched
        self.assertNotIn("://[::1", s.paths())  # malformed target never


class RobotsTrailingSlashTests(ServerTestCase):
    def test_disallowed_slash_form_blocks_redirect(self):
        # "Disallow: /private/$" blocks exactly /private/, not /private.
        # Checking the normalized form (trailing slash stripped) would
        # wrongly allow the redirect into the disallowed path.
        s = self.server
        s.add_robots("User-agent: *\nDisallow: /private/$\n")
        s.add("/seed", page("Seed", "seed text", ["/go"]))
        s.add("/go", redirect=s.url("/private/"), status=302)
        s.add("/private/", page("Private", "must not be crawled"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           threads=1)

        self.assertEqual(stats["robots_blocked"], 1)
        self.assertNotIn("/private/", s.paths())
        self.assertEqual(self.indexed_paths(), ["/seed"])

    def test_allowed_slash_form_follows_when_only_plain_disallowed(self):
        # "Disallow: /private$" blocks exactly /private; the redirect
        # target /private/ is a distinct path and allowed. Checking the
        # normalized "/private" would wrongly over-block it.
        s = self.server
        s.add_robots("User-agent: *\nDisallow: /private$\n")
        s.add("/seed", page("Seed", "seed text", ["/go"]))
        s.add("/go", redirect=s.url("/private/"), status=302)
        s.add("/private/", page("Private page", "its own rules"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           threads=1)

        self.assertEqual(stats["robots_blocked"], 0)
        self.assertIn("/private/", s.paths())
        # The page was fetched at /private/; storage canonicalizes the
        # final URL (trailing slash stripped), so the DB row is /private.
        self.assertEqual(sorted(self.indexed_paths()),
                         ["/private", "/seed"])


if __name__ == "__main__":
    unittest.main()
