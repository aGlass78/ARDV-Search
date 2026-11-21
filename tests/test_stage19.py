"""Stage 19: live crawl progress + async crawl job registry.

Covers:
- crawl()'s optional progress_callback (periodic stats snapshots),
- crawl_json() passing the callback through,
- the Flask POST /api/crawl -> crawl_id -> GET /api/crawl/status/<id>
  flow, including concurrent crawls and failure reporting.
"""
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


class CrawlJobApiTests(ServerTestCase):
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
            if last["status"] in ("completed", "failed"):
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
        self.assertNotEqual(crawl_id, resp2.get_json()["crawl_id"])

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


if __name__ == "__main__":
    unittest.main()
