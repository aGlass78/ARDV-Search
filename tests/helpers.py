"""Shared helpers for the crawler test suite.

Provides a tiny local HTTP server (no external network is ever touched),
plus base test classes that isolate the database, scope the crawler to
localhost, and capture console output.
"""
import contextlib
import io
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlparse

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


def page(title, body, links=()):
    """Build a small HTML document with optional title, text, and links.

    Links are rendered as empty-text anchors so they are discoverable
    but contribute no visible text (useful for "unindexable" pages).
    """
    head = f"<title>{title}</title>" if title else ""
    anchors = "".join(f'<a href="{href}"></a>' for href in links)
    return f"<!doctype html><html><head>{head}</head><body>{body}{anchors}</body></html>"


def run_cli(argv):
    """Invoke main.main() with the given CLI words, capturing output.

    Returns (exit_code, stdout, stderr); exit_code is None when the
    command completed normally (argparse errors raise SystemExit(2)).
    """
    out, err = io.StringIO(), io.StringIO()
    code = None
    try:
        with mock.patch.object(sys, "argv", ["main.py", *argv]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                main.main()
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def provision_v2(db_path):
    """Create the v2 schema at db_path (idempotent).

    Phase 2+ crawl tests run against the v2 schema. The crawler's
    get_database() still issues the legacy `CREATE VIRTUAL TABLE IF NOT
    EXISTS pages USING fts5(...)`, which is a silent no-op once an
    ordinary `pages` table exists here, so provisioning up front lets
    the unchanged production code co-exist with a v2 database.

    Safe under concurrency: the connection sets a busy timeout and the
    DDL is a no-op when the v2 schema already exists (so parallel job
    workers provisioning the same database do not race on DDL writes).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        present = conn.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE name='pages_fts'"
        ).fetchone()[0]
        if not present:
            main.create_schema_v2(conn)
            conn.commit()  # caller-owned commit (transaction-ownership contract)
    finally:
        conn.close()


class TestServer:
    """Minimal threaded HTTP server with a timestamped request log."""

    def __init__(self):
        self.routes = {}
        self.requests = []       # (monotonic timestamp, path) per GET
        self.user_agents = []    # User-Agent header per GET
        self.robots_failures = 0  # respond 503 to the next N robots.txt hits
        self.slow = {}           # path -> seconds to sleep before answering
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path):
        # 127.0.0.1, not "localhost": on this machine "localhost" tries
        # ::1 first (the server binds IPv4 only), costing ~2s per request.
        return f"http://127.0.0.1:{self.port}{path}"

    def add(self, path, body="", status=200, content_type="text/html; charset=utf-8",
            headers=None, redirect=None, no_content_length=False,
            fail=None):
        """Register a route.

        `fail={"times": N, "status": 429, "headers": {...}}` makes the
        first N hits return the given status/headers instead of the
        normal response (for testing retries and rate limiting).
        """
        self.routes[path] = {
            "body": body.encode() if isinstance(body, str) else body,
            "status": status,
            "content_type": content_type,
            "headers": headers or {},
            "redirect": redirect,
            "no_content_length": no_content_length,
            "fail": fail or {"times": 0},
        }

    def add_robots(self, text):
        self.add("/robots.txt", text, content_type="text/plain; charset=utf-8")

    def paths(self):
        with self._lock:
            return [path for _, path in self.requests]

    def gaps(self):
        """Seconds between consecutive request arrivals."""
        with self._lock:
            times = [t for t, _ in self.requests]
        return [b - a for a, b in zip(times, times[1:])]

    def _build_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                path = urlparse(self.path).path
                with server._lock:
                    server.requests.append((time.monotonic(), path))
                    server.user_agents.append(
                        self.headers.get("User-Agent"))
                    fail_robots = (path == "/robots.txt"
                                   and server.robots_failures > 0)
                    if fail_robots:
                        server.robots_failures -= 1
                    route = server.routes.get(path)
                    fail = None
                    if (route is not None
                            and route["fail"]["times"] > 0):
                        route["fail"]["times"] -= 1
                        fail = route["fail"]
                    slow_for = server.slow.get(path, 0.0)
                if slow_for:
                    time.sleep(slow_for)  # outside the lock
                if fail_robots:
                    self._send(503, b"robots unavailable", "text/plain")
                    return
                if fail is not None:
                    self._send(fail.get("status", 429),
                               b"rate limited", "text/plain",
                               fail.get("headers"))
                    return
                if route is None:
                    self._send(404, b"not found", "text/plain")
                    return
                if route["redirect"] is not None:
                    self._send_redirect(route["status"], route["redirect"])
                    return
                self._send(route["status"], route["body"], route["content_type"],
                           route["headers"], route["no_content_length"])

            def _send_redirect(self, status, location):
                try:
                    self.send_response(status)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (ConnectionError, BrokenPipeError):
                    pass

            def _send(self, status, body, ctype, extra=None, no_length=False):
                extra = extra or {}
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    # Extra headers may override the default Content-Length
                    # (used to test lying/garbage Content-Length handling).
                    if not no_length and "Content-Length" not in extra:
                        self.send_header("Content-Length", str(len(body)))
                    for key, value in extra.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                except (ConnectionError, BrokenPipeError):
                    pass

            def log_message(self, *args):
                pass

        return Handler


class IsolatedDbTestCase(unittest.TestCase):
    """Runs the crawler/search against a throwaway database."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = pathlib.Path(tmp.name)
        self.db_path = self.tmp_path / "test.db"
        patcher = mock.patch.object(main, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def insert_page(self, url, title="", description="", body=""):
        provision_v2(self.db_path)  # v2 schema (Phase 3+ search tests)
        conn = main.get_database()  # legacy DDL no-ops over v2 pages
        try:
            conn.execute(
                "INSERT INTO pages (url, title, description, body) VALUES (?, ?, ?, ?)",
                (url, title, description, body),
            )
            conn.commit()
        finally:
            conn.close()


class ServerTestCase(IsolatedDbTestCase):
    """Adds a local HTTP server; scope is derived from the seed URL."""

    def setUp(self):
        super().setUp()
        server = TestServer().start()
        self.addCleanup(server.stop)
        self.server = server

        # Crawl scope comes from the seed (http://127.0.0.1:<port>/...)
        # and any port is crawlable by default, so nothing needs
        # patching for the local test server.

        # Phase 2+: crawl tests run against the v2 schema. Patch
        # get_database to provision v2 at the requested path lazily (so
        # a custom --db target file is created on first use, exactly as
        # in production), then delegate to the real implementation whose
        # legacy FTS5 DDL no-ops once an ordinary `pages` table exists.
        _real_get_database = main.get_database

        def _v2_get_database(db_path=None):
            provision_v2(main.resolve_db_path(db_path))
            return _real_get_database(db_path)

        self._patch_get_database = mock.patch.object(
            main, "get_database", new=_v2_get_database)
        self._patch_get_database.start()
        self.addCleanup(self._patch_get_database.stop)

    def crawl(self, url, **kwargs):
        """Run main.crawl with output captured; returns its result."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = main.crawl(url, **kwargs)
        self.stdout = out.getvalue()
        return result

    def indexed_paths(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("SELECT url FROM pages ORDER BY url").fetchall()
        finally:
            conn.close()
        prefix = f"http://127.0.0.1:{self.server.port}"
        return [row[0][len(prefix):] for row in rows]
