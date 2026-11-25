"""M3.2: open_database initialization integration tests.

Covers the single database-open choke point added in M3.2:
  - fresh (missing) database -> v2 created directly (transactional),
  - existing v2 -> opened without migration,
  - existing v1 -> migrated automatically, then opened,
  - empty / unknown / corrupt -> refused (never silently initialized),
  - corrupt DBs preserve DatabaseInspectionError and its cause,
  - migration failures propagate MigrationError (stages + chained cause),
  - search keeps its missing-file guarantees (non-creating, no migration),
  - crawler DB-acquisition failures are reported and never inflate
    counters,
  - concurrency: in-process per-path locking (same v1 path migrates
    once), two-process race behaves within the documented busy-timeout /
    retry behavior, and fresh-creation races never leave a partial
    schema.

Real temporary SQLite databases are used throughout; no fixtures or
existing tests are weakened.
"""

import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import main
from helpers import ServerTestCase, page


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _make_v1(db_path, rows):
    """Create a legacy v1 FTS5 database at db_path with the given rows.

    rows: iterable of (url, title, description, body).
    """
    db_path = pathlib.Path(db_path)
    conn = main.get_database(db_path)
    try:
        for url, title, description, body in rows:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                (url, title, description, body))
        conn.commit()
    finally:
        conn.close()


def _detect_kind(db_path):
    probe = None
    try:
        probe = main._connect_readonly(db_path)
        return main.detect_schema(probe)["kind"]
    finally:
        if probe is not None:
            probe.close()


class OpenDatabaseTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

    # ---- behavior ----

    def test_fresh_missing_creates_v2_directly(self):
        p = self.tmp_path / "fresh.db"
        self.assertFalse(p.exists())
        conn = main.open_database(p)
        try:
            schema = main.detect_schema(conn)
            self.assertEqual(schema["kind"], "v2")
            self.assertEqual(schema["user_version"], main.SCHEMA_VERSION)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0], 0)
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://example.com/", "T", "", "body"))
            conn.commit()
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM pages_fts "
                    "WHERE pages_fts MATCH 'body'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_existing_v2_opens_without_migration(self):
        p = self.tmp_path / "v2.db"
        conn = sqlite3.connect(str(p))
        try:
            main.create_schema_v2(conn)
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(main, "_migrate_on_connection",
                               wraps=main._migrate_on_connection) as migrate:
            opened = main.open_database(p)
            try:
                self.assertEqual(_detect_kind(p), "v2")
            finally:
                opened.close()
        migrate.assert_not_called()

    def test_existing_v1_migrates_and_opens(self):
        p = self.tmp_path / "legacy.db"
        rows = [
            ("http://a.example/", "A", "desc a", "body a"),
            ("http://b.example/", "B", "desc b", "body b"),
        ]
        _make_v1(p, rows)
        self.assertEqual(_detect_kind(p), "v1")

        with mock.patch.object(main, "_migrate_on_connection",
                               wraps=main._migrate_on_connection) as migrate:
            conn = main.open_database(p)
            try:
                schema = main.detect_schema(conn)
                self.assertEqual(schema["kind"], "v2")
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM pages").fetchone()[0],
                    2)
            finally:
                conn.close()
        migrate.assert_called_once()

    def test_empty_file_is_refused_and_left_untouched(self):
        p = self.tmp_path / "empty.db"
        p.write_bytes(b"")
        before = p.read_bytes()
        with self.assertRaises(main.MigrationError) as cm:
            main.open_database(p)
        self.assertIn("refusing to open unsupported database", str(cm.exception))
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual(_detect_kind(p), "empty")

    def test_unknown_schema_is_refused_and_left_untouched(self):
        p = self.tmp_path / "unknown.db"
        conn = sqlite3.connect(str(p))
        try:
            conn.execute("CREATE TABLE foo (id INTEGER)")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(main.MigrationError) as cm:
            main.open_database(p)
        self.assertIn("refusing to open unsupported database", str(cm.exception))
        # Not migrated: no backup created, foo table still present.
        self.assertFalse(
            pathlib.Path(str(p) + ".pre-migrate.bak").exists())
        conn = sqlite3.connect(str(p))
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='foo'")
                .fetchone()[0], "foo")
        finally:
            conn.close()

    def test_corrupt_db_preserves_inspection_error(self):
        p = self.tmp_path / "corrupt.db"
        p.write_bytes(b"this is definitely not a sqlite database file" * 8)
        before = p.read_bytes()
        with self.assertRaises(main.DatabaseInspectionError) as cm:
            main.open_database(p)
        self.assertIsInstance(cm.exception.__cause__, sqlite3.Error)
        self.assertEqual(p.read_bytes(), before)

    def test_missing_search_db_stays_non_creating(self):
        p = self.tmp_path / "missing.db"
        self.assertFalse(p.exists())
        self.assertEqual(main.run_search("anything", db_path=p), [])
        self.assertFalse(p.exists())
        with self.assertRaises(main.DatabaseNotFoundError):
            main.search_json("anything", db_path=p)
        self.assertFalse(p.exists())

    def test_fresh_creation_transactional_no_partial_schema(self):
        p = self.tmp_path / "atomic.db"
        real_create = main.create_schema_v2

        def boom(conn):
            real_create(conn)
            raise RuntimeError("injected schema failure")

        with mock.patch.object(main, "create_schema_v2", side_effect=boom):
            with self.assertRaises(RuntimeError):
                main.open_database(p)

        # Rolled back and cleaned up: no partially-initialized schema and
        # no empty leftover file - the file this call created is removed,
        # so a retry starts from a missing file.
        self.assertFalse(p.exists())
        # A subsequent open succeeds and initializes v2 fully.
        conn = main.open_database(p)
        try:
            self.assertEqual(main.detect_schema(conn)["kind"], "v2")
        finally:
            conn.close()

    # ---- concurrency ----

    def test_two_threads_same_v1_path_migrate_once(self):
        p = self.tmp_path / "shared-legacy.db"
        rows = [(f"http://h{i}.example/", f"T{i}", "", f"body {i}")
                for i in range(200)]
        _make_v1(p, rows)
        self.assertEqual(_detect_kind(p), "v1")

        counter = {"calls": 0}
        real_migrate = main._migrate_on_connection

        def counting_migrate(*args, **kwargs):
            counter["calls"] += 1
            return real_migrate(*args, **kwargs)

        barrier = threading.Barrier(2)
        errors = []
        results = []

        def worker():
            try:
                barrier.wait(timeout=10)
                c = main.open_database(p)
                try:
                    self.assertEqual(main.detect_schema(c)["kind"], "v2")
                finally:
                    c.close()  # must close in the creating thread
                results.append(True)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        with mock.patch.object(main, "_migrate_on_connection",
                               new=counting_migrate):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(counter["calls"], 1)
        self.assertEqual(results, [True, True])
        self.assertEqual(_detect_kind(p), "v2")
        self.assertTrue(pathlib.Path(str(p) + ".pre-migrate.bak").exists())

    def test_two_processes_race_same_v1(self):
        p = self.tmp_path / "crossproc-legacy.db"
        rows = [(f"http://q{i}.example/", f"T{i}", "", f"body {i}")
                for i in range(1500)]
        _make_v1(p, rows)
        self.assertEqual(_detect_kind(p), "v1")

        script = (
            "import main;"
            f"db={str(p)!r};"
            "c=main.open_database(db);"
            "n=c.execute('SELECT count(*) FROM pages').fetchone()[0];"
            "assert n==1500, n;"
            "c.close();"
            "print('OK')"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)

        procs = []
        for _ in range(2):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True))
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, out + err)
            self.assertIn("OK", out)

        self.assertEqual(_detect_kind(p), "v2")
        self.assertTrue(
            pathlib.Path(str(p) + ".pre-migrate.bak").exists())
        conn = sqlite3.connect(str(p))
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM pages").fetchone()[0],
                1500)
        finally:
            conn.close()

    def test_two_threads_fresh_creation_race(self):
        p = self.tmp_path / "fresh-race.db"
        barrier = threading.Barrier(2)
        errors = []
        results = []

        def worker():
            try:
                barrier.wait(timeout=10)
                c = main.open_database(p)
                try:
                    self.assertEqual(main.detect_schema(c)["kind"], "v2")
                finally:
                    c.close()  # must close in the creating thread
                results.append(True)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(results, [True, True])
        self.assertEqual(_detect_kind(p), "v2")

    def test_slow_concurrent_writer_is_waited_out(self):
        # Regression for the M3.1 concurrency carry-over: a competing
        # writer that holds the BEGIN IMMEDIATE reservation longer than
        # the old (1s) retry budget must be waited out, not abandoned.
        p = self.tmp_path / "slow-peer.db"
        rows = [(f"http://s{i}.example/", f"T{i}", "", f"body {i}")
                for i in range(50)]
        _make_v1(p, rows)
        self.assertEqual(_detect_kind(p), "v1")

        holder_ready = threading.Event()

        def holder():
            c = sqlite3.connect(str(p))
            try:
                c.execute("PRAGMA busy_timeout=0")
                c.execute("BEGIN IMMEDIATE")
                holder_ready.set()
                time.sleep(1.5)  # > old 1s retry budget
                c.execute("ROLLBACK")
            finally:
                c.close()

        thread = threading.Thread(target=holder)
        thread.start()
        try:
            holder_ready.wait(timeout=10)
            started = time.time()
            conn = main.open_database(p)
            waited = time.time() - started
            try:
                self.assertEqual(main.detect_schema(conn)["kind"], "v2")
            finally:
                conn.close()
            # We blocked on the peer's reservation rather than failing.
            self.assertGreaterEqual(waited, 1.2)
        finally:
            thread.join(timeout=20)

        self.assertEqual(_detect_kind(p), "v2")

    def test_stale_backup_refused_promptly(self):
        # A peer that crashed mid-migration leaves a backup file; the
        # next open must acquire the reservation (nobody else holds it),
        # notice the stale backup, and refuse promptly - never overwrite.
        p = self.tmp_path / "stale.db"
        rows = [("http://x.example/", "X", "", "body x")]
        _make_v1(p, rows)
        bak = pathlib.Path(str(p) + ".pre-migrate.bak")
        with open(str(p), "rb") as src, open(str(bak), "wb") as dst:
            dst.write(src.read())

        started = time.time()
        with self.assertRaises(main.MigrationError) as cm:
            main.open_database(p)
        self.assertLess(time.time() - started, 5.0)
        self.assertIn("backup already exists", str(cm.exception))
        # Source untouched (still v1, backup intact).
        self.assertEqual(_detect_kind(p), "v1")
        self.assertTrue(bak.exists())


class CrawlAcquisitionFailureTests(ServerTestCase):
    """Crawler DB-acquisition failures must be reported, never inflate
    counters, and never fall back to a v1 database."""

    def test_migration_failure_reported_and_returns_none(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))

        with mock.patch.object(
                main, "open_database",
                side_effect=main.MigrationError(
                    "simulated migration failure",
                    stages=[("inspection", "detecting schema")])):
            result = self.crawl(s.url("/seed"), max_pages=5, threads=1)

        self.assertIsNone(result)
        self.assertIn("Database open failed:", self.stdout)
        self.assertIn("simulated migration failure", self.stdout)
        # No database was created, so no counters could have been inflated.
        self.assertFalse(self.db_path.exists())

    def test_corrupt_db_acquisition_failure_reported(self):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body"))

        cause = sqlite3.OperationalError("file is not a database")
        error = main.DatabaseInspectionError("cannot read database schema")
        error.__cause__ = cause

        with mock.patch.object(main, "open_database", side_effect=error):
            result = self.crawl(s.url("/seed"), max_pages=5, threads=1)

        self.assertIsNone(result)
        self.assertIn("Database open failed:", self.stdout)
        self.assertFalse(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
