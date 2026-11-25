"""Stage 20 (Phase 1): schema v2 helpers + FTS5 synchronization triggers.

Tests the new ordinary `pages` table + external-content `pages_fts` table
(create_schema_v2 / _create_fts_triggers) WITHOUT switching the crawler
search path or migration to v2. The crawler and search implementations are
left untouched; these tests exercise the v2 helpers directly on isolated
databases.
"""
import sqlite3
import unittest

from helpers import IsolatedDbTestCase

import main


class SchemaV2TestCase(IsolatedDbTestCase):
    """Shared v2 connection helper on an isolated database."""

    def v2(self):
        conn = sqlite3.connect(str(self.db_path))
        main.create_schema_v2(conn)
        conn.commit()  # caller owns the commit (transaction-ownership contract)
        return conn

    def rows_in_fts(self, conn, match):
        return conn.execute(
            "SELECT count(*) FROM pages_fts WHERE pages_fts MATCH ?",
            (match,),
        ).fetchone()[0]

    def fts_integrity_ok(self, conn):
        # FTS5 'integrity-check' emits a row per detected problem.
        problems = conn.execute(
            "INSERT INTO pages_fts(pages_fts) VALUES('integrity-check')"
        ).fetchall()
        return problems == []


class SchemaStructureTests(SchemaV2TestCase):
    def test_pages_is_ordinary_table_with_expected_columns(self):
        conn = self.v2()
        try:
            row = conn.execute(
                "SELECT type FROM sqlite_master WHERE name='pages'"
            ).fetchone()
            self.assertEqual(row[0], "table")

            cols = {name: (notnull, pk) for cid, name, typ, notnull, dflt, pk
                    in conn.execute("PRAGMA table_info(pages)")}
            self.assertEqual(set(cols), {"rowid", "url", "title",
                                         "description", "body"})
            self.assertEqual(cols["rowid"], (0, 1))      # INTEGER PRIMARY KEY
            self.assertEqual(cols["url"], (1, 0))        # NOT NULL, not PK
        finally:
            conn.close()

    def test_url_has_unique_index(self):
        conn = self.v2()
        try:
            indexes = conn.execute("PRAGMA index_list(pages)").fetchall()
            self.assertEqual(len(indexes), 1)
            _, name, unique, _, _ = indexes[0]
            self.assertTrue(unique)
            self.assertTrue(name.startswith("sqlite_autoindex_pages"))
        finally:
            conn.close()

    def test_pages_fts_is_external_content_virtual_table(self):
        conn = self.v2()
        try:
            # sqlite_master stores virtual tables as type='table'; the
            # authoritative source is pragma_table_list.
            row = conn.execute(
                "SELECT type FROM pragma_table_list WHERE name='pages_fts'"
            ).fetchone()
            self.assertEqual(row[0], "virtual")

            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='pages_fts'"
            ).fetchone()[0]
            self.assertTrue(ddl.startswith("CREATE VIRTUAL TABLE"))
            self.assertIn("content='pages'", ddl)
            self.assertIn("content_rowid='rowid'", ddl)
            self.assertIn("tokenize='unicode61'", ddl)
        finally:
            conn.close()

    def test_user_version_set_to_2(self):
        conn = self.v2()
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            conn.close()


class UniquenessTests(SchemaV2TestCase):
    def test_duplicate_url_raises_integrity_error(self):
        conn = self.v2()
        try:
            conn.execute(
                "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                ("http://example.com/x", "One", "body"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                    ("http://example.com/x", "Two", "other body"),
                )
        finally:
            conn.close()


class TriggerSynchronizationTests(SchemaV2TestCase):
    def test_insert_mirrors_into_fts(self):
        conn = self.v2()
        try:
            conn.execute(
                "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                ("http://example.com/apple", "Apple", "a crisp fruit"),
            )
            conn.execute(
                "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                ("http://example.com/banana", "Banana", "a yellow fruit"),
            )
            conn.commit()

            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0], 2)
            self.assertEqual(self.rows_in_fts(conn, "apple"), 1)
            self.assertEqual(self.rows_in_fts(conn, "yellow"), 1)
            self.assertTrue(self.fts_integrity_ok(conn))

            urls = [r[0] for r in conn.execute(
                "SELECT p.url FROM pages_fts f JOIN pages p"
                " ON p.rowid = f.rowid ORDER BY p.url")]
            self.assertEqual(urls, ["http://example.com/apple",
                                    "http://example.com/banana"])
        finally:
            conn.close()

    def test_update_syncs_fts_content(self):
        conn = self.v2()
        try:
            conn.execute(
                "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                ("http://example.com/p", "Old title", "apple banana"),
            )
            conn.commit()
            self.assertEqual(self.rows_in_fts(conn, "apple"), 1)

            conn.execute(
                "UPDATE pages SET body = 'cherry' WHERE url = ?",
                ("http://example.com/p",),
            )
            conn.commit()

            self.assertEqual(self.rows_in_fts(conn, "apple"), 0)
            self.assertEqual(self.rows_in_fts(conn, "cherry"), 1)
            self.assertTrue(self.fts_integrity_ok(conn))
        finally:
            conn.close()

    def test_delete_removes_from_fts(self):
        conn = self.v2()
        try:
            conn.execute(
                "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                ("http://example.com/gone", "Gone", "unique vocabulary"),
            )
            conn.commit()
            self.assertEqual(self.rows_in_fts(conn, "vocabulary"), 1)

            conn.execute("DELETE FROM pages WHERE url = ?",
                         ("http://example.com/gone",))
            conn.commit()

            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0], 0)
            self.assertEqual(self.rows_in_fts(conn, "vocabulary"), 0)
            self.assertTrue(self.fts_integrity_ok(conn))
        finally:
            conn.close()


