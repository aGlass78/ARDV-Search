import argparse
import os
import pathlib
import re
import sqlite3
import time
import threading
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


DB_PATH = pathlib.Path(__file__).resolve().with_name("result.db")
ENV_PATH = pathlib.Path(__file__).resolve().with_name(".env")

DEFAULT_DELAY = 0.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_THREADS = 8
MAX_REDIRECTS = 10
DB_COMMIT_BATCH = 25
PROGRESS_INTERVAL = 1.0  # seconds between progress-callback reports
ROBOTS_RETRY_SECONDS = 300.0

# Sitemap discovery: probed (in order) when robots.txt declares no
# Sitemap lines. <urlset> and <sitemapindex> XML are both supported.
SITEMAP_COMMON_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
)
MAX_SITEMAP_DEPTH = 5

# Optional, explicit port policy. None (the default) means any valid
# port may be crawled; set to a set of ints, e.g. {80, 443}, to
# deliberately restrict the crawler (say, away from internal
# services). Malformed ports are rejected regardless of this setting.
ALLOWED_PORTS = None

# Seconds before a page request is abandoned as a timeout.
REQUEST_TIMEOUT = 20.0

STANDARD_PORTS = {"http": 80, "https": 443}

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".zip", ".mp4", ".mp3", ".css", ".js", ".ico",
    ".svg", ".woff", ".woff2"
)


def load_env_value(key, env_path=None):
    """Read KEY=VALUE from a .env file - no dependencies needed.

    Returns the last definition of `key`, or None when the file is
    missing/unreadable or the key is absent or empty. Only simple
    KEY=value lines are understood; blank lines and # comments are
    ignored, and surrounding whitespace and quotes are stripped.
    """
    if env_path is None:
        env_path = ENV_PATH

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    value = None

    for line in lines:
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        found_key, _, found_value = line.partition("=")

        if found_key.strip() != key:
            continue

        found_value = found_value.strip().strip("'\"")
        value = found_value or None

    return value


def build_user_agent(contact_email):
    """Assemble the User-Agent; the contact section is optional.

    An empty or whitespace-only email (e.g. .env missing, or
    CONTACT_EMAIL left blank) simply drops the contact section.
    """
    contact_email = (contact_email or "").strip()

    if not contact_email:
        return "ModelSearchEngine (educational crawler)"

    return (f"ModelSearchEngine (educational crawler; "
            f"contact: {contact_email})")


# The contact address is not hardcoded: it comes from .env (next to
# main.py) so every checkout can use its own. Missing .env or an
# empty CONTACT_EMAIL falls back to the no-contact agent above.
USER_AGENT = build_user_agent(load_env_value("CONTACT_EMAIL"))


HELP_TEXT = r"""WHAT THIS PROGRAM DOES
  Two commands, run from a terminal:
    crawl   - visits a website, reads its pages, and saves their text
              into a local database file (result.db, created next to
              main.py).
    search  - looks through everything that was crawled and prints the
              best-matching pages.

QUICK START
    1)  python main.py crawl https://quotes.toscrape.com/
    2)  python main.py search "quote about life"

    Show this help at any time:
        python main.py --h


CRAWL Command

Basic form:

      python main.py crawl <url> [options]

  The crawl always stays inside the seed's hostname and its subdomains.
  Seeding https://quotes.toscrape.com/ crawls quotes.toscrape.com (and
  anything under it like en.quotes.toscrape.com) but never
  books.toscrape.com or toscrape.com. To allow another site too, use
  --allow; with a path it limits that site to one folder:
  --allow example.com/page allows example.com/page/quote but not
  example.com/page2. To allow a whole domain family, seed the bare
  domain: https://toscrape.com/

  --------------------------------------------------------------------
  url                    The web address to start crawling from; the
                         crawl is limited to this hostname + subdomains.
                         Example:
                         python main.py crawl https://quotes.toscrape.com/

  --max-pages N          Stop after N pages have been indexed (omit for
                         no limit).
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --max-pages 25

  --delay SECONDS        Wait at least this many seconds between
                         requests to the same host (0 = no built-in
                         waiting; robots.txt rules still apply on top).
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --delay 1

  --max-bytes N          Skip any page whose download would exceed N
                         bytes (protects against huge files).
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --max-bytes 500000

  --threads N           Download with N parallel workers (politeness
                         limits are still enforced per host).
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --threads 4

  --allow HOST[/PATH]   Also allow this extra site during the crawl
                         (repeatable). With a path, the site is limited
                         to that folder and everything below it:
                         --allow example.com/page allows
                         example.com/page/quote but not example.com/page2.
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --allow books.toscrape.com
                         (the old spelling --allowed still works)

  By default the crawler also feeds the crawl from sitemaps. It first
  checks the site's robots.txt for a Sitemap declaration, then probes
  the common locations (/sitemap.xml, /sitemap_index.xml,
  /sitemap-index.xml, /wp-sitemap.xml). Both <urlset> and
  <sitemapindex> XML sitemaps are supported (namespaced too); index
  documents are followed recursively. Page URLs found are normalized,
  checked against the crawl scope, and seeded into the queue before
  HTML link discovery begins. If no sitemap is found the crawl simply
  continues with HTML discovery.

  --no-sitemap          Skip sitemap discovery entirely; discover
                         pages only by following HTML links.
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --no-sitemap

  --only-sitemap        Crawl only the page URLs listed in the site's
                         sitemaps; pages are indexed but their links
                         are NOT followed. Aborts when no sitemap is
                         found.
                         Example:
                         python main.py crawl https://quotes.toscrape.com/ --only-sitemap
  Press Ctrl+C during a crawl to stop early: queued work is
  cancelled, requests already in flight are finished, everything
  indexed so far is saved, and the statistics are still printed.

  --------------------------------------------------------------------

SEARCH Command

  Basic form:

      python main.py search <query> [options]

  How matching works: pages containing ALL your words are returned
  first; if none contain all of them, pages containing ANY of them are
  returned. Words match by prefix, so "cat" also finds "cats". Results
  are ranked with BM25 — a page with your word in its title outranks a
  page that only mentions it in the body.

  --------------------------------------------------------------------
  query                 The words to search for (any characters are
                         safe; punctuation is ignored).
                         Example:
                         python main.py search "famous quote about life"

  --limit N              Show at most N results (any positive number).
                         Example:
                         python main.py search "famous quote" --limit 20

  --e                   Exact phrase search: all your words must appear
                         together, in order (no prefix matching, no
                         stemming). "google bomb" finds "google bomb
                         controversy" but not "google bombing", "bomb
                         google", or "google controversial bomb".
                         Example:
                         python main.py search "google bomb" --e

  --es                  Stemming: word endings are ignored, so a search
                         for "google bomb" also finds "google bombing".
                         Example:
                         python main.py search "google bomb" --es
  --------------------------------------------------------------------


============================================================================
 HARDCODED VALUES (built into the code, not settable from the CLI)
============================================================================

  Change these by editing main.py near the top of the file.

  DB_PATH                The database file: result.db, always created
                        next to main.py (never in whatever folder you
                        run the command from).

  USER_AGENT             How the crawler introduces itself to servers:
                        "ModelSearchEngine (educational crawler)", plus
                        "; contact: <address>" when CONTACT_EMAIL is
                        set in a .env file next to main.py. A missing
                        .env or an empty value simply drops the contact
                        section.

  DEFAULT_DELAY          0.0 seconds — the wait used when --delay is
                        omitted.

  DEFAULT_MAX_BYTES      2,000,000 bytes (2 MB) — the download cap used
                        when --max-bytes is omitted.

  DEFAULT_THREADS        8 workers — the parallelism used when
                        --threads is omitted.

  MAX_REDIRECTS          10 — a page that keeps redirecting more than
                        10 times (or loops forever) is abandoned.

  DB_COMMIT_BATCH        25 — indexed pages are saved to the database in
                        batches of 25, then the batch is written to
                        disk. A crash loses at most one batch, never
                        corrupts the index.

  ROBOTS_RETRY_SECONDS   300 (5 minutes) — if a site's robots.txt cannot
                        be loaded, its pages are skipped and the
                        robots.txt is retried after this long.

  ALLOWED_PORTS          Optional port policy: None (the default)
                        means any valid port is crawled; set it to a
                        set of ports, e.g. {80, 443}, to deliberately
                        restrict where the crawler may connect.

  STANDARD_PORTS         http://site:80 is treated the same as
                        http://site (and https://site:443 the same as
                        https://site) so the same page is not indexed
                        twice.

  SKIP_EXTENSIONS        Files like .pdf, .jpg, .zip, .mp4, .css are
                        never downloaded or indexed.

  SITEMAP_COMMON_PATHS   /sitemap.xml, /sitemap_index.xml,
                         /sitemap-index.xml, /wp-sitemap.xml — these
                         are probed when robots.txt declares no
                         Sitemap lines.

  MAX_SITEMAP_DEPTH      5 — how deep <sitemapindex> trees are
                         followed (guards against cycles/runaway).
"""


class RateLimiter:
    """Coordinate request pacing between worker threads.

    Keys are hostnames, not host:port, so one machine on several ports
    shares a single rate limit. wait() reserves the next time slot
    under the lock but sleeps *outside* it, so a worker waiting on a
    paused host never blocks workers headed for other hosts.
    """

    def __init__(self, default_delay):
        self.default_delay = float(default_delay)
        self._min_delays = {}    # host -> sticky minimum (Crawl-delay)
        self._pause_until = {}   # host -> monotonic deadline (Retry-After)
        self._last_request = {}  # host -> reserved slot for next request
        self._lock = threading.Lock()

    def set_min_delay(self, host, delay):
        """Raise the sticky per-host minimum (robots.txt Crawl-delay)."""
        if delay and delay > 0:
            with self._lock:
                self._min_delays[host] = max(
                    float(delay),
                    self._min_delays.get(host, 0.0)
                )

    def pause_host(self, host, seconds):
        """Pause a host until a deadline (HTTP Retry-After).

        Unlike set_min_delay, a pause is temporary: once the deadline
        passes, pacing returns to normal instead of keeping a new
        permanent floor for the rest of the run.
        """
        with self._lock:
            until = time.monotonic() + max(0.0, float(seconds))
            if until > self._pause_until.get(host, 0.0):
                self._pause_until[host] = until

    def wait(self, host):
        """Reserve this host's next request slot, then sleep if needed."""
        with self._lock:
            delay = max(
                self.default_delay,
                self._min_delays.get(host, 0.0)
            )
            now = time.monotonic()
            last = self._last_request.get(host)
            earliest = last + delay if last is not None else now
            pause_until = self._pause_until.get(host)
            if pause_until is not None and pause_until > earliest:
                earliest = pause_until
            # Reserve the slot so parallel workers for the same host
            # queue up behind it without sleeping under the lock.
            self._last_request[host] = earliest
            sleep_for = max(0.0, earliest - now)

        if sleep_for > 0:
            time.sleep(sleep_for)


def resolve_db_path(value):
    """Turn a --db value into a path, anchored next to main.py.

    Relative values keep the project invariant that the database
    location does not depend on the current working directory (just
    like the default result.db). None means the default DB_PATH.

    A value that already ends with ".db" is used verbatim; otherwise
    ".db" is appended (case-insensitive check), so a bare --db name
    never creates an extensionless file (e.g. --db custom creates
    custom.db, not a file named "custom").
    """
    if value is None:
        return DB_PATH

    path = pathlib.Path(value)

    if path.suffix.lower() != ".db":
        path = pathlib.Path(str(path) + ".db")

    if not path.is_absolute():
        path = pathlib.Path(__file__).resolve().parent / path

    return path


def get_database(db_path=None):
    """Open or create the FTS5 database at db_path (default DB_PATH)."""
    conn = sqlite3.connect(resolve_db_path(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
            url UNINDEXED,
            title,
            description,
            body,
            tokenize='unicode61'
        )
    """)

    conn.commit()
    return conn


SCHEMA_VERSION = 2


def create_schema_v2(conn):
    """Create the v2 two-table schema on an open connection.

    v2 replaces the single FTS5 virtual table with an ordinary `pages`
    table (url UNIQUE, so upserts are O(log N) instead of a full-index
    scan) plus an external-content FTS5 `pages_fts` table kept in sync
    by triggers. The database is stamped with PRAGMA user_version = 2.

    Transaction ownership belongs to the caller: this helper only
    executes schema SQL and never issues BEGIN/COMMIT/ROLLBACK, so it
    can run inside a larger transaction (e.g. the Phase 4 migration)
    without committing the caller's work. Fresh initialization should
    call this then commit explicitly.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            rowid      INTEGER PRIMARY KEY,
            url        TEXT NOT NULL UNIQUE,
            title      TEXT,
            description TEXT,
            body       TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title,
            description,
            body,
            content='pages',
            content_rowid='rowid',
            tokenize='unicode61'
        )
    """)
    _create_fts_triggers(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _create_fts_triggers(conn):
    """Create the FTS5 external-content synchronization triggers.

    Every INSERT/UPDATE/DELETE on `pages` is mirrored into `pages_fts`
    so the search index never drifts from the content table. The UPDATE
    trigger carries a WHEN guard: a change to the URL alone (e.g. a
    dedup relabel) does not touch the FTS index, whose content is the
    title/description/body.
    """
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, description, body)
            VALUES (new.rowid, new.title, new.description, new.body);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, description, body)
            VALUES ('delete', old.rowid, old.title, old.description, old.body);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages
        WHEN old.title IS NOT new.title
          OR old.description IS NOT new.description
          OR old.body IS NOT new.body
        BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, description, body)
            VALUES ('delete', old.rowid, old.title, old.description, old.body);
            INSERT INTO pages_fts(rowid, title, description, body)
            VALUES (new.rowid, new.title, new.description, new.body);
        END
    """)


