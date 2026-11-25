"""Deterministic seeded synthetic page corpora for benchmarking.

Same seed + mix => identical rows (reproducibility requirement).
"""

import random

_SOURCE_WORDS = (
    "the quick brown fox jumps over lazy dog data index search page web "
    "crawl engine local network server request response html body text "
    "title description content word paragraph sentence example benchmark "
    "system performance scale grow many pages links find results query "
    "match rank builder index storage record row batch commit wal sync "
    "trigger upsert delete insert select match bm25 snippet token stem "
    "phrase rank score corpus seed deterministic reproducible measure "
    "median p95 latency throughput memory rss size disk read write order "
    "unique canonical normalize scheme host port path fragment redirect "
    "robot sitemap queue thread worker future timeout error retry "
    "bounded unbounded growth curve slope intercept extrapolate inspect"
).split()


def _text(rng, target_len):
    out = []
    n = 0
    while n < target_len:
        w = rng.choice(_SOURCE_WORDS)
        out.append(w)
        n += len(w) + 1
    return " ".join(out)[:target_len].rstrip()


def _mix_b_size(rng):
    r = rng.random()
    if r < 0.75:
        return 2000
    if r < 0.95:
        return 18000
    return 60000


def gen_rows(n, seed=1, mix="A"):
    """Return n rows of (url, title, description, body).

    mix="A": ~18KB fixed body (mirrors the original O(N^2) probe).
    mix="B": 75% 2KB / 20% 18KB / 5% 60KB bodies.
    """
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        if mix == "A":
            size = 18000 + (i % 7)  # small variance, stays ~18KB
        else:
            size = _mix_b_size(rng)
        url = f"http://bench{i % 500}.example/{i}"
        title = f"Title {i}"
        desc = f"Description for page {i}."
        rows.append((url, title, desc, _text(rng, size)))
    return rows


def mutate_rows(rows):
    """Return a copy where every body differs (forces FTS UPDATE triggers)."""
    return [(u, t, d, b + " [mutated]") for (u, t, d, b) in rows]
