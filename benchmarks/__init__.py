"""Benchmark harness for Phase 5 (write-performance + scaling).

Package layout
--------------
env.py         environment fingerprint, peak-RSS (Windows-compatible),
               consistent DB-size measurement
corpus.py      deterministic seeded synthetic page corpora
writers.py     production-faithful v2 (UPSERT) and legacy v1
               (DELETE+INSERT) write loops
experiments.py E1-E4 measurement functions (no orchestration)
runner.py      CLI: runs repetitions, enforces gates/budget, saves raw
               JSON results and prints median tables
self_test.py   harness self-checks (run before collecting data)
"""
