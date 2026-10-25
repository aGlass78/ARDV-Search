"""End-to-end CLI smoke test (run manually, not part of unittest).

Copies main.py into a fresh temp directory, serves a few pages on a
local HTTP server, and exercises the real CLI:

    crawl  -> indexes 3 pages from the live server
    search -> finds them back through FTS5

Also proves the database is created next to main.py regardless of the
current working directory, and measures wall-clock times.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from helpers import TestServer  # noqa: E402

PROJECT = HERE.parent
PYTHON = sys.executable


def main():
    server = TestServer().start()
    try:
        server.add_robots("User-agent: *\nAllow: /\n")
        server.add("/seed", '<!doctype html><html><head><title>Seed Page'
                    '</title></head><body>welcome to the garden'
                    '<a href="/herbs"></a><a href="/tools"></a>'
                    # Different hostname, same server: must be ignored
                    # because scope is the seed host (127.0.0.1).
                    f'<a href="http://localhost:{server.port}/other"></a>'
                    '</body></html>')
        server.add("/herbs", '<!doctype html><html><head><title>Herb Guide'
                    '</title></head><body>basil thyme and rosemary grow here'
                    '</body></html>')
        server.add("/tools", '<!doctype html><html><head><title>Garden Tools'
                    '</title></head><body>spade and rake rest here'
                    '</body></html>')
        server.add("/other", '<!doctype html><html><head><title>Other Host'
                    '</title></head><body>this must never be crawled'
                    '</body></html>')

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            shutil.copy2(PROJECT / "main.py", tmp / "main.py")
            # Scope is derived from the seed URL automatically, and any
            # port is crawlable by default, so the copy needs no edits.

            def run(*args):
                started = time.perf_counter()
                proc = subprocess.run(
                    [PYTHON, str(tmp / "main.py"), *args],
                    capture_output=True, text=True, cwd=PROJECT,
                )
                took = time.perf_counter() - started
                return proc, took

            proc, crawl_s = run("crawl", server.url("/seed"),
                                 "--max-pages", "3", "--delay", "0")
            print("== crawl exit:", proc.returncode, f"({crawl_s:.1f}s)")
            print(proc.stdout.strip())
            if proc.returncode != 0:
                print(proc.stderr)
                return 1

            other_crawled = "/other" in [
                p for _, p in server.requests]
            print("cross-host link ignored:", not other_crawled)

            proc, search_s = run("search", "garden tools", "--limit", "5")
            print("\n== search exit:", proc.returncode, f"({search_s:.2f}s)")
            print(proc.stdout.strip())

            proc2, _ = run("search", "basil", "--limit", "5")
            print("\n== search 'basil' exit:", proc2.returncode)
            print(proc2.stdout.strip())

            # Database must live next to the script, not the CWD.
            db = tmp / "result.db"
            print("\nresult.db next to main.py:", db.exists())
            print("stray DB in CWD:",
                  list(PROJECT.glob("result.db")) or "none")
            ok = (db.exists() and "Garden Tools" in proc.stdout
                  and not other_crawled)
            return 0 if ok else 1
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
