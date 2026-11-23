"""Stage 17: statistics stay honest when database writes fail.

When an INSERT raises, the crawler rolls back the whole open batch
(the uncommitted pages from earlier in the same batch are undone too).
Those rolled-back pages were already counted in stats["indexed"], so
the count must be reduced accordingly - otherwise the summary
overstates how many pages were written. Same for a failing final
commit flush.
"""
import sqlite3
import unittest
from unittest import mock

import main
from helpers import ServerTestCase, page


def _real_db_path(tmp_path):
    return tmp_path / "real.db"


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


class FailingCommitConn:
    """Wraps a real connection; every commit() raises."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        raise sqlite3.OperationalError("disk full")

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


def _page_count(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    finally:
        conn.close()


class DbFailureStatsTests(ServerTestCase):
    """Six pages: seed + p1..p5. All are inserted before the failure, all
    uncommitted if the batch threshold is kept large, so a rollback must
    un-count every one of them."""

    def _server_with_six_pages(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body",
                            [f"/p{i}" for i in range(1, 6)]))
        for i in range(1, 6):
            s.add(f"/p{i}", page(f"P{i}", f"page {i}"))
        return s

    def test_rollback_un_counts_pending_pages(self):
        s = self._server_with_six_pages()
        db_path = _real_db_path(self.tmp_path)
        real = main.get_database(db_path)
        # Seed + p1..p4 insert fine (5 pending); p5's INSERT fails.
        proxy = FailingAfterNInsertConn(real, fail_at=6)

        with mock.patch.object(main, "open_database", return_value=proxy), \
             mock.patch.object(main, "DB_COMMIT_BATCH", 100):
            stats = self.crawl(s.url("/seed"), max_pages=20,
                               delay=0.02, threads=1)

        # The rollback undid all 5 uncommitted inserts.
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(_page_count(db_path), 0)
        self.assertGreaterEqual(stats["db_errors"], 1)
        self.assertGreaterEqual(stats["errors"], 1)
        self.assertIn("Database error", self.stdout)

    def test_final_commit_failure_un_counts_pending_pages(self):
        s = self._server_with_six_pages()
        db_path = _real_db_path(self.tmp_path)
        real = main.get_database(db_path)
        proxy = FailingCommitConn(real)

        with mock.patch.object(main, "open_database", return_value=proxy), \
             mock.patch.object(main, "DB_COMMIT_BATCH", 100):
            stats = self.crawl(s.url("/seed"), max_pages=20,
                               delay=0.02, threads=1)

        # Nothing was ever committed, so nothing is actually stored.
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(_page_count(db_path), 0)
        # Only the final flush commit fails (the crawl-start dedup pass
        # made no changes on an empty database, so it never commits).
        self.assertEqual(stats["db_errors"], 1)
        self.assertIn("Final database commit failed", self.stdout)

    def test_committed_pages_survive_a_later_rollback(self):
        s = self._server_with_six_pages()
        db_path = _real_db_path(self.tmp_path)
        real = main.get_database(db_path)
        # Fail on the 3rd INSERT: seed + p1 are pending (batch=2 commits
        # them), p2 joins the new batch, p3 fails -> rollback undoes
        # only p2.
        proxy = FailingAfterNInsertConn(real, fail_at=3)

        with mock.patch.object(main, "open_database", return_value=proxy), \
             mock.patch.object(main, "DB_COMMIT_BATCH", 2):
            stats = self.crawl(s.url("/seed"), max_pages=20,
                               delay=0.02, threads=1)

        # seed + p1 committed (2); p2 rolled back after p3 failed.
        self.assertEqual(stats["indexed"], 2)
        self.assertEqual(_page_count(db_path), 2)
        self.assertGreaterEqual(stats["db_errors"], 1)


if __name__ == "__main__":
    unittest.main()
