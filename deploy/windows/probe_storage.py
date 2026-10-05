"""Measure whether a directory is a fast and safe home for InfoHub's SQLite database (stdlib only).

Answers decision U02 on the Windows host: compare the current NTFS bind mount with a Docker
named volume before deciding whether to move the data. Run it in a python:3.12-slim container
with the directory to test mounted at /probe, e.g. (PowerShell, from the infohub folder):

    cmd /c "git show origin/main:deploy/windows/probe_storage.py > %TEMP%\\probe_storage.py"
    mkdir probe-tmp
    docker run --rm -v "${PWD}\\probe-tmp:/probe" -v "$env:TEMP\\probe_storage.py:/probe_storage.py:ro" `
      python:3.12-slim python /probe_storage.py /probe

It only writes inside a new probe-<random> subdirectory, removes it afterwards, never touches
InfoHub data and makes no network calls. The concurrency check runs two processes writing one
WAL database at once, as web and worker do: lost updates or a failed integrity_check mean the
directory must not hold the live database.
"""

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import statistics
import sys
import time
import uuid
from pathlib import Path


def _percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"p50_ms": round(statistics.median(ordered) * 1000, 3),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 3)}


def small_file_fsync(root: Path, count: int) -> dict:
    timings = []
    payload = os.urandom(4096)
    for index in range(count):
        started = time.perf_counter()
        with (root / f"blob-{index}").open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        timings.append(time.perf_counter() - started)
    return {"files": count, **_percentiles(timings)}


def sqlite_commits(root: Path, count: int) -> dict:
    db = sqlite3.connect(root / "commits.db", isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, body TEXT)")
    timings = []
    for index in range(count):
        started = time.perf_counter()
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO t(body) VALUES(?)", ("x" * 200,))
        db.execute("COMMIT")
        timings.append(time.perf_counter() - started)
    db.close()
    return {"transactions": count, **_percentiles(timings),
            "per_second": round(count / sum(timings), 1)}


def sequential_read(root: Path, megabytes: int) -> dict:
    path = root / "sequential.bin"
    block = os.urandom(1024 * 1024)
    started = time.perf_counter()
    with path.open("xb") as handle:
        for _ in range(megabytes):
            handle.write(block)
        handle.flush()
        os.fsync(handle.fileno())
    written = time.perf_counter() - started
    digest = hashlib.sha256()
    started = time.perf_counter()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    read = time.perf_counter() - started
    return {"megabytes": megabytes, "write_mb_per_s": round(megabytes / written, 1),
            "read_mb_per_s": round(megabytes / read, 1)}


def _increment(path: str, count: int) -> None:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.execute("PRAGMA busy_timeout=30000")
    for _ in range(count):
        db.execute("BEGIN IMMEDIATE")
        value = db.execute("SELECT value FROM counter WHERE id=1").fetchone()[0]
        db.execute("UPDATE counter SET value=? WHERE id=1", (value + 1,))
        db.execute("COMMIT")
    db.close()


def concurrent_wal_writers(root: Path, processes: int, count: int) -> dict:
    path = root / "concurrent.db"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE counter(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
    db.execute("INSERT INTO counter VALUES(1,0)")
    db.close()
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_increment, args=(str(path), count)) for _ in range(processes)]
    started = time.perf_counter()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    elapsed = time.perf_counter() - started
    db = sqlite3.connect(path)
    value = db.execute("SELECT value FROM counter WHERE id=1").fetchone()[0]
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    result = {"processes": processes, "increments": processes * count, "final_value": value,
              "exit_codes": [worker.exitcode for worker in workers], "integrity": integrity,
              "seconds": round(elapsed, 2)}
    return {**result, "safe": wal_writers_safe(result)}


def wal_writers_safe(result: dict) -> bool:
    """Every increment landed, the file is intact and no writer crashed."""
    return (result["final_value"] == result["increments"] and result["integrity"] == "ok"
            and all(code == 0 for code in result["exit_codes"]))


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    quick = "--quick" in argv
    base = Path(argv[0]).resolve(strict=True)
    root = base / f"probe-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        report = {
            "directory": str(base),
            "sqlite_version": sqlite3.sqlite_version,
            "small_file_fsync": small_file_fsync(root, 20 if quick else 300),
            "sqlite_commits": sqlite_commits(root, 50 if quick else 1000),
            "sequential": sequential_read(root, 8 if quick else 256),
            "concurrent_wal_writers": concurrent_wal_writers(root, 2, 50 if quick else 500),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
    report["verdict"] = ("concurrency check passed" if report["concurrent_wal_writers"]["safe"]
                         else "UNSAFE: do not keep the live database here")
    print(json.dumps(report, indent=2))
    return 0 if report["concurrent_wal_writers"]["safe"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
