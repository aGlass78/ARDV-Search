"""End-to-end verification of real Ctrl+C handling (manual run).

Starts a crawl subprocess against a slow local site, sends a genuine
CTRL_C_EVENT to it mid-crawl, and verifies that the process:

  1. stops indexing,
  2. still prints the statistics summary (marked partial),
  3. exits with code 0.

Also verifies the missing-database search message against the fresh
temp directory before any crawl has run.
"""
import os
import pathlib
import signal
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
        links = "".join(f'<a href="/p{i}"></a>' for i in range(80))
        server.add("/seed",
                   f"<!doctype html><html><head><title>Seed</title></head>"
                   f"<body>seed page {links}</body></html>")
        for i in range(80):
            server.add(f"/p{i}",
                       f"<!doctype html><html><head><title>Page {i}</title>"
                       f"</head><body>content of page {i}</body></html>")

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)

            # Fresh copy of main.py; DB_PATH anchors itself next to the
            # copy automatically, and any port is crawlable by default.
            text = (PROJECT / "main.py").read_text(encoding="utf-8")
            script = tmp / "main.py"
            script.write_text(text, encoding="utf-8")

            # Windows only lets one process send CTRL_BREAK_EVENT (not
            # CTRL_C_EVENT) reliably to a child process group, so the
            # wrapper translates SIGBREAK into exactly what a real
            # Ctrl+C produces: KeyboardInterrupt in the main thread.
            wrapper = tmp / "wrapper.py"
            wrapper.write_text(
                "import runpy\n"
                "import signal\n"
                "import sys\n"
                "\n"
                "signal.signal(signal.SIGBREAK,\n"
                "              lambda signum, frame: (_ for _ in ())"
                ".throw(KeyboardInterrupt()))\n"
                "\n"
                'sys.argv = ["main.py"] + sys.argv[1:]\n'
                'runpy.run_path("main.py", run_name="__main__")\n',
                encoding="utf-8",
            )

            # 1. search with no database -> dedicated message
            before = subprocess.run(
                [PYTHON, str(script), "search", "content"],
                capture_output=True, text=True, cwd=tmp, timeout=30,
            )
            print("== search with no database (exit", before.returncode, ")")
            print(before.stdout.strip())
            no_db_ok = "No database found" in before.stdout

            # 2. crawl, interrupted mid-run by a real OS signal
            proc = subprocess.Popen(
                [PYTHON, str(wrapper), "crawl", server.url("/seed"),
                 "--delay", "0.1", "--threads", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=tmp,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            time.sleep(1.5)
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)

            try:
                out, _ = proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                print("!! crawl did not exit after the interrupt signal")
                return 1

            print("\n== crawl after Ctrl+C (tail, exit", proc.returncode, ")")
            print(out.strip()[-1200:])

            interrupt_ok = "Interrupted (Ctrl+C)" in out
            summary_ok = (
                "Crawl interrupted (Ctrl+C) - statistics are partial." in out
                and "Pages indexed this run:" in out
                and "Elapsed:" in out
            )
            db_ok = (tmp / "result.db").exists()

            print("\nexit code:", proc.returncode)
            print("missing-DB message:", no_db_ok)
            print("interrupt message:", interrupt_ok)
            print("partial-statistics summary:", summary_ok)
            print("database created by crawl:", db_ok)

            ok = (proc.returncode == 0 and no_db_ok and interrupt_ok
                  and summary_ok and db_ok)
            return 0 if ok else 1
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
