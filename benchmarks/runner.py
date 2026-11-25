"""Phase 5.2 runner: runs E1-E4 with repetitions, gates, and raw saving.

Usage (from repo root):
    python -m benchmarks.runner --out results
    python -m benchmarks.runner --quick        # tiny smoke (self-test-ish)
    python -m benchmarks.runner --only e1 --quick
"""

import argparse
import json
import pathlib
import statistics
import sys
import tempfile

from . import env as benv
from .corpus import gen_rows
from .experiments import run_e1, run_e2, run_e3, run_e4
from .writers import v2_fresh_db, legacy_v1_db, insert_rows, legacy_write_rows

NSWEEP = [1000, 5000, 10000, 20000, 30000]
MIXES = ("A", "B")


def _median(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return statistics.median(vals)


def _save_json(path, obj):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)


def _run_scaling_exp(kind, n, mix, seed, budget, reps, tmpbase, env):
    raw = []
    for rep in range(reps):
        with tempfile.TemporaryDirectory(dir=tmpbase) as td:
            result = (run_e1(n, mix, seed, budget, td)
                      if kind == "v2" else run_e2(n, mix, seed, budget, td))
        rec = {
            "experiment": f"E1" if kind == "v2" else "E2",
            "kind": kind, "n": n, "mix": mix, "seed": seed, "rep": rep,
            "config": {"batch": 25, "journal_mode": "WAL",
                       "synchronous": "FULL", "busy_timeout": 5000},
            "env": env, "result": result,
        }
        raw.append(rec)
        _save_json(pathlib.Path(tmpbase) / "raw"
                   / f"e{1 if kind == 'v2' else 2}_{kind}_{n}_{mix}_rep{rep}.json",
                   rec)
    return raw


def _aggregate_scaling(raw, kind):
    by = {}
    for rec in raw:
        if rec["kind"] != kind:
            continue
        if rec["result"].get("status") == "failed":
            continue
        key = (rec["n"], rec["mix"])
        by.setdefault(key, []).append(rec["result"])
    agg = {}
    for (n, mix), results in sorted(by.items()):
        agg[f"{n}|{mix}"] = {
            "n": n, "mix": mix, "reps": len(results),
            "rows_per_second_total": _median(
                [r["rows_per_second_total"] for r in results]),
            "total_seconds_median": _median(
                [r["total_seconds"] for r in results]),
            "max_status": max((r["status"] for r in results),
                              default="none"),
            "bands": [],
        }
        # align bands by end_index
        ends = sorted({b["end_index"] for r in results for b in r["bands"]})
        for end in ends:
            secs = []
            for r in results:
                for b in r["bands"]:
                    if b["end_index"] == end and b.get("status") != "skipped" \
                            and b["rows"]:
                        secs.append(b["seconds"])
            agg[f"{n}|{mix}"]["bands"].append({
                "end_index": end,
                "seconds_median": _median(secs),
                "rows_per_s_median": (
                    end / _median(secs) if _median(secs) else None),
            })
    return agg


def _run_e3(seed, reps, tmpbase, env):
    raw = []
    for rep in range(reps):
        with tempfile.TemporaryDirectory(dir=tmpbase) as td:
            rec = {
                "experiment": "E3", "seed": seed, "rep": rep,
                "env": env, "result": run_e3(seed, td),
            }
            raw.append(rec)
            _save_json(pathlib.Path(tmpbase) / "raw"
                       / f"e3_seed{seed}_rep{rep}.json", rec)
    agg = {}
    if raw:
        agg = {
            "fill_seconds_median": _median(
                [r["result"]["fill_seconds"] for r in raw]),
            "identical_recrawl_seconds_median": _median(
                [r["result"]["identical_recrawl_seconds"] for r in raw]),
            "mutated_recrawl_seconds_median": _median(
                [r["result"]["mutated_recrawl_seconds"] for r in raw]),
            "fts_sync_cost_seconds_median": _median(
                [r["result"]["fts_sync_cost_seconds"] for r in raw]),
            "n": raw[0]["result"].get("n"),
        }
    return raw, agg


def _run_e4(seed, reps, tmpbase, env, pages=400):
    raw = []
    for rep in range(reps):
        with tempfile.TemporaryDirectory(dir=tmpbase) as td:
            result = run_e4(seed, td, n_pages=pages)
            stats = result.get("stats") or {}
            gate_ok = (stats.get("errors", 1) == 0
                        and stats.get("db_errors", 1) == 0)
            result["gate_ok"] = gate_ok
            raw.append({
                "experiment": "E4", "seed": seed, "rep": rep,
                "pages": pages, "env": env, "result": result,
            })
    ok = [r["result"] for r in raw if r["result"].get("gate_ok")]
    agg = {}
    if ok:
        agg = {
            "reps": len(ok),
            "pages_per_second_median": _median(
                [r["pages_per_second"] for r in ok]),
            "fetch_share_median": _median(
                [r["fetch_seconds"] / r["total_seconds"] for r in ok]),
            "parse_share_median": _median(
                [r["parse_seconds"] / r["total_seconds"] for r in ok]),
            "write_share_median": _median(
                [r["write_seconds"] / r["total_seconds"] for r in ok]),
            "other_share_median": _median(
                [r["other_seconds"] / r["total_seconds"] for r in ok]),
            "main_db_bytes_median": _median(
                [r["main_db_bytes"] for r in ok]),
            "wal_bytes_median": _median(
                [r["wal_bytes"] for r in ok]),
            "peak_rss_bytes_median": _median(
                [r["peak_rss_bytes"] for r in ok]),
            "pages": ok[0].get("n_pages"),
        }
    return raw, agg, sum(1 for r in raw if not r["result"].get("gate_ok"))