class DatabaseInspectionError(Exception):
    """Raised when a database cannot be inspected (corrupt/unreadable).

    The original SQLite error is preserved as ``__cause__`` so callers
    can distinguish a corrupt/unreadable database from a readable one
    with an unrecognized schema (which instead reports ``kind="unknown"``).
    """


def _connect_readonly(db_path):
    """Open a database for inspection without creating or modifying it.

    Strategy:

      - The target must exist *before* we attempt any connection. A
        missing file raises :class:`DatabaseInspectionError` instead of
        ever being created (SQLite's ``immutable=1`` URI can silently
        create a missing file on some platforms, so we never rely on it
        for the missing-file case).
      - Clean databases (no ``-wal`` sidecar at open time) are opened
        with ``file:...?immutable=1``. An immutable connection requires
        no shared-memory files, so it creates nothing on disk.
      - If a ``-wal`` sidecar exists, committed data may live only in
        the WAL, so an immutable open would silently miss it. That case
        uses a read-only connection (``file:...?mode=ro``), which still
        reads the WAL.
      - The WAL state is rechecked after an immutable open: if a
        ``-wal`` sidecar appears between the first check and the open
        (absent-to-present race), the immutable connection is closed and
        the connection is reopened read-only so no committed data is
        silently ignored.
      - The open file's identity (device + inode) is captured before and
        after the immutable open; if they differ, the file was deleted
        and/or recreated underneath us, and inspection refuses rather
        than silently reading a recreated artifact.

    Remaining race/limitation (documented): between the final recheck
    and the first read, a concurrent writer could still checkpoint or
    replace the WAL. Within the explicit read transaction used by
    :func:`inspect_database`, reads observe one fixed snapshot of
    whatever the connection opened against, so a late WAL transition
    can at worst make that snapshot slightly older - never
    inconsistent. On Windows, a read-only WAL open may create ``-shm``
    / ``-wal`` sidecars (SQLite behavior); the main database file itself
    is never created or modified by inspection.
    """
    import os
    import urllib.parse
    path = pathlib.Path(db_path)
    if not path.exists():
        raise DatabaseInspectionError(
            f"database file does not exist: {path}")
    posix = path.resolve().as_posix()
    wal_path = pathlib.Path(str(path) + "-wal")

    def _open(uri_tail):
        uri = "file:" + urllib.parse.quote(posix, safe="/:") + uri_tail
        return sqlite3.connect(uri, uri=True)

    def _identity():
        st = os.stat(path)
        return (st.st_dev, st.st_ino)

    if wal_path.exists():
        try:
            return _open("?mode=ro")
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseInspectionError(
                f"cannot open database read-only: {exc}") from exc

    before = _identity()
    try:
        conn = _open("?immutable=1")
    except (sqlite3.Error, OSError) as exc:
        raise DatabaseInspectionError(
            f"cannot open database for inspection: {exc}") from exc

    if _identity() != before:
        # The file changed underneath us (deleted and/or recreated):
        # do not inspect a silently-recreated artifact.
        try:
            conn.close()
        except sqlite3.Error:
            pass
        raise DatabaseInspectionError(
            "database file changed during inspection open (deleted or "
            "recreated)")

    # Handle the absent-to-present WAL race: recheck and reopen read-only.
    if wal_path.exists():
        conn.close()
        try:
            return _open("?mode=ro")
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseInspectionError(
                f"cannot open database read-only: {exc}") from exc
    return conn


def detect_schema(conn):
    """Classify an open database's schema without writing to it.

    Returns a dict describing which schema family the database belongs
    to. This is a read-only inspection used by migration preflight
    (M1) and later by production activation (M4).

      - v1: legacy standalone FTS5 virtual table ``pages``
             (user_version 0).
      - v2: ordinary ``pages`` + external-content ``pages_fts``
             (user_version 2).
      - empty: the file exists but contains no objects.
      - unknown: ``pages`` is absent or has an unrecognized shape
                 (the database is readable; only the shape is unusual).

    Raises :class:`DatabaseInspectionError` if the database is corrupt
    or unreadable - an inspection failure is never reported as a
    successful schema classification. The underlying SQLite error is
    preserved as ``__cause__``.
    """
    try:
        user_version = conn.execute(
            "PRAGMA user_version").fetchone()[0]
        total_objects = conn.execute(
            "SELECT count(*) FROM sqlite_master").fetchone()[0]
        page_types = {
            name: typ
            for name, typ in conn.execute(
                "SELECT name, type FROM pragma_table_list"
                " WHERE name IN ('pages', 'pages_fts')")
        }
    except sqlite3.Error as exc:
        raise DatabaseInspectionError(
            f"cannot read database schema: {exc}") from exc

    pages_type = page_types.get("pages")
    fts_present = "pages_fts" in page_types

    if total_objects == 0:
        return {"kind": "empty", "user_version": user_version,
                "pages_type": None, "pages_fts_present": False,
                "note": "database file contains no objects"}
    if pages_type == "virtual":
        return {"kind": "v1", "user_version": user_version,
                "pages_type": "virtual", "pages_fts_present": fts_present,
                "note": "legacy standalone FTS5 table"}
    if pages_type == "table" and fts_present:
        return {"kind": "v2", "user_version": user_version,
                "pages_type": "table", "pages_fts_present": True,
                "note": "ordinary pages + external-content pages_fts"}
    if pages_type == "table":
        return {"kind": "unknown", "user_version": user_version,
                "pages_type": "table", "pages_fts_present": False,
                "note": "ordinary pages without pages_fts"}
    return {"kind": "unknown", "user_version": user_version,
            "pages_type": pages_type, "pages_fts_present": fts_present,
            "note": "no pages table"}


def inspect_database(db_path=None):
    """Migration preflight (M1): inspect a database, never modify it.

    Returns a JSON-safe report about the selected database: schema
    family, row counts, duplicate/canonical-collision/invalid-URL
    statistics with example rows, and rowid anomalies. This is the
    input for the Phase 4 migration decision and preview. It performs
    only SELECT/PRAGMA reads (read-only connection) and never writes to
    the database. All reads run inside one explicit read transaction, so
    every section of the report observes the same database snapshot.

    If the database is corrupt or unreadable, raises
    :class:`DatabaseInspectionError` (the underlying SQLite error is
    preserved as ``__cause__``). An inspection failure is never
    represented as a successful report with ``needs_migration=False``.

    Legacy v1 report fields:

      - rows.total / rows.distinct_urls / rows.min_rowid /
        rows.max_rowid
      - duplicates.exact_url_extra_rows: rows beyond the first sharing
        an identical URL string.
      - duplicates.exact_url_groups / .exact_url_examples
      - duplicates.canonical_conflict_groups: sets of rows whose
        canonical (normalized) URL is identical but whose raw URL
        strings differ (e.g. trailing-slash or default-port variants).
      - duplicates.canonical_conflict_examples
      - invalid_urls.count / .examples: rows whose URL fails
        normalize_url (malformed, non-http(s), etc.).
      - rowid_issues.count / .examples: rows with a rowid < 1.
      - summary.needs_migration: True for v1; reasons list the findings.
    """
    db_file = resolve_db_path(db_path)
    report = {
        "database": str(db_file),
        "exists": db_file.exists(),
        "schema": None,
        "rows": None,
        "duplicates": None,
        "invalid_urls": None,
        "rowid_issues": None,
        "summary": None,
    }

    if not db_file.exists():
        report["summary"] = {
            "needs_migration": False,
            "reasons": ["database file does not exist"],
        }
        return report

    conn = None
    try:
        conn = _connect_readonly(db_file)
        # One explicit read transaction: every section of the report
        # (schema detection, row data, derived statistics) observes the
        # same database snapshot, even if a concurrent writer commits
        # in the middle of the inspection.
        conn.execute("BEGIN")
        schema = detect_schema(conn)
        report["schema"] = schema

        if schema["kind"] != "v1":
            if schema["kind"] == "v2":
                reason = "already on the v2 schema (no migration needed)"
            else:
                reason = (f"schema kind {schema['kind']!r} is not a "
                          "legacy v1 database")
            report["summary"] = {
                "needs_migration": False,
                "reasons": [reason],
            }
            return report

        rows = conn.execute(
            "SELECT rowid, url FROM pages ORDER BY rowid"
        ).fetchall()
        total = len(rows)
        distinct_urls = len({url for _, url in rows})

        invalid = []
        valid = []  # (rowid, raw_url, canonical_url)
        for rowid, url in rows:
            canonical = normalize_url(url)
            if canonical is None:
                invalid.append((rowid, url))
            else:
                valid.append((rowid, url, canonical))

        by_url = {}
        for rowid, url, _ in valid:
            by_url.setdefault(url, []).append(rowid)
        exact_groups = {
            url: rids for url, rids in by_url.items() if len(rids) > 1
        }
        exact_extra_rows = sum(len(r) - 1 for r in exact_groups.values())

        by_canonical = {}
        for rowid, url, canonical in valid:
            by_canonical.setdefault(canonical, set()).add(url)
        canonical_groups = {
            canon: urls
            for canon, urls in by_canonical.items() if len(urls) > 1
        }

        rowid_issues = [(r, u) for r, u in rows if r < 1]

        report["rows"] = {
            "total": total,
            "distinct_urls": distinct_urls,
            "min_rowid": min((r for r, _ in rows), default=None),
            "max_rowid": max((r for r, _ in rows), default=None),
        }
        report["duplicates"] = {
            "exact_url_extra_rows": exact_extra_rows,
            "exact_url_groups": len(exact_groups),
            "exact_url_examples": [
                {"url": url, "rowids": rids[:10]}
                for url, rids in list(exact_groups.items())[:5]
            ],
            "canonical_conflict_groups": len(canonical_groups),
            "canonical_conflict_examples": [
                {"canonical": canon, "urls": sorted(urls)[:10]}
                for canon, urls in list(canonical_groups.items())[:5]
            ],
        }
        report["invalid_urls"] = {
            "count": len(invalid),
            "examples": [
                {"rowid": rowid, "url": url}
                for rowid, url in invalid[:10]
            ],
        }
        report["rowid_issues"] = {
            "count": len(rowid_issues),
            "examples": [
                {"rowid": rowid, "url": url}
                for rowid, url in rowid_issues[:10]
            ],
        }

        reasons = []
        if exact_extra_rows:
            reasons.append(
                f"{exact_extra_rows} extra row(s) sharing an exact URL")
        if canonical_groups:
            reasons.append(
                f"{len(canonical_groups)} canonical URL conflict group(s)")
        if invalid:
            reasons.append(f"{len(invalid)} invalid URL(s)")
        if rowid_issues:
            reasons.append(f"{len(rowid_issues)} rowid issue(s)")
        report["summary"] = {
            "needs_migration": True,
            "reasons": reasons or ["legacy v1 schema present"],
        }
        return report
    except sqlite3.Error as exc:
        raise DatabaseInspectionError(
            f"inspection failed: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()


class MigrationError(Exception):
    """Raised when a v1 -> v2 migration cannot proceed or fails.

    ``stages`` carries the migration-stage history accumulated up to the
    failure (matching the stage names in :func:`migrate_database`).
    """

    def __init__(self, message, stages=None):
        super().__init__(message)
        self.stages = stages


def _validate_migration(conn, expected_rows):
    """Validate a migrated v2 database (called inside the migration txn).

    Returns a list of problem descriptions (empty list = valid). All
    checks run BEFORE the migration transaction commits, so any failure
    rolls back atomically:

      - schema family is v2 (detect_schema)
      - PRAGMA user_version == SCHEMA_VERSION
      - pages is an ordinary table
      - pages_fts is the external-content FTS5 table
      - all three sync triggers exist (pages_ai / pages_ad / pages_au)
      - pages/pages_fts row counts equal expected_rows
      - no duplicate retained URLs
      - FTS5 integrity-check passes
    """
    problems = []
    try:
        schema = detect_schema(conn)
    except DatabaseInspectionError as exc:
        problems.append(f"cannot read schema during validation: {exc}")
        return problems

    if schema["kind"] != "v2":
        problems.append(
            f"schema kind is {schema['kind']!r}, expected 'v2'")
    if schema["user_version"] != SCHEMA_VERSION:
        problems.append(
            f"user_version is {schema['user_version']!r}, "
            f"expected {SCHEMA_VERSION}")
    if schema["pages_type"] != "table":
        problems.append(
            f"pages type is {schema['pages_type']!r}, expected ordinary table")
    if not schema["pages_fts_present"]:
        problems.append("pages_fts external-content table is missing")
    else:
        fts_row = conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE name='pages_fts' AND type='table'"
        ).fetchone()
        if (fts_row is None
                or "content='pages'" not in fts_row[0]
                or "content_rowid='rowid'" not in fts_row[0]):
            problems.append(
                "pages_fts is not the expected external-content table")

    present_triggers = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
        " AND name IN ('pages_ai', 'pages_ad', 'pages_au')")}
    for required in ("pages_ai", "pages_ad", "pages_au"):
        if required not in present_triggers:
            problems.append(f"required trigger {required} is missing")

    n_pages = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
    n_fts = conn.execute("SELECT count(*) FROM pages_fts").fetchone()[0]
    if n_pages != expected_rows or n_fts != expected_rows:
        problems.append(
            f"row counts differ: pages={n_pages} fts={n_fts} "
            f"expected={expected_rows}")

    duplicate_urls = conn.execute(
        "SELECT url, count(*) FROM pages GROUP BY url"
        " HAVING count(*) > 1 ORDER BY url LIMIT 5"
    ).fetchall()
    if duplicate_urls:
        problems.append(
            f"duplicate retained URLs present: {duplicate_urls}")

    integrity = conn.execute(
        "INSERT INTO pages_fts(pages_fts) VALUES('integrity-check')"
    ).fetchall()
    if integrity:
        problems.append(f"FTS integrity check failed: {integrity}")

    return problems


