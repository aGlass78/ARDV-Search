"""Phase 4 M2.1: transactional v1 -> v2 migration (migrate_database).

Covers: schema refusal policy (already-v2 noop, missing/empty/unknown/
unreadable refuse), mandatory SQLite-backup-API backup + verification,
existing-backup abort, deterministic deduplication priority rules, rowid
preservation, single-transaction conversion with full rollback on
failure, user_version stamping only on success, chunked copying, progress
stage reporting, and the production search working against the migrated
database.
"""
import hashlib
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import main  # noqa: E402


def _sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


DIRTY_ROWS = [
    # (url, title, description, body)
    ("http://example.com/a", "A", "", "a body"),
    ("http://example.com/a", "A2", "", "a body again"),   # exact duplicate
    ("http://example.com/b", "B", "", "b body"),
    ("http://example.com/b/", "BT", "", "b trailing slash"),  # canonical w/ above
    ("http://example.com/c", "C", "", "c body"),
    ("not a url", "Bad", "", ""),                          # invalid
    ("http://[::1", "IPv6", "", ""),                       # invalid (unmatched bracket)
    ("", "Empty URL", "", ""),                             # invalid
    ("http://EXAMPLE.com:80/d", "DE", "", "d body"),       # canonical w/ below
    ("http://example.com/d", "D", "", "d body"),
]


class Stage24TestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = pathlib.Path(tmp.name)

    def _make_v1(self, rows):
        """Build a genuine legacy v1 FTS5 database via production init."""
        db = self.tmp_path / "v1.db"
        conn = main.get_database(db)
        try:
            conn.executemany(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return db

    def _make_v2(self):
        db = self.tmp_path / "v2.db"
        conn = sqlite3.connect(str(db))
        try:
            main.create_schema_v2(conn)
            conn.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://x/a", "A", "", "a body"),
            )
            conn.commit()
        finally:
            conn.close()
        return db

    def _page_rows(self, db):
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(
                "SELECT rowid, url, title, description, body "
                "FROM pages ORDER BY rowid").fetchall()
        finally:
            conn.close()

    def _detect_kind(self, db):
        conn = sqlite3.connect(str(db))
        try:
            return main.detect_schema(conn)["kind"]
        finally:
            conn.close()

    def _assert_v1_intact(self, db, rows):
        """The v1 database and its verified backup hold exactly `rows`."""
        self.assertEqual(self._page_rows(db), rows)
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(main.detect_schema(conn)["kind"], "v1")
            self.assertEqual(conn.execute(
                "PRAGMA user_version").fetchone()[0], 0)
        finally:
            conn.close()
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        self.assertTrue(bak.exists())
        self.assertEqual(self._page_rows(bak), rows)


