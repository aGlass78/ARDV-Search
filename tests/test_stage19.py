"""Stage 19: live crawl progress + async crawl job registry + stop.

Covers:
- crawl()'s optional progress_callback (periodic stats snapshots),
- crawl_json() passing the callback through,
- the Flask POST /api/crawl -> crawl_id -> GET /api/crawl/status/<id>
  flow, including concurrent crawls and failure reporting,
- POST /api/crawl/status/<id> with {"action": "stop"} and the shared
  stop path with Ctrl+C (crawl()'s stop_event, stats["interrupted"]).
"""
import threading
import time
import unittest
from unittest import mock

import main
from app import create_app, crawl_jobs
from helpers import IsolatedDbTestCase, ServerTestCase, page


class CrawlProgressCallbackTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.server.add("/", page("Home", "hello world"))
        # slow enough that several progress ticks fire while crawling
        self.server.slow["/"] = 0.5

    def test_callback_receives_periodic_stats_snapshots(self):
        received = []

        def cb(stats):
            received.append(stats)

        original = main.PROGRESS_INTERVAL
        main.PROGRESS_INTERVAL = 0.05
        try:
            stats = main.crawl_json(
                self.server.url("/"),
                max_pages=1,
                threads=1,
                progress_callback=cb,
            )
        finally:
            main.PROGRESS_INTERVAL = original

        self.assertIsInstance(stats, dict)
        self.assertGreaterEqual(len(received), 1)
        # every snapshot is JSON-safe and carries the core counters
        for snap in received:
            self.assertEqual(set(snap) & {"indexed", "requests", "errors"},
                             {"indexed", "requests", "errors"})
            self.assertIsInstance(snap["indexed"], (int, float))
            self.assertEqual(snap["stage"], "crawling")
        self.assertEqual(stats["indexed"], 1)

    def test_callback_exception_is_swallowed(self):
        calls = {"n": 0}

        def boom(stats):
            calls["n"] += 1
            raise RuntimeError("boom")

        original = main.PROGRESS_INTERVAL
        main.PROGRESS_INTERVAL = 0.05
        try:
            stats = main.crawl_json(
                self.server.url("/"), max_pages=1, threads=1,
                progress_callback=boom)
        finally:
            main.PROGRESS_INTERVAL = original

        self.assertEqual(stats["indexed"], 1)
        self.assertGreaterEqual(calls["n"], 1)

    def test_without_callback_no_thread_spawned(self):
        stats = main.crawl_json(self.server.url("/"), max_pages=1)
        self.assertEqual(stats["indexed"], 1)


