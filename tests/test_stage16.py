"""Stage 16: --db database selection for crawl and search.

Tests that crawl() indexes into a chosen database file and that
search()/run_search() query the chosen file - reporting "No database
found" when that specific file is missing, without falling back to the
default result.db. Also checks the .db-extension appending rule: a
--db value that does not end in ".db" gets ".db" appended.
"""
import contextlib
import io
import pathlib
import unittest

import main
from helpers import IsolatedDbTestCase, ServerTestCase, page, run_cli


class ResolveDbPathTests(unittest.TestCase):
    def test_none_returns_default_db_path(self):
        self.assertEqual(main.resolve_db_path(None), main.DB_PATH)

    def test_absolute_path_is_kept(self):
        p = pathlib.Path("C:/tmp/custom.db")
        self.assertEqual(main.resolve_db_path(p), p)

    def test_relative_path_is_anchored_next_to_main(self):
        resolved = main.resolve_db_path("custom.db")
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.parent, main.DB_PATH.parent)
        self.assertEqual(resolved.name, "custom.db")

    def test_relative_subpath_is_anchored(self):
        resolved = main.resolve_db_path("data/nested.db")
        self.assertEqual(
            resolved, main.DB_PATH.parent / "data" / "nested.db")

    def test_missing_extension_appends_db(self):
        self.assertEqual(
            main.resolve_db_path("custom"),
            main.DB_PATH.parent / "custom.db")

    def test_existing_db_extension_is_kept(self):
        self.assertEqual(
            main.resolve_db_path("custom.db"),
            main.DB_PATH.parent / "custom.db")

    def test_existing_db_extension_case_insensitive(self):
        self.assertEqual(
            main.resolve_db_path("custom.DB"),
            main.DB_PATH.parent / "custom.DB")

    def test_dot_in_name_without_db_extension_appends(self):
        self.assertEqual(
            main.resolve_db_path("archive.v1"),
            main.DB_PATH.parent / "archive.v1.db")

    def test_absolute_without_extension_appends_db(self):
        p = pathlib.Path("C:/tmp/custom")
        self.assertEqual(
            main.resolve_db_path(p),
            pathlib.Path("C:/tmp/custom.db"))


class CrawlDbTests(ServerTestCase):
    def test_crawl_indexes_into_chosen_db(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body", links=("/one", "/two")))
        s.add("/one", "one body")
        s.add("/two", "two body")

        custom = self.tmp_path / "custom.db"
        stats = self.crawl(s.url("/seed"), max_pages=10,
                           threads=1, db_path=custom)

        self.assertTrue(custom.exists())
        self.assertFalse(main.DB_PATH.exists())
        self.assertEqual(stats["db_path"], str(custom))

        conn = main.get_database(custom)
        try:
            rows = conn.execute("SELECT url FROM pages ORDER BY url").fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [r[0] for r in rows],
            ["http://127.0.0.1:%d/one" % s.port,
             "http://127.0.0.1:%d/seed" % s.port,
             "http://127.0.0.1:%d/two" % s.port])

    def test_summary_prints_chosen_db(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", "seed body")

        custom = self.tmp_path / "other.db"
        stats = self.crawl(s.url("/seed"), max_pages=5, threads=1,
                           db_path=custom)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.print_crawl_summary(stats)
        self.assertIn(str(custom), out.getvalue())
        self.assertNotIn(str(main.DB_PATH), out.getvalue())

    def test_cli_db_flag_creates_named_db(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", "seed body")

        custom = self.tmp_path / "cli.db"
        code, out, err = run_cli([
            "crawl", s.url("/seed"), "--db", str(custom),
            "--max-pages", "5", "--threads", "1",
        ])
        self.assertIsNone(code, out + err)
        self.assertTrue(custom.exists())
        self.assertFalse(main.DB_PATH.exists())
        self.assertIn(str(custom), out)

    def test_cli_db_flag_without_extension_appends_db(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", "seed body")

        bare = self.tmp_path / "bare"
        code, out, err = run_cli([
            "crawl", s.url("/seed"), "--db", str(bare),
            "--max-pages", "5", "--threads", "1",
        ])
        self.assertIsNone(code, out + err)
        expected = pathlib.Path(str(bare) + ".db")
        self.assertTrue(expected.exists())
        self.assertFalse(bare.exists())
        self.assertIn(str(expected), out)


class SearchDbTests(IsolatedDbTestCase):
    def test_search_reads_chosen_db(self):
        custom = self.tmp_path / "custom.db"
        conn = main.get_database(custom)
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://example.com/", "Hello", "", "custom word"))
            conn.commit()
        finally:
            conn.close()

        rows = main.run_search("custom", db_path=custom)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "http://example.com/")

    def test_search_does_not_fall_back_to_default_db(self):
        # Seed the default DB (the one search would otherwise use).
        conn = main.get_database()
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://default.example/", "Default", "", "default word"))
            conn.commit()
        finally:
            conn.close()

        # Searching a DIFFERENT (missing) db must not read it.
        missing = self.tmp_path / "missing.db"
        self.assertFalse(missing.exists())
        self.assertEqual(main.run_search("default", db_path=missing), [])

    def test_search_prints_no_database_for_missing_choice(self):
        missing = self.tmp_path / "missing.db"
        out = self._search_output("anything", db_path=missing)
        self.assertIn("No database found", out)
        self.assertIn(str(missing), out)
        self.assertNotIn("No results found", out)

    def test_search_works_with_missing_default_present_choice(self):
        # Default DB absent, chosen DB present and searchable.
        custom = self.tmp_path / "custom.db"
        conn = main.get_database(custom)
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://example.com/", "Hello", "", "custom word"))
            conn.commit()
        finally:
            conn.close()

        self.assertFalse(main.DB_PATH.exists())
        out = self._search_output("custom", db_path=custom)
        self.assertIn("Results for: custom", out)
        self.assertIn("http://example.com/", out)

    def test_cli_search_db_flag(self):
        custom = self.tmp_path / "custom.db"
        conn = main.get_database(custom)
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://example.com/", "Hello", "", "cli word"))
            conn.commit()
        finally:
            conn.close()

        result = run_cli(["search", "cli", "--db", str(custom)])
        code, out, err = result
        self.assertIsNone(code, out + err)
        self.assertIn("Results for: cli", out)
        self.assertIn("http://example.com/", out)

    def test_cli_search_missing_db_prints_no_database(self):
        # A default result.db exists, but --db points at a missing file.
        conn = main.get_database()
        conn.close()

        missing = self.tmp_path / "missing.db"
        code, out, err = run_cli(["search", "anything", "--db", str(missing)])
        self.assertIn("No database found", out)
        self.assertIn(str(missing), out)

    def _search_output(self, query, db_path=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.search(query, db_path=db_path)
        return out.getvalue()


if __name__ == "__main__":
    unittest.main()
