# ModelSearchEngine

A completely **local** web crawler and full-text search engine in a single Python file.
Point it at any website, and it crawls (politely, in parallel), indexes the pages into
a local SQLite database, and lets you search them from the command line — no external
services, no API keys, no data leaving your machine.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-410%20passing-brightgreen)

## Key Features

- **Scoped, general-purpose crawling** - the crawl stays on the seed's hostname and its
  subdomains. `--allow` adds extra sites, and can even limit a site to a single folder:
  `--allow example.com/page` matches `example.com/page/quote` but never `example.com/page2`.
- **Politeness built in** - robots.txt is always respected (including `Crawl-delay`),
  HTTP `Retry-After` is honored with a temporary pause plus one automatic retry, requests
  are paced per host, and every redirect target is scope- and robots-checked *before* it
  is fetched. Timeouts, network, parsing, and database errors are counted separately in
  the crawl summary.
- **Fast and multithreaded** - configurable worker pool (`--threads`), streamed downloads
  with a per-page size cap, and batched database commits.
- **Quality search ranking** - SQLite FTS5 with BM25, weighting title matches 5x and
  description matches 3x over body text. AND queries with automatic OR fallback,
  prefix matching (`cat` also finds `cats`), and query-syntax injection is impossible.
- **Crash-safe storage** - WAL mode with full durability; a crash loses at most one
  small batch of pages and never corrupts the index.
- **Sitemap based crawling** - supports sitemap parsing and adding urls to queue with only sitemap and no sitemap scrawl options

## Requirements

