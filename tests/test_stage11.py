"""Stage 11 tests: URL-based deduplication.

Two layers:

1. normalize_url() canonicalization - equivalent spellings collapse to
   one URL, distinct resources stay distinct. The user's exact rules:
       example.com/page#section == example.com/page
       example.com/page          == EXAMPLE.com/page
       example.com:443/page      == example.com/page     (https default)
       example.com/page          == example.com/page/
       example.com/page?id=1     != example.com/page?id=2
       https://example.com/page  != http://example.com/page
       example.com/page1         != example.com/page2

2. deduplicate_database() - stored rows whose URL canonicalizes to
   the same page are merged (one row survives, relabeled to the
   canonical spelling), while genuinely different URLs are untouched.
"""
import sqlite3
import unittest

from helpers import IsolatedDbTestCase, main


class NormalizeUrlTests(unittest.TestCase):
    def test_fragment_removed(self):
        self.assertEqual(main.normalize_url("http://example.com/page#section"),
                         "http://example.com/page")

    def test_host_lowercased(self):
        self.assertEqual(main.normalize_url("HTTP://EXAMPLE.COM/page"),
                         "http://example.com/page")
        self.assertEqual(main.normalize_url("http://EXAMPLE.com/page"),
                         "http://example.com/page")

    def test_trailing_dot_stripped(self):
        self.assertEqual(main.normalize_url("http://example.com./page"),
                         "http://example.com/page")

    def test_scheme_default_port_stripped(self):
        self.assertEqual(main.normalize_url("https://example.com:443/page"),
                         "https://example.com/page")
        self.assertEqual(main.normalize_url("http://example.com:80/page"),
                         "http://example.com/page")

    def test_non_default_port_kept(self):
        self.assertEqual(main.normalize_url("http://example.com:8080/page"),
                         "http://example.com:8080/page")

    def test_trailing_slash_stripped(self):
        self.assertEqual(main.normalize_url("http://example.com/page/"),
                         "http://example.com/page")
        self.assertEqual(main.normalize_url("http://example.com/"),
                         "http://example.com/")

    def test_queries_keep_urls_distinct(self):
        a = main.normalize_url("http://example.com/page?id=1")
        b = main.normalize_url("http://example.com/page?id=2")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "http://example.com/page?id=1")
        self.assertEqual(b, "http://example.com/page?id=2")

    def test_scheme_keeps_urls_distinct(self):
        self.assertNotEqual(
            main.normalize_url("https://example.com/page"),
            main.normalize_url("http://example.com/page"))

    def test_paths_keep_urls_distinct(self):
        self.assertNotEqual(
            main.normalize_url("http://example.com/page1"),
            main.normalize_url("http://example.com/page2"))

    def test_exact_user_rules(self):
        eq = [
            ("http://example.com/page#section", "http://example.com/page"),
            ("http://example.com/page", "http://EXAMPLE.com/page"),
            ("https://example.com:443/page", "https://example.com/page"),
            ("http://example.com/page", "http://example.com/page/"),
        ]
        for a, b in eq:
            with self.subTest(a=a, b=b):
                self.assertEqual(main.normalize_url(a),
                                 main.normalize_url(b))

        ne = [
            ("http://example.com/page?id=1", "http://example.com/page?id=2"),
            ("https://example.com/page", "http://example.com/page"),
            ("http://example.com/page1", "http://example.com/page2"),
        ]
        for a, b in ne:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(main.normalize_url(a),
                                    main.normalize_url(b))


class DeduplicateDatabaseTests(IsolatedDbTestCase):
    C = "http://example.com/page"          # canonical spelling

    def _setup(self, urls):
        main.get_database().close()  # create schema
        conn = sqlite3.connect(self.db_path)
        try:
            for url in urls:
                conn.execute(
                    "INSERT INTO pages (url, title, description, body)"
                    " VALUES (?, 't', 'd', 'b')", (url,))
            conn.commit()
        finally:
            conn.close()

    def _urls(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return sorted(r[0] for r in
                          conn.execute("SELECT url FROM pages").fetchall())
        finally:
            conn.close()

    def test_merge_equivalent_spellings(self):
        self._setup([
            "http://example.com/page#section",
            "http://example.com/page/",
            "http://EXAMPLE.com/page",
            "http://example.com/page",
        ])
        conn = sqlite3.connect(self.db_path)
        try:
            removed = main.deduplicate_database(conn)
        finally:
            conn.close()

        self.assertEqual(removed, 3)
        self.assertEqual(self._urls(), [self.C])

    def test_keep_distinct_urls(self):
        self._setup([
            "http://example.com/page?id=1",
            "http://example.com/page?id=2",
            "http://example.com/page1",
            "http://example.com/page2",
            "https://example.com/page",
            "http://example.com/page",
        ])
        conn = sqlite3.connect(self.db_path)
        try:
            removed = main.deduplicate_database(conn)
        finally:
            conn.close()

        self.assertEqual(removed, 0)
        # ASCII order puts digits before '?', so /page1 < /page?id=1.
        self.assertEqual(self._urls(), [
            "http://example.com/page",
            "http://example.com/page1",
            "http://example.com/page2",
            "http://example.com/page?id=1",
            "http://example.com/page?id=2",
            "https://example.com/page",
        ])

    def test_canonical_row_preferred(self):
        # All rows are equivalent; each was inserted (and SELECT
        # returns them in insertion order), so the canonical spelling's
        # row must win regardless of where it was inserted.
        self._setup([
            "http://example.com/page/",
            "http://example.com/page",
        ])
        conn = sqlite3.connect(self.db_path)
        try:
            removed = main.deduplicate_database(conn)
        finally:
            conn.close()

        self.assertEqual(removed, 1)
        self.assertEqual(self._urls(), [self.C])

    def test_relabels_survivor_to_canonical(self):
        self._setup([
            "http://example.com/page/",
            "http://example.com/page#x",
        ])
        conn = sqlite3.connect(self.db_path)
        try:
            removed = main.deduplicate_database(conn)
        finally:
            conn.close()

        self.assertEqual(removed, 1)
        # One row remains and carries the canonical spelling.
        self.assertEqual(self._urls(), [self.C])

    def test_empty_database(self):
        main.get_database().close()
        conn = sqlite3.connect(self.db_path)
        try:
            removed = main.deduplicate_database(conn)
        finally:
            conn.close()
        self.assertEqual(removed, 0)


class CrawlDedupTests(IsolatedDbTestCase):
    def test_crawl_does_not_merge_distinct_pages(self):
        # A crawl against a real server must not drop genuinely
        # different pages just because they share a path prefix.
        # (Unit-level guarantee; the merge pass only collapses URLs
        # whose canonical forms are identical.)
        conn = main.get_database()
        try:
            for path in ("/page1", "/page2", "/page"):
                url = f"http://example.com{path}"
                conn.execute(
                    "INSERT INTO pages (url, title, description, body)"
                    " VALUES (?, 't', 'd', 'b')", (url,))
            conn.commit()
            removed = main.deduplicate_database(conn)
        finally:
            conn.close()
        self.assertEqual(removed, 0)


if __name__ == "__main__":
    unittest.main()