class MigrationHappyPathTests(Stage24TestCase):
    def test_migrates_v1_to_v2_end_to_end(self):
        rows = [
            ("http://example.com/a", "Alpha", "", "alpha body"),
            ("http://example.com/b", "Beta", "", "beta body"),
        ]
        db = self._make_v1(rows)
        conn = sqlite3.connect(str(db))
        try:
            schema = main.detect_schema(conn)
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v1")

        report = main.migrate_database(db)

        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["stats"]["original_rows"], 2)
        self.assertEqual(report["stats"]["retained_rows"], 2)
        self.assertEqual(report["stats"]["removed_duplicates"], 0)
        # user_version stamped only as part of successful migration
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(conn.execute(
                "PRAGMA user_version").fetchone()[0], 2)
            schema = main.detect_schema(conn)
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v2")
        self.assertEqual(self._page_rows(db), [
            (1, "http://example.com/a", "Alpha", "", "alpha body"),
            (2, "http://example.com/b", "Beta", "", "beta body"),
        ])

    def test_migrated_database_is_searchable(self):
        db = self._make_v1([
            ("http://example.com/cat", "Cats", "", "cats run around"),
        ])
        main.migrate_database(db)
        result = main.search_json("cats", db_path=db)
        self.assertEqual(result["total_results"], 1)
        self.assertEqual(result["results"][0]["url"],
                         "http://example.com/cat")

    def test_chunked_copy_many_rows(self):
        rows = [
            (f"http://example.com/p{i}", f"Title {i}", "",
             f"body text number {i}")
            for i in range(450)
        ]
        db = self._make_v1(rows)
        report = main.migrate_database(db)
        self.assertEqual(report["stats"]["retained_rows"], 450)
        self.assertEqual(len(self._page_rows(db)), 450)
        conn = sqlite3.connect(str(db))
        try:
            fts = conn.execute(
                "SELECT count(*) FROM pages_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(fts, 450)

    def test_progress_stages_reported_in_order(self):
        db = self._make_v1([
            ("http://example.com/a", "A", "", "a body"),
        ])
        seen = []
        main.migrate_database(db, progress_callback=lambda s, m: seen.append((s, m)))
        names = [s for s, _ in seen]
        self.assertEqual(names[0], "inspection")
        for expected in ("backup", "copying", "fts_population",
                         "validation", "completed"):
            self.assertIn(expected, names)
        self.assertEqual(names[-1], "completed")

    def test_never_uses_provision_v2(self):
        import inspect
        source = inspect.getsource(main.migrate_database)
        self.assertNotIn("provision_v2", source)


class AlreadyV2Tests(Stage24TestCase):
    def test_already_v2_returns_noop_and_leaves_backup_absent(self):
        db = self._make_v2()
        before = _sha256(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        report = main.migrate_database(db)
        self.assertEqual(report["status"], "noop")
        self.assertEqual(_sha256(db), before)
        self.assertFalse(bak.exists())


class RefusalTests(Stage24TestCase):
    def test_refuses_missing_database(self):
        db = self.tmp_path / "missing.db"
        with self.assertRaises(main.MigrationError):
            main.migrate_database(db)
        self.assertFalse(db.exists())

    def test_refuses_empty_database(self):
        db = self.tmp_path / "empty.db"
        db.write_bytes(b"")
        with self.assertRaises(main.MigrationError):
            main.migrate_database(db)

    def test_refuses_unknown_database(self):
        db = self.tmp_path / "unknown.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE foo (x TEXT)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(main.MigrationError):
            main.migrate_database(db)

    def test_refuses_corrupt_database_and_preserves_chain(self):
        db = self.tmp_path / "corrupt.db"
        db.write_bytes(b"\x00\x01\x02 not a sqlite database")
        with self.assertRaises(main.MigrationError) as ctx:
            main.migrate_database(db)
        self.assertIsInstance(ctx.exception.__cause__,
                             main.DatabaseInspectionError)


class BackupTests(Stage24TestCase):
    def test_backup_created_and_verified(self):
        rows = [
            ("http://example.com/a", "Alpha", "", "alpha body"),
            ("http://example.com/b", "Beta", "", "beta body"),
        ]
        db = self._make_v1(rows)
        main.migrate_database(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        self.assertTrue(bak.exists())
        conn = sqlite3.connect(str(bak))
        try:
            schema = main.detect_schema(conn)
            count = conn.execute(
                "SELECT count(*) FROM pages").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v1")
        self.assertEqual(count, 2)

    def test_existing_backup_aborts_without_touching_database(self):
        rows = [
            ("http://example.com/a", "Alpha", "", "alpha body"),
        ]
        db = self._make_v1(rows)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        bak.write_text("sentinel")
        before = _sha256(db)
        with self.assertRaises(main.MigrationError):
            main.migrate_database(db)
        self.assertEqual(_sha256(db), before)
        self.assertEqual(bak.read_text(), "sentinel")  # untouched


class DedupPriorityTests(Stage24TestCase):
    ROWS = [
        # canonical http://x/a: row 1 raw==canonical vs row 2 richer content
        (1, "http://x/a", "", "", "short"),
        (2, "http://x/a/", "T", "B", "loooong"),
        # canonical http://y/b: both raws != canonical; row 4 nonempty title+body
        (3, "http://y:80/b/", "", "", ""),
        (4, "http://y:80/b", "T", "B", "x"),
        # canonical http://z/c: both raws != canonical; row 5 longer body
        (5, "http://z:80/c/", "", "", "aaaaaa"),
        (6, "http://z:80/c", "", "", "bb"),
        # canonical http://w/d: both raws != canonical; tie -> lowest rowid
        (7, "http://w:80/d/", "", "", "same"),
        (8, "http://w:80/d", "", "", "same"),
        # invalid unique retained
        (9, "not a url", "N", "", ""),
        # invalid duplicate raw -> content quality picks row 11 (title+body)
        (10, "bad", "", "", ""),
        (11, "bad", "S", "", "body"),
    ]

    def test_deduplication_priority_rules(self):
        db = self._make_v1([
            (url, title, desc, body)
            for _rowid, url, title, desc, body in self.ROWS
        ])
        report = main.migrate_database(db)
        stats = report["stats"]
        self.assertEqual(stats["original_rows"], 11)
        self.assertEqual(stats["retained_rows"], 6)
        self.assertEqual(stats["removed_duplicates"], 5)
        self.assertEqual(stats["canonical_conflict_groups"], 4)
        self.assertEqual(stats["invalid_encountered"], 3)
        self.assertEqual(stats["invalid_retained"], 2)

        pages = self._page_rows(db)
        # Original rowids preserved; exactly the chosen winners remain.
        self.assertEqual([p[0] for p in pages], [1, 4, 5, 7, 9, 11])
        retained_urls = {p[1] for p in pages}
        self.assertEqual(retained_urls, {
            "http://x/a",        # raw==canonical beats content
            "http://y:80/b",     # nonempty title+body
            "http://z:80/c/",    # longer body
            "http://w:80/d/",    # lowest rowid tie-break
            "not a url",         # invalid unique retained
            "bad",               # invalid duplicate resolved to row 11
        })

    def test_dropped_content_not_searchable(self):
        db = self._make_v1([
            (url, title, desc, body)
            for _rowid, url, title, desc, body in self.ROWS
        ])
        main.migrate_database(db)
        # dropped richer sibling (row 2) is gone
        self.assertEqual(
            main.search_json("loooong", db_path=db)["total_results"], 0)
        # retained invalid row still present/searchable
        self.assertGreaterEqual(
            main.search_json("S", db_path=db)["total_results"], 1)


class RollbackTests(Stage24TestCase):
    def test_failure_rolls_back_schema_and_data(self):
        rows = [
            ("http://example.com/a", "Alpha", "", "alpha body"),
            ("http://example.com/b", "Beta", "", "beta body"),
        ]
        db = self._make_v1(rows)
        before = _sha256(db)

        with mock.patch.object(
            main, "create_schema_v2", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(main.MigrationError) as ctx:
                main.migrate_database(db)
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

        # Original v1 database restored exactly (schema + data + version).
        self.assertEqual(_sha256(db), before)
        conn = sqlite3.connect(str(db))
        try:
            schema = main.detect_schema(conn)
            self.assertEqual(conn.execute(
                "PRAGMA user_version").fetchone()[0], 0)
            count = conn.execute(
                "SELECT count(*) FROM pages").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v1")
        self.assertEqual(count, 2)
        # Backup file from the pre-failure step still exists (verified).
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        self.assertTrue(bak.exists())

    def test_validation_failure_rolls_back(self):
        rows = [("http://example.com/a", "A", "", "a body")]
        db = self._make_v1(rows)
        before = _sha256(db)

        with mock.patch.object(
            main, "_validate_migration", return_value=["simulated failure"]
        ):
            with self.assertRaises(main.MigrationError):
                main.migrate_database(db)

        self.assertEqual(_sha256(db), before)
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(main.detect_schema(conn)["kind"], "v1")
        finally:
            conn.close()


class OrderingRegressionTests(Stage24TestCase):
    """M2.1.2: write-reservation-first ordering + pre-commit-only validation."""

    def test_concurrent_writer_blocked_while_reservation_held(self):
        rows = [
            (f"http://example.com/p{i}", f"T{i}", "", f"body{i}")
            for i in range(100)
        ]
        db = self._make_v1(rows)
        orig_valid = main._validate_migration
        entered = threading.Event()

        def slow_valid(conn, expected):
            entered.set()
            time.sleep(0.4)
            return orig_valid(conn, expected)

        result = {}

        def runner():
            result["rep"] = main.migrate_database(db)

        with mock.patch.object(
            main, "_validate_migration", side_effect=slow_valid
        ):
            t = threading.Thread(target=runner)
            t.start()
            self.assertTrue(entered.wait(timeout=5),
                            "migration did not reach validation")
            writer = sqlite3.connect(str(db))
            writer.execute("PRAGMA busy_timeout=0")
            try:
                with self.assertRaises(sqlite3.OperationalError) as ctx:
                    writer.execute(
                        "INSERT INTO pages (url, title, description, body) "
                        "VALUES (?, ?, ?, ?)",
                        ("http://x/intruder", "I", "", "x"),
                    )
                self.assertIn("locked", str(ctx.exception).lower())
            finally:
                writer.close()
            t.join(timeout=10)
        self.assertEqual(result["rep"]["status"], "completed")
        self.assertEqual(result["rep"]["stats"]["retained_rows"], 100)

    def test_backup_and_migrated_share_pre_migration_state(self):
        db = self._make_v1(DIRTY_ROWS)
        src = sqlite3.connect(str(db))
        try:
            before_rows = src.execute(
                "SELECT rowid, url FROM pages ORDER BY rowid").fetchall()
        finally:
            src.close()
        rep = main.migrate_database(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        bak_conn = sqlite3.connect(str(bak))
        try:
            bak_rows = bak_conn.execute(
                "SELECT rowid, url FROM pages ORDER BY rowid").fetchall()
        finally:
            bak_conn.close()
        # Backup == exact pre-migration source content.
        self.assertEqual(bak_rows, before_rows)
        # Migrated rows are a subset of that same source state.
        migrated = self._page_rows(db)
        self.assertEqual(len(migrated), rep["stats"]["retained_rows"])
        migrated_set = {(r[0], r[1]) for r in migrated}
        self.assertTrue(migrated_set <= set(before_rows))

    def test_backup_failure_before_ddl_leaves_source_unchanged(self):
        rows = [
            ("http://example.com/a", "A", "", "a body"),
            ("http://example.com/b", "B", "", "b body"),
        ]
        db = self._make_v1(rows)
        before = _sha256(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        bak.mkdir()  # collide with a directory -> backup creation fails
        try:
            with self.assertRaises(main.MigrationError):
                main.migrate_database(db)
        finally:
            bak.rmdir()
        self.assertEqual(_sha256(db), before)
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(main.detect_schema(conn)["kind"], "v1")
            count = conn.execute(
                "SELECT count(*) FROM pages").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 2)

    def test_trigger_validation_failure_rolls_back_before_commit(self):
        rows = [
            ("http://example.com/a", "A", "", "a body"),
            ("http://example.com/b", "B", "", "b body"),
        ]
        db = self._make_v1(rows)
        before = _sha256(db)
        orig_create = main.create_schema_v2

        def create_then_drop_trigger(conn):
            orig_create(conn)
            conn.execute("DROP TRIGGER IF EXISTS pages_ai")

        with mock.patch.object(
            main, "create_schema_v2",
            side_effect=create_then_drop_trigger,
        ):
            with self.assertRaises(main.MigrationError):
                main.migrate_database(db)
        self.assertEqual(_sha256(db), before)
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(main.detect_schema(conn)["kind"], "v1")
        finally:
            conn.close()

    def test_user_version_validation_failure_rolls_back_before_commit(self):
        rows = [("http://example.com/a", "A", "", "a body")]
        db = self._make_v1(rows)
        before = _sha256(db)
        orig_create = main.create_schema_v2

        def create_then_unstamp(conn):
            orig_create(conn)
            conn.execute("PRAGMA user_version = 0")

        with mock.patch.object(
            main, "create_schema_v2", side_effect=create_then_unstamp
        ):
            with self.assertRaises(main.MigrationError):
                main.migrate_database(db)
        self.assertEqual(_sha256(db), before)

    def test_no_fallible_post_commit_validation(self):
        calls = {"n": 0}
        orig_detect = main.detect_schema

        def counting_detect(conn):
            calls["n"] += 1
            if calls["n"] > 3:
                raise AssertionError("post-commit schema check ran")
            return orig_detect(conn)

        db = self._make_v1([
            ("http://example.com/a", "A", "", "a body"),
            ("http://example.com/b", "B", "", "b body"),
        ])
        with mock.patch.object(
            main, "detect_schema", side_effect=counting_detect
        ):
            rep = main.migrate_database(db)
        self.assertEqual(rep["status"], "completed")
        # Exactly 3 schema detections: classification, backup verification,
        # pre-commit validation. No fourth (post-commit) detection.
        self.assertEqual(calls["n"], 3)

    def test_existing_backup_never_overwritten_even_after_success(self):
        db = self._make_v1([
            ("http://example.com/a", "A", "", "a body"),
        ])
        main.migrate_database(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        bak_bytes = bak.read_bytes()
        # Re-running against the now-v2 database must be a noop that
        # leaves the prior verified backup untouched.
        rep = main.migrate_database(db)
        self.assertEqual(rep["status"], "noop")
        self.assertEqual(bak.read_bytes(), bak_bytes)


class RecoveryTests(Stage24TestCase):
    """M2.2: post-migration recovery, backup restoration, retry semantics."""

    def test_successful_migration_then_reopen(self):
        rows = [
            ("http://example.com/a", "Alpha", "desc a", "alpha body"),
            ("http://example.com/b", "Beta", "desc b", "beta body"),
        ]
        db = self._make_v1(rows)
        rep = main.migrate_database(db)
        self.assertEqual(rep["status"], "completed")
        # Reopen and read through a fresh connection.
        conn = sqlite3.connect(str(db))
        try:
            schema = main.detect_schema(conn)
            user_version = conn.execute(
                "PRAGMA user_version").fetchone()[0]
            n_pages = conn.execute(
                "SELECT count(*) FROM pages").fetchone()[0]
            n_fts = conn.execute(
                "SELECT count(*) FROM pages_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v2")
        self.assertEqual(user_version, 2)
        self.assertEqual(n_pages, 2)
        self.assertEqual(n_fts, 2)
        self.assertEqual(self._page_rows(db), [
            (1, "http://example.com/a", "Alpha", "desc a", "alpha body"),
            (2, "http://example.com/b", "Beta", "desc b", "beta body"),
        ])

    def test_backup_restorable_into_separate_database(self):
        db = self._make_v1(DIRTY_ROWS)
        source_rows = self._page_rows(db)
        self.assertEqual(len(source_rows), 10)
        main.migrate_database(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")

        restored = self.tmp_path / "restored.db"
        bak_conn = sqlite3.connect(str(bak))
        restored_conn = sqlite3.connect(str(restored))
        try:
            bak_conn.backup(restored_conn)  # into a SEPARATE database
        finally:
            restored_conn.close()
            bak_conn.close()

        self.assertNotEqual(_sha256(restored), _sha256(db))
        self.assertEqual(self._page_rows(restored), source_rows)
        self.assertEqual(self._detect_kind(restored), "v1")
        # The restored v1 database is usable by the v1 inspection path.
        report = main.inspect_database(restored)
        self.assertTrue(report["summary"]["needs_migration"])
        self.assertEqual(report["rows"]["total"], 10)
        # The original migrated database is untouched by the restore.
        self.assertEqual(self._detect_kind(db), "v2")

    def test_restore_never_overwrites_original(self):
        db = self._make_v1([
            ("http://example.com/a", "A", "", "a body"),
        ])
        main.migrate_database(db)
        migrated_sha = _sha256(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        restored = self.tmp_path / "restored2.db"
        bak_conn = sqlite3.connect(str(bak))
        restored_conn = sqlite3.connect(str(restored))
        try:
            bak_conn.backup(restored_conn)
        finally:
            restored_conn.close()
            bak_conn.close()
        self.assertEqual(_sha256(db), migrated_sha)
        self.assertEqual(self._page_rows(restored),
                         self._page_rows(bak))


class InjectionFailureTests(Stage24TestCase):
    """M2.2/M2.3: injected pre-commit failures — standardized MigrationError,
    correct stage, chained cause, full rollback."""

    ROWS = [
        ("http://example.com/a", "Alpha", "", "alpha body"),
        ("http://example.com/b", "Beta", "", "beta body"),
        ("http://example.com/c", "Gamma", "", "gamma body"),
    ]

    def test_failure_during_chunked_copy_rolls_back_and_reports_stage(self):
        db = self._make_v1(self.ROWS)
        source_rows = self._page_rows(db)
        injected = RuntimeError("injected copy failure")
        real_insert = main._insert_retained_rows

        def partial_then_raise(conn, retained, chunk_size=200):
            real_insert(conn, retained[:1], chunk_size)
            raise injected

        with mock.patch.object(
            main, "_insert_retained_rows", side_effect=partial_then_raise
        ):
            with self.assertRaises(main.MigrationError) as ctx:
                main.migrate_database(db)
        self.assertIs(ctx.exception.__cause__, injected)
        self.assertEqual(ctx.exception.stages[-1][0], "copying")
        self._assert_v1_intact(db, source_rows)

    def test_failure_during_fts_population_rolls_back_and_reports_stage(self):
        db = self._make_v1(self.ROWS)
        source_rows = self._page_rows(db)
        orig_create = main.create_schema_v2

        def create_with_breaking_trigger(conn):
            orig_create(conn)
            conn.execute("DROP TRIGGER IF EXISTS pages_ai")
            conn.execute(
                "CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN "
                "SELECT RAISE(ABORT, 'injected fts population failure'); END")

        with mock.patch.object(
            main, "create_schema_v2", side_effect=create_with_breaking_trigger
        ):
            with self.assertRaises(main.MigrationError) as ctx:
                main.migrate_database(db)
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.IntegrityError)
        self.assertIn("injected fts population failure",
                      str(ctx.exception.__cause__))
        self.assertEqual(ctx.exception.stages[-1][0], "copying")
        self._assert_v1_intact(db, source_rows)

    def test_validation_failure_reports_validation_stage_and_rolls_back(self):
        db = self._make_v1([self.ROWS[0]])
        source_rows = self._page_rows(db)
        with mock.patch.object(
            main, "_validate_migration", return_value=["simulated failure"]
        ):
            with self.assertRaises(main.MigrationError) as ctx:
                main.migrate_database(db)
        self.assertEqual(ctx.exception.stages[-1][0], "validation")
        self._assert_v1_intact(db, source_rows)


class RetryTests(Stage24TestCase):
    """M2.2: cleanup, no partial state, and retry-per-policy."""

    def test_failed_migration_releases_connections_and_no_partial_state(self):
        db = self._make_v1([
            ("http://example.com/a", "Alpha", "", "alpha body"),
            ("http://example.com/b", "Beta", "", "beta body"),
        ])
        with mock.patch.object(
            main, "_validate_migration", return_value=["simulated failure"]
        ):
            with self.assertRaises(main.MigrationError):
                main.migrate_database(db)

        # The migration connection is released: the db can be renamed now.
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        renamed = self.tmp_path / "v1-renamed.db"
        os.replace(db, renamed)
        self.assertTrue(renamed.exists())
        self.assertTrue(bak.exists())  # backup keeps its original name
        # Backup remains independently readable after the rename.
        conn = sqlite3.connect(str(bak))
        try:
            self.assertEqual(main.detect_schema(conn)["kind"], "v1")
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM pages").fetchone()[0], 2)
        finally:
            conn.close()
        # No partial migration state in the directory (db + bak only).
        self.assertEqual(
            sorted(p.name for p in self.tmp_path.iterdir()),
            sorted([renamed.name, bak.name]))

    def test_retry_after_failure_requires_backup_removal_per_policy(self):
        rows = [("http://example.com/a", "Alpha", "", "alpha body")]
        db = self._make_v1(rows)
        source_rows = self._page_rows(db)

        with mock.patch.object(
            main, "_validate_migration", return_value=["simulated failure"]
        ):
            with self.assertRaises(main.MigrationError):
                main.migrate_database(db)
        self._assert_v1_intact(db, source_rows)

        # Retry WITHOUT removing the verified backup -> policy abort.
        with self.assertRaises(main.MigrationError) as ctx:
            main.migrate_database(db)
        self.assertIn("backup already exists", str(ctx.exception))
        self._assert_v1_intact(db, source_rows)

        # Per policy: remove/rename the backup, then retry succeeds.
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        os.remove(bak)
        rep = main.migrate_database(db)
        self.assertEqual(rep["status"], "completed")
        self.assertEqual(rep["stats"]["retained_rows"], 1)
        self.assertEqual(self._detect_kind(db), "v2")
        self.assertTrue(bak.exists())  # fresh backup for the new migration
        self.assertEqual(self._page_rows(bak), source_rows)

    def test_repeated_migration_after_success_is_noop_and_keeps_backup(self):
        db = self._make_v1([
            ("http://example.com/a", "Alpha", "", "alpha body"),
        ])
        main.migrate_database(db)
        db_sha = _sha256(db)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        bak_bytes = bak.read_bytes()
        rep = main.migrate_database(db)
        self.assertEqual(rep["status"], "noop")
        self.assertEqual(_sha256(db), db_sha)
        self.assertEqual(bak.read_bytes(), bak_bytes)


class ExceptionApiAuditTests(Stage24TestCase):
    """M2.3: exception consistency + migration API/report audit."""

    STAGES = {"inspection", "backup", "copying", "fts_population",
              "validation", "commit", "completed"}

    def test_report_fields_consistent_for_completed_and_noop(self):
        db = self._make_v1([
            ("http://example.com/a", "Alpha", "", "alpha body"),
        ])
        rep = main.migrate_database(db)
        self.assertEqual(set(rep), {"status", "stages", "stats", "database"})
        self.assertEqual(rep["status"], "completed")
        self.assertEqual(rep["database"], str(db))
        self.assertIsInstance(rep["stats"], dict)
        for key in ("original_rows", "retained_rows", "removed_duplicates",
                    "canonical_conflict_groups", "invalid_encountered",
                    "invalid_retained"):
            self.assertIn(key, rep["stats"])
        for stage, _message in rep["stages"]:
            self.assertIn(stage, self.STAGES)
        self.assertEqual(rep["stages"][-1][0], "completed")

        db2 = self._make_v2()
        noop = main.migrate_database(db2)
        self.assertEqual(set(noop), {"status", "stages", "stats", "database"})
        self.assertEqual(noop["status"], "noop")
        self.assertIsNone(noop["stats"])
        self.assertEqual(noop["database"], str(db2))

    def test_progress_callback_exceptions_never_mask_committed_migration(self):
        db = self._make_v1([
            ("http://example.com/cat", "Cats", "", "cats run around"),
        ])
        calls = []

        def boom(stage, message):
            calls.append((stage, message))
            raise RuntimeError(f"callback boom at {stage}")

        rep = main.migrate_database(db, progress_callback=boom)
        self.assertEqual(rep["status"], "completed")
        self.assertEqual(calls[-1][0], "completed")
        self.assertEqual(self._detect_kind(db), "v2")
        result = main.search_json("cats", db_path=db)
        self.assertEqual(result["total_results"], 1)

    def test_commit_failure_standardized_as_migrationerror_with_stage(self):
        db = self._make_v1([
            ("http://example.com/a", "Alpha", "", "alpha body"),
        ])
        source_rows = self._page_rows(db)

        class CommitFailureConnection(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("injected commit failure")

        real_connect = main.sqlite3.connect

        def failing_connect(database, *args, **kwargs):
            kwargs.setdefault("factory", CommitFailureConnection)
            return real_connect(database, *args, **kwargs)

        with mock.patch.object(
            main.sqlite3, "connect", side_effect=failing_connect
        ):
            with self.assertRaises(main.MigrationError) as ctx:
                main.migrate_database(db)
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(ctx.exception.stages[-1][0], "commit")
        # commit failure is still pre-commit: full rollback to v1.
        self._assert_v1_intact(db, source_rows)

    def test_refusal_failures_record_correct_stage(self):
        # missing -> no stage recorded
        missing = self.tmp_path / "missing.db"
        with self.assertRaises(main.MigrationError) as ctx:
            main.migrate_database(missing)
        self.assertEqual(ctx.exception.stages, [])

        # corrupt -> inspection
        corrupt = self.tmp_path / "corrupt.db"
        corrupt.write_bytes(b"\x00\x01\x02 not a sqlite database")
        with self.assertRaises(main.MigrationError) as ctx:
            main.migrate_database(corrupt)
        self.assertEqual(ctx.exception.stages[-1][0], "inspection")

        # unsupported schema -> inspection
        unknown = self.tmp_path / "unknown.db"
        conn = sqlite3.connect(str(unknown))
        try:
            conn.execute("CREATE TABLE foo (x TEXT)")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(main.MigrationError) as ctx:
            main.migrate_database(unknown)
        self.assertEqual(ctx.exception.stages[-1][0], "inspection")

        # existing backup abort (pre-lock) -> inspection
        rows = [("http://example.com/a", "A", "", "a body")]
        db = self._make_v1(rows)
        bak = pathlib.Path(str(db) + ".pre-migrate.bak")
        bak.write_text("sentinel")
        with self.assertRaises(main.MigrationError) as ctx:
            main.migrate_database(db)
        self.assertEqual(ctx.exception.stages[-1][0], "inspection")

        # backup creation failure -> backup (connection fails for the bak
        # path after the existence check has passed, so the failure is
        # reported at the backup stage).
        with tempfile.TemporaryDirectory() as d:
            db3 = pathlib.Path(d) / "v1.db"
            conn3 = main.get_database(db3)
            try:
                conn3.executemany(
                    "INSERT INTO pages (url, title, description, body) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn3.commit()
            finally:
                conn3.close()
            source_rows = self._page_rows(db3)
            bak3 = pathlib.Path(str(db3) + ".pre-migrate.bak")
            real_connect = main.sqlite3.connect

            def bak_failing_connect(database, *args, **kwargs):
                if str(database).endswith(".pre-migrate.bak"):
                    raise sqlite3.OperationalError("injected backup failure")
                return real_connect(database, *args, **kwargs)

            with mock.patch.object(
                main.sqlite3, "connect", side_effect=bak_failing_connect
            ):
                with self.assertRaises(main.MigrationError) as ctx:
                    main.migrate_database(db3)
            self.assertEqual(ctx.exception.stages[-1][0], "backup")
            # Source is untouched; the backup was never created.
            self.assertEqual(self._page_rows(db3), source_rows)
            self.assertEqual(self._detect_kind(db3), "v1")
            conn3b = sqlite3.connect(str(db3))
            try:
                self.assertEqual(conn3b.execute(
                    "PRAGMA user_version").fetchone()[0], 0)
            finally:
                conn3b.close()
            self.assertFalse(bak3.exists())


if __name__ == "__main__":
    unittest.main()