def _insert_retained_rows(conn, retained, chunk_size=200):
    """Copy retained rows into a v2 ``pages`` table in fixed chunks.

    Each insert fires the ``pages_ai`` AFTER INSERT trigger, which
    populates the external-content FTS index. The caller owns the
    transaction (the migration runs this inside its ``BEGIN IMMEDIATE``
    reservation). Extracted as a named function so failure-injection
    tests can target the copy loop precisely.
    """
    for i in range(0, len(retained), chunk_size):
        conn.executemany(
            "INSERT INTO pages "
            "(rowid, url, title, description, body) "
            "VALUES (?, ?, ?, ?, ?)",
            retained[i:i + chunk_size],
        )


def _migrate_on_connection(src, db_file, progress_callback=None,
                           reservation_held=False):
    """Run the v1 -> v2 migration on an already-open connection ``src``.

    Shared implementation used by :func:`migrate_database` (which opens
    its own connection and acquires the ``BEGIN IMMEDIATE`` write
    reservation) and by :func:`open_database` (which already holds the
    reservation when it discovers a legacy v1 schema).

    When ``reservation_held`` is True the caller already owns the write
    reservation on ``src``; this helper then skips acquiring another one.
    The under-reservation backup recheck still applies in both modes - an
    existing backup file aborts the migration (it is never silently
    overwritten; remove/rename it to retry).

    Behavior (identical to :func:`migrate_database`):

      - Detects the schema with :func:`detect_schema`.
      - Returns a ``status: "noop"`` report if already v2.
      - Refuses empty, unknown, or unreadable databases (raises
        :class:`MigrationError`; a corrupt DB is wrapped as such with its
        ``DatabaseInspectionError`` cause preserved).
      - Runs the entire migration under ONE ``BEGIN IMMEDIATE`` write
        reservation: the write reservation is acquired BEFORE the source
        data is read, the backup is created, or any DDL runs, so no
        concurrent writer can commit between the backup and the
        conversion. The backup (``<db>.pre-migrate.bak``) is created
        with SQLite's ``Connection.backup()`` API from a SEPARATE read
        connection (the reserved connection cannot be used for the
        backup), then verified to hold the exact pre-migration v1
        schema, row count, and full row content (rowid, url, title,
        description, body).
      - The schema/data conversion runs inside the same transaction:
        drop the v1 FTS5 virtual table, create the v2 ordinary ``pages``
        + external-content ``pages_fts`` (reusing
        :func:`create_schema_v2` / :func:`_create_fts_triggers`, which
        never commit), copy retained rows (chunked) preserving original
        rowids and letting the AFTER INSERT trigger populate the FTS
        index, then run ALL validation pre-commit via
        :func:`_validate_migration` (schema family, user_version, table
        shapes, triggers, row counts, duplicate URLs, FTS integrity),
        then COMMIT. Any failure ROLLBACKs, restoring the original v1
        database exactly; an error is never reported as a successful
        migration. There are no fallible post-commit checks - a
        committed migration is always reported as completed.
      - Deduplication is deterministic (see the priority rules below)
        and invalid URLs are never silently discarded.

    Progress reporting: ``progress_callback(stage, message)`` is invoked
    at each stage boundary (``backup``, ``inspection``, ``copying``,
    ``fts_population``, ``validation``, ``commit``, ``completed`` /
    ``failed``) and the full stage list is included in the returned
    report. Exceptions raised by the callback are always swallowed - a
    callback can never turn a committed migration into a failure report.

    Returns a report dict with a consistent field set:
    ``status`` (``"completed"`` / ``"noop"``), ``stages`` (list of
    (stage, message) tuples), ``stats`` (the migration statistics dict,
    or ``None`` for a noop), and ``database`` (the resolved database
    path). Failure is never returned as a report - it raises
    :class:`MigrationError` whose ``stages`` attribute records the
    stages reached and whose ``__cause__`` chains the original
    exception. Stats: original_rows, retained_rows, removed_duplicates,
    canonical_conflict_groups, invalid_encountered, invalid_retained.
    """
    stages = []

    def _report(stage, message):
        stages.append((stage, message))
        if progress_callback is not None:
            try:
                progress_callback(stage, message)
            except Exception:
                pass

    def _fail(message, cause=None):
        raise MigrationError(message, stages=list(stages)) from cause

    bak_path = pathlib.Path(str(db_file) + ".pre-migrate.bak")

    _report("inspection", "detecting schema")
    try:
        try:
            schema = detect_schema(src)
        except DatabaseInspectionError as exc:
            raise MigrationError(
                f"refusing to migrate unreadable database: {exc}",
                stages=list(stages),
            ) from exc

        if schema["kind"] == "v2":
            _report("completed", "already on v2 schema; nothing to migrate")
            return {
                "status": "noop",
                "stages": stages,
                "stats": None,
                "database": str(db_file),
            }

        if schema["kind"] != "v1":
            _fail(
                f"refusing to migrate database with schema kind "
                f"{schema['kind']!r} ({schema['note']})")

        if not reservation_held:
            # Quick pre-lock check: never overwrite an existing backup.
            if bak_path.exists():
                _fail(
                    f"backup already exists, refusing to overwrite it: "
                    f"{bak_path}")

        _report("backup", "creating pre-migrate backup")
        try:
            # 1. Acquire the write reservation FIRST (unless the caller
            # already holds it). No concurrent writer can commit
            # afterwards until we finish, so the backup and the converted
            # state both derive from exactly this pre-migration snapshot.
            if not reservation_held:
                src.execute("BEGIN IMMEDIATE")

            # Recheck under the reservation (the pre-lock check can race).
            if bak_path.exists():
                _fail(
                    f"backup already exists, refusing to overwrite it: "
                    f"{bak_path}")

            # 2. Read the complete source dataset while holding the
            # reservation, then build the deterministic retention plan.
            rows = src.execute(
                "SELECT rowid, url, title, description, body FROM pages "
                "ORDER BY rowid"
            ).fetchall()

            valid_groups = {}   # canonical -> list of row tuples
            invalid_by_raw = {}  # raw url -> list of row tuples
            for row in rows:
                rowid, url, _title, _desc, _body = row
                canonical = normalize_url(url)
                if canonical is None:
                    invalid_by_raw.setdefault(url, []).append(row)
                else:
                    valid_groups.setdefault(canonical, []).append(row)

            def _pick(candidates, canonical):
                best = None
                best_key = None
                for cand in candidates:
                    rowid, url, title, _desc, body = cand
                    url_is_canon = canonical is not None and url == canonical
                    full_content = bool(title) and bool(body)
                    key = (not url_is_canon, not full_content,
                           -len(body or ""), rowid)
                    if best_key is None or key < best_key:
                        best, best_key = cand, key
                return best

            retained = []
            canonical_groups = 0
            for canonical, candidates in valid_groups.items():
                if len({c[1] for c in candidates}) > 1:
                    canonical_groups += 1
                retained.append(_pick(candidates, canonical))
            for raw_url, candidates in invalid_by_raw.items():
                retained.append(_pick(candidates, None))

            retained.sort(key=lambda r: r[0])  # deterministic rowid order

            invalid_encountered = sum(
                len(c) for c in invalid_by_raw.values())
            invalid_retained = len(invalid_by_raw)
            original_rows = len(rows)
            retained_rows = len(retained)
            stats = {
                "original_rows": original_rows,
                "retained_rows": retained_rows,
                "removed_duplicates": original_rows - retained_rows,
                "canonical_conflict_groups": canonical_groups,
                "invalid_encountered": invalid_encountered,
                "invalid_retained": invalid_retained,
            }

            # 3. Backup via a SEPARATE read connection. `src` holds the
            # write reservation (begin, no writes yet), so this snapshot is
            # exactly the pre-migration state being converted.
            backup_src = None
            try:
                bak_conn = sqlite3.connect(str(bak_path))
                backup_src = sqlite3.connect(str(db_file))
                try:
                    backup_src.backup(bak_conn)
                finally:
                    bak_conn.close()

                # 4. Verify backup: v1 schema, row count, and exact full
                # row content (rowid, url, title, description, body) of
                # the pre-migration source.
                bak_check = sqlite3.connect(str(bak_path))
                try:
                    bak_schema = detect_schema(bak_check)
                    bak_rows = bak_check.execute(
                        "SELECT rowid, url, title, description, body "
                        "FROM pages ORDER BY rowid"
                    ).fetchall()
                finally:
                    bak_check.close()
                if (bak_schema["kind"] != "v1"
                        or len(bak_rows) != original_rows
                        or bak_rows != rows):
                    try:
                        os.remove(bak_path)
                    except OSError:
                        pass
                    _fail(
                        f"backup verification failed (schema={bak_schema['kind']}, "
                        f"rows={len(bak_rows)}, expected {original_rows}); "
                        f"partial backup removed")
            except MigrationError:
                raise
            except (sqlite3.Error, OSError) as exc:
                try:
                    os.remove(bak_path)
                except OSError:
                    pass
                _fail(f"backup creation/verification failed: {exc}")
            finally:
                if backup_src is not None:
                    backup_src.close()
            _report("backup", "backup created and verified")

            # 5. Schema conversion + chunked copy (same transaction). Any
            # failure here is standardized to MigrationError with the
            # original cause chained (exception consistency).
            _report("copying", "converting schema and copying retained rows")
            try:
                src.execute("DROP TABLE pages")
                src.execute("DROP TABLE IF EXISTS pages_fts")
                create_schema_v2(src)
                _insert_retained_rows(src, retained)
            except Exception as exc:
                _fail(f"conversion failed: {exc}", cause=exc)
            _report("fts_population",
                    "FTS index populated via AFTER INSERT triggers")

            # 6. All validation happens before COMMIT.
            _report("validation", "validating migrated schema and index")
            try:
                problems = _validate_migration(src, retained_rows)
            except Exception as exc:
                _fail(f"validation query failed: {exc}", cause=exc)
            if problems:
                _fail(f"validation failed: {'; '.join(problems)}")

            # 7. Record the commit phase, then COMMIT. A failed COMMIT is
            # still pre-commit (the transaction is rolled back) and is
            # standardized the same way.
            _report("commit", "committing migration")
            try:
                src.commit()
            except Exception as exc:
                _fail(f"commit failed: {exc}", cause=exc)
        except BaseException:
            try:
                src.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        _report("completed", "migration completed")
        return {
            "status": "completed",
            "stages": stages,
            "stats": stats,
            "database": str(db_file),
        }
    except BaseException:
        raise


def migrate_database(db_path=None, progress_callback=None):
    """Migrate a legacy v1 database to the v2 schema (Phase 4 M2).

    Standalone entry point over :func:`_migrate_on_connection`: opens the
    selected database, acquires the ``BEGIN IMMEDIATE`` write reservation
    itself, runs the migration, and closes the connection. See
    :func:`_migrate_on_connection` for the full behavior, progress
    reporting, and return/failure contract.
    """
    db_file = resolve_db_path(db_path)
    if not db_file.exists():
        raise MigrationError(
            f"database file does not exist: {db_file}", stages=[])
    src = sqlite3.connect(str(db_file))
    src.execute("PRAGMA busy_timeout=5000")
    try:
        return _migrate_on_connection(
            src, db_file, progress_callback=progress_callback)
    finally:
        src.close()


