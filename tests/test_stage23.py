"""Phase 4 M1: migration preflight (inspect_database / detect_schema).

Covers schema family detection (v1/v2/empty/unknown), duplicate /
canonical-collision / invalid-URL statistics, rowid checks, the
summary/needs_migration decision, and the strict read-only guarantee
(the inspected database must be byte-identical afterwards and no side
files may appear).
"""
import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

import main  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
GOLDEN_V2 = FIXTURES / "golden_v2.db"

DIRTY_V1_ROWS = [
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


def _sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


class Stage23TestCase(unittest.TestCase):
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


class DetectSchemaTests(Stage23TestCase):
    def test_v1_detection(self):
        db = self._make_v1(DIRTY_V1_ROWS)
        conn = sqlite3.connect(str(db))
        try:
            schema = main.detect_schema(conn)
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v1")
        self.assertEqual(schema["user_version"], 0)
        self.assertEqual(schema["pages_type"], "virtual")
        self.assertFalse(schema["pages_fts_present"])

    def test_v2_detection(self):
        conn = sqlite3.connect(str(GOLDEN_V2))
        try:
            schema = main.detect_schema(conn)
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "v2")
        self.assertEqual(schema["user_version"], 2)
        self.assertEqual(schema["pages_type"], "table")
        self.assertTrue(schema["pages_fts_present"])

    def test_empty_file_detection(self):
        db = self.tmp_path / "empty.db"
        db.write_bytes(b"")
        conn = sqlite3.connect(str(db))
        try:
            schema = main.detect_schema(conn)
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "empty")

    def test_unknown_schema_detection(self):
        db = self.tmp_path / "unknown.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE foo (x TEXT)")
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(str(db))
        try:
            schema = main.detect_schema(conn)
        finally:
            conn.close()
        self.assertEqual(schema["kind"], "unknown")


class InspectV1Tests(Stage23TestCase):
    def test_dirty_v1_report(self):
        db = self._make_v1(DIRTY_V1_ROWS)
        report = main.inspect_database(db)
        self.assertTrue(report["exists"])
        self.assertEqual(report["schema"]["kind"], "v1")

        # JSON-safe payload.
        json.dumps(report)

        self.assertEqual(report["rows"]["total"], 10)
        self.assertEqual(report["rows"]["distinct_urls"], 9)
        self.assertEqual(report["rows"]["min_rowid"], 1)
        self.assertEqual(report["rows"]["max_rowid"], 10)

        dup = report["duplicates"]
        self.assertEqual(dup["exact_url_extra_rows"], 1)
        self.assertEqual(dup["exact_url_groups"], 1)
        self.assertEqual(dup["exact_url_examples"][0]["url"],
                         "http://example.com/a")
        self.assertEqual(dup["exact_url_examples"][0]["rowids"], [1, 2])
        self.assertEqual(dup["canonical_conflict_groups"], 2)
        canon_examples = {
            (e["canonical"], tuple(e["urls"]))
            for e in dup["canonical_conflict_examples"]
        }
        self.assertIn(
            ("http://example.com/b",
             ("http://example.com/b", "http://example.com/b/")),
            canon_examples,
        )
        self.assertIn(
            ("http://example.com/d",
             ("http://EXAMPLE.com:80/d", "http://example.com/d")),
            canon_examples,
        )

        invalid = report["invalid_urls"]
        self.assertEqual(invalid["count"], 3)
        invalid_urls = {e["url"] for e in invalid["examples"]}
        self.assertEqual(invalid_urls, {"not a url", "http://[::1", ""})

        self.assertEqual(report["rowid_issues"]["count"], 0)

        summary = report["summary"]
        self.assertTrue(summary["needs_migration"])
        self.assertTrue(any("exact" in r for r in summary["reasons"]))
        self.assertTrue(any("canonical" in r for r in summary["reasons"]))
        self.assertTrue(any("invalid" in r for r in summary["reasons"]))

    def test_clean_v1_report(self):
        rows = [
            ("http://x/a", "A", "", "a"),
            ("http://x/b", "B", "", "b"),
        ]
        db = self._make_v1(rows)
        report = main.inspect_database(db)
        self.assertEqual(report["rows"]["total"], 2)
        self.assertEqual(report["duplicates"]["exact_url_extra_rows"], 0)
        self.assertEqual(report["duplicates"]["canonical_conflict_groups"], 0)
        self.assertEqual(report["invalid_urls"]["count"], 0)
        self.assertTrue(report["summary"]["needs_migration"])
        self.assertEqual(report["summary"]["reasons"],
                         ["legacy v1 schema present"])


