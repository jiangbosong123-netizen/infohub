from __future__ import annotations

"""Versioned SQLite migration, verification and consistent backup tools."""

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from . import database


class DatabaseSafetyError(RuntimeError):
    """Base class for errors that must stop startup or deployment."""


class UnsupportedSchemaError(DatabaseSafetyError):
    """The database cannot be changed safely by this release."""


class DatabaseVerificationError(DatabaseSafetyError):
    """SQLite integrity, foreign keys or expected schema did not verify."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    definition: str
    operation: Callable[[sqlite3.Connection], None]

    @property
    def checksum(self) -> str:
        payload = f"{self.version}\0{self.name}\0{self.definition}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class VerificationReport:
    path: str
    state: str
    schema_version: int
    integrity: str
    foreign_key_violations: int
    size_bytes: int
    file_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MigrationReport:
    path: str
    previous_state: str
    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...]
    backup_path: str | None
    verification: VerificationReport

    def to_dict(self) -> dict:
        value = asdict(self)
        value["applied_versions"] = list(self.applied_versions)
        return value


MIGRATION_TABLE_SQL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

FTS_V2_SQL = """
DROP TRIGGER IF EXISTS items_ai;
DROP TRIGGER IF EXISTS items_ad;
DROP TRIGGER IF EXISTS items_au;
DROP TABLE items_fts;
CREATE VIRTUAL TABLE items_fts USING fts5(
    title, title_zh, summary, content='items', content_rowid='id', tokenize='trigram');
CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid,title,title_zh,summary)
    VALUES(new.id,new.title,new.title_zh,new.summary);
END;
CREATE TRIGGER items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts,rowid,title,title_zh,summary)
    VALUES('delete',old.id,old.title,old.title_zh,old.summary);
END;
CREATE TRIGGER items_au AFTER UPDATE OF title,title_zh,summary ON items BEGIN
    INSERT INTO items_fts(items_fts,rowid,title,title_zh,summary)
    VALUES('delete',old.id,old.title,old.title_zh,old.summary);
    INSERT INTO items_fts(rowid,title,title_zh,summary)
    VALUES(new.id,new.title,new.title_zh,new.summary);
END;
INSERT INTO items_fts(items_fts) VALUES('rebuild');
"""

DERIVED_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS items_derived_update;
CREATE TRIGGER items_derived_update
AFTER UPDATE OF title,title_zh,summary,raw_summary,companies,score,tmt,event_type,
                ai_cat,official,extra,published_at,channel ON items BEGIN
    INSERT OR IGNORE INTO derived_dirty(item_id) VALUES(new.id);
END;
"""


