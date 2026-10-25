# ModelSearchEngine

A completely **local** web crawler and full-text search engine in a single Python file.
Point it at any website, and it crawls (politely, in parallel), indexes the pages into
a local SQLite database, and lets you search them from the command line — no external
services, no API keys, no data leaving your machine.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-131%20passing-brightgreen)

## Key Features

- **Scoped, general-purpose crawling** — the crawl stays on the seed's hostname and its
  subdomains. `--allow` adds extra sites, and can even limit a site to a single folder:
  `--allow example.com/page` matches `example.com/page/quote` but never `example.com/page2`.
- **Politeness built in** — robots.txt is always respected (including `Crawl-delay`),
  HTTP `Retry-After` is honored with a temporary pause plus one automatic retry, requests
  are paced per host, and redirect targets are scope-checked *before* they are fetched.
- **Fast and multithreaded** — configurable worker pool (`--threads`), streamed downloads
  with a per-page size cap, and batched database commits.
- **Quality search ranking** — SQLite FTS5 with BM25, weighting title matches 5x and
  description matches 3x over body text. AND queries with automatic OR fallback,
  prefix matching (`cat` also finds `cats`), and query-syntax injection is impossible.
- **Crash-safe storage** — WAL mode with full durability; a crash loses at most one
  small batch of pages and never corrupts the index.

## Requirements

- Python 3.10+ (developed and tested on 3.14)
- SQLite with FTS5 (bundled with Python's standard `sqlite3` module)
- `requests` and `beautifulsoup4` — pinned in [`requirements.txt`](requirements.txt)

## Setup

```bash
git clone https://github.com/aGlass78/ARDV-Search
cd "path/to/repo" #The path to the copied repo

# virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate          # Windows

# install dependencies
python -m pip install -r requirements.txt
```

### Contact email (optional)

The crawler's User-Agent header tells servers who is crawling. To include your own
contact address, create a `.env` file next to `main.py` with one line:

```env
CONTACT_EMAIL=you@example.com
```

This is entirely optional — if `.env` is missing, unreadable, or `CONTACT_EMAIL` is
left empty, the crawler simply identifies itself as `ModelSearchEngine (educational
crawler)` with no contact section. No setup step is required and nothing errors out.
`.env` is in `.gitignore`, so your address stays local to your checkout.

## Usage

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
Database: ...\SE\result.db
```

Example search result:

```
Results for: life

1. Quotes to Scrape
   URL: https://quotes.toscrape.com/
     quote about [life] with ...
```

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

**`python main.py crawl <url> [options]`**

| Option | Default | Description |
|---|---|---|
| `url` | — | Seed URL; the crawl is scoped to its hostname + subdomains |
| `--max-pages N` | unlimited | Stop after N pages have been indexed |
| `--delay SECONDS` | `0.0` | Minimum seconds between requests to one host (robots.txt Crawl-delay and Retry-After always apply on top) |
| `--max-bytes N` | `2,000,000` | Skip pages whose download exceeds N bytes |
| `--threads N` | `8` | Number of concurrent worker threads |
| `--allow HOST[/PATH]` | none | Extra site to allow, optionally limited to a path folder (repeatable; `--allowed` also works) |

**`python main.py search <query> [options]`**

| Option | Default | Description |
|---|---|---|
| `query` | — | Words to find (all words required; falls back to any-word; prefix-matched) |
| `--limit N` | `10` | Maximum number of results (any positive number) |

**`python main.py --h`** — print the full built-in guide and exit.

## How It Works

- **Crawl:** a breadth-first frontier feeds a thread pool. Each page is checked against
  robots.txt, downloaded with a size cap, parsed for links (which are scoped and queued
  even if the page itself cannot be indexed), and its text is upserted into the index —
  batched and crash-safe. Redirects are followed manually with scope checks before every
  hop and a 10-hop limit. Rate limiting is per hostname, so workers on different hosts
  never slow each other down.
- **Search:** queries are reduced to plain words (letters/digits only — FTS5 syntax
  cannot be injected), matched with AND semantics and prefix expansion, falling back to
  OR when AND finds nothing, and ranked by BM25 with title/description weighting.
- **Storage:** a single SQLite file, `result.db`, created next to `main.py` —
  independent of the directory you run from. WAL mode lets searches run while a crawl
  is writing.

## Running the Tests

131 tests run on the standard library's `unittest` against a local HTTP server —
**no external network access is needed**:

```bash
python -m unittest discover -s tests -v   # full suite (~40s)

python tests/smoke_cli.py                  # end-to-end CLI crawl + search check
python tests/verify_ctrlc.py              # real-signal Ctrl+C verification
```

## Project Layout

```
main.py            crawler + search engine + CLI (single file)
requirements.txt   pinned dependencies
param.txt          every parameter: accepted inputs and explanations
PROJECT.md         technical docs: architecture, decisions, full test changelog
tests/             unit tests, CLI smoke test, Ctrl+C verification script
```

## Notes

- This is an educational crawler. It identifies itself to servers as
  `ModelSearchEngine (educational crawler)` — plus a contact address if you configure
  one (see [Contact email](#contact-email-optional)) — and obeys robots.txt. Please use
  it respectfully and in line with the terms of the sites you crawl.
- The index (`result.db`) is created on first crawl and reused afterwards; re-crawling
  a page updates it rather than duplicating it. Searching before any crawl prints a
  friendly "No database found" message instead of failing.
