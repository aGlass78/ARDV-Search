"""Stage 7 tests: Ctrl+C handling and the missing-database message.

1. Ctrl+C during a crawl stops indexing but still returns the stats:
   queued work is cancelled, in-flight results are not indexed, the
   open database batch is flushed, and the summary reports the run
   as interrupted (partial statistics).
2. Searching without a database reports "No database found" instead
   of "No results found.", and searching never creates the database
   file as a side effect.
"""
import contextlib
import io
import unittest
from unittest import mock

from helpers import IsolatedDbTestCase, ServerTestCase, main, page


def make_interrupting_wait(real_wait, raise_on_call):
    """Wrap concurrent.futures.wait to raise KeyboardInterrupt after
    the Nth call, simulating Ctrl+C while the crawl is in progress."""
    state = {"calls": 0}

    def fake_wait(futures, return_when=None):
        state["calls"] += 1
        if state["calls"] >= raise_on_call:
            raise KeyboardInterrupt
        return real_wait(futures, return_when=return_when)

    return fake_wait


class KeyboardInterruptTests(ServerTestCase):
    def _add_site(self, count=5):
        s = self.server
        s.add_robots("User-agent: *\nAllow: /\n")
        s.add("/seed", page("Seed", "seed body",
                            [f"/p{i}" for i in range(count)]))
        for i in range(count):
            s.add(f"/p{i}", page(f"P{i}", f"page {i}"))

    def test_interrupt_mid_crawl_returns_stats(self):
        self._add_site()
        real_wait = main.wait

        with mock.patch.object(main, "wait",
                               make_interrupting_wait(real_wait, 2)), \
             mock.patch.object(main, "DB_COMMIT_BATCH", 1_000_000):
            stats = self.crawl(self.server.url("/seed"),
                               max_pages=20, delay=0.02, threads=1)

        # No exception escaped; the partial run is described honestly.
        self.assertTrue(stats["interrupted"])
        self.assertEqual(stats["indexed"], 1)          # only the seed
        self.assertEqual(self.indexed_paths(), ["/seed"])
        self.assertGreater(stats["queued"], 0)          # unvisited URLs left
        # The interrupt path still flushed the open batch to disk.
        self.assertEqual(len(self.indexed_paths()), 1)

    def test_interrupt_before_first_result_returns_empty_stats(self):
        self._add_site()
        real_wait = main.wait

        with mock.patch.object(main, "wait",
                               make_interrupting_wait(real_wait, 1)):
            stats = self.crawl(self.server.url("/seed"),
                               max_pages=20, delay=0.02, threads=1)

        self.assertTrue(stats["interrupted"])
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(self.indexed_paths(), [])

    def test_normal_crawl_is_not_marked_interrupted(self):
        self._add_site(count=2)
        stats = self.crawl(self.server.url("/seed"),
                           max_pages=10, delay=0.02, threads=1)

        self.assertFalse(stats["interrupted"])
        self.assertEqual(stats["indexed"], 3)  # seed + 2 pages


class InterruptSummaryTests(unittest.TestCase):
    def _full_stats(self, **overrides):
        stats = {
            "indexed": 4,
            "elapsed_seconds": 2.0,
            "threads": 2,
            "requests": 9,
            "links_discovered": 9,
            "queued": 0,
            "robots_blocked": 0,
            "http_errors": 0,
            "rate_limited": 0,
            "rate_limited_retries": 0,
            "non_html": 0,
            "too_large": 0,
            "not_indexable": 0,
            "duplicates": 0,
            "out_of_scope": 0,
            "robots_unavailable": 0,
            "errors": 0,
            "interrupted": False,
        }
        stats.update(overrides)
        return stats

    def test_summary_reports_interrupted_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.print_crawl_summary(self._full_stats(interrupted=True))
        self.assertIn("interrupted (Ctrl+C)", out.getvalue())
        self.assertNotIn("Crawl finished.", out.getvalue())

    def test_summary_reports_finished_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.print_crawl_summary(self._full_stats())
        self.assertIn("Crawl finished.", out.getvalue())
        self.assertNotIn("interrupted", out.getvalue())


class MissingDatabaseTests(IsolatedDbTestCase):
    def test_search_reports_missing_database(self):
        self.assertFalse(self.db_path.exists())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.search("anything", limit=5)

        text = out.getvalue()
        self.assertIn("No database found", text)
        self.assertIn(str(self.db_path), text)
        self.assertNotIn("No results found.", text)

    def test_search_does_not_create_the_database(self):
        with contextlib.redirect_stdout(io.StringIO()):
            main.search("anything", limit=5)
        self.assertFalse(self.db_path.exists())

    def test_run_search_returns_empty_without_database(self):
        self.assertEqual(main.run_search("anything", limit=5), [])
        self.assertFalse(self.db_path.exists())

    def test_search_reports_no_results_when_database_exists(self):
        main.get_database().close()  # create an empty index

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.search("anything", limit=5)

        self.assertIn("No results found.", out.getvalue())
        self.assertNotIn("No database found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