def main():
    ap = argparse.ArgumentParser(description="Phase 5.2 benchmarks")
    ap.add_argument("--out", default="results")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--budget-per-band-sec", type=float, default=180.0)
    ap.add_argument("--only", choices=["e1", "e2", "e3", "e4"],
                    action="append")
    ap.add_argument("--n", type=int, action="append",
                    help="N sweep entries to run (repeatable; default all)")
    ap.add_argument("--mix", choices=["A", "B"], action="append",
                    help="mixes to run (repeatable; default A and B)")
    ap.add_argument("--quick", action="store_true",
                    help="tiny smoke: N=1200, 1 rep, e1/e2; e3 n=200; "
                         "e4 pages=20")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tmpbase = str(out / "tmp")
    pathlib.Path(tmpbase).mkdir(exist_ok=True)

    env = benv.fingerprint()
    reps = 1 if args.quick else args.reps
    budget = 1000.0 if args.quick else args.budget_per_band_sec
    sweep = [1200] if args.quick else (args.n or NSWEEP)
    mixes = ("A",) if args.quick else (args.mix or MIXES)
    only = set(args.only or ["e1", "e2", "e3", "e4"])

    # Warm-up priming (unmeasured): prime OS caches/AV before any
    # measured run, so first-run cold effects do not bias any rep.
    try:
        with tempfile.TemporaryDirectory(dir=tmpbase) as _td:
            rows = gen_rows(50, seed=0, mix="A")
            c = v2_fresh_db(pathlib.Path(_td) / "warm.db")
            insert_rows(c, rows)
            c.close()
            c = legacy_v1_db(pathlib.Path(_td) / "warm1.db")
            legacy_write_rows(c, rows)
            c.close()
        env["warmup_primed"] = True
    except Exception:  # noqa: BLE001 - never fail the run for warm-up
        env["warmup_primed"] = False

    results = {"args": vars(args), "env": env}
    agg = {}

    if "e1" in only or "e2" in only:
        for kind in ("v2", "v1"):
            if ("e1" in only and kind == "v2") or \
               ("e2" in only and kind == "v1"):
                for n in sweep:
                    for mix in mixes:
                        raw = _run_scaling_exp(
                            kind, n, mix, args.seed, budget, reps,
                            tmpbase, env)
                        results.setdefault("raw", []).extend(raw)
                        agg[f"E{'1' if kind == 'v2' else '2'}"] = \
                            _aggregate_scaling(raw, kind)

    if "e3" in only:
        n = 200 if args.quick else 10000
        raw, a3 = _run_e3(args.seed, reps, tmpbase, env)
        results.setdefault("raw", []).extend(raw)
        agg["E3"] = a3
        agg["E3"]["n"] = n

    if "e4" in only:
        pages = 20 if args.quick else 400
        raw, a4, failed_gate = _run_e4(args.seed, reps, tmpbase, env, pages)
        results.setdefault("raw", []).extend(raw)
        agg["E4"] = a4
        agg["E4"]["pages"] = pages
        agg["E4"]["gate_failed_reps"] = failed_gate
        for rec in raw:
            _save_json(pathlib.Path(tmpbase) / "raw"
                       / f"e4_seed{args.seed}_rep{rec['rep']}.json", rec)

    results["summary"] = agg
    _save_json(out / "summary.json", results)

    # console table
    print("=== Phase 5.2 summary (median across accepted reps) ===")
    for exp in ("E1", "E2"):
        if exp in agg and agg[exp]:
            print(f"\n{exp} rows/s total by N|mix:")
            for key, row in agg[exp].items():
                print(f"  N|mix={key}: {row['rows_per_second_total']} rows/s "
                      f"(reps={row['reps']}, status={row['max_status']})")
    if "E3" in agg and agg["E3"]:
        print("\nE3 recrawl (10k/200 rows):")
        for k, v in agg["E3"].items():
            print(f"  {k}: {v}")
    if "E4" in agg and agg["E4"]:
        print("\nE4 pipeline shares (threads=1):")
        for k, v in agg["E4"].items():
            print(f"  {k}: {v}")
    print(f"\nRaw results saved under {out / 'raw'}")
    print(f"Summary: {out / 'summary.json'}")


if __name__ == "__main__":
    main()
