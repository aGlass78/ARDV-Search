"""E1-E4 measurement functions (pure measurements; orchestration in runner).

Fairness notes baked into the code:
  - v1/v2 runs use identical rows, transaction size (batch=25), SQLite
    settings, and band structure.
  - Each run/repetition uses a fresh, isolated database.
  - E4 attributes fetch/parse/write with threads=1 (no overlapping worker
    wall time); threads=8 is measured separately only as a throughput
    reference.
"""

import pathlib
import re
import sqlite3
import sys
import time
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = _ROOT / "tests"
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import main  # noqa: E402
from helpers import TestServer, page  # noqa: E402

from . import env as benv  # noqa: E402
from .corpus import gen_rows, mutate_rows  # noqa: E402
from .writers import (  # noqa: E402
    v2_fresh_db, legacy_v1_db, insert_rows, legacy_write_rows,
)

BANDS = [0, 1000, 5000, 10000, 20000, 30000]

_UPSERT_RE = re.compile(r"\s*INSERT INTO pages \(url,\s*title", re.I)


def _clamped_bands(n):
    edges = [b for b in BANDS if b < n]
    edges.append(n)
    return edges


def _write_scaling_run(kind, n, mix, seed, budget_per_band, tmpdir):
    """One scaling run for kind='v2'|'v1'.

    Returns a JSON-able dict. Bands that exceed budget_per_band seconds
    are still fully recorded but flagged truncated; higher bands are then
    skipped and recorded as such (honest incomplete comparison).
    """
    rows = gen_rows(n, seed=seed, mix=mix)
    path = pathlib.Path(tmpdir) / f"{kind}_{n}_{mix}.db"
    edges = _clamped_bands(n)
    bands = []
    truncated = False
    total_t0 = time.perf_counter()
    try:
        conn = (v2_fresh_db(path) if kind == "v2" else legacy_v1_db(path))
        for lo, hi in zip(edges[:-1], edges[1:]):
            if truncated:
                bands.append({"end_index": hi, "rows": 0,
                              "seconds": 0.0, "status": "skipped"})
                continue
            t0 = time.perf_counter()
            if kind == "v2":
                insert_rows(conn, rows[lo:hi])
            else:
                legacy_write_rows(conn, rows[lo:hi])
            elapsed = time.perf_counter() - t0
            rec = {"end_index": hi, "rows": hi - lo, "seconds": elapsed}
            if elapsed > budget_per_band:
                rec["status"] = "truncated"
                truncated = True
            else:
                rec["status"] = "ok"
            bands.append(rec)
        total = time.perf_counter() - total_t0
        conn.close()
    except Exception as exc:  # noqa: BLE001 - captured, not swallowed
        return {"status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}

    sizes = benv.db_sizes(path)
    ok_rows = sum(b["rows"] for b in bands if b["status"] != "skipped")
    return {
        "status": "truncated" if truncated else "ok",
        "bands": bands,
        "total_seconds": total,
        "rows_per_second_total": ok_rows / total if total > 0 else None,
        "main_db_bytes": sizes["main"],
        "wal_bytes": sizes["wal"],
        "peak_rss_bytes": benv.peak_rss_bytes(),
    }


def run_e1(n, mix, seed, budget_per_band, tmpdir):
    """E1: v2 raw insert (UPSERT, batch=25) scaling."""
    return _write_scaling_run("v2", n, mix, seed, budget_per_band, tmpdir)


def run_e2(n, mix, seed, budget_per_band, tmpdir):
    """E2: legacy v1 (DELETE+INSERT) scaling, identical data/settings."""
    return _write_scaling_run("v1", n, mix, seed, budget_per_band, tmpdir)


def run_e3(seed, tmpdir, n=10000):
    """E3: re-crawl cost on a populated v2 DB (n rows).

    (a) byte-identical re-upserts - UPDATE trigger WHEN guard skips FTS;
    (b) mutated re-upserts - FTS delete+insert fires.
    FTS sync cost = (b) - (a).
    """
    rows = gen_rows(n, seed=seed, mix="A")
    path = pathlib.Path(tmpdir) / "e3.db"
    conn = v2_fresh_db(path)
    try:
        t0 = time.perf_counter()
        insert_rows(conn, rows)          # initial fill (setup)
        fill = time.perf_counter() - t0

        t0 = time.perf_counter()
        insert_rows(conn, rows)          # (a) identical
        identical = time.perf_counter() - t0

        mutated = mutate_rows(rows)
        t0 = time.perf_counter()
        insert_rows(conn, mutated)       # (b) mutated
        mutated_t = time.perf_counter() - t0
    finally:
        conn.close()

    sizes = benv.db_sizes(path)
    return {
        "n": n,
        "fill_seconds": fill,
        "identical_recrawl_seconds": identical,
        "mutated_recrawl_seconds": mutated_t,
        "fts_sync_cost_seconds": max(mutated_t - identical, 0.0),
        "main_db_bytes": sizes["main"],
        "wal_bytes": sizes["wal"],
        "peak_rss_bytes": benv.peak_rss_bytes(),
    }


class _WriteTimingConn:
    """Delegating connection that times v2 write statements/commits."""

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)
        object.__setattr__(self, "write_seconds", 0.0)
        object.__setattr__(self, "write_statements", 0)

    def execute(self, sql, *args):
        c = self._c
        if isinstance(sql, str) and _UPSERT_RE.match(sql):
            t = time.perf_counter()
            try:
                return c.execute(sql, *args)
            finally:
                self.write_seconds += time.perf_counter() - t
                self.write_statements += 1
        return c.execute(sql, *args)

    def executemany(self, sql, seq_of_params):
        return self._c.executemany(sql, seq_of_params)

    def commit(self):
        t = time.perf_counter()
        self._c.commit()
        self.write_seconds += time.perf_counter() - t

    def close(self):
        self._c.close()

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def run_e4(seed, tmpdir, n_pages=400):
    """E4: pipeline decomposition on a local server, threads=1.

    fetch = cumulative main.fetch_html time; parse = cumulative
    BeautifulSoup time; write = cumulative v2 write statements + commits
    timed via a delegating connection returned by a patched
    open_database; other = total - (fetch+parse+write) (init, queue,
    robots, limiter, dedup, final flush).
    """
    rows = gen_rows(n_pages, seed=seed, mix="A")
    server = TestServer().start()
    try:
        server.add_robots("User-agent: *\nAllow: /\n")
        for i, (_, title, _, body) in enumerate(rows):
            # chain-link each page to the next so the crawler can reach
            # all n_pages from the seed.
            links = (f"/{i + 1}",) if i + 1 < n_pages else ()
            server.add(f"/{i}", page(title, body, links=links))
        start_url = server.url("/0")
        db_path = pathlib.Path(tmpdir) / "e4.db"

        fetch_acc = [0.0]
        parse_acc = [0.0]
        real_fetch = main.fetch_html
        real_soup = main.BeautifulSoup

        def tf(*a, **k):
            t = time.perf_counter()
            try:
                return real_fetch(*a, **k)
            finally:
                fetch_acc[0] += time.perf_counter() - t

        def tp(*a, **k):
            t = time.perf_counter()
            try:
                return real_soup(*a, **k)
            finally:
                parse_acc[0] += time.perf_counter() - t

        holder = {}
        real_open = main.open_database

        def open_proxy(db_path=None, progress_callback=None):
            real = real_open(
                db_path, progress_callback=progress_callback)
            wrap = _WriteTimingConn(real)
            holder["conn"] = wrap
            return wrap

        t_total = time.perf_counter()
        with (mock.patch.object(main, "fetch_html", new=tf),
              mock.patch.object(main, "BeautifulSoup", new=tp),
              mock.patch.object(main, "open_database", new=open_proxy)):
            stats = main.crawl(
                start_url, max_pages=n_pages, delay=0.0, threads=1,
                sitemap="skip", db_path=str(db_path))
        total = time.perf_counter() - t_total

        write = holder["conn"].write_seconds
        other = max(total - fetch_acc[0] - parse_acc[0] - write, 0.0)
        holder["conn"].close()
    finally:
        server.stop()

    sizes = benv.db_sizes(db_path)
    return {
        "stats": stats,
        "pages": n_pages,
        "total_seconds": total,
        "fetch_seconds": fetch_acc[0],
        "parse_seconds": parse_acc[0],
        "write_seconds": write,
        "other_seconds": other,
        "pages_per_second": n_pages / total if total > 0 else None,
        "main_db_bytes": sizes["main"],
        "wal_bytes": sizes["wal"],
        "peak_rss_bytes": benv.peak_rss_bytes(),
    }