class ReadOnlyGuaranteeTests(Stage23TestCase):
    def test_inspect_never_modifies_database(self):
        db = self._make_v1(DIRTY_V1_ROWS)
        before = _sha256(db)
        listing_before = set(os.listdir(self.tmp_path))

        report = main.inspect_database(db)
        self.assertEqual(_sha256(db), before)
        self.assertEqual(set(os.listdir(self.tmp_path)), listing_before)
        self.assertEqual(report["schema"]["kind"], "v1")

    def test_missing_database_is_not_created(self):
        missing = self.tmp_path / "nope.db"
        report = main.inspect_database(missing)
        self.assertFalse(report["exists"])
        self.assertFalse(report["summary"]["needs_migration"])
        self.assertEqual(report["summary"]["reasons"],
                         ["database file does not exist"])
        self.assertFalse(missing.exists())


class InspectNonV1Tests(Stage23TestCase):
    def test_v2_database_needs_no_migration(self):
        report = main.inspect_database(GOLDEN_V2)
        self.assertEqual(report["schema"]["kind"], "v2")
        self.assertFalse(report["summary"]["needs_migration"])
        self.assertIn("v2", report["summary"]["reasons"][0])

    def test_empty_database_needs_no_migration(self):
        db = self.tmp_path / "empty.db"
        db.write_bytes(b"")
        report = main.inspect_database(db)
        self.assertEqual(report["schema"]["kind"], "empty")
        self.assertFalse(report["summary"]["needs_migration"])

    def test_unknown_schema_needs_no_migration(self):
        db = self.tmp_path / "unknown.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE foo (x TEXT)")
            conn.commit()
        finally:
            conn.close()
        report = main.inspect_database(db)
        self.assertEqual(report["schema"]["kind"], "unknown")
        self.assertFalse(report["summary"]["needs_migration"])
        self.assertIn("unknown", report["summary"]["reasons"][0])


class InspectConcurrencyTests(Stage23TestCase):
    """Documents the current read semantics (M1.1 audit findings).

    - A concurrent writer's UNCOMMITTED rows are never visible to the
      inspector (reports reflect committed state only).
    - Each inspect_database() call opens a fresh connection and is a
      point-in-time snapshot: a commit between two calls becomes visible
      to the second call. There is no cross-call snapshot guarantee.
    """

    def test_inspect_ignores_uncommitted_concurrent_write(self):
        db = self._make_v1([
            ("http://x/a", "A", "", "a"),
            ("http://x/b", "B", "", "b"),
        ])
        writer = sqlite3.connect(str(db))
        try:
            writer.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://x/c", "C", "", "c"),
            )  # not committed
            report = main.inspect_database(db)
            self.assertEqual(report["rows"]["total"], 2)
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(main.inspect_database(db)["rows"]["total"], 2)

    def test_inspect_is_point_in_time_per_call(self):
        db = self._make_v1([
            ("http://x/a", "A", "", "a"),
            ("http://x/b", "B", "", "b"),
        ])
        self.assertEqual(main.inspect_database(db)["rows"]["total"], 2)
        writer = sqlite3.connect(str(db))
        try:
            writer.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://x/c", "C", "", "c"),
            )
            writer.commit()
        finally:
            writer.close()
        # A fresh inspection after the commit sees the new row.
        self.assertEqual(main.inspect_database(db)["rows"]["total"], 3)


