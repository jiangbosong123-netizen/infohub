"""Read-only snapshot of an InfoHub host before an upgrade (standard library only).

Run it in any Python 3.10+ container with the project's data directory and .env mounted
read-only, e.g. on Windows (PowerShell, from the infohub folder):

    cmd /c "git show origin/main:deploy/windows/inspect_host.py > %TEMP%\\inspect_host.py"
    docker run --rm -v "${PWD}\\data:/data:ro" -v "${PWD}\\.env:/env/.env:ro" `
      -v "$env:TEMP\\inspect_host.py:/inspect_host.py:ro" python:3.12-slim python /inspect_host.py

(cmd's redirection keeps the bytes as-is; PowerShell 5.1's ``>`` would write UTF-16.)
It never writes, never prints configuration values (only key names and whether the private
HTTPS origin is well formed) and opens SQLite with ``immutable=1`` so a read-only mount works.
"""

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DATA = Path(os.environ.get("INSPECT_DATA", "/data"))
ENV_FILE = Path(os.environ.get("INSPECT_ENV", "/env/.env"))
REQUIRED_FOR_UPGRADE = ("INFOHUB_PUBLIC_ORIGIN",)
ORIGIN = re.compile(r"https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.ts\.net/?")


def _env_summary() -> dict:
    if not ENV_FILE.is_file():
        return {"present": False}
    keys, origin = [], None
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        keys.append(key)
        if key == "INFOHUB_PUBLIC_ORIGIN":
            origin = value.strip().strip("'\"")
    return {
        "present": True,
        "keys": sorted(set(keys)),
        "missing_for_upgrade": [key for key in REQUIRED_FOR_UPGRADE if key not in keys],
        "public_origin_well_formed": bool(origin and ORIGIN.fullmatch(origin)),
    }


def _one(db: sqlite3.Connection, sql: str, default=None):
    try:
        row = db.execute(sql).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def _rows(db: sqlite3.Connection, sql: str) -> dict:
    try:
        return {str(key): value for key, value in db.execute(sql).fetchall()}
    except sqlite3.Error:
        return {}


def _database_summary(path: Path) -> dict:
    if not path.is_file():
        return {"present": False}
    summary = {
        "present": True,
        "file_bytes": path.stat().st_size,
        "wal_bytes": (path.parent / (path.name + "-wal")).stat().st_size
        if (path.parent / (path.name + "-wal")).exists() else 0,
    }
    db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    summary["schema_version"] = (
        _one(db, "SELECT MAX(version) FROM schema_migrations") if "schema_migrations" in tables else 0
    )
    summary["items"] = _one(db, "SELECT COUNT(*) FROM items", 0)
    summary["items_by_channel"] = _rows(db, "SELECT channel, COUNT(*) FROM items GROUP BY channel")
    summary["fetched_range"] = [_one(db, "SELECT MIN(fetched_at) FROM items"),
                                _one(db, "SELECT MAX(fetched_at) FROM items")]
    summary["published_range"] = [_one(db, "SELECT MIN(published_at) FROM items"),
                                  _one(db, "SELECT MAX(published_at) FROM items")]
    summary["sources_enabled"] = _one(db, "SELECT COUNT(*) FROM sources WHERE enabled=1", 0)
    summary["stories"] = _one(db, "SELECT COUNT(*) FROM stories", 0) if "stories" in tables else None
    summary["story_match_versions"] = (
        _rows(db, "SELECT match_reason, COUNT(*) FROM story_items GROUP BY match_reason")
        if "story_items" in tables else {}
    )
    summary["daily_reports"] = [_one(db, "SELECT COUNT(*) FROM daily_reports", 0),
                                _one(db, "SELECT MIN(date) FROM daily_reports"),
                                _one(db, "SELECT MAX(date) FROM daily_reports")]
    summary["documents"] = _one(db, "SELECT COUNT(*) FROM documents", 0) if "documents" in tables else None
    summary["jobs_by_state"] = (
        _rows(db, "SELECT state, COUNT(*) FROM jobs GROUP BY state") if "jobs" in tables else {}
    )
    db.close()
    return summary


def main() -> int:
    report = {
        "data_directory": str(DATA),
        "database": _database_summary(DATA / "app.db"),
        "blob_files": sum(1 for _ in (DATA / "blobs").rglob("*") if _.is_file())
        if (DATA / "blobs").is_dir() else 0,
        "backup_files": sorted(p.name for p in (DATA / "backups").iterdir())
        if (DATA / "backups").is_dir() else [],
        "env": _env_summary(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