def _execute_script(db: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without sqlite3.executescript's implicit COMMIT."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                db.execute(statement)
    if pending.strip():
        raise DatabaseSafetyError("migration contains incomplete SQL")


def _legacy_baseline(db: sqlite3.Connection) -> None:
    _execute_script(db, database.SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(items)")}
    additions = (
        ("title_zh", "TEXT DEFAULT ''"),
        ("tmt", "INTEGER"),
        ("reason", "TEXT DEFAULT ''"),
        ("ai_cat", "TEXT DEFAULT ''"),
        ("raw_summary", "TEXT"),
    )
    for column, ddl in additions:
        if column not in columns:
            db.execute(f"ALTER TABLE items ADD COLUMN {column} {ddl}")

    fts_columns = {row["name"] for row in db.execute("PRAGMA table_info(items_fts)")}
    if "title_zh" not in fts_columns:
        _execute_script(db, FTS_V2_SQL)

    _execute_script(db, database.DERIVED_SCHEMA)
    _execute_script(db, DERIVED_TRIGGER_SQL)
    db.execute("""INSERT OR IGNORE INTO derived_dirty(item_id)
                  SELECT id FROM items
                  WHERE id NOT IN (SELECT item_id FROM indexed_items)""")


# Migration 1 freezes the exact legacy schema at main@88a2a1e. Future schema
# changes must append a new Migration instead of editing this definition.
MIGRATIONS = (
    Migration(
        1,
        "legacy schema baseline",
        database.SCHEMA + database.DERIVED_SCHEMA + FTS_V2_SQL + DERIVED_TRIGGER_SQL
        + "\nconditional-columns:title_zh,tmt,reason,ai_cat,raw_summary",
        _legacy_baseline,
    ),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version
REQUIRED_MIGRATION_COLUMNS = {"version", "name", "checksum", "applied_at"}
LEGACY_ANCHORS = {"companies", "sources", "items"}
EXPECTED_TABLES = LEGACY_ANCHORS | {
    "clusters", "cluster_members", "daily_reports", "fetch_log", "item_companies", "item_discoveries",
    "topics", "item_topics", "stories", "story_items", "derived_dirty",
    "indexed_items", "schema_migrations",
}
EXPECTED_ITEM_COLUMNS = {
    "id", "source_id", "url", "title", "title_zh", "summary", "raw_summary",
    "channel", "tmt", "reason", "ai_cat", "published_at", "fetched_at",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    absolute = path.expanduser().resolve(strict=True)
    connection = sqlite3.connect(absolute.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _migration_map(migrations: Iterable[Migration]) -> dict[int, Migration]:
    result = {migration.version: migration for migration in migrations}
    versions = sorted(result)
    if versions != list(range(1, len(versions) + 1)):
        raise DatabaseSafetyError("migration versions must be contiguous and start at 1")
    return result


def _read_history(
    db: sqlite3.Connection, migrations: Iterable[Migration] = MIGRATIONS
) -> list[sqlite3.Row]:
    tables = _table_names(db)
    if "schema_migrations" not in tables:
        return []
    columns = {row["name"] for row in db.execute("PRAGMA table_info(schema_migrations)")}
    if not REQUIRED_MIGRATION_COLUMNS.issubset(columns):
        raise UnsupportedSchemaError(
            "schema_migrations has an unsupported layout; restore a known backup or use a compatible release"
        )
    rows = db.execute(
        "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    if not rows:
        raise UnsupportedSchemaError("schema_migrations exists but contains no completed migration")
    known = _migration_map(migrations)
    expected_versions = list(range(1, rows[-1]["version"] + 1))
    if [row["version"] for row in rows] != expected_versions:
        raise UnsupportedSchemaError("migration history has a gap or does not start at version 1")
    for row in rows:
        migration = known.get(row["version"])
        if migration is None:
            raise UnsupportedSchemaError(
                f"database schema version {row['version']} is newer or unknown to this release"
            )
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise UnsupportedSchemaError(
                f"migration {row['version']} metadata does not match this release"
            )
    return rows


def database_state(
    db: sqlite3.Connection, migrations: Iterable[Migration] = MIGRATIONS
) -> tuple[str, int]:
    tables = _table_names(db)
    if not tables:
        return "empty", 0
    if "schema_migrations" in tables:
        rows = _read_history(db, migrations)
        return ("current" if rows[-1]["version"] == CURRENT_SCHEMA_VERSION else "versioned"), rows[-1]["version"]
    if LEGACY_ANCHORS.issubset(tables):
        return "legacy_unversioned", 0
    visible = ", ".join(sorted(tables)[:8]) or "none"
    raise UnsupportedSchemaError(
        f"database is neither empty nor a recognized InfoHub legacy database (tables: {visible})"
    )


def apply_migrations(
    db: sqlite3.Connection, migrations: Iterable[Migration] = MIGRATIONS
) -> tuple[int, ...]:
    """Apply all pending migrations in one explicit, rollback-safe transaction."""
    ordered = tuple(migrations)
    known = _migration_map(ordered)
    state, version = database_state(db, ordered)
    if state == "current":
        return ()
    pending = [known[number] for number in sorted(known) if number > version]
    try:
        db.execute("BEGIN IMMEDIATE")
        if "schema_migrations" not in _table_names(db):
            db.execute(MIGRATION_TABLE_SQL)
        for migration in pending:
            migration.operation(db)
            db.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                (migration.version, migration.name, migration.checksum, _utc_now()),
            )
        if ordered == MIGRATIONS:
            _assert_current_schema(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return tuple(migration.version for migration in pending)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_current_schema(db: sqlite3.Connection) -> None:
    tables = _table_names(db)
    missing_tables = EXPECTED_TABLES - tables
    item_columns = {row["name"] for row in db.execute("PRAGMA table_info(items)")}
    missing_columns = EXPECTED_ITEM_COLUMNS - item_columns
    fts_columns = {row["name"] for row in db.execute("PRAGMA table_info(items_fts)")}
    if missing_tables or missing_columns or "title_zh" not in fts_columns:
        raise DatabaseVerificationError(
            "current schema is incomplete: "
            f"missing tables={sorted(missing_tables)}, columns={sorted(missing_columns)}, "
            f"fts_title_zh={'title_zh' in fts_columns}"
        )
    integrity_rows = [row[0] for row in db.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise DatabaseVerificationError("integrity_check failed: " + "; ".join(integrity_rows[:5]))
    foreign_key_violations = sum(1 for _ in db.execute("PRAGMA foreign_key_check"))
    if foreign_key_violations:
        raise DatabaseVerificationError(
            f"foreign_key_check found {foreign_key_violations} violation(s)"
        )


def verify_database(path: Path | str, require_current: bool = False) -> VerificationReport:
    target = Path(path).expanduser().resolve(strict=True)
    with _connect_readonly(target) as db:
        state, version = database_state(db)
        if require_current and state != "current":
            raise DatabaseVerificationError(
                f"expected schema version {CURRENT_SCHEMA_VERSION}, found {state} version {version}"
            )
        if state == "current":
            _assert_current_schema(db)
        else:
            integrity_rows = [row[0] for row in db.execute("PRAGMA integrity_check")]
            if integrity_rows != ["ok"]:
                raise DatabaseVerificationError(
                    "integrity_check failed: " + "; ".join(integrity_rows[:5])
                )
            foreign_key_violations = sum(1 for _ in db.execute("PRAGMA foreign_key_check"))
            if foreign_key_violations:
                raise DatabaseVerificationError(
                    f"foreign_key_check found {foreign_key_violations} violation(s)"
                )
    return VerificationReport(
        path=str(target),
        state=state,
        schema_version=version,
        integrity="ok",
        foreign_key_violations=0,
        size_bytes=target.stat().st_size,
        # This hashes the named database file. A published backup is converted
        # to DELETE mode and is self-contained; a live WAL database may also
        # have committed bytes in -wal, so callers must not use this as a
        # logical live-dataset fingerprint.
        file_sha256=_sha256(target),
    )


def backup_database(
    source_path: Path | str | None = None, destination: Path | str | None = None
) -> VerificationReport:
    """Create, fsync, verify and atomically publish a SQLite backup."""
    source = Path(source_path or database.DB_PATH).expanduser().resolve(strict=True)
    if destination is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = source.parent / "backups" / f"{source.stem}.{stamp}.db"
    else:
        target = Path(destination).expanduser().resolve()
    if target == source:
        raise DatabaseSafetyError("backup destination must differ from the live database")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"backup destination already exists: {target}")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with _connect_readonly(source) as source_db, sqlite3.connect(temporary) as backup_db:
            source_db.backup(backup_db)
            # Publish one self-contained file. Inheriting WAL mode can require
            # sidecar files merely to open the backup read-only on Windows.
            backup_db.execute("PRAGMA journal_mode=DELETE")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        verify_database(temporary)
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is unavailable on some Windows filesystems. The
            # verified file remains atomically renamed within one directory.
            pass
    finally:
        if temporary.exists():
            temporary.unlink()
    return verify_database(target)


def migrate_database(
    path: Path | str | None = None, backup_before_change: bool = True
) -> MigrationReport:
    target = Path(path or database.DB_PATH).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size:
        with _connect_readonly(target) as before_db:
            previous_state, previous_version = database_state(before_db)
    else:
        previous_state, previous_version = "empty", 0

    needs_change = previous_state != "current"
    backup_path = None
    if needs_change and previous_state != "empty" and backup_before_change:
        backup_path = backup_database(target).path

    with database.get_db(target) as db:
        applied = apply_migrations(db)
    verification = verify_database(target, require_current=True)
    return MigrationReport(
        path=str(target),
        previous_state=previous_state,
        previous_version=previous_version,
        current_version=verification.schema_version,
        applied_versions=applied,
        backup_path=backup_path,
        verification=verification,
    )


def report_json(report: VerificationReport | MigrationReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
