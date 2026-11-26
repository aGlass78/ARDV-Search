"""
Flask HTTP API for ModelSearchEngine (main.py).

The search engine itself lives in main.py and is fully independent of
Flask; this module is a thin web adapter. The engine is never modified
here - app.py only calls main.search_json() / main.crawl_json() and
reads a couple of main-module constants.

Start the server (pick one)::

    python -m flask --app app run
    .venv\\Scripts\\flask.exe --app app run
    python app.py          # binds 0.0.0.0:5000 (all interfaces)

`python app.py` binds to 0.0.0.0:5000 so the server is reachable on
every interface — localhost, LAN addresses, and public addresses such
as sslip.io wildcard DNS (e.g. http://<ip>.sslip.io:5000). `flask
--app app run` defaults to 127.0.0.1:5000; pass `--host 0.0.0.0` to
bind all interfaces too.

Endpoints
---------
GET /api/search?q=<query>[&limit=N][&exact=1][&stem=1][&db=FILE]
    JSON search response (the payload schema returned by
    main.search_json()). See that function for the field list.
    Errors:
      - 400 limit is not an integer, limit < 1, or exact+stem both set
      - 404 the requested database file does not exist
      - 500 any other database/unexpected failure (logged)

POST /api/crawl
    Starts a crawl in the background and returns immediately. The
    request body is a JSON object with the crawl config:

        {
          "start_url": "https://quotes.toscrape.com",   # required
          "max_pages": 100,       # optional, positive int
          "threads": 8,           # optional, positive int
          "delay": 0.5,           # optional, non-negative number
          "max_bytes": 2000000,   # optional, positive int
          "allow": ["host", "host/path"],   # optional, list of str
          "sitemap": "auto",      # optional: "auto" | "skip" | "only"
          "db": "myindex.db",     # optional, str (also accepts "url"
                                  #   as an alias for "start_url")
          "crawl_unavailable_robots": false,  # optional, bool: crawl
                                  #   pages whose robots.txt cannot be
                                  #   verified (default false)
        }

    Omitted/null fields use engine defaults. Response (HTTP 202):

        {"crawl_id": "c83f91a2", "status": "running"}

    Validation failures return 400 with {"error": <message>} and do
    NOT create a job. Multiple crawls may run at the same time, each
    with its own crawl_id.

GET /api/crawl/status/<crawl_id>
    Live status for a crawl job (poll every second or so):

        {
          "crawl_id": "c83f91a2",
          "status": "running" | "completed" | "stopped" | "failed",
          "stats": { ...current crawl stats dict... },
          "error": null | "concise failure message",
          "started_at": <unix seconds>,
          "finished_at": null | <unix seconds>
        }

    "stopped" means the crawl was stopped via the POST action below
    (or Ctrl+C in the CLI); its stats carry "interrupted": true.
    Unknown crawl_id -> 404 {"error": "crawl not found"}.

POST /api/crawl/status/<crawl_id>
    Same URL; the body is `{"action": "stop"}` to request a graceful
    stop of a running crawl. Sets the job's stop event (the same
    shared stop path as Ctrl+C in main.py); in-flight fetches finish,
    queued work is cancelled, and the job ends with status "stopped"
    and stats "interrupted": true. Response is the current job snapshot
    (same shape as GET). Stopping a non-running job is a no-op that
    returns its current state. Missing/other action -> 400; unknown
    crawl_id -> 404 {"error": "crawl not found"}.

DELETE /api/database
    Deletes a database file (the frontend "Delete Database" control in
    both Search and Crawl settings). Body is JSON:

        {"database": "<db name>"}

    The name is resolved exactly like --db: relative names anchor next
    to main.py, and ".db" is appended when the name does not already
    end with ".db" (the frontend already normalizes this before
    sending). Responses:

      - 200 {"deleted": true, "database": "<resolved name>"} — on
        success; any residual -wal/-shm sidecars are best-effort
        removed too.
      - 404 {"error": "database not found", "database": "<name>"} —
        the resolved file does not exist.
      - 400 {"error": "<message>"} — body is not a JSON object, or
        "database" is missing/not a non-empty string.
      - 409 {"error": "<message>"} — the database is currently in use
        and cannot be safely removed (e.g. a running crawl job is
        writing to it, or the file is locked by another process).

GET /api/status
    Lightweight liveness: engine name, default database path, and
    whether that database exists.

GET /
    Serves templates/index.html (the UI shell).
"""