# In-process per-path locks for open_database: migrations of the same
# database file are serialized within this process, while different
# paths never share a lock (so opening one database never blocks opening
# another).
_open_locks = {}
_open_locks_guard = threading.Lock()

# How long open_database waits for a concurrent writer to release the
# BEGIN IMMEDIATE reservation before giving up. Cross-process migrations
# legitimately take seconds to minutes for large databases, so this is
# deliberately generous (the wait is a blocking SQLite busy-timeout, not
# a poll loop). A peer that crashes mid-migration is detected right after
# we acquire the reservation: the schema is still v1 and a stale backup
# file is present, which is refused (never overwritten) - see
# _migrate_on_connection.
_OPEN_RESERVATION_TIMEOUT_MS = 180_000


def _db_path_lock(db_file):
    """Return the per-path lock that serializes opens of db_file."""
    with _open_locks_guard:
        lock = _open_locks.get(db_file)
        if lock is None:
            lock = threading.Lock()
            _open_locks[db_file] = lock
        return lock


def open_database(db_path=None, progress_callback=None):
    """Open the production database at db_path (default DB_PATH).

    Single internal choke point for opening a database for crawling or
    searching. Behavior:

      - Missing file -> a fresh **v2** database is created directly
        (never a v1 database that would then need migrating).
      - Existing v2 -> opened normally (no migration).
      - Existing v1 -> migrated automatically (migrate_database), then
        opened.
      - Empty / unknown / unsupported -> refused with MigrationError.
      - Corrupt -> DatabaseInspectionError propagates, with its original
        SQLite cause preserved as ``__cause__``.
      - Migration failure -> MigrationError propagates (stages + chained
        cause). There is never a fallback to v1.

    Concurrency:

      - In-process: a per-path lock serializes opens/migrations of the
        same database file (different paths never share a lock).
      - Cross-process: migration uses SQLite's ``BEGIN IMMEDIATE`` write
        reservation. open_database acquires that reservation itself and
        reclassifies the schema INSIDE it: a competing opener blocks (up
        to ``_OPEN_RESERVATION_TIMEOUT_MS``) until a slow peer migration
        commits, then opens the now-v2 database normally; if the peer
        crashed, the reservation is acquired immediately and the leftover
        backup file is refused rather than overwritten. No lockfiles are
        used.

    Fresh v2 creation runs inside one transaction (the DDL is
    idempotent, so concurrent creation is safe), and the returned
    connection carries the same PRAGMAs as get_database: WAL journal
    mode, synchronous=FULL, busy_timeout=5000.
    """
    db_file = resolve_db_path(db_path)

    with _db_path_lock(db_file):
        if not db_file.exists():
            # Fresh v2 creation: transactional (one BEGIN IMMEDIATE ...
            # COMMIT), so a crash leaves an empty file rather than a
            # partially-initialized schema, and idempotent under
            # concurrent creation by another process.
            # The parent directory may not exist yet (e.g. a relative
            # db name like "data/foo.db" anchored next to main.py) —
            # create it so the crawl can populate the database instead
            # of failing on a missing directory.
            db_file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_file))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    create_schema_v2(conn)
                    conn.commit()
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            except BaseException:
                conn.close()
                # This call created the file; now that the connection is
                # closed (Windows holds the handle otherwise), remove it
                # and any WAL sidecars so a later open retries from
                # scratch instead of hitting the empty-file refusal.
                for suffix in ("", "-wal", "-shm"):
                    try:
                        pathlib.Path(str(db_file) + suffix).unlink()
                    except OSError:
                        pass
                raise
            except BaseException:
                conn.close()
                raise
            return conn

        probe = None
        try:
            probe = _connect_readonly(db_file)
            schema = detect_schema(probe)
        finally:
            if probe is not None:
                probe.close()

        if schema["kind"] == "v1":
            # Acquire the write reservation FIRST, then reclassify inside
            # it. If another process is mid-migration it holds the
            # reservation, so this BEGIN blocks - up to
            # _OPEN_RESERVATION_TIMEOUT_MS - until the peer commits: a
            # slow, still-progressing migration is waited out rather than
            # abandoned after a fixed retry window. Once acquired the
            # schema is authoritative: v2 means the peer finished (open
            # normally); v1 means we are the sole writer, so migrate here.
            conn = sqlite3.connect(str(db_file))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute(
                    f"PRAGMA busy_timeout={_OPEN_RESERVATION_TIMEOUT_MS}")
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                conn.close()
                raise MigrationError(
                    f"timed out waiting for a concurrent database "
                    f"operation/migration to finish: {exc}",
                    stages=[]) from exc
            except BaseException:
                conn.close()
                raise

            try:
                schema = detect_schema(conn)
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                conn.close()
                raise

            if schema["kind"] == "v2":
                # A concurrent process completed the migration while we
                # waited; release the reservation and open normally.
                conn.execute("ROLLBACK")
                return conn

            if schema["kind"] != "v1":
                conn.execute("ROLLBACK")
                conn.close()
                raise MigrationError(
                    f"refusing to open unsupported database "
                    f"(schema kind {schema['kind']!r}: {schema['note']})",
                    stages=[])

            # Still v1 under our reservation: migrate on this connection,
            # then hand it back (committed, v2).
            try:
                _migrate_on_connection(
                    conn, db_file,
                    progress_callback=progress_callback,
                    reservation_held=True)
            except BaseException:
                conn.close()
                raise
            return conn

        if schema["kind"] != "v2":
            raise MigrationError(
                f"refusing to open unsupported database "
                f"(schema kind {schema['kind']!r}: {schema['note']})",
                stages=[])

        conn = sqlite3.connect(str(db_file))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            conn.close()
            raise
        return conn


class DatabaseNotFoundError(Exception):
    """Raised when the database file selected for a search does not exist.

    Distinct from sqlite3.Error so callers (e.g. the Flask API in
    app.py) can map "the requested database is missing" to a specific
    response without conflating it with a corrupt or failing database.
    """


def normalize_url(url):
    """Return the canonical form of an HTTP(S) URL, or None if unusable.

    Canonicalization is what makes URL-based deduplication work: two
    spellings of the same page collapse to one string, so the crawl
    frontier ('queued'/'visited') and the database upsert both treat
    them as a single URL, and rows already stored are merged by
    deduplicate_database().

    The rules (equivalent spellings map to the same canonical URL):

        example.com/page#section == example.com/page   (fragments are
                                                        never sent)
        EXAMPLE.com/page         == example.com/page   (host lowercase;
                                                        a trailing dot
                                                        is also dropped:
                                                        example.com. ==
                                                        example.com)
        https://example.com:443/page == https://example.com/page
                                                       (scheme-default
                                                        ports stripped)
        example.com/page/        == example.com/page   (trailing slashes
                                                        stripped; the
                                                        root "/" stays)

    while distinct resources stay distinct:

        example.com/page?id=1  != example.com/page?id=2 (queries kept)
        https://example.com/p  != http://example.com/p  (schemes kept)
        example.com/page1      != example.com/page2     (paths kept,
                                                         case-sensitive)

    Unusable URLs return None: unsupported schemes, userinfo
    (@user:pass), malformed URLs (e.g. unmatched IPv6 brackets) or
    out-of-range ports, and (when the optional ALLOWED_PORTS policy is
    set) disallowed ports.
    """
    if not isinstance(url, str) or not url:
        return None

    try:
        url, _ = urldefrag(url)
        parsed = urlparse(url)
    except (ValueError, TypeError):
        # Malformed URLs (e.g. unmatched IPv6 brackets like
        # "http://[::1") raise inside urldefrag()/urlparse(); fail
        # closed and treat them as unusable instead of crashing the
        # caller.
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    if parsed.username or parsed.password:
        return None

    # .hostname is already lowercase; a trailing dot is the FQDN form
    # of the same host (example.com. == example.com).
    hostname = (parsed.hostname or "").rstrip(".")
    if not hostname:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None

    if (ALLOWED_PORTS is not None
            and port is not None
            and port not in ALLOWED_PORTS):
        return None

    if port is not None and STANDARD_PORTS.get(parsed.scheme) == port:
        port = None

    host_part = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_part if port is None else f"{host_part}:{port}"

    # "" and "/page/" and "/page" are one page; keep the bare root as
    # "/" so every canonical URL still has a path.
    path = (parsed.path or "/").rstrip("/") or "/"

    return urlunparse((
        parsed.scheme, netloc, path,
        parsed.params, parsed.query, "",
    ))


def deduplicate_database(conn):
    """Merge stored rows whose URLs canonicalize to the same page.

    The database is keyed by URL, so a crawl must never hold two rows
    for one page just because they were stored with different
    spellings (".../page" and ".../page/"). This walks the stored
    URLs through normalize_url() and, for every group of equivalent
    URLs, keeps exactly one row: a row already stored in canonical
    form is preferred, otherwise the first-seen row survives and is
    relabeled to the canonical spelling. Genuinely different URLs
    (?id=1 vs ?id=2, http vs https, page1 vs page2) are untouched.

    Returns the number of duplicate rows removed.
    """
    rows = conn.execute("SELECT rowid, url FROM pages").fetchall()

    keep = {}          # canonical url -> (rowid, already canonical?)
    removed = 0

    for rowid, url in rows:
        canonical = normalize_url(url)

        if canonical is None:
            # Legacy non-HTTP row: leave it alone.
            continue

        if canonical not in keep:
            keep[canonical] = (rowid, url == canonical)
            continue

        kept_rowid, kept_is_canonical = keep[canonical]

        if url == canonical and not kept_is_canonical:
            # Prefer the row that already has the canonical spelling.
            conn.execute(
                "DELETE FROM pages WHERE rowid = ?", (kept_rowid,))
            keep[canonical] = (rowid, True)
        else:
            conn.execute(
                "DELETE FROM pages WHERE rowid = ?", (rowid,))

        removed += 1

    # Relabel survivors so a later crawl's upsert finds them.
    changed = removed

    for canonical, (rowid, is_canonical) in keep.items():
        if not is_canonical:
            conn.execute(
                "UPDATE pages SET url = ? WHERE rowid = ?",
                (canonical, rowid))
            changed += 1

    if changed:
        conn.commit()

    return removed


def parse_scope_entry(entry):
    """Parse one allowed entry: 'host' or 'host/path' (scheme tolerated).

    Returns a (host, path_prefix) rule, or None when the entry is
    unusable. A bare host allows that host and all its subdomains
    anywhere on the site. A host/path entry limits the host to that
    path subtree: 'example.com/page' allows example.com/page/quote but
    not example.com/page2 (the prefix must end at a '/' boundary).
    """
    if not entry:
        return None

    entry = str(entry).strip().lower()

    if "://" in entry:
        scheme, _, entry = entry.partition("://")
        if scheme not in ("http", "https"):
            return None

    host, slash, path = entry.partition("/")
    host = host.strip().rstrip(".")

    if not host or ":" in host or any(c.isspace() for c in host):
        return None

    path = path.strip("/") if slash else ""
    prefix = f"/{path}" if path else ""

    return host, prefix


def get_scope(seed_url, extra=()):
    """Build the crawl scope: the seed's host plus extra --allow entries.

    The seed contributes its whole host (all subdomains, all paths);
    each extra entry contributes one rule per parse_scope_entry.
    Invalid extras are skipped leniently so library callers never
    crash on a bad string.
    """
    hostname = urlparse(seed_url).hostname

    if not hostname:
        return ()

    scope = [(hostname.lower(), "")]

    for entry in extra:
        rule = parse_scope_entry(entry)
        if rule is not None:
            scope.append(rule)

    return tuple(scope)


def host_of(url):
    """Pacing key for a URL: the hostname only.

    robots.txt is per origin (scheme://host:port), but politeness is
    per machine: one hostname on several ports shares a single rate
    limit, so the crawler cannot hammer a server by alternating ports.
    """
    return (urlparse(url).hostname or "").lower()


