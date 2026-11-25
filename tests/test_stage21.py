"""Stage 21 (Phase 2): crawler write path on the v2 schema.

The crawler now persists pages with a single UPSERT
(INSERT ... ON CONFLICT(url) DO UPDATE SET ...) instead of DELETE + INSERT,
against the ordinary `pages` table; the pages_fts triggers keep the
external-content FTS5 search index in sync. These tests exercise that
write path end-to-end through main.crawl() on v2 databases (ServerTestCase
provisions v2 in helpers). The search implementation itself is unchanged
here (Phase 3); FTS correctness is asserted via direct SQLite.
"""
import sqlite3
import unittest
from unittest import mock

import main
from helpers import ServerTestCase, page, provision_v2


class FailingAfterNInsertConn:
    """Wraps a real connection; raises on the Nth INSERT INTO pages."""

    def __init__(self, conn, fail_at):
        self._conn = conn
        self._fail_at = fail_at
        self._inserts = 0

    def execute(self, *args, **kwargs):
        sql = args[0] if args else kwargs.get("sql", "")
        if isinstance(sql, str) and sql.lstrip().upper().startswith("INSERT"):
            self._inserts += 1
            if self._inserts >= self._fail_at:
                raise sqlite3.OperationalError("disk full")
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


class CommitCountingConn:
    """Wraps a real connection; counts commit() calls."""

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        self.commits += 1
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


