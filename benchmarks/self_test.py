"""Harness self-checks (run before collecting performance data).

python -m benchmarks.self_test
"""

import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from .corpus import gen_rows, mutate_rows  # noqa: E402
from .writers import v2_fresh_db, legacy_v1_db, insert_rows, legacy_write_rows  # noqa: E402
from . import env as benv  # noqa: E402
from .experiments import run_e1, run_e2, run_e3, run_e4  # noqa: E402

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print(("  ok  " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main():
    print("self-test: corpus determinism")
    a = gen_rows(300, seed=7, mix="A")
    b = gen_rows(300, seed=7, mix="A")
    check("gen_rows A deterministic", a == b)
    c = gen_rows(300, seed=7, mix="B")
    d = gen_rows(300, seed=7, mix="B")
    check("gen_rows B deterministic", c == d)
    check("corpus A != B", a != c)
    check("urls unique", len({r[0] for r in a}) == 300)
    mb = mutate_rows(a)
    check("mutate_rows changes all bodies",
          all(x[3].endswith(" [mutated]") for x in mb) and len(mb) == 300)

    print("self-test: writers on tiny fresh DBs")
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        rows = gen_rows(120, seed=3, mix="A")

        p2 = td / "w2.db"
        conn = v2_fresh_db(p2)
        try:
            info = insert_rows(conn, rows)
            check("v2 insert commits=5 (batch25)", info["commits"] == 5,
                  str(info))
            n_pages = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
            n_fts = conn.execute(
                "SELECT count(*) FROM pages_fts").fetchone()[0]
            check("v2 pages+fts rows == 120", (n_pages, n_fts) == (120, 120),
                  f"{n_pages}/{n_fts}")
        finally:
            conn.close()
        sizes = benv.db_sizes(p2)
        check("db_sizes returns main >= size",
              sizes["main"] > 0 and set(sizes) == {"main", "wal", "shm"},
              str(sizes))
        check("peak_rss > 0", benv.peak_rss_bytes() > 0)

        p1 = td / "w1.db"
        conn = legacy_v1_db(p1)
        try:
            info1 = legacy_write_rows(conn, rows)
            check("v1 legacy commits=5", info1["commits"] == 5, str(info1))
            n1 = conn.execute("SELECT count(*) FROM pages").fetchone()[0]
            check("v1 pages rows == 120", n1 == 120, str(n1))
        finally:
            conn.close()

    print("self-test: experiment smoke runs (single rep, tiny)")
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        r1 = run_e1(1200, "A", 1, 1000.0, td)
        check("E1 v2 run ok", r1.get("status") == "ok", str(r1.get("status")))
        check("E1 v2 bands have 2 entries", len(r1.get("bands", [])) == 2)
        r2 = run_e2(1200, "A", 1, 1000.0, td)
        check("E2 v1 run ok", r2.get("status") == "ok", str(r2.get("status")))
        r3 = run_e3(1, td, n=200)
        check("E3 returns sync cost >= 0",
              r3.get("fts_sync_cost_seconds", -1) >= 0,
              f"{r3.get('fts_sync_cost_seconds')}")
        check("E3 identical <= mutated",
              r3["identical_recrawl_seconds"] <= r3["mutated_recrawl_seconds"])
        r4 = run_e4(1, td, n_pages=20)
        stats4 = r4.get("stats") or {}
        gate4 = (stats4.get("errors", 1) == 0
                 and stats4.get("db_errors", 1) == 0)
        check("E4 gate_ok (errors=0, db_errors=0)", gate4 is True, str(gate4))
        check("E4 indexed all pages",
              stats4.get("indexed", -1) == 20,
              f"indexed={stats4.get('indexed')}")

    print(f"\n{PASS.__len__()} passed, {len(FAIL)} failed")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAILED: {name} {detail}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
