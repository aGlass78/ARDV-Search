"""Phase 5.2 aggregator: read raw per-rep JSON files and produce medians.

Reads every raw file under <out>/tmp/raw/ (each written by the runner after
the rep completes), aligns scaling bands by end_index, excludes failed reps,
and prints median tables. Also writes <out>/summary.json in the same shape
the runner would have produced.

Usage:
    python -m benchmarks.aggregate --out results
"""

import argparse
import json
import pathlib
import statistics


def _median(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return statistics.median(vals)


def _load_raw(tmpbase):
    rawdir = pathlib.Path(tmpbase) / "raw"
    if not rawdir.exists():
        return []
    recs = []
    for p in sorted(rawdir.glob("*.json")):
        with open(p, encoding="utf-8") as fh:
            recs.append(json.load(fh))
    return recs


def aggregate_scaling(recs, kind):
    by = {}
    for rec in recs:
        if rec.get("kind") != kind:
            continue
        if rec.get("result", {}).get("status") == "failed":
            continue
        key = (rec["n"], rec["mix"])
        by.setdefault(key, []).append(rec["result"])
    agg = {}
    for (n, mix), results in sorted(by.items()):
        rows_s = [_median([r["rows_per_second_total"] for r in results])]
        row = {
            "n": n, "mix": mix, "reps": len(results),
            "rows_per_second_total": _median(
                [r["rows_per_second_total"] for r in results]),
            "total_seconds_median": _median(
                [r["total_seconds"] for r in results]),
            "max_status": max((r["status"] for r in results),
                              default="none"),
            "bands": [],
        }
        ends = sorted({b["end_index"] for r in results for b in r["bands"]})
        for end in ends:
            secs = []
            for r in results:
                for b in r["bands"]:
                    if b["end_index"] == end and b.get("status") != "skipped" \
                            and b["rows"]:
                        secs.append(b["seconds"])
            if secs:
                row["bands"].append({
                    "end_index": end,
                    "seconds_median": _median(secs),
                    "rows_per_s_median": (
                        end / _median(secs) if _median(secs) else None),
                })
        agg[f"{n}|{mix}"] = row
    return agg


def aggregate_e3(recs):
    raw = [r for r in recs if r.get("experiment") == "E3"]
    if not raw:
        return {}
    return {
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


def aggregate_e4(recs):
    raw = [r for r in recs if r.get("experiment") == "E4"]
    ok = [r["result"] for r in raw if r["result"].get("gate_ok")]
    if not ok:
        return {"gate_failed_reps": len(raw) - len(ok), "reps": len(ok)}
    return {
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
        "pages": raw[0].get("pages") if raw else None,
        "gate_failed_reps": len(raw) - len(ok),
    }


def main():
    ap = argparse.ArgumentParser(description="Phase 5.2 aggregator")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    tmpbase = str(out / "tmp")
    recs = _load_raw(tmpbase)
    if not recs:
        print(f"no raw files under {tmpbase}/raw")
        return

    env = recs[0].get("env", {})
    agg = {
        "E1": aggregate_scaling(recs, "v2"),
        "E2": aggregate_scaling(recs, "v1"),
        "E3": aggregate_e3(recs),
        "E4": aggregate_e4(recs),
    }
    results = {"args": {"aggregated_from_raw": str(pathlib.Path(tmpbase) / "raw")},
               "env": env, "summary": agg}
    with open(out / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)

    print("=== Phase 5.2 summary (median across accepted reps) ===")
    for exp in ("E1", "E2"):
        if agg[exp]:
            print(f"\n{exp} rows/s total by N|mix:")
            for key, row in agg[exp].items():
                print(f"  N|mix={key}: {row['rows_per_second_total']} rows/s "
                      f"(reps={row['reps']}, status={row['max_status']})")
            print(f"  {exp} band seconds (median) by N|mix:")
            for key, row in agg[exp].items():
                bstr = ", ".join(
                    f"{b['end_index']}:{b['seconds_median']:.2f}s"
                    for b in row["bands"])
                print(f"  {key}: [{bstr}]")
    if agg["E3"]:
        print("\nE3 recrawl (10k):")
        for k, v in agg["E3"].items():
            print(f"  {k}: {v}")
    if agg["E4"]:
        print("\nE4 pipeline shares (threads=1):")
        for k, v in agg["E4"].items():
            print(f"  {k}: {v}")
    print(f"\nSummary: {out / 'summary.json'}")


if __name__ == "__main__":
    main()