class CrawlUpsertTests(ServerTestCase):
    """Ran against the v2 schema (provisioned by helpers.ServerTestCase)."""

    def _add_robots(self):
        self.server.add_robots("User-agent: *\nAllow: /\n")

    def _conn(self, db_path=None):
        return sqlite3.connect(str(db_path or self.db_path))

    def fts_count(self, term, db_path=None):
        conn = self._conn(db_path)
        try:
            return conn.execute(
                "SELECT count(*) FROM pages_fts WHERE pages_fts MATCH ?",
                (term,),
            ).fetchone()[0]
        finally:
            conn.close()

    def fts_integrity_ok(self, db_path=None):
        conn = self._conn(db_path)
        try:
            problems = conn.execute(
                "INSERT INTO pages_fts(pages_fts) VALUES('integrity-check')"
            ).fetchall()
            return problems == []
        finally:
            conn.close()

    def test_crawl_inserts_new_pages_into_v2_schema(self):
        self._add_robots()
        s = self.server
        s.add("/seed", page("Seed", "seed body", links=("/one", "/two")))
        s.add("/one", page("One", "pear tree"))
        s.add("/two", page("Two", "cherry blossom"))

        stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                           threads=1)

        self.assertEqual(stats["indexed"], 3)
        self.assertEqual(sorted(self.indexed_paths()),
                         ["/one", "/seed", "/two"])

        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0], 3)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0], 3)
            self.assertEqual(self.fts_count("pear"), 1)
            self.assertEqual(self.fts_count("cherry"), 1)
        finally:
            conn.close()
        self.assertTrue(self.fts_integrity_ok())

    def test_recrawl_updates_content_in_place_without_duplicates(self):
        self._add_robots()
        s = self.server
        s.add("/seed", page("Seed", "seed body", links=("/p",)))
        s.add("/p", page("P", "apple pie"))

        self.crawl(s.url("/seed"), max_pages=5, delay=0.02, threads=1)
        self.assertEqual(self.fts_count("apple"), 1)

        conn = sqlite3.connect(str(self.db_path))
        try:
            rowid_before = conn.execute(
                "SELECT rowid FROM pages WHERE url = ?",
                (s.url("/p"),),
            ).fetchone()[0]
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0], 2)
        finally:
            conn.close()

        # Re-serve the same URL with new content and crawl again.
        s.add("/p", page("P", "banana split"))
        stats = self.crawl(s.url("/seed"), max_pages=5, delay=0.02,
                           threads=1)

        self.assertEqual(stats["indexed"], 2)
        conn = sqlite3.connect(str(self.db_path))
        try:
            # Upsert: same row updated in place, no duplicate row.
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0], 2)
            rowid_after = conn.execute(
                "SELECT rowid FROM pages WHERE url = ?",
                (s.url("/p"),),
            ).fetchone()[0]
            self.assertEqual(rowid_after, rowid_before)
        finally:
            conn.close()

        # Old terms disappear, new terms become searchable.
        self.assertEqual(self.fts_count("apple"), 0)
        self.assertEqual(self.fts_count("banana"), 1)
        self.assertTrue(self.fts_integrity_ok())

    def test_url_uniqueness_enforced(self):
        self._add_robots()
        s = self.server
        s.add("/seed", page("Seed", "seed body", links=("/p",)))
        s.add("/p", page("P", "content"))

        self.crawl(s.url("/seed"), max_pages=5, delay=0.02, threads=1)
        self.crawl(s.url("/seed"), max_pages=5, delay=0.02, threads=1)

        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM pages WHERE url = ?",
                    (s.url("/p"),),
                ).fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pages (url, title, body) VALUES (?, ?, ?)",
                    (s.url("/p"), "dup", "dup body"),
                )
        finally:
            conn.close()
        self.assertTrue(self.fts_integrity_ok())

    def test_failed_writes_do_not_inflate_indexed_stats(self):
        self._add_robots()
        s = self.server
        s.add("/seed", page("Seed", "seed body", links=("/p1", "/p2")))
        s.add("/p1", page("P1", "one"))
        s.add("/p2", page("P2", "two"))

        db_path = self.tmp_path / "real.db"
        provision_v2(db_path)
        real = main.get_database(db_path)
        # First INSERT fails: nothing is successfully written, nothing
        # should be counted as indexed.
        proxy = FailingAfterNInsertConn(real, fail_at=1)

        with mock.patch.object(main, "open_database", return_value=proxy), \
             mock.patch.object(main, "DB_COMMIT_BATCH", 100):
            stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                               threads=1, db_path=db_path)

        self.assertEqual(stats["indexed"], 0)
        self.assertGreaterEqual(stats["db_errors"], 1)
        self.assertGreaterEqual(stats["errors"], 1)
        conn = sqlite3.connect(str(db_path))
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0], 0)
        finally:
            conn.close()

    def test_rollback_un_counts_only_pending_after_committed_batch(self):
        self._add_robots()
        s = self.server
        s.add("/seed", page("Seed", "seed body",
                            links=("/p1", "/p2", "/p3")))
        s.add("/p1", page("P1", "one"))
        s.add("/p2", page("P2", "two"))
        s.add("/p3", page("P3", "three"))

        db_path = self.tmp_path / "real.db"
        provision_v2(db_path)
        real = main.get_database(db_path)
        # Batch=2: seed + p1 commit; p2 joins the open batch; p3's INSERT
        # fails -> rollback undoes only p2. indexed must equal committed
        # rows (2), not the 3 attempted.
        proxy = FailingAfterNInsertConn(real, fail_at=3)

        with mock.patch.object(main, "open_database", return_value=proxy), \
             mock.patch.object(main, "DB_COMMIT_BATCH", 2):
            stats = self.crawl(s.url("/seed"), max_pages=10, delay=0.02,
                               threads=1, db_path=db_path)

        self.assertEqual(stats["indexed"], 2)
        self.assertGreaterEqual(stats["db_errors"], 1)
        conn = sqlite3.connect(str(db_path))
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0], 2)
        finally:
            conn.close()
        self.assertTrue(self.fts_integrity_ok(db_path))

    def test_batch_commit_behavior_unchanged(self):
        self._add_robots()
        s = self.server
        s.add("/seed", page("Seed", "seed body",
                            [f"/p{i}" for i in range(9)]))
        for i in range(9):
            s.add(f"/p{i}", page(f"P{i}", f"page {i}"))

        real = main.get_database()
        proxy = CommitCountingConn(real)

        with mock.patch.object(main, "DB_COMMIT_BATCH", 3), \
             mock.patch.object(main, "open_database", return_value=proxy):
            stats = self.crawl(s.url("/seed"), max_pages=20, delay=0.02,
                               threads=1)

        # 10 indexed pages, one commit per 3 pages plus the final
        # flush of the open batch: 4 commits instead of 10.
        self.assertEqual(proxy.commits, 4)
        self.assertEqual(stats["indexed"], 10)
        self.assertEqual(len(self.indexed_paths()), 10)


if __name__ == "__main__":
    unittest.main()