class UrlOnlyUpdateTests(SchemaV2TestCase):
    def test_trigger_has_when_guard(self):
        conn = self.v2()
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger'"
                " AND name='pages_au'"
            ).fetchone()[0]
            self.assertIn("WHEN", sql)
            self.assertIn("old.title IS NOT new.title", sql)
        finally:
            conn.close()

    def test_url_only_update_keeps_index_intact(self):
        conn = self.v2()
        try:
            conn.execute(
                "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                ("http://example.com/old", "Alpha", "apple core"),
            )
            conn.commit()

            conn.execute(
                "UPDATE pages SET url = ? WHERE url = ?",
                ("http://example.com/new", "http://example.com/old"),
            )
            conn.commit()

            # Same row, same content, still findable through the FTS index.
            self.assertEqual(self.rows_in_fts(conn, "apple"), 1)
            url = conn.execute(
                "SELECT p.url FROM pages_fts f JOIN pages p"
                " ON p.rowid = f.rowid WHERE p.url = ?",
                ("http://example.com/new",),
            ).fetchone()[0]
            self.assertEqual(url, "http://example.com/new")
            self.assertTrue(self.fts_integrity_ok(conn))
        finally:
            conn.close()


class IdempotencyTests(SchemaV2TestCase):
    def test_create_schema_v2_is_idempotent(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            main.create_schema_v2(conn)

            # Second call must not raise nor duplicate objects.
            main.create_schema_v2(conn)

            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            triggers = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
                " AND name IN ('pages_ai','pages_ad','pages_au')"
            ).fetchone()[0]
            self.assertEqual(triggers, 3)

            virtual = conn.execute(
                "SELECT count(*) FROM pragma_table_list"
                " WHERE name='pages_fts' AND type='virtual'"
            ).fetchone()[0]
            self.assertEqual(virtual, 1)
        finally:
            conn.close()


class TransactionOwnershipTests(IsolatedDbTestCase):
    """The schema helper must never commit a caller's transaction.

    Phase 4 migration runs schema creation inside one BEGIN IMMEDIATE …
    COMMIT; if create_schema_v2 committed internally it would prematurely
    commit that outer transaction, breaking the all-or-nothing rollback
    guarantee. This regression test pins the no-commit contract.
    """

    def test_create_schema_v2_does_not_commit_outer_transaction(self):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN")
            main.create_schema_v2(conn)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

        # Reopen a fresh connection: nothing may have survived.
        reopened = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                reopened.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                reopened.execute(
                    "SELECT count(*) FROM sqlite_master"
                    " WHERE name IN ('pages', 'pages_fts')"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                reopened.execute(
                    "SELECT count(*) FROM sqlite_master"
                    " WHERE type='trigger'"
                    " AND name IN ('pages_ai', 'pages_ad', 'pages_au')"
                ).fetchone()[0],
                0,
            )
        finally:
            reopened.close()


class DirectSearchTests(SchemaV2TestCase):
    def test_search_ranking_and_snippet_via_direct_sqlite(self):
        conn = self.v2()
        try:
            docs = [
                ("http://example.com/a", "Alpha", "the quick brown fox"),
                ("http://example.com/b", "Beta", "the slow dog"),
                ("http://example.com/c", "Gamma", "fox and fox and fox"),
            ]
            for url, title, body in docs:
                conn.execute(
                    "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                    (url, title, body),
                )
            conn.commit()

            # Ranking: bm25 with the v2 three-column weights.
            ranked = conn.execute(
                "SELECT p.url FROM pages_fts f JOIN pages p"
                " ON p.rowid = f.rowid"
                " WHERE pages_fts MATCH 'fox'"
                " ORDER BY bm25(pages_fts, 5.0, 3.0, 1.0)"
            ).fetchall()
            self.assertEqual(
                [r[0] for r in ranked],
                ["http://example.com/c", "http://example.com/a"],
            )

            # snippet() over external content pulls from the content table.
            snippet = conn.execute(
                "SELECT snippet(pages_fts, 2, '[', ']', '...', 20)"
                " FROM pages_fts WHERE pages_fts MATCH 'fox'"
                " ORDER BY bm25(pages_fts, 5.0, 3.0, 1.0) LIMIT 1"
            ).fetchone()[0]
            self.assertIn("fox", snippet)
            self.assertTrue(self.fts_integrity_ok(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
