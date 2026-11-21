"""
Flask HTTP API for ModelSearchEngine (main.py).

The search engine itself lives in main.py and is fully independent of
Flask; this module is a thin web adapter. The engine is never modified
here - app.py only calls main.search_json() / main.crawl_json() and
reads a couple of main-module constants.

Start the server (pick one)::

    python -m flask --app app run
    .venv\\Scripts\\flask.exe --app app run

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
          "db": "myindex.db"      # optional, str (also accepts "url"
                                  #   as an alias for "start_url")
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
          "status": "running" | "completed" | "failed",
          "stats": { ...current crawl stats dict... },
          "error": null | "concise failure message",
          "started_at": <unix seconds>,
          "finished_at": null | <unix seconds>
        }

    Unknown crawl_id -> 404 {"error": "crawl not found"}.

GET /api/status
    Lightweight liveness: engine name, default database path, and
    whether that database exists.

GET /
    Serves templates/index.html (the UI shell).
"""

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
                if (job["status"] in ("completed", "failed")
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
                **kwargs, progress_callback=_progress)
            with _jobs_lock:
                job["stats"] = final_stats
                job["status"] = "completed"
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
        }

        try:
            normalized = main._normalize_crawl_args(url, **kwargs)
        except ValueError as exc:
            return _error(str(exc), 400)

        crawl_id = _new_crawl_id()
        job = {
            "status": "running",
            "stats": {},
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
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

    @app.get("/api/crawl/status/<crawl_id>")
    def api_crawl_status(crawl_id):
        with _jobs_lock:
            job = crawl_jobs.get(crawl_id)
        if job is None:
            return _error("crawl not found", 404)
        return jsonify(
            crawl_id=crawl_id,
            status=job["status"],
            stats=job["stats"],
            error=job["error"],
            started_at=job["started_at"],
            finished_at=job["finished_at"],
        )

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
    app.run(host="127.0.0.1", port=5000, debug=True)