def origin_of(url):
    """Origin key for robots.txt: scheme + hostname + effective port.

    Two schemes can serve completely different robots policies, so
    http://example.com and https://example.com must never share
    cached rules; neither may the same host on another port. Default
    ports fold into their scheme (http://x:80 == http://x).
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    try:
        port = parsed.port
    except ValueError:
        port = None

    if port is None:
        port = STANDARD_PORTS.get(scheme, "")

    return f"{scheme}://{hostname}:{port}"


def is_allowed_url(url, scope):
    """True when the URL matches any scope rule.

    A rule (host, prefix) matches when the URL's host is that host or
    a subdomain of it and, when the rule carries a path prefix, the
    URL's path equals the prefix or continues below it
    ('/page' matches '/page' and '/page/quote' but not '/page2').
    """
    if not url:
        return False

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path or "/"

    for host, prefix in scope:
        if not (hostname == host or hostname.endswith("." + host)):
            continue

        if (not prefix or path == prefix
                or path.startswith(prefix + "/")):
            return True

    return False


class RobotsCache:
    """Thread-safe per-origin robots.txt cache.

    The HTTP fetch happens OUTSIDE the lock: holding a lock across a
    network round-trip would let one slow robots.txt stall every
    worker, even ones crawling entirely different hosts. Only the tiny
    cache reads/writes are locked, and published rules are immutable,
    so can_fetch/crawl_delay need no locking at all. A cold cache
    raced by several workers may fetch the same robots.txt a bounded
    number of times; correctness is unaffected.
    """

    def __init__(self):
        self._rules = {}
        self._failures = {}
        self._raw = {}
        self._lock = threading.Lock()

    def rules_for(self, session, url, limiter=None):
        """Return parsed rules for the URL's origin, or None.

        None means robots.txt could not be verified right now; the
        caller skips the URL. Failures are remembered only briefly
        (ROBOTS_RETRY_SECONDS) so a transient outage never excludes a
        host permanently.
        """
        origin = origin_of(url)
        parsed = urlparse(url)

        with self._lock:
            if origin in self._rules:
                return self._rules[origin]

            failed_at = self._failures.get(origin)

            if failed_at is not None:
                if (time.monotonic() - failed_at
                        < ROBOTS_RETRY_SECONDS):
                    return None
                del self._failures[origin]

        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

        if limiter is not None:
            limiter.wait(host_of(url))

        try:
            response = session.get(robots_url, timeout=15)

            try:
                if response.status_code == 404:
                    rules = RobotFileParser()
                    rules.parse([])
                elif response.status_code == 200:
                    rules = RobotFileParser()
                    rules.parse(response.text.splitlines())
                else:
                    print(
                        f"Cannot verify robots.txt for {origin}; "
                        "will retry later."
                    )
                    self._note_failure(origin)
                    return None
            finally:
                response.close()

        except requests.RequestException:
            print(
                f"Cannot retrieve robots.txt for {origin}; "
                "will retry later."
            )
            self._note_failure(origin)
            return None

        raw = response.text if response.status_code == 200 else ""

        with self._lock:
            self._rules[origin] = rules
            self._raw[origin] = raw
            self._failures.pop(origin, None)

        return rules

    def sitemaps(self, session, url, limiter=None):
        """Return Sitemap URLs declared in robots.txt for url's origin.

        An empty list means robots.txt declared none (or could not be
        verified); callers then fall back to the common sitemap
        locations. Reuses the same per-origin robots cache as
        rules_for, so discovery never fetches robots.txt twice.
        """
        origin = origin_of(url)

        with self._lock:
            raw = self._raw.get(origin)

        if raw is None:
            rules = self.rules_for(session, url, limiter)

            if rules is None:
                return []

            if limiter is not None:
                crawl_delay = rules.crawl_delay(USER_AGENT)

                if crawl_delay is not None:
                    limiter.set_min_delay(host_of(url), crawl_delay)

            with self._lock:
                raw = self._raw.get(origin, "")

        declared = []

        for line in raw.splitlines():
            line = line.strip()

            if line.lower().startswith("sitemap:"):
                value = line[8:].strip()

                if value:
                    declared.append(value)

        return declared

    def _note_failure(self, origin):
        with self._lock:
            self._failures[origin] = time.monotonic()


def parse_sitemap_xml(data):
    """Parse sitemap XML bytes -> (kind, locs) or None.

    kind is "urlset" (page URLs) or "sitemapindex" (child sitemap
    URLs); locs are the <loc> element texts in document order. Both
    plain and namespaced sitemaps are accepted ({*} matches any
    namespace). Returns None when data is not well-formed sitemap XML.
    """
    if not data:
        return None

    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    tag = root.tag

    if tag.endswith("}urlset") or tag == "urlset":
        kind = "urlset"
    elif tag.endswith("}sitemapindex") or tag == "sitemapindex":
        kind = "sitemapindex"
    else:
        return None

    locs = [
        el.text.strip()
        for el in root.findall(".//{*}loc")
        if el.text and el.text.strip()
    ]

    return kind, locs


def fetch_sitemap(session, url, limiter, max_bytes=DEFAULT_MAX_BYTES):
    """Download one sitemap document; return its bytes, or None."""
    limiter.wait(host_of(url))

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException:
        return None

    try:
        if response.status_code != 200:
            return None

        chunks = []
        total = 0

        try:
            for chunk in response.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)

                if total > max_bytes:
                    return None
        except requests.RequestException:
            return None

        return b"".join(chunks)
    finally:
        response.close()


def collect_sitemap_urls(session, sitemap_url, scope, limiter, depth,
                         seen, page_urls, sources):
    """Fetch one sitemap document and recurse through index children.

    Populates `page_urls` (normalized, scope-filtered page URLs) and
    `sources` (every sitemap document successfully parsed). `seen`
    guards against cycles; `depth` bounds recursion. Out-of-scope
    sitemap documents (declared on other hosts) are skipped, keeping
    the crawler inside its allowed scope.
    """
    if depth > MAX_SITEMAP_DEPTH or sitemap_url in seen:
        return

    seen.add(sitemap_url)

    data = fetch_sitemap(session, sitemap_url, limiter)
    parsed = parse_sitemap_xml(data) if data else None

    if parsed is None:
        return

    kind, locs = parsed
    sources.append(sitemap_url)

    if kind == "urlset":
        for loc in locs:
            child = normalize_url(urljoin(sitemap_url, loc))

            if child and is_allowed_url(child, scope):
                page_urls.add(child)
    else:  # sitemapindex
        for loc in locs:
            child = normalize_url(urljoin(sitemap_url, loc))

            if child and is_allowed_url(child, scope):
                collect_sitemap_urls(
                    session, child, scope, limiter,
                    depth + 1, seen, page_urls, sources
                )


def discover_sitemaps(session, seed_url, scope, limiter, robots_cache):
    """Discover the seed site's sitemaps; return (page_urls, sources).

    Order of discovery: robots.txt Sitemap declarations first; when
    robots.txt declares none, probe the common locations
    (SITEMAP_COMMON_PATHS). Every declared Sitemap line is honored and
    parsed — the first success does not hide the rest — while the
    common-location fallback stops at the first success. Both
    <urlset> and <sitemapindex> documents are parsed; index children
    are fetched recursively (bounded). Returned page URLs are
    normalized and filtered to the crawl scope. `sources` lists every
    sitemap document successfully parsed.
    """
    parsed = urlparse(seed_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    declared = (
        robots_cache.sitemaps(session, seed_url, limiter)
        if robots_cache else []
    )

    seen = set()
    page_urls = set()
    sources = []

    def collect(candidate):
        if not is_allowed_url(candidate, scope):
            return False

        collect_sitemap_urls(
            session, candidate, scope, limiter, 0,
            seen, page_urls, sources
        )

        return bool(sources)

    if declared:
        for u in declared:
            candidate = normalize_url(urljoin(seed_url, u))

            if candidate:
                collect(candidate)
    else:
        for p in SITEMAP_COMMON_PATHS:
            if collect(base + p):
                break

    return sorted(page_urls), sources


def parse_retry_after(value):
    """Parse Retry-After as seconds or an HTTP date."""
    if not value:
        return None

    value = value.strip()

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_html(session, url, max_bytes, scope=None,
               limiter=None, robots_cache=None):
    """Download a bounded HTML response, following redirects manually.

    The scope AND robots.txt of every redirect destination are checked
    BEFORE the hop is followed: an out-of-scope target, a disallowed
    path, or an origin whose robots.txt cannot be verified stops the
    chain without fetching the target - the old auto-redirect behavior
    downloaded the other site's page first and only then discarded it.
    Each hop is paced by the limiter, and chains stop after
    MAX_REDIRECTS hops (loops are abandoned, not followed forever).
    Robots checks go through robots_cache, so a chain that stays on
    one origin never fetches robots.txt twice, and workers sharing the
    cache never hold its lock across network I/O (no deadlocks, and no
    duplicate fetch storms beyond a bounded cold-cache race).

    Returns (response, text, final_url, reason). final_url is None
    when the chain was abandoned; reason then says why:
    "out_of_scope" (scope violation, invalid Location, or too many
    hops), "robots_blocked", or "robots_unavailable". Otherwise reason
    is None, and text is None only when the body was skipped (non-200,
    non-HTML, or larger than max_bytes). The caller must close the
    response.
    """
    current = url
    response = None

    try:
        for _ in range(MAX_REDIRECTS + 1):
            if limiter is not None:
                limiter.wait(host_of(current))

            try:
                response = session.get(
                    current,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    stream=True
                )
            except (ValueError, TypeError):
                # requests still resolves redirects even with
                # allow_redirects=False (it validates the Location via
                # urlparse), so a malformed Location (e.g. unmatched
                # IPv6 brackets) raises here. Abandon the chain safely.
                return None, None, None, "out_of_scope"

            if response.status_code not in (
                    301, 302, 303, 307, 308):
                break

            location = response.headers.get("Location")
            response.close()
            response = None

            try:
                raw_next = (
                    urljoin(current, location) if location else None
                )
            except (ValueError, TypeError):
                # A malformed Location (e.g. unmatched IPv6 brackets)
                # can make urljoin raise; abandon the chain safely.
                raw_next = None

            if not raw_next:
                return None, None, None, "out_of_scope"

            # Check scope against the canonical (normalized) form, but
            # follow the exact Location for the next hop: some servers
            # 308 to a canonical trailing slash, and if we stripped it
            # the next hop would land on the same URL again and the
            # chain would loop until MAX_REDIRECTS. A target that
            # cannot be normalized (None) is out of scope.
            next_url = normalize_url(raw_next)

            if not next_url or (
                    scope is not None
                    and not is_allowed_url(
                        next_url, scope)):
                return None, None, None, "out_of_scope"

            # Verify the destination's robots rules before following
            # the hop - never fetch a target we may not crawl. The
            # cache makes this cheap for same-origin chains. Rules are
            # checked against the EXACT URL we will fetch (raw_next):
            # /private and /private/ are distinct paths under robots.txt
            # (e.g. with "$" exact-match rules), so checking the
            # normalized form would evaluate the wrong path.
            if robots_cache is not None:
                robots = robots_cache.rules_for(
                    session, raw_next, limiter)

                if robots is None:
                    return (None, None, None,
                            "robots_unavailable")

                crawl_delay = robots.crawl_delay(USER_AGENT)

                if crawl_delay and limiter is not None:
                    limiter.set_min_delay(
                        host_of(raw_next), crawl_delay)

                if not robots.can_fetch(USER_AGENT, raw_next):
                    return None, None, None, "robots_blocked"

            # Follow the exact Location (keeping any canonical trailing
            # slash) so the server does not redirect again; the scope
            # check used the normalized form, robots the exact URL.
            current = raw_next
        else:
            # Still redirecting after MAX_REDIRECTS hops.
            return None, None, None, "out_of_scope"

        if response.status_code != 200:
            return response, None, current, None

        if "text/html" not in response.headers.get(
            "Content-Type", ""
        ).lower():
            return response, None, current, None

        try:
            content_length = int(
                response.headers.get("Content-Length", "")
            )
        except ValueError:
            content_length = None

        if content_length is not None and content_length > max_bytes:
            return response, None, current, None

        chunks = []
        total = 0

        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)

            if total > max_bytes:
                return response, None, current, None

            chunks.append(chunk)

        encoding = (
            response.encoding
            or response.apparent_encoding
            or "utf-8"
        )

        html = b"".join(chunks).decode(
            encoding, errors="replace"
        )
        return response, html, current, None

    except requests.RequestException:
        if response is not None:
            response.close()
        raise

# Each worker gets its own HTTP session.
_thread_local = threading.local()


def get_session():
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _thread_local.session = session

    return _thread_local.session


def process_url(
    url,
    scope,
    max_bytes,
    limiter,
    robots_cache,
):
    """Fetch and parse one URL in a worker thread."""
    result = {
        "url": url,
        "final_url": None,
        "title": "",
        "description": "",
        "body": "",
        "links": [],
        "status": "error",
        "retry_after": None,
        "message": None,
    }

    if not is_allowed_url(url, scope):
        result["status"] = "out_of_scope"
        return result

    session = get_session()
    host = host_of(url)

    # The robots check no longer runs under a lock: RobotsCache fetches
    # robots.txt outside its lock (fetching under it would let one slow
    # robots.txt stall every worker, even for unrelated hosts), and
    # published rules are immutable, so they are safe to read freely.
    robots = robots_cache.rules_for(session, url, limiter)

    if robots is None:
        result["status"] = "robots_unavailable"
        return result

    crawl_delay = robots.crawl_delay(USER_AGENT)

    if crawl_delay:
        limiter.set_min_delay(host, crawl_delay)

    if not robots.can_fetch(USER_AGENT, url):
        result["status"] = "robots_blocked"
        result["message"] = f"Blocked by robots.txt: {url}"
        return result

    response = None

    try:
        # fetch_html paces and robots-checks every hop itself before
        # following it.
        response, html, final_url, abandon_reason = fetch_html(
            session, url, max_bytes, scope, limiter, robots_cache
        )

        if final_url is None:
            # Map the abandon reason to its own status so the stats
            # stay honest. At least one request (the hop that
            # redirected) was already made, so count it.
            if abandon_reason == "robots_blocked":
                result["status"] = "robots_blocked"
                result["message"] = (
                    f"Redirect target blocked by robots.txt: {url}"
                )
            elif abandon_reason == "robots_unavailable":
                result["status"] = "robots_unavailable"
                result["message"] = (
                    "Redirect target robots.txt could not be "
                    f"verified: {url}"
                )
            else:
                result["status"] = "out_of_scope"
                result["message"] = (
                    "Redirect left the allowed scope "
                    f"(or looped); skipping: {url}"
                )
            result["requests_made"] = True
            return result

        if response.status_code in (429, 503):
            result["status"] = "rate_limited"
            retry_after = parse_retry_after(
                response.headers.get("Retry-After")
            )
            result["retry_after"] = retry_after

            if retry_after is not None:
                # A temporary pause, not a new permanent floor: once
                # it elapses, normal pacing resumes. The crawl loop
                # requeues this URL for exactly one retry.
                limiter.pause_host(host, retry_after)
                result["message"] = (
                    f"HTTP {response.status_code}; honoring "
                    f"Retry-After ({retry_after:.0f}s): {url}"
                )
            else:
                result["message"] = (
                    f"HTTP {response.status_code}: {url}"
                )
            return result

        if response.status_code != 200:
            result["status"] = "http_error"
            result["message"] = (
                f"HTTP {response.status_code}: {url}"
            )
            return result

        result["final_url"] = normalize_url(final_url)

        if "text/html" not in response.headers.get(
            "Content-Type", ""
        ).lower():
            result["status"] = "non_html"
            return result

        if html is None:
            result["status"] = "too_large"
            return result

        soup = BeautifulSoup(html, "html.parser")

        # Discover links before removing navigation elements.
        for link in soup.find_all("a", href=True):
            next_url = normalize_url(
                urljoin(final_url, link["href"])
            )

            if not next_url:
                continue

            if not is_allowed_url(next_url, scope):
                continue

            if urlparse(next_url).path.lower().endswith(
                SKIP_EXTENSIONS
            ):
                continue

            result["links"].append(next_url)

        for tag in soup([
            "script", "style", "noscript",
            "svg", "nav", "footer"
        ]):
            tag.decompose()

        result["title"] = (
            soup.title.get_text(" ", strip=True)
            if soup.title else ""
        )

        meta = soup.find(
            "meta",
            attrs={"name": re.compile("^description$", re.I)}
        )

        result["description"] = (
            meta.get("content", "").strip()
            if meta else ""
        )

        result["body"] = soup.get_text(" ", strip=True)[:100000]

        if not result["title"] and not result["body"]:
            result["status"] = "not_indexable"
            return result

        result["status"] = "success"
        return result

    except requests.Timeout as error:
        # Timeouts get their own counter: they usually mean the site
        # is slow or gone, not that the request was malformed.
        result["status"] = "timeout"
        result["message"] = f"Request timed out: {url} ({error})"
        return result

    except requests.RequestException as error:
        result["status"] = "network_error"
        result["message"] = f"Request failed: {url} ({error})"
        return result

    except Exception as error:
        # Parsing/processing failures (BeautifulSoup etc.).
        result["status"] = "parse_error"
        result["message"] = f"Processing failed: {url} ({error})"
        return result

    finally:
        if response is not None:
            response.close()


def crawl(
    start_url,
    max_pages=None,
    delay=DEFAULT_DELAY,
    max_bytes=DEFAULT_MAX_BYTES,
    threads=DEFAULT_THREADS,
    allowed=(),
    sitemap="auto",
    db_path=None,
    progress_callback=None,
    stop_event=None,
):
    """Crawl concurrently; max_pages=None means unlimited.

    The crawl is scoped to the seed's hostname and its subdomains,
    plus any extra `--allow` entries (bare hosts, or host/path
    prefixes that limit a site to one subtree).

    `sitemap` selects discovery behavior: "auto" (default) seeds the
    queue from the site's sitemap when one exists, "skip" ignores
    sitemaps entirely, and "only" crawls sitemap URLs without
    following page links (aborting when no sitemap exists).

    `progress_callback`, when given, is called every
    PROGRESS_INTERVAL seconds during the crawl with a JSON-safe
    snapshot of the current stats dict. The callback runs on a
    background thread and must not block for long; exceptions raised
    by it are swallowed so a broken callback never breaks the crawl.

    `stop_event`, when given, is a threading.Event. Once set, the
    crawl stops gracefully - like Ctrl+C - between units of work (the
    currently in-flight page fetches finish, queued work is cancelled,
    and `stats["interrupted"]` becomes True). Both the HTTP stop API
    (app.py) and the CLI's KeyboardInterrupt handler share this stop
    path.
    """
    start_url = normalize_url(start_url)
    scope = (
        get_scope(start_url, allowed) if start_url else ()
    )

    if not start_url or not scope:
        print("Start URL is not a valid, crawlable http(s) URL.")
        return None

    if max_pages is not None and max_pages < 1:
        print("max_pages must be at least 1.")
        return None

    if delay < 0:
        print("delay cannot be negative.")
        return None

    if max_bytes < 1:
        print("max_bytes must be positive.")
        return None

    if threads < 1:
        print("threads must be at least 1.")
        return None

    if sitemap not in ("auto", "skip", "only"):
        print("sitemap must be 'auto', 'skip', or 'only'.")
        return None

    started = time.perf_counter()

    stats = {
        "indexed": 0,
        "links_discovered": 0,
        "queued": 0,
        "requests": 0,
        "http_errors": 0,
        "rate_limited": 0,
        "rate_limited_retries": 0,
        "errors": 0,           # total of all failures below
        "timeouts": 0,         # requests that timed out
        "network_errors": 0,   # other transport failures
        "parse_errors": 0,     # HTML processing failures
        "db_errors": 0,        # SQLite failures
        "robots_blocked": 0,
        "robots_unavailable": 0,
        "non_html": 0,
        "too_large": 0,
        "not_indexable": 0,
        "out_of_scope": 0,
        "duplicates": 0,
        "sitemaps_found": 0,
        "sitemap_urls": 0,
        "interrupted": False,
        "delay": delay,
        "threads": threads,
        "max_pages": max_pages,
        "elapsed_seconds": 0.0,
    }

    try:
        conn = open_database(db_path)
    except (MigrationError, DatabaseInspectionError, OSError) as error:
        print(f"Database open failed: {error}")
        return None

    merged = 0

    try:
        merged = deduplicate_database(conn)
    except sqlite3.Error as error:
        stats["db_errors"] += 1
        stats["errors"] += 1
        print(f"Database error during deduplication: {error}")

    if merged:
        print(
            f"Deduplicated {merged} stored URL(s) that canonicalize "
            "to the same page."
        )

    limiter = RateLimiter(delay)
    robots_cache = RobotsCache()

    # ---- Sitemap discovery (skipped by --no-sitemap) ----
    sitemap_urls = []
    sitemap_sources = []

    if sitemap != "skip":
        # Use a short-lived local session for discovery so the
        # main-thread session is not polluted (and threads don't leak a
        # stale User-Agent into later searches/tests). Closed as soon as
        # discovery is done; per-worker sessions are used for fetching.
        discovery_session = requests.Session()
        discovery_session.headers.update({"User-Agent": USER_AGENT})

        try:
            sitemap_urls, sitemap_sources = discover_sitemaps(
                discovery_session, start_url, scope, limiter, robots_cache
            )
        finally:
            discovery_session.close()

        stats["sitemaps_found"] = len(sitemap_sources)
        stats["sitemap_urls"] = len(sitemap_urls)

    visited = set()
    rate_limit_retried = set()
    follow_links = True
    queue = deque([start_url])
    queued = {start_url}

    if sitemap == "only":
        if not sitemap_urls:
            print(
                f"No sitemap found for {host_of(start_url)}; "
                "aborting crawl (--only-sitemap)."
            )
            queue = deque()
            queued = set()
        else:
            queue = deque(sitemap_urls)
            queued = set(sitemap_urls)
            follow_links = False
            print("Found sitemap(s): " + ", ".join(sitemap_sources))
            print(
                f"Seeding {len(sitemap_urls)} URL(s) from "
                "sitemap(s); HTML link discovery disabled."
            )
    elif sitemap == "auto":
        if sitemap_urls:
            for url in sitemap_urls:
                if url not in queued:
                    queued.add(url)
                    queue.append(url)

            print("Found sitemap(s): " + ", ".join(sitemap_sources))
            print(
                f"Seeding {len(sitemap_urls)} URL(s) from "
                "sitemap(s)."
            )
        else:
            print(
                f"No sitemap found for {host_of(start_url)}; "
                "continuing with HTML-based discovery."
            )

    saved = 0
    pending_commits = 0
    # Sitemap discovery has finished by here; from this point the crawl
    # is in the crawling phase. The stage is included in progress
    # snapshots so callers (e.g. the Flask job registry) can report
    # "discovering_sitemaps" before this point and "crawling" after.
    stats["stage"] = "crawling"

    # Optional periodic progress reporting: the callback receives a
    # JSON-safe snapshot of `stats` roughly every PROGRESS_INTERVAL
    # seconds on a background daemon thread until the crawl finishes.
    progress_stop = threading.Event()
    progress_thread = None

    if progress_callback is not None:
        def _report_progress():
            while not progress_stop.wait(PROGRESS_INTERVAL):
                try:
                    progress_callback(
                        {k: v for k, v in stats.items()
                         if isinstance(v, (int, float, str, bool))
                         or v is None}
                    )
                except Exception:
                    pass  # a broken callback never breaks the crawl

        progress_thread = threading.Thread(
            target=_report_progress,
            name="crawl-progress",
            daemon=True,
        )
        progress_thread.start()

    executor = ThreadPoolExecutor(
        max_workers=threads,
        thread_name_prefix="crawler"
    )
    pending = {}

    # Single stop path shared by Ctrl+C (keyboard) and the HTTP stop
    # API (stop_event). Both funnel into _stop_crawl() below; keeping
    # a flag lets the message distinguish the requester.
    stop_requested = False

    def _stop_crawl(requested=False):
        """Stop the crawl early but keep everything fetched so far.

        Queued work is cancelled; requests already in flight are
        allowed to finish (bounded by the 20s timeout) so the
        database stays consistent. The stats below still describe
        everything processed before the stop.
        """
        stats["interrupted"] = True
        print()
        if requested:
            print("Interrupted (stop requested) - stopping the crawl early...")
        else:
            print("Interrupted (Ctrl+C) - stopping the crawl early...")

    try:
        while queue or pending:
            if stop_event is not None and stop_event.is_set():
                stop_requested = True
                raise KeyboardInterrupt
            while queue and len(pending) < threads:
                if (
                    max_pages is not None
                    and saved + len(pending) >= max_pages
                ):
                    break

                url = queue.popleft()

                if url in visited:
                    stats["duplicates"] += 1
                    continue

                visited.add(url)

                if not is_allowed_url(url, scope):
                    stats["out_of_scope"] += 1
                    continue

                future = executor.submit(
                    process_url,
                    url,
                    scope,
                    max_bytes,
                    limiter,
                    robots_cache,
                )
                pending[future] = url

            if not pending:
                break

            completed, _ = wait(
                pending,
                return_when=FIRST_COMPLETED
            )

            for future in completed:
                requested_url = pending.pop(future)

                try:
                    result = future.result()
                except Exception as error:
                    stats["errors"] += 1
                    print(
                        f"Worker failed: {requested_url} ({error})"
                    )
                    continue

                status = result["status"]

                if result["message"]:
                    print(result["message"])

                if status == "robots_blocked":
                    # Initial-URL blocks made no request; mid-chain
                    # blocks (redirect targets) already made one.
                    if result.get("requests_made"):
                        stats["requests"] += 1
                    stats["robots_blocked"] += 1
                    continue

                if status == "robots_unavailable":
                    if result.get("requests_made"):
                        stats["requests"] += 1
                    stats["robots_unavailable"] += 1
                    continue

                if status == "http_error":
                    stats["requests"] += 1
                    stats["http_errors"] += 1
                    continue

                if status == "rate_limited":
                    stats["requests"] += 1
                    stats["rate_limited"] += 1

                    # One automatic retry per URL, but only when the
                    # server told us how long to wait (Retry-After).
                    if (result["retry_after"] is not None
                            and requested_url
                            not in rate_limit_retried):
                        rate_limit_retried.add(requested_url)
                        visited.discard(requested_url)
                        queue.append(requested_url)
                        stats["rate_limited_retries"] += 1
                    continue

                if status == "non_html":
                    stats["requests"] += 1
                    stats["non_html"] += 1
                    continue

                if status == "too_large":
                    stats["requests"] += 1
                    stats["too_large"] += 1
                    continue

                if status == "out_of_scope":
                    # A worker-side out_of_scope means a redirect
                    # escaped after at least one real request.
                    stats["requests"] += 1
                    stats["out_of_scope"] += 1
                    continue

                if status == "timeout":
                    stats["timeouts"] += 1
                    stats["errors"] += 1
                    continue

                if status == "network_error":
                    stats["network_errors"] += 1
                    stats["errors"] += 1
                    continue

                if status == "parse_error":
                    stats["parse_errors"] += 1
                    stats["errors"] += 1
                    continue

                if status == "error":
                    # Unclassified worker crash.
                    stats["errors"] += 1
                    continue

                stats["requests"] += 1

                if status == "not_indexable":
                    stats["not_indexable"] += 1

                final_url = result["final_url"]

                if final_url and final_url != requested_url:
                    if final_url in visited:
                        stats["duplicates"] += 1
                        continue

                    visited.add(final_url)
                    queued.add(final_url)

                # Queue links even when a page cannot be indexed
                # (unless --only-sitemap disabled link discovery).
                new_links = []

                if follow_links:
                    for next_url in result["links"]:
                        if next_url not in queued:
                            queued.add(next_url)
                            new_links.append(next_url)

                queue.extend(new_links)
                stats["links_discovered"] += len(new_links)

                if status != "success":
                    continue

                # Only the main thread writes to SQLite.
                try:
                    # v2 upsert: update in place when the URL already
                    # exists (O(log N) via the unique index) instead of
                    # DELETE + INSERT (an O(N) FTS5 full-index scan).
                    # The pages_fts triggers keep the search index in
                    # sync with the content table.
                    conn.execute(
                        """
                        INSERT INTO pages
                            (url, title, description, body)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(url) DO UPDATE SET
                            title = excluded.title,
                            description = excluded.description,
                            body = excluded.body
                        """,
                        (
                            final_url,
                            result["title"],
                            result["description"],
                            result["body"],
                        )
                    )
                except sqlite3.Error as error:
                    # Roll back the open batch rather than keep a
                    # half-written page; affected pages are simply
                    # re-crawled on the next run. The pages in the
                    # open batch were already counted in `saved` /
                    # stats["indexed"], but the rollback undoes them,
                    # so un-count them to keep the statistics honest.
                    stats["db_errors"] += 1
                    stats["errors"] += 1
                    print(
                        f"Database error for {final_url}: {error}"
                    )
                    try:
                        conn.rollback()
                        saved -= pending_commits
                        if saved < 0:
                            saved = 0
                        stats["indexed"] = saved
                        pending_commits = 0
                    except sqlite3.Error:
                        pass
                    continue

                # Batch commits: one fsync per DB_COMMIT_BATCH
                # pages instead of one per page. A crash loses at
                # most the current batch - never a corrupt index.
                pending_commits += 1

                if pending_commits >= DB_COMMIT_BATCH:
                    conn.commit()
                    pending_commits = 0

                saved += 1
                stats["indexed"] = saved

                page_limit = (
                    str(max_pages)
                    if max_pages is not None
                    else "unlimited"
                )

                print(
                    f"[{saved}/{page_limit}] Indexed: "
                    f"{result['title'] or final_url}"
                )
                print(
                    f"    New links discovered: {len(new_links)}"
                )
                print(f"    Queue size: {len(queue)}")
                print(f"    Active workers: {len(pending)}")

    except KeyboardInterrupt:
        _stop_crawl(stop_requested)

    finally:
        for future in pending:
            future.cancel()

        try:
            executor.shutdown(wait=True)
        except KeyboardInterrupt:
            pass  # a second Ctrl+C skips the (bounded) wait

        # Flush any pages still waiting in the open batch.
        if pending_commits:
            try:
                conn.commit()
            except sqlite3.Error as error:
                print(f"Final database commit failed: {error}")
                # The uncommitted pages never made it into the file -
                # un-count them and record the database error.
                saved -= pending_commits
                if saved < 0:
                    saved = 0
                stats["indexed"] = saved
                stats["db_errors"] += 1
                stats["errors"] += 1
        conn.close()

        if progress_thread is not None:
            progress_stop.set()
            progress_thread.join(timeout=2.0)

    stats["elapsed_seconds"] = time.perf_counter() - started
    stats["queued"] = len(queue)
    stats["db_path"] = str(resolve_db_path(db_path))
    return stats

def print_crawl_summary(stats):
    """Print a human-readable crawl summary."""
    elapsed = stats["elapsed_seconds"]
    rate = stats["indexed"] / elapsed if elapsed > 0 else 0.0

    if stats.get("interrupted"):
        print("\nCrawl interrupted (Ctrl+C) - statistics are partial.")
    else:
        print("\nCrawl finished.")
    print(f"Pages indexed this run: {stats['indexed']}")
    print(f"Elapsed: {elapsed:.1f}s ({rate:.1f} pages/s)")
    print(f"Worker threads: {stats['threads']}")
    print(
        f"Fetch attempts: {stats['requests']}, "
        f"links discovered: {stats['links_discovered']}, "
        f"queue left: {stats['queued']}"
    )
    print(
        "Skipped: "
        f"{stats['robots_blocked']} robots-blocked, "
        f"{stats['http_errors']} HTTP errors, "
        f"{stats['rate_limited']} rate-limited "
        f"({stats['rate_limited_retries']} retried), "
        f"{stats['non_html']} non-HTML, "
        f"{stats['too_large']} too-large, "
        f"{stats['not_indexable']} empty, "
        f"{stats['duplicates']} duplicates, "
        f"{stats['out_of_scope']} out-of-scope"
    )
    print(
        f"Robots unavailable: {stats['robots_unavailable']}"
    )
    print(
        f"Sitemaps found: {stats.get('sitemaps_found', 0)}, "
        f"URLs seeded from sitemaps: {stats.get('sitemap_urls', 0)}"
    )
    print(
        f"Errors: {stats['errors']} total "
        f"({stats['timeouts']} timeouts, "
        f"{stats['network_errors']} network, "
        f"{stats['parse_errors']} parsing, "
        f"{stats['db_errors']} database)"
    )
    print("Database:", stats.get("db_path", DB_PATH))


def _normalize_crawl_args(url, max_pages=None, delay=None, max_bytes=None,
                          threads=None, allow=None, sitemap=None, db=None):
    """Validate crawl inputs and return normalized kwargs for crawl_json().

    Raises ValueError(msg) on invalid input. None (or omitted) means
    "use the engine default"; the returned dict's keys match crawl_json()'s
    public parameter names, so app.py can pass it straight through.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if normalize_url(url) is None:
        raise ValueError(f"invalid URL: {url!r}")

    def _positive_int(name, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < 1:
            raise ValueError(f"{name} must be at least 1")
        return value

    def _non_negative_float(name, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        value = float(value)
        if value < 0:
            raise ValueError(f"{name} cannot be negative")
        return value

    max_pages = _positive_int("max_pages", max_pages)
    max_bytes = _positive_int("max_bytes", max_bytes)
    threads = _positive_int("threads", threads)
    delay = _non_negative_float("delay", delay)

    if sitemap is None:
        sitemap = "auto"
    elif sitemap not in ("auto", "skip", "only"):
        raise ValueError("sitemap must be 'auto', 'skip', or 'only'")

    if allow is None:
        allow = ()
    else:
        try:
            allow = tuple(allow)
        except TypeError:
            raise ValueError("allow must be a list of strings") from None
        for entry in allow:
            if not isinstance(entry, str) or parse_scope_entry(entry) is None:
                raise ValueError(f"invalid allow entry: {entry!r}")

    if db is not None and not isinstance(db, str):
        raise ValueError("db must be a string")

    return {
        "url": url,
        "max_pages": max_pages,
        "delay": delay if delay is not None else DEFAULT_DELAY,
        "max_bytes": max_bytes if max_bytes is not None else DEFAULT_MAX_BYTES,
        "threads": threads if threads is not None else DEFAULT_THREADS,
        "allow": allow,
        "sitemap": sitemap,
        "db": db,
    }


def crawl_json(url, max_pages=None, delay=None, max_bytes=None,
               threads=None, allow=None, sitemap=None, db=None,
               progress_callback=None, stop_event=None):
    """Run a crawl and return the JSON-compatible stats dictionary.

    Programmatic twin of crawl() for web frontends / the Flask API.
    It accepts the same options as crawl(), but:

      - None (or omitted) means "use the engine default";
      - values are type/range-checked BEFORE the crawl starts, and a
        bad value raises ValueError with a reason (the Flask API maps
        that to HTTP 400) instead of printing and returning None;
      - on success it returns exactly what crawl() returns - the stats
        dict - which is JSON-serializable (ints, floats, strings,
        booleans, None).

    Parameter notes (mirror the CLI):

      - ``url``: required seed URL (must be a valid http(s) URL).
      - ``max_pages`` / ``max_bytes`` / ``threads``: positive integers.
      - ``delay``: non-negative number of seconds.
      - ``allow``: iterable of "host" or "host/path" scope entries.
      - ``sitemap``: "auto" (default), "skip", or "only".
      - ``db``: database file for this crawl (None = engine default;
        the name matches the JSON payload and the CLI's --db). See
        resolve_db_path for anchoring/.db rules.
      - ``progress_callback``: passed straight through to crawl(); see
        its docstring. This is how the web API streams live stats.
      - ``stop_event``: passed straight through to crawl() - a
        threading.Event; once set, the crawl stops gracefully between
        units of work (same shared stop path as Ctrl+C). This is how
        the web API stops a running crawl.
    """
    normalized = _normalize_crawl_args(
        url,
        max_pages=max_pages,
        delay=delay,
        max_bytes=max_bytes,
        threads=threads,
        allow=allow,
        sitemap=sitemap,
        db=db,
    )
    result = crawl(
        normalized["url"],
        max_pages=normalized["max_pages"],
        delay=normalized["delay"],
        max_bytes=normalized["max_bytes"],
        threads=normalized["threads"],
        allowed=normalized["allow"],
        sitemap=normalized["sitemap"],
        db_path=normalized["db"],
        progress_callback=progress_callback,
        stop_event=stop_event,
    )
    if result is None:
        # crawl() only returns None when the database could not be
        # opened/migrated (all input validation already happened above,
        # so the validation-return-None path is unreachable here).
        raise ValueError("database could not be opened (migration failed)")
    return result


def _porter_stem(word):
    """Return the Porter stem of a lowercase word (pure Python).

    A faithful implementation of the classic Porter stemming algorithm
    (M.F. Porter, 1980), used to give --es queries their stemming
    behaviour without adding a third-party dependency. Words of two
    letters or fewer are returned unchanged.
    """
    if len(word) <= 2:
        return word

    vowels = set("aeiou")

    def is_consonant(w, i):
        ch = w[i]
        if ch in vowels:
            return False
        if ch == "y":
            return True if i == 0 else not is_consonant(w, i - 1)
        return True

    def measure(w):
        """m = number of vowel-consonant sequences (Porter's m)."""
        m = 0
        i = 0
        n = len(w)
        while i < n:
            if is_consonant(w, i):
                i += 1
                continue
            while i < n and not is_consonant(w, i):
                i += 1
            j = i
            while j < n and is_consonant(w, j):
                j += 1
            if j > i:
                m += 1
                i = j
        return m

    def has_vowel(w):
        return any(not is_consonant(w, i) for i in range(len(w)))

    def double_consonant(w):
        return (len(w) >= 2
                and is_consonant(w, -1)
                and w[-1] == w[-2])

    def cvc(w):
        """Ends CVC where the final C is not w, x, or y."""
        if len(w) < 3:
            return False
        if not is_consonant(w, -1) or is_consonant(w, -2):
            return False
        if not is_consonant(w, -3) or w[-1] in "wxy":
            return False
        return True

    def step1a(w):
        if w.endswith("sses"):
            return w[:-4] + "ss"
        if w.endswith("ies"):
            return w[:-3] + "i"
        if w.endswith("ss"):
            return w
        if w.endswith("s"):
            return w[:-1]
        return w

    def step1b(w):
        flag = False
        if w.endswith("eed"):
            if measure(w[:-3]) > 0:
                return w[:-3] + "ee"
            return w
        if w.endswith("ed"):
            stem = w[:-2]
            if has_vowel(stem):
                w, flag = stem, True
        elif w.endswith("ing"):
            stem = w[:-3]
            if has_vowel(stem):
                w, flag = stem, True
        if flag:
            if w.endswith(("at", "bl", "iz")):
                return w + "e"
            if double_consonant(w) and w[-1] not in "lsz":
                return w[:-1]
            if measure(w) == 1 and cvc(w):
                return w + "e"
        return w

    def step1c(w):
        if w.endswith("y") and len(w) > 1 and is_consonant(w, -2):
            return w[:-1] + "i"
        return w

    def step2(w):
        replacements = (
            ("ational", "ate"), ("tional", "tion"), ("enci", "ence"),
            ("anci", "ance"), ("izer", "ize"), ("abli", "able"),
            ("alli", "al"), ("entli", "ent"), ("eli", "e"),
            ("ousli", "ous"), ("ization", "ate"), ("ation", "ate"),
            ("ator", "ate"), ("alism", "al"), ("iveness", "ive"),
            ("fulness", "ful"), ("ousness", "ous"), ("aliti", "al"),
            ("iviti", "ive"), ("biliti", "ble"), ("logi", "log"),
        )
        for suffix, rep in replacements:
            if w.endswith(suffix):
                stem = w[:-len(suffix)]
                if measure(stem) > 0:
                    return stem + rep
                return w
        return w

    def step3(w):
        replacements = (
            ("icate", "ic"), ("ative", ""), ("alize", "al"),
            ("iciti", "ic"), ("ical", "ic"), ("ful", ""),
            ("ness", ""),
        )
        for suffix, rep in replacements:
            if w.endswith(suffix):
                stem = w[:-len(suffix)]
                if measure(stem) > 0:
                    return stem + rep
                return w
        return w

    def step4(w):
        suffixes = (
            "al", "ance", "ence", "er", "ic", "able", "ible",
            "ant", "ement", "ment", "ent", "ion", "ou", "ism",
            "ate", "iti", "ous", "ive", "ize",
        )
        for suffix in suffixes:
            if w.endswith(suffix):
                stem = w[:-len(suffix)]
                if measure(stem) > 1:
                    if suffix == "ion" and stem[-1:] not in ("s", "t"):
                        return w
                    return stem
                return w
        return w

    def step5(w):
        if w.endswith("e"):
            stem = w[:-1]
            m = measure(stem)
            if m > 1 or (m == 1 and not cvc(stem)):
                return stem
            return w
        if double_consonant(w) and w[-1] == "l" and measure(w) > 1:
            return w[:-1]
        return w

    return step5(step4(step3(step2(step1c(step1b(step1a(word)))))))


def extract_words(query):
    """Split a query into safe FTS5 search terms."""
    return re.findall(r"[^\W_]+", query, flags=re.UNICODE)


def build_match(words, operator="AND", stem=False):
    """Build an FTS5 prefix-match expression.

    With stem=True each word is Porter-stemmed first, so a search for
    "google bomb" also matches "google bombing" (stemmed prefixes
    against the un-stemmed index).
    """
    if stem:
        terms = (_porter_stem(word) for word in words)
    else:
        terms = words
    return f" {operator} ".join(
        f'"{term}"*' for term in terms
    )


def build_exact_match(words):
    """Build an FTS5 exact-phrase expression.

    '"google bomb"' matches pages containing those tokens consecutively
    and in order ("google bomb controversy"), not reordered ("bomb
    google"), interrupted ("google controversial bomb"), or inflected
    ("google bombing"). No prefix expansion, no stemming.
    """
    return '"' + " ".join(words) + '"'


def _fts_query(conn, match, limit):
    """Search FTS5 (v2 external-content) and rank results with BM25.

    The v2 index is a separate `pages_fts` external-content table whose
    columns are (title, description, body): column indices 0, 1, 2.
    The content table is joined back on rowid to fetch url/title/
    description. BM25 weights stay 5.0 / 3.0 / 1.0 (title, description,
    body), and equal scores are broken deterministically by rowid so
    rankings are stable run-to-run.
    """
    return conn.execute(
        """
        SELECT
            p.url,
            p.title,
            p.description,
            snippet(pages_fts, 2, '[', ']', '...', 20)
        FROM pages_fts
        JOIN pages p ON p.rowid = pages_fts.rowid
        WHERE pages_fts MATCH ?
        ORDER BY bm25(pages_fts, 5.0, 3.0, 1.0), p.rowid ASC
        LIMIT ?
        """,
        (match, limit)
    ).fetchall()


def run_search(query, limit=10, exact=False, stem=False, db_path=None):
    """Return up to limit search results.

    exact (--e) searches for the whole query as one exact phrase -
    tokens in order, no prefix expansion, no stemming, and no OR
    fallback. stem (--es) Porter-stems each word so inflected forms
    match ("google bomb" finds "google bombing"). `db_path` selects the
    database file to search (default DB_PATH). Returns [] when the
    selected database file does not exist; searching never creates the
    database as a side effect, and never falls back to another file.
    """
    words = extract_words(query)

    if not words or limit < 1:
        return []

    db_file = resolve_db_path(db_path)

    if not db_file.exists():
        return []

    try:
        conn = open_database(db_file)
    except (MigrationError, DatabaseInspectionError) as error:
        print("Search error:", error)
        return []

    try:
        if exact:
            # Exact phrase: strict, no fallback. A reordered or
            # interrupted word sequence must not match.
            return _fts_query(
                conn,
                build_exact_match(words),
                limit
            )

        rows = _fts_query(
            conn,
            build_match(words, "AND", stem),
            limit
        )

        # Fall back to any-word matching when AND finds nothing.
        if not rows:
            rows = _fts_query(
                conn,
                build_match(words, "OR", stem),
                limit
            )

        return rows
    except sqlite3.OperationalError as error:
        print("Search error:", error)
        return []
    finally:
        conn.close()


def search(query, limit=10, exact=False, stem=False, db_path=None):
    """Search the index and print results."""
    if not extract_words(query):
        print("Enter a search term.")
        return

    db_file = resolve_db_path(db_path)

    if not db_file.exists():
        print("No database found - nothing has been crawled yet.")
        print("Database location:", db_file)
        print("Run a crawl first, e.g.: python main.py crawl <url>")
        return

    rows = run_search(query, limit, exact, stem, db_file)

    if not rows:
        print("No results found.")
        return

    mode = " (exact phrase)" if exact else " (stemmed)" if stem else ""
    print(f"\nResults for: {query}{mode}\n")

    for number, (url, title, description, excerpt) in enumerate(
        rows, start=1
    ):
        print(f"{number}. {title or '(Untitled page)'}")
        print("   URL:", url)
        print("   ", description or excerpt)
        print()



def search_json(query, limit=10, exact=False, stem=False, db_path=None):
    """Run a search and return a JSON-compatible result dictionary.

    This is the programmatic entry point for web frontends or any
    Python caller. It is independent of the CLI and of Flask: call it
    directly (import main; main.search_json(...)) and it never writes
    to the database and never creates one.

    Returned structure (stable, documented):

        {
          "query": <str, the original query string>,
          "results": [
            {
              "title": <str, page title (may be empty)>,
              "url": <str, canonical page URL>,
              "snippet": <str, description if present else FTS5
                            highlighted excerpt>
            },
            ...
          ],
          "total_results": <int, total matches before the limit>,
          "search_time": <float, seconds spent searching>
        }

    Behavior:

    - An empty or unparseable query (or a limit < 1) returns an empty
      result set without error ({"results": [], "total_results": 0}).
    - If the selected database file does not exist, DatabaseNotFoundError
      is raised - the caller decides how to surface it (the Flask API
      maps it to 404).
    - If the database is corrupt or unreadable, DatabaseInspectionError
      propagates (with the original SQLite error chained) - the caller
      maps it to a 5xx response. A failed automatic v1 -> v2 migration
      raises MigrationError instead (also 5xx from the caller).
    - The same search semantics as the CLI apply: exact (--e) phrase
      matching, stem (--es) Porter stemming, and the AND -> OR
      fallback. "total_results" is counted with whichever match
      expression produced the returned rows.
    """

    started = time.perf_counter()
    words = extract_words(query)

    if not words or limit < 1:
        return {
            "query": query,
            "results": [],
            "total_results": 0,
            "search_time": round(time.perf_counter() - started, 6),
        }

    db_file = resolve_db_path(db_path)

    if not db_file.exists():
        raise DatabaseNotFoundError(str(db_file))

    conn = open_database(db_file)

    try:
        if exact:
            match = build_exact_match(words)
            rows = _fts_query(conn, match, limit)
        else:
            match = build_match(words, "AND", stem)
            rows = _fts_query(conn, match, limit)

            # Fall back to any-word matching when AND finds nothing.
            if not rows:
                match = build_match(words, "OR", stem)
                rows = _fts_query(conn, match, limit)

        total = conn.execute(
            "SELECT COUNT(*) FROM pages_fts WHERE pages_fts MATCH ?",
            (match,),
        ).fetchone()[0]

        results = [
            {
                "title": title,
                "url": url,
                "snippet": description or excerpt,
            }
            for url, title, description, excerpt in rows
        ]
    finally:
        conn.close()

    return {
        "query": query,
        "results": results,
        "total_results": total,
        "search_time": round(time.perf_counter() - started, 6),
    }



def main():
    parser = argparse.ArgumentParser(
        description="A multithreaded, database-backed web search engine"
    )

    parser.add_argument(
        "--h",
        action="store_true",
        help="print the full help text and exit"
    )

    commands = parser.add_subparsers(
        dest="command"
    )

    crawl_parser = commands.add_parser("crawl")
    crawl_parser.add_argument("url")
    crawl_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="maximum pages to index (default: unlimited)"
    )
    crawl_parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="minimum seconds between requests to one host"
    )
    crawl_parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="maximum bytes to download per page"
    )
    crawl_parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help="number of concurrent worker threads (default: 8)"
    )
    crawl_parser.add_argument(
        "--allow",
        "--allowed",
        action="append",
        default=[],
        metavar="HOST[/PATH]",
        help="extra site to allow in addition to the seed's host and "
             "its subdomains; with a path, the site is limited to that "
             "folder (repeatable, e.g. --allow books.toscrape.com or "
             "--allow example.com/page)"
    )
    crawl_parser.add_argument(
        "--no-sitemap",
        action="store_true",
        help="skip sitemap discovery; discover pages via HTML links only"
    )
    crawl_parser.add_argument(
        "--only-sitemap",
        action="store_true",
        help="crawl only URLs from sitemaps; do not follow page links "
             "(aborts if no sitemap is found)"
    )
    crawl_parser.add_argument(
        "--db",
        default=None,
        metavar="FILE",
        help="database file to create and index into (default: "
             "result.db next to main.py; relative paths are anchored "
             "there too)"
    )

    search_parser = commands.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument(
        "--e",
        action="store_true",
        help="match the exact phrase: all words in order, no prefix "
             "expansion, no stemming"
    )
    search_parser.add_argument(
        "--es",
        action="store_true",
        help="enable stemming, e.g. 'google bombing' matches a search "
             "for 'google bomb'"
    )
    search_parser.add_argument(
        "--db",
        default=None,
        metavar="FILE",
        help="database file to search (default: result.db next to "
             "main.py; relative paths are anchored there too). If the "
             "file does not exist, no fallback is attempted - the "
             "search reports 'No database found'."
    )

    args = parser.parse_args()

    if args.h:
        print(HELP_TEXT)
        return

    if not args.command:
        parser.error(
            "a command is required (crawl or search); use --h for help"
        )

    if args.command == "crawl":
        if args.max_pages is not None and args.max_pages < 1:
            parser.error("--max-pages must be at least 1")

        if args.delay < 0:
            parser.error("--delay must be zero or positive")

        if args.max_bytes < 1:
            parser.error("--max-bytes must be a positive number of bytes")

        if args.threads < 1:
            parser.error("--threads must be at least 1")

        if args.no_sitemap and args.only_sitemap:
            parser.error(
                "--no-sitemap and --only-sitemap are mutually exclusive"
            )

        sitemap_mode = "auto"

        if args.no_sitemap:
            sitemap_mode = "skip"
        elif args.only_sitemap:
            sitemap_mode = "only"

        extra_entries = []

        for entry in args.allow:
            if parse_scope_entry(entry) is None:
                parser.error(
                    "--allow expects 'host' or 'host/path' entries "
                    f"(no ports or spaces), got {entry!r}"
                )

            extra_entries.append(entry)

        stats = crawl(
            args.url,
            max_pages=args.max_pages,
            delay=args.delay,
            max_bytes=args.max_bytes,
            threads=args.threads,
            allowed=extra_entries,
            sitemap=sitemap_mode,
            db_path=args.db,
        )

        if stats:
            print_crawl_summary(stats)

    elif args.command == "search":
        if args.limit < 1:
            parser.error("--limit must be a positive integer")

        if args.e and args.es:
            parser.error("--e and --es are mutually exclusive")

        search(args.query, args.limit, args.e, args.es, args.db)


if __name__ == "__main__":
    main()