import os
import pathlib
import secrets
import threading
import time

from flask import Flask, jsonify, render_template, request

import main


crawl_jobs = {}          # crawl_id -> job dict
_jobs_lock = threading.Lock()
_JOB_TTL_SECONDS = 3600  # finished jobs are pruned after this long


def create_app():
    app = Flask(__name__)

    def _error(message, status=400):
        return jsonify(error=message), status

    def _job_snapshot(job):
        """Consistent, JSON-safe view of a crawl job.

        Reads every serializable field into a plain dict. The stop_event
        (a threading.Event) is intentionally excluded.
        """
        return {
            "status": job["status"],
            "stats": dict(job["stats"]),
            "error": job["error"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        }

    def _flag(value):
        return value is not None and value.lower() in ("1", "true", "yes", "on")

    # ---------- search ----------

    @app.get("/api/search")
    def api_search():
        raw_limit = request.args.get("limit")
        limit = 10
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                return _error("limit must be an integer", 400)
            if limit < 1:
                return _error("limit must be at least 1", 400)

        query = request.args.get("q", "")

        if _flag(request.args.get("exact")) and _flag(request.args.get("stem")):
            return _error("exact and stem are mutually exclusive", 400)

        try:
            payload = main.search_json(
                query,
                limit=limit,
                exact=_flag(request.args.get("exact")),
                stem=_flag(request.args.get("stem")),
                db_path=request.args.get("db") or None,
            )
        except main.DatabaseNotFoundError as exc:
            return jsonify(
                error="database not found",
                database=str(exc),
            ), 404
        except ValueError:
            return _error("invalid search parameters", 400)
        except Exception:
            app.logger.exception("search request failed")
            return _error("internal search error", 500)

        return jsonify(payload)

    # ---------- crawl (async job registry) ----------

    def _new_crawl_id():
        with _jobs_lock:
            now = time.time()
            # prune old finished jobs so the registry does not grow forever
            for cid in list(crawl_jobs):
                job = crawl_jobs[cid]
                if (job["status"] in ("completed", "failed", "stopped")
                        and job.get("finished_at")
                        and now - job["finished_at"] > _JOB_TTL_SECONDS):
                    del crawl_jobs[cid]
            while True:
                cid = secrets.token_hex(4)
                if cid not in crawl_jobs:
                    return cid

    def _run_crawl_job(crawl_id, kwargs):
        job = crawl_jobs[crawl_id]

        def _progress(stats):
            with _jobs_lock:
                job["stats"] = stats

        try:
            final_stats = main.crawl_json(
                **kwargs,
                progress_callback=_progress,
                stop_event=job["stop_event"])
            status = "stopped" if final_stats.get("interrupted") else "completed"
            with _jobs_lock:
                job["stats"] = final_stats
                job["status"] = status
                job["finished_at"] = time.time()
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            if len(message) > 500:
                message = message[:500] + "…"
            with _jobs_lock:
                job["stats"] = {}
                job["status"] = "failed"
                job["error"] = message
                job["finished_at"] = time.time()

    @app.post("/api/crawl")
    def api_crawl():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error("request body must be a JSON object", 400)

        url = data.get("start_url") or data.get("url")
        if not isinstance(url, str) or not url.strip():
            return _error("start_url must be a non-empty string", 400)

        allowed_keys = {
            "start_url", "url", "max_pages", "delay", "max_bytes",
            "threads", "allow", "sitemap", "db",
            "crawl_unavailable_robots",
        }
        unexpected = set(data) - allowed_keys
        if unexpected:
            return _error(
                f"unexpected parameter: {sorted(unexpected)[0]}", 400)

        kwargs = {
            "max_pages": data.get("max_pages"),
            "delay": data.get("delay"),
            "max_bytes": data.get("max_bytes"),
            "threads": data.get("threads"),
            "allow": data.get("allow"),
            "sitemap": data.get("sitemap"),
            "db": data.get("db"),
            "crawl_unavailable_robots": data.get("crawl_unavailable_robots"),
        }

        try:
            normalized = main._normalize_crawl_args(url, **kwargs)
        except ValueError as exc:
            return _error(str(exc), 400)

        crawl_id = _new_crawl_id()
        # Before the crawl worker starts reporting real stats, show a
        # stage so the frontend can display what the crawl is doing.
        # Sitemap discovery runs before crawling; in "skip" mode there
        # is no discovery, so we start in the crawling stage.
        initial_stage = (
            "crawling"
            if normalized.get("sitemap") == "skip"
            else "discovering_sitemaps"
        )
        job = {
            "status": "running",
            "stats": {"stage": initial_stage},
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
            "stop_event": threading.Event(),
            "db_path": str(main.resolve_db_path(normalized.get("db"))),
        }
        with _jobs_lock:
            crawl_jobs[crawl_id] = job

        thread = threading.Thread(
            target=_run_crawl_job,
            args=(crawl_id, normalized),
            name=f"crawl-job-{crawl_id}",
            daemon=True,
        )
        thread.start()

        return jsonify(crawl_id=crawl_id, status="running"), 202

    @app.route("/api/crawl/status/<crawl_id>", methods=["GET", "POST"])
    def api_crawl_status(crawl_id):
        # One endpoint, two verbs:
        #   GET  -> the job snapshot (status polling).
        #   POST -> body {"action": "stop"} requests a graceful stop of
        #           a running crawl. It sets the job's stop_event,
        #           which the crawl worker polls between units of work -
        #           the exact same shared stop path used by Ctrl+C
        #           (main._stop_crawl). The response is the current job
        #           snapshot; keep polling GET to watch the crawl reach
        #           the terminal "stopped" status.
        # The worker thread updates job fields under the lock, so read
        # everything into a snapshot while holding it: the response is
        # a consistent view of the job at one instant.
        with _jobs_lock:
            job = crawl_jobs.get(crawl_id)
            if job is None:
                return _error("crawl not found", 404)
            if request.method == "POST":
                data = request.get_json(silent=True)
                if not isinstance(data, dict) or data.get("action") != "stop":
                    return _error(
                        'POST requires a JSON body {"action": "stop"}', 400)
                if job["status"] == "running":
                    job["stop_event"].set()
            snapshot = _job_snapshot(job)
        return jsonify(crawl_id=crawl_id, **snapshot)

    # ---------- database management ----------

    @app.delete("/api/database")
    def api_delete_database():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "database" not in data:
            return _error(
                'request body must be a JSON object with a "database" field',
                400,
            )
        name = data["database"]
        if not isinstance(name, str) or not name.strip():
            return _error("database must be a non-empty string", 400)

        path = main.resolve_db_path(name)

        if not path.exists():
            return jsonify(error="database not found", database=name), 404

        # A running crawl holds its database connection open for the
        # whole crawl; deleting such a file would fail on Windows (and
        # silently produce a half-deleted state elsewhere). Refuse
        # deterministically instead of relying on the OS lock error.
        with _jobs_lock:
            for job in crawl_jobs.values():
                if (job["status"] == "running"
                        and os.path.normcase(job["db_path"])
                        == os.path.normcase(str(path))):
                    return _error(
                        "database is currently in use by a running crawl",
                        409,
                    )

        try:
            path.unlink()
        except FileNotFoundError:
            return jsonify(error="database not found", database=name), 404
        except OSError as exc:
            return _error(
                f"database is in use and cannot be removed: {exc}", 409)

        # Best-effort cleanup of WAL sidecars left behind by a live
        # connection (e.g. a finished crawl that created them).
        for suffix in ("-wal", "-shm"):
            sidecar = pathlib.Path(str(path) + suffix)
            try:
                sidecar.unlink()
            except OSError:
                pass

        return jsonify(deleted=True, database=str(path))

    # ---------- misc ----------

    @app.get("/api/status")
    def api_status():
        return jsonify(
            status="ok",
            engine="ModelSearchEngine",
            database=str(main.DB_PATH),
            database_exists=main.DB_PATH.exists(),
        )

    @app.get("/")
    def index():
        return render_template("index.html")

    return app


app = create_app()


if __name__ == "__main__":
    # Convenience for `python app.py`; for real deployments prefer
    # `flask --app app run` (or a WSGI server such as waitress).
    # Bind to 0.0.0.0 so the server is reachable on every interface:
    # localhost (127.0.0.1), LAN addresses, and public addresses such
    # as sslip.io wildcard DNS (e.g. http://<ip>.sslip.io:5000).
    app.run(host="0.0.0.0", port=5000, debug=True)