class CorruptDatabaseTests(Stage23TestCase):
    """M1.2: corrupt/unreadable DBs must raise, never report success."""

    def _corrupt_db(self):
        db = self.tmp_path / "corrupt.db"
        db.write_bytes(b"\x00\x01\x02 not a sqlite database file at all")
        return db

    def test_corrupt_raises_inspection_error(self):
        db = self._corrupt_db()
        with self.assertRaises(main.DatabaseInspectionError) as ctx:
            main.inspect_database(db)
        # Preserve the underlying SQLite error information.
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.Error)

    def test_corrupt_never_yields_successful_report(self):
        db = self._corrupt_db()
        try:
            main.inspect_database(db)
        except main.DatabaseInspectionError:
            pass
        else:
            self.fail("expected DatabaseInspectionError for corrupt database")


class UnknownReadableDistinctTests(Stage23TestCase):
    """Readable but unrecognized schema stays 'unknown' (not corruption)."""

    def test_unknown_readable_reports_unknown_not_raise(self):
        db = self.tmp_path / "unknown.db"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE foo (x TEXT)")
            conn.commit()
        finally:
            conn.close()
        report = main.inspect_database(db)  # must not raise
        self.assertEqual(report["schema"]["kind"], "unknown")
        self.assertFalse(report["summary"]["needs_migration"])


class WalCommittedDataTests(Stage23TestCase):
    """M1.2: inspection must include data committed in an active WAL."""

    def test_inspection_includes_committed_wal_data(self):
        db = self._make_v1([
            ("http://x/a", "A", "", "a"),
            ("http://x/b", "B", "", "b"),
        ])
        writer = sqlite3.connect(str(db))
        try:
            writer.execute(
                "INSERT INTO pages (url, title, description, body) "
                "VALUES (?, ?, ?, ?)",
                ("http://x/c", "C", "", "c"),
            )
            writer.commit()
            # Keep writer open so committed data still lives in the WAL.
            self.assertTrue(pathlib.Path(str(db) + "-wal").exists())
            self.assertEqual(
                main.inspect_database(db)["rows"]["total"], 3)
        finally:
            writer.close()


class SnapshotConsistencyTests(Stage23TestCase):
    """M1.2: the inspection connection supports one-consistent-snapshot reads."""

    def test_explicit_read_transaction_keeps_snapshot(self):
        db = self._make_v1([
            ("http://x/a", "A", "", "a"),
            ("http://x/b", "B", "", "b"),
        ])
        inspector = main._connect_readonly(db)
        try:
            inspector.execute("BEGIN")
            before = inspector.execute(
                "SELECT count(*) FROM pages").fetchone()[0]
            writer = sqlite3.connect(str(db))
            try:
                writer.execute(
                    "INSERT INTO pages (url, title, description, body) "
                    "VALUES (?, ?, ?, ?)",
                    ("http://x/c", "C", "", "c"),
                )
                writer.commit()
            finally:
                writer.close()
            after = inspector.execute(
                "SELECT count(*) FROM pages").fetchone()[0]
            self.assertEqual(before, 2)
            self.assertEqual(after, 2)  # same snapshot as before
            inspector.execute("ROLLBACK")
        finally:
            inspector.close()
        self.assertEqual(
            main.inspect_database(db)["rows"]["total"], 3)


class DisappearanceRaceTests(Stage23TestCase):
    """M1.2: target disappearing during inspection raises, never creates."""

    def test_missing_target_never_created(self):
        db = self.tmp_path / "gone.db"
        self.assertFalse(db.exists())
        with self.assertRaises(main.DatabaseInspectionError):
            main._connect_readonly(db)
        self.assertFalse(db.exists())

    def test_disappearance_between_check_and_open_raises(self):
        db = self._make_v1([
            ("http://x/a", "A", "", "a"),
        ])
        db.unlink()  # exists-check then open race: file vanishes
        with self.assertRaises(main.DatabaseInspectionError):
            main._connect_readonly(db)
        self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