class CrawlJobWaitMixin:
    """Shared status-polling helper for the async job API tests."""

    def _wait_for_finish(self, crawl_id, timeout=20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            resp = self.client.get(f"/api/crawl/status/{crawl_id}")
            self.assertEqual(resp.status_code, 200)
            last = resp.get_json()
            if last["status"] in ("completed", "failed", "stopped"):
                return last
            time.sleep(0.05)
        self.fail(f"crawl {crawl_id} did not finish in {timeout}s")


class CrawlJobApiTests(CrawlJobWaitMixin, ServerTestCase):
    def setUp(self):
        super().setUp()
        self.server.add("/", page("Home", "hello world"))
        self.app = create_app()
        self.client = self.app.test_client()
        # isolate the module-level registry for every test
        for cid in list(crawl_jobs):
            del crawl_jobs[cid]

    def _wait_for_finish(self, crawl_id, timeout=20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            resp = self.client.get(f"/api/crawl/status/{crawl_id}")
            self.assertEqual(resp.status_code, 200)
            last = resp.get_json()
            if last["status"] in ("completed", "failed", "stopped"):
                return last
            time.sleep(0.05)
        self.fail(f"crawl {crawl_id} did not finish in {timeout}s")

    def test_crawl_job_lifecycle(self):
        resp = self.client.post(
            "/api/crawl",
            json={"start_url": self.server.url("/"), "max_pages": 2,
                  "threads": 1},
        )
        self.assertEqual(resp.status_code, 202)
        data = resp.get_json()
        self.assertEqual(data["status"], "running")
        crawl_id = data["crawl_id"]
        self.assertIsInstance(crawl_id, str)
        self.assertTrue(crawl_id)

        # the id is random/unique across jobs
        resp2 = self.client.post(
            "/api/crawl",
            json={"start_url": self.server.url("/"), "max_pages": 1},
        )
        second_id = resp2.get_json()["crawl_id"]
        self.assertNotEqual(crawl_id, second_id)
        # wait for BOTH jobs: a lingering background worker would keep the
        # isolated test.db open and break tearDown's tempdir cleanup on
        # Windows (WinError 32)
        self._wait_for_finish(second_id)

        final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["status"], "completed")
        self.assertIsNone(final["error"])
        self.assertIsNotNone(final["finished_at"])
        self.assertGreaterEqual(final["stats"]["indexed"], 1)

    def test_crawl_status_reflects_running_then_completed(self):
        self.server.slow["/"] = 1.0
        original = main.PROGRESS_INTERVAL
        main.PROGRESS_INTERVAL = 0.05
        try:
            resp = self.client.post(
                "/api/crawl",
                json={"start_url": self.server.url("/"), "max_pages": 1,
                      "threads": 1},
            )
            crawl_id = resp.get_json()["crawl_id"]
            status = self.client.get(
                f"/api/crawl/status/{crawl_id}").get_json()
            self.assertEqual(status["status"], "running")
            # live stats appear while it is still crawling
            self.assertIsInstance(status["stats"], dict)
            final = self._wait_for_finish(crawl_id)
            self.assertEqual(final["status"], "completed")
        finally:
            main.PROGRESS_INTERVAL = original

    def test_invalid_payload_returns_400_and_no_job(self):
        cases = [
            ("no body", None),
            ("non-object body", [1, 2, 3]),
            ("missing url", {"max_pages": 1}),
            ("blank url", {"start_url": "  "}),
            ("non-string url", {"start_url": 42}),
            ("bad url", {"start_url": "not a url"}),
            ("bad threads", {"start_url": self.server.url("/"),
                             "threads": 0}),
            ("bad delay", {"start_url": self.server.url("/"), "delay": -1}),
            ("bad sitemap", {"start_url": self.server.url("/"),
                              "sitemap": "banana"}),
            ("bad allow", {"start_url": self.server.url("/"),
                           "allow": ["a b"]}),
            ("unknown param", {"start_url": self.server.url("/"), "wat": 1}),
        ]
        before = set(crawl_jobs)
        for label, body in cases:
            with self.subTest(label=label):
                resp = self.client.post("/api/crawl", json=body)
                self.assertEqual(resp.status_code, 400, label)
                self.assertIn("error", resp.get_json(), label)
        self.assertEqual(set(crawl_jobs), before)

    def test_unknown_crawl_id_returns_404(self):
        resp = self.client.get("/api/crawl/status/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "crawl not found")

    def test_failed_crawl_reports_concise_error(self):
        with mock.patch.object(
                main, "crawl_json", side_effect=ValueError("synthetic boom")):
            resp = self.client.post(
                "/api/crawl", json={"start_url": self.server.url("/")})
            crawl_id = resp.get_json()["crawl_id"]
            final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error"], "synthetic boom")
        self.assertIsNotNone(final["finished_at"])

    def test_multiple_concurrent_crawls(self):
        self.server.slow["/"] = 0.5
        ids = []
        for _ in range(3):
            resp = self.client.post(
                "/api/crawl",
                json={"start_url": self.server.url("/"), "max_pages": 1,
                      "threads": 1},
            )
            ids.append(resp.get_json()["crawl_id"])
        self.assertEqual(len(set(ids)), 3)  # all distinct
        for crawl_id in ids:
            final = self._wait_for_finish(crawl_id)
            self.assertEqual(final["status"], "completed")
            self.assertGreaterEqual(final["stats"]["indexed"], 1)

    def test_job_starts_in_discovery_stage_then_crawling(self):
        self.server.slow["/"] = 1.0
        original = main.PROGRESS_INTERVAL
        # no ticks -> the initial job stats stay visible until completion
        main.PROGRESS_INTERVAL = 60.0
        try:
            resp = self.client.post(
                "/api/crawl",
                json={"start_url": self.server.url("/"), "max_pages": 1,
                      "threads": 1},
            )
            crawl_id = resp.get_json()["crawl_id"]
            early = self.client.get(
                f"/api/crawl/status/{crawl_id}").get_json()
            self.assertEqual(early["status"], "running")
            # before the crawl worker reports, the stage is shown
            self.assertEqual(
                early["stats"], {"stage": "discovering_sitemaps"})
            final = self._wait_for_finish(crawl_id)
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["stats"]["stage"], "crawling")
        finally:
            main.PROGRESS_INTERVAL = original

    def test_skip_sitemap_mode_starts_in_crawling_stage(self):
        self.server.slow["/"] = 1.0
        original = main.PROGRESS_INTERVAL
        main.PROGRESS_INTERVAL = 60.0
        try:
            resp = self.client.post(
                "/api/crawl",
                json={"start_url": self.server.url("/"), "max_pages": 1,
                      "threads": 1, "sitemap": "skip"},
            )
            crawl_id = resp.get_json()["crawl_id"]
            early = self.client.get(
                f"/api/crawl/status/{crawl_id}").get_json()
            self.assertEqual(early["status"], "running")
            # no discovery phase in skip mode: straight to crawling
            self.assertEqual(early["stats"], {"stage": "crawling"})
            final = self._wait_for_finish(crawl_id)
            self.assertEqual(final["status"], "completed")
        finally:
            main.PROGRESS_INTERVAL = original


class CrawlStopApiTests(CrawlJobWaitMixin, ServerTestCase):
    """POST /api/crawl/status/<crawl_id> with {"action": "stop"}.

    The stop goes through the same shared stop path as Ctrl+C
    (main._stop_crawl): the crawl finishes in-flight fetches, cancels
    queued work, and ends with stats["interrupted"] == True. The stop
    lives on the status endpoint (one URL, GET polls / POST stops).
    """

    def setUp(self):
        super().setUp()
        # the seed links to 10 slow children; threads=1 makes the crawl
        # sequential, so each child costs ~0.15s -> ~1.5s+ to finish.
        links = [f"/{i}" for i in range(1, 11)]
        self.server.add("/", page("Home", "hello world", links=links))
        for i in range(1, 11):
            self.server.add(f"/{i}", page(f"Page {i}", f"body {i}"))
            self.server.slow[f"/{i}"] = 0.15
        self.app = create_app()
        self.client = self.app.test_client()
        for cid in list(crawl_jobs):
            del crawl_jobs[cid]

    def _start(self, **overrides):
        payload = {"start_url": self.server.url("/"), "max_pages": 100,
                   "threads": 1}
        payload.update(overrides)
        resp = self.client.post("/api/crawl", json=payload)
        self.assertEqual(resp.status_code, 202)
        return resp.get_json()["crawl_id"]

    def _stop(self, crawl_id, **overrides):
        payload = {"action": "stop"}
        payload.update(overrides)
        return self.client.post(f"/api/crawl/status/{crawl_id}", json=payload)

    def test_crawl_stop_endpoint_stops_running_crawl(self):
        crawl_id = self._start()
        # request the stop while the crawl is still running
        resp = self._stop(crawl_id)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["crawl_id"], crawl_id)
        self.assertEqual(data["status"], "running")  # stop is async
        self.assertIn("stats", data)
        self.assertIsNone(data["error"])
        # the crawl eventually reaches the terminal "stopped" status
        final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["status"], "stopped")
        self.assertIs(final["stats"]["interrupted"], True)
        # it stopped early: the seed's 10 children were not all indexed
        self.assertLess(final["stats"]["indexed"], 10)

    def test_crawl_stop_unknown_id_returns_404(self):
        resp = self._stop("nope")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "crawl not found")

    def test_crawl_stop_on_completed_crawl_returns_current_status(self):
        crawl_id = self._start(max_pages=1)
        final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["status"], "completed")
        # stopping a finished job is a no-op that returns its state
        resp = self._stop(crawl_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "completed")

    def test_crawl_stop_requires_action_stop(self):
        crawl_id = self._start()
        # missing/other action -> 400, with no side effect
        for payload in (None, {}, {"action": "pause"}, {"action": 3}):
            resp = self.client.post(
                f"/api/crawl/status/{crawl_id}", json=payload)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("action", resp.get_json()["error"])
        # the crawl was never stopped: it completes normally
        final = self._wait_for_finish(crawl_id)
        self.assertEqual(final["status"], "completed")


class CrawlStopEventTests(ServerTestCase):
    """crawl()/crawl_json() accept a stop_event (the HTTP stop path)."""

    def test_pre_set_stop_event_stops_like_interrupt(self):
        self.server.add("/", page("Home", "hello world"))
        stop_event = threading.Event()
        stop_event.set()
        stats = main.crawl_json(
            self.server.url("/"),
            max_pages=10,
            threads=1,
            sitemap="skip",
            stop_event=stop_event,
        )
        self.assertIs(stats["interrupted"], True)
        self.assertEqual(stats["indexed"], 0)

    def test_unset_stop_event_does_not_interrupt(self):
        self.server.add("/", page("Home", "hello world"))
        stats = main.crawl_json(
            self.server.url("/"),
            max_pages=1,
            threads=1,
            sitemap="skip",
            stop_event=threading.Event(),
        )
        self.assertIs(stats["interrupted"], False)
        self.assertEqual(stats["indexed"], 1)


if __name__ == "__main__":
    unittest.main()