- [Python 3.10+](https://www.python.org/) (developed and tested on 3.14)
- SQLite with FTS5 (bundled with Python's standard `sqlite3` module)
- `requests` and `beautifulsoup4` — pinned in [`requirements.txt`](requirements.txt)

## Setup (For Windows)

- Search and open "powershell"

- Run the command `winget install --id Git.Git -e --source winget` and continue with the installation. This installs git for easier setup.

- Open your desired directory(folder) in which you want the program to be installed.

- Click on the address bar.

<img src="images/address bar.png" alt="Address bar image" width="600">

- Type "cmd" in the address bar.

- Run the following commands.

```bash
git clone https://github.com/aGlass78/ARDV-Search
cd ARDV-Search
python -m venv .venv
.venv\Scripts\activate          
python -m pip install -r requirements.txt
```

- Open powershell again 

- Run the command `winget uninstall --id Git.Git`, this uninstalls git if you have no further use of it.

- The setup is complete! To run the program open the folder named "ARDV-Search" and open cmd inside the folder from the address bar as done before.

### Contact email (optional)

The crawler's User-Agent header tells servers who is crawling. To include your own
contact address, create a `.env` file next to `main.py` with one line:

```env
CONTACT_EMAIL=you@example.com
```

This is entirely optional — if `.env` is missing, unreadable, or `CONTACT_EMAIL` is
left empty, the crawler simply identifies itself as `ModelSearchEngine (educational
crawler)` with no contact section. 

## Usage (CLI)

Open cmd from the address bar inside the folder to run these commands.

Crawl a site, then search it (Example 'https://quotes.toscrape.com'):

```bash
python main.py crawl https://quotes.toscrape.com/
python main.py search "life"
```

Example crawl summary:

```
Crawl finished.
Pages indexed this run: 3
Elapsed: 0.1s (56.6 pages/s)
Worker threads: 8
Fetch attempts: 3, links discovered: 2, queue left: 0
Skipped: 0 robots-blocked, 0 HTTP errors, 0 rate-limited (0 retried),
        0 non-HTML, 0 too-large, 0 empty, 0 duplicates, 0 out-of-scope
Database: ...\.venv\result.db
```

Example search result:

```
Results for: life

1. Quotes to Scrape
   URL: https://quotes.toscrape.com/
     quote about [life] with ...
```

## Usage (WEB)

- Open cmd from the address bar again and activate the venv by running `.venv\scripts\activate`
- Run the command `py app.py`
- Open 127.0.0.1:5000 in any web browser. (`py app.py` binds to
  0.0.0.0:5000 — all interfaces — so it is reachable at your LAN/IP
  address too, e.g. via sslip.io: `http://<ip>.sslip.io:5000`.)

- The arguments in the cli are present as settings options in the web version, enable developer settings for the complete list.

### Common recipes

```bash
# Crawl a whole domain family (bare domain = all subdomains)
python main.py crawl https://toscrape.com/ --max-pages 100

# Be gentler — or as fast as politeness allows
python main.py crawl https://example.com/ --delay 1 --threads 2
python main.py crawl https://example.com/ --delay 0 --threads 16

# Allow an extra site — or limit it to a single folder
python main.py crawl https://quotes.toscrape.com/ --allow books.toscrape.com
python main.py crawl https://elsewhere.org/ --allow example.com/page

# Cap downloads at 500 KB per page
python main.py crawl https://example.com/ --max-bytes 500000

# Show 25 results instead of 10
python main.py search "interesting topic" --limit 25

# Stop a crawl early: just press Ctrl+C (statistics still print)
```

### Command reference

**Crawl:**
**`python main.py crawl <url> [options]`**

| Option | Default | Description |
|---|---|---|
| `url` | — | Seed URL; the crawl is scoped to its hostname + subdomains |
| `--max-pages N` |  unlimited | Stop after N pages have been indexed |
| `--delay SECONDS` | `0.0` | Minimum seconds between requests to one host (robots.txt Crawl-delay and Retry-After always apply on top) |
| `--max-bytes N` | `2,000,000` | Skip pages whose download exceeds N bytes |
| `--threads N` | `8` | Number of concurrent worker threads |
| `--allow HOST[/PATH]` | none | Extra site to allow (repeatable to allow more sites) |
| `--no-sitemap` | off / html + sitemap based discovery | Skip sitemap discovery entirely (robots.txt `Sitemap:` declaration + common-location probes); just crawl HTML links |
| `--only-sitemap` | off / html + sitemap based discovery | Crawl only from sitemap URLs (no HTML link discovery); aborts with a message if no sitemap is found |
| `--db FILE` | `result.db` | Database file for this crawl; relative paths are anchored next to main.py; missing parent directories are auto-created |
| `--crawl-unavailable-robots` | off | Crawl pages whose robots.txt cannot be verified (fetch failure / timeout / non-200) instead of skipping them; explicit `Disallow` rules are still honored |

**Search:**
**`python main.py search <query> [options]`**

| Option | Default | Description |
|---|---|---|
| `query` | — | Words to find (all words required; falls back to any-word; prefix-matched) |
| `--limit N` | `10` | Maximum number of results (any positive number) |
| `--e` | Disabled / Basic search functionality | Searches for the exact phrase without stemming |
| `--es` | Disabled / Basic search functionality | Searches for the exact phrase with stemming |
| `--db FILE` | `result.db` | Database file to search; if it doesn't exist, prints `No database found`|

**Stemming:** Treats variations of the word as the same word; 'Bomb' would be treated same as 'Bombing'. Works by splitting the word into its base + a tense or any addition.                            
**`python main.py --h`** — prints the guide

## How It Works

- **Crawl:** a breadth-first frontier feeds a thread pool. Before HTML discovery,
  sitemaps are seeded: a robots.txt `Sitemap:` declaration is honored (every
  declared line is parsed), and if none exists `/sitemap.xml` (etc.) are probed;
  `<urlset>`/`<sitemapindex>` XML is parsed (namespace-tolerant, recursive) and
  its URLs are normalized, scope-checked, and queued. Each page is then checked
  against robots.txt, downloaded with a size cap, parsed for links (which are
  scoped and queued even if the page itself cannot be indexed), and its text is
  upserted into the index, batched and crash-safe. Redirects are followed manually
  with scope checks before every hop and a 10-hop limit.
- **Search:** queries are reduced to plain words (letters/digits only, FTS5 syntax
  cannot be injected), matched with AND semantics and prefix expansion, falling back to
  OR when AND finds nothing, and ranked by BM25 with title/description weighting.
- **Storage:** a single SQLite file, `result.db`, created next to `main.py`,
  independent of the directory you run from. WAL mode lets searches run while a crawl
  is writing.

## Web API (Flask) **INCOMPLETE**

The engine itself is independent of Flask — `main.search_json()` returns a
consistent JSON payload and can be called directly from any Python code:

```python
import main
main.search_json("python", limit=5)  # {"query", "results", "total_results", "search_time"}
```

`app.py` is a thin HTTP adapter around it:

- `GET /api/search?q=<query>[&limit=N][&exact=1][&stem=1][&db=FILE]`
  → search JSON. When provided, `limit` must be an integer ≥ 1 (else 400);
  `exact` and `stem` are mutually exclusive (else 400); a blank `q`
  returns an empty result set. A missing database → 404
  `{"error": "database not found", "database": ...}`. Other failures
  → 500 with a logged traceback.
- `POST /api/crawl` — **asynchronous**. Body is a JSON object mirroring
  the crawl options: `{"start_url": ..., "max_pages": ..., "delay": ...,
  "max_bytes": ..., "threads": ..., "allow": [...], "sitemap":
  "auto"|"skip"|"only", "db": ..., "crawl_unavailable_robots": false}`
  (`start_url` required; `url` accepted as an alias; omitted/null =
  engine default; `crawl_unavailable_robots` is a bool, default false —
  when true, crawl pages whose robots.txt cannot be verified
  (fetch failure / timeout / non-200) instead of skipping them, while
  explicit `Disallow` rules are still honored). The config is
  validated up front (400 with `{"error": ...}` on bad input, no job
  created), then a crawl job is started in the background and the
  endpoint responds immediately with
  `202 {"crawl_id": "<8-hex-chars>", "status": "running"}`. Multiple
  crawls can run concurrently. `main.crawl_json()` is the underlying
  programmatic entry point (usable without Flask).
- `GET /api/crawl/status/<crawl_id>` — poll roughly every second for
  live status: `{"crawl_id", "status": "running"|"completed"|"stopped"
  |"failed", "stats": {...live/final stats...}, "error": null | "<concise
  message>", "started_at", "finished_at"}`. Unknown id → 404. While
  `running`, `stats` is a live snapshot (`indexed`, `requests`,
  `links_discovered`, `queued`, `errors`, …); when `completed` it is
  final; when `stopped`, the crawl was stopped early via the POST stop
  action below and `stats` carries `"interrupted": true`; when `failed`,
  `error` holds a one-line message to display.
  Before the crawl worker starts reporting, `stats` is `{"stage":
  "discovering_sitemaps"}` (or `{"stage": "crawling"}` in skip mode);
  after that, every snapshot carries `"stage": "crawling"` too. The
  endpoint returns a consistent snapshot taken under the job lock.
- `POST /api/crawl/status/<crawl_id>` — the same URL; send body
  `{"action": "stop"}` to request a graceful stop of a running crawl.
  It sets the job's stop event (the same shared stop path as Ctrl+C in
  `main.py`): in-flight fetches finish, queued work is cancelled, and
  the job ends with status `"stopped"` and stats `"interrupted": true`.
  Responds with the current job snapshot (same shape as GET); stopping a
  non-running job is a no-op that returns its current state; missing/
  other action → 400; unknown id → 404.
- `GET /api/status` → `{"status": "ok", "engine": "ModelSearchEngine",
  "database": ..., "database_exists": ...}`.
- `DELETE /api/database` — deletes a database file (the frontend
  "Delete Database" control). Body is `{"database": "<name>"}`; names
  resolve exactly like `--db` (relative anchored next to main.py, `.db`
  appended when missing). Success →
  `200 {"deleted": true, "database": "<resolved name>"}`; missing file →
  `404 {"error": "database not found", "database": "<name>"}`;
  empty/invalid name → 400; in use (running crawl job or OS lock) →
  409. Any leftover `-wal/-shm` sidecars are removed best-effort.
- `GET /` → the UI shell `templates/index.html`.

Start it (from the repo root):

```bash
.venv\Scripts\flask.exe --app app run   # dev server, 127.0.0.1:5000
.venv\Scripts\python.exe app.py          # all interfaces, 0.0.0.0:5000
# flask defaults to localhost only; add --host 0.0.0.0 to bind all interfaces
# or: .venv\Scripts\python.exe app.py
```

```bash
curl "http://127.0.0.1:5000/api/search?q=python&limit=5"
curl "http://127.0.0.1:5000/api/status"
curl -X POST -H "Content-Type: application/json" -d '{"start_url":"https://quotes.toscrape.com","max_pages":100,"threads":8,"delay":0.5}' http://127.0.0.1:5000/api/crawl
# -> {"crawl_id":"c83f91a2","status":"running"}
curl "http://127.0.0.1:5000/api/crawl/status/c83f91a2"
curl -X POST -H "Content-Type: application/json" -d '{"action":"stop"}' http://127.0.0.1:5000/api/crawl/status/c83f91a2
# -> {"crawl_id":"c83f91a2","status":"running", ...}
curl "http://127.0.0.1:5000/api/crawl/status/c83f91a2"
# -> when stopped: {"crawl_id":"c83f91a2","status":"stopped","stats":{...,"interrupted":true}, ...}
curl -X DELETE -H "Content-Type: application/json" -d '{"database":"myindex.db"}' http://127.0.0.1:5000/api/database
# -> {"deleted":true,"database":"C:\\Users\\Admin\\ARDV-Search\\myindex.db"}
```

Each request opens its own SQLite connection (WAL mode) — searches can run
while the crawler writes to the same database.

## Running the Tests (Running or not running tests doesnt effect app functionality; skip if not interested.)

410 tests run on the standard library's `unittest` against a local HTTP server —
the Flask/JSON API tests (stages 18-19, 26-28) use Flask's test client, so no
server process needs to be started for them.
**no external network access is needed**:

```bash
python -m unittest discover -s tests -v   # full suite (~72s)

python tests/smoke_cli.py                  # end-to-end CLI crawl + search check
python tests/verify_ctrlc.py              # real-signal Ctrl+C verification
```

## Project Layout

```
main.py            crawler + search engine + CLI (single file)
app.py            Flask web API (thin adapter over main.search_json)
templates/        UI shell (index.html) served by app.py
requirements.txt   pinned dependencies
tests/             unit tests, CLI smoke test, Ctrl+C verification script
```

## Notes

- This is an educational crawler. It identifies itself to servers as
  `ModelSearchEngine (educational crawler)` plus a contact address if you configure
  one (see [Contact email](#contact-email-optional)) and obeys robots.txt. Please use
  it respectfully and in line with the terms of the sites you crawl.
- The index (`result.db`) is created on first crawl and reused afterwards; re-crawling
  a page updates it rather than duplicating it. 
- The default search functionality (Without --e or --es) splits phrases into words and searches those words, for proper phrase searches use --e or --es refer [Command ref.](#command-reference) for description.
- The [releases](https://github.com/aGlass78/ARDV-Search/releases) section includes a pre-indexed database of 15k wikipedia pages available to download and use directly without having to crawl first. To use download the `wiki15k.db` file, place it in your copied repo, and rename it to `result.db` then run the search function and it will search that file.

## Known limitations / Future fixes

- Currently the crawler cant parse compressed sitemaps (.xml.gz files)
- **Stemming Limitations** - The implemented porter algorithm for stemming can create issues like splitting the word studies into studi and es which is incorrect.
- Running multiple crawl sessions on the same database simultaneously can cause database writing errors because of how SQLite (the database used in this project) handles concurrent writting. 