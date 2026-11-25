"""Environment fingerprint, peak-RSS, and consistent DB-size helpers."""

import ctypes
import ctypes.wintypes
import datetime
import os
import pathlib
import platform
import sqlite3
import sys
import threading
import time

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - Windows
    resource = None


def fingerprint():
    """Full environment fingerprint; must accompany every result set."""
    return {
        "python": sys.version.split()[0],
        "python_full": sys.version.strip(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def peak_rss_bytes():
    """Peak process RSS in bytes.

    Windows: K32GetProcessMemoryInfo via ctypes (no third-party deps).
    POSIX: resource.getrusage().ru_maxrss.
    """
    if sys.platform == "win32":
        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.wintypes.DWORD,
        ]
        kernel32.K32GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
        proc = kernel32.GetCurrentProcess()
        if not kernel32.K32GetProcessMemoryInfo(
                proc, ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.PeakWorkingSetSize)
    if resource is not None:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    return 0


class RssSampler(threading.Thread):
    """Background thread recording peak RSS; call .stop() when done."""

    def __init__(self, interval=0.2):
        super().__init__(daemon=True)
        self.interval = interval
        self._stop = threading.Event()
        self.peak = 0

    def run(self):
        while not self._stop.is_set():
            try:
                self.peak = max(self.peak, peak_rss_bytes())
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=2.0)


def db_sizes(db_path):
    """Return {main, wal, shm} byte sizes at a CONSISTENT state.

    Runs ``PRAGMA wal_checkpoint(TRUNCATE)`` on a fresh connection after
    the measured work is done, so the WAL is flushed and sizes can be
    compared across runs/experiments.
    """
    db_path = pathlib.Path(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    sizes = {}
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(db_path) + suffix)
        key = "main" if suffix == "" else suffix[1:]
        sizes[key] = p.stat().st_size if p.exists() else 0
    return sizes
