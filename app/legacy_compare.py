from __future__ import annotations

"""Compare legacy rows in two SQLite snapshots without exposing row content."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .db_admin import _connect_readonly, _table_names


LEGACY_TABLES = (
    "companies", "sources", "items", "item_companies", "item_discoveries",
    "topics", "item_topics", "stories", "story_items", "daily_reports", "fetch_log",
)


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')]


def _order_by(db: sqlite3.Connection, table: str) -> str:
    keys = sorted((row["pk"], row["name"]) for row in db.execute(
        f'PRAGMA table_info("{table}")') if row["pk"])
    return ",".join(f'"{name}"' for _, name in keys) if keys else "rowid"


def _fingerprint(db: sqlite3.Connection, table: str, columns: list[str]) -> dict:
    selected = ",".join(f'"{name}"' for name in columns)
    order = _order_by(db, table)
    digest = hashlib.sha256()
    count = 0
    for row in db.execute(f'SELECT {selected} FROM "{table}" ORDER BY {order}'):
        encoded = json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"),
                             default=str).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def compare_legacy_snapshots(before: str | Path, after: str | Path) -> dict:
    """Hash every pre-existing legacy column; added migration columns are ignored."""
    first = Path(before).expanduser().resolve(strict=True)
    second = Path(after).expanduser().resolve(strict=True)
    if first == second:
        raise ValueError("before and after must be different database files")
    with closing(_connect_readonly(first)) as old, closing(_connect_readonly(second)) as new:
        old.execute("BEGIN")
        new.execute("BEGIN")
        old_tables, new_tables = _table_names(old), _table_names(new)
        result = {}
        for table in LEGACY_TABLES:
            if table not in old_tables:
                continue
            if table not in new_tables:
                result[table] = {"status": "missing_after"}
                continue
            columns = _columns(old, table)
            if not set(columns).issubset(_columns(new, table)):
                result[table] = {"status": "missing_original_column"}
                continue
            baseline = _fingerprint(old, table, columns)
            current = _fingerprint(new, table, columns)
            result[table] = {
                "status": "unchanged" if baseline == current else "changed",
                "rows_before": baseline["rows"], "rows_after": current["rows"],
                "sha256_before": baseline["sha256"], "sha256_after": current["sha256"],
            }
        return {"status": "ok" if result and all(v["status"] == "unchanged" for v in result.values())
                else "failed", "tables": result}
