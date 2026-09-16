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

from . import config, database
from .timeutil import utc_now


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
    dataset_id: str | None
    dataset_epoch: str | None
    change_high_water: int | None

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
    applied_at TEXT NOT NULL,
    release_id TEXT NOT NULL
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

JOB_SCHEMA_SQL = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_id TEXT,
    input_version TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    request_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN (
        'pending','running','succeeded','retry_wait','blocked','dead_letter','cancelled'
    )),
    priority INTEGER NOT NULL DEFAULT 0,
    scheduled_for TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
    error_code TEXT,
    error_detail TEXT,
    result_ref TEXT,
    completed_lease_token TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    CHECK (attempt_count <= max_attempts),
    CHECK (
        (state = 'running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL
         AND lease_expires_at IS NOT NULL)
        OR (state != 'running' AND lease_owner IS NULL AND lease_token IS NULL
            AND lease_expires_at IS NULL)
    ),
    CHECK (completed_lease_token IS NULL OR state = 'succeeded')
);
CREATE INDEX idx_jobs_claim
    ON jobs(state, next_attempt_at, priority DESC, scheduled_for, created_at);
CREATE INDEX idx_jobs_lease ON jobs(state, lease_expires_at);

CREATE TABLE job_attempts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'running','succeeded','failed','lease_expired','blocked','cancelled'
    )),
    error_code TEXT,
    error_detail TEXT,
    result_ref TEXT,
    UNIQUE(job_id, attempt_number)
);
CREATE INDEX idx_job_attempts_job ON job_attempts(job_id, attempt_number);

CREATE TABLE schedules (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    config_hash TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK(interval_seconds > 0),
    priority INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    next_due_at TEXT NOT NULL,
    last_enqueued_at TEXT,
    last_success_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_schedules_due ON schedules(enabled, next_due_at);
"""

PUBLICATION_SCHEMA_SQL = """
ALTER TABLE jobs ADD COLUMN dataset_epoch TEXT;
ALTER TABLE jobs ADD COLUMN idempotency_scope TEXT;
ALTER TABLE jobs ADD COLUMN logical_idempotency_key TEXT;
UPDATE jobs SET idempotency_scope='legacy',logical_idempotency_key=idempotency_key
WHERE idempotency_scope IS NULL;
CREATE UNIQUE INDEX idx_jobs_epoch_idempotency
    ON jobs(dataset_epoch, logical_idempotency_key);
CREATE INDEX idx_jobs_epoch_claim
    ON jobs(dataset_epoch, state, next_attempt_at, priority DESC, scheduled_for, created_at);

CREATE TABLE dataset_epochs (
    dataset_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    previous_epoch TEXT,
    reason TEXT NOT NULL,
    owner_environment_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    release_id TEXT NOT NULL,
    PRIMARY KEY(dataset_id, epoch),
    UNIQUE(epoch),
    UNIQUE(dataset_id, previous_epoch),
    FOREIGN KEY(dataset_id, previous_epoch)
        REFERENCES dataset_epochs(dataset_id, epoch)
);

CREATE TABLE dataset_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    dataset_id TEXT NOT NULL,
    current_epoch TEXT NOT NULL,
    owner_environment_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(dataset_id, current_epoch)
        REFERENCES dataset_epochs(dataset_id, epoch)
);

CREATE TABLE change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN (
        'item','event','entity','topic','source','analysis','signal','report','evidence'
    )),
    resource_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN (
        'create','update','withdraw','merge','split','delete'
    )),
    available_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    hash_algorithm TEXT NOT NULL CHECK(hash_algorithm = 'jcs-sha256-v1'),
    job_id TEXT REFERENCES jobs(id),
    lease_token TEXT REFERENCES job_attempts(lease_token),
    UNIQUE(dataset_id, epoch, idempotency_key),
    CHECK((job_id IS NULL AND lease_token IS NULL)
       OR (job_id IS NOT NULL AND lease_token IS NOT NULL)),
    FOREIGN KEY(dataset_id, epoch)
        REFERENCES dataset_epochs(dataset_id, epoch)
);
CREATE INDEX idx_change_log_epoch_seq ON change_log(dataset_id, epoch, seq);
CREATE INDEX idx_change_log_resource
    ON change_log(dataset_id, epoch, resource_type, resource_id, seq);
CREATE INDEX idx_change_log_job ON change_log(job_id, seq);

CREATE TABLE clock_checks (
    id TEXT PRIMARY KEY,
    environment_id TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source TEXT,
    offset_ms REAL,
    status TEXT NOT NULL CHECK(status IN ('verified','suspect','unknown')),
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE knowledge_checkpoints (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    epoch TEXT NOT NULL,
    high_water INTEGER NOT NULL CHECK(high_water >= 0),
    observed_at TEXT NOT NULL,
    clock_status TEXT NOT NULL CHECK(clock_status IN ('verified','suspect','unknown')),
    clock_check_id TEXT REFERENCES clock_checks(id),
    FOREIGN KEY(dataset_id, epoch)
        REFERENCES dataset_epochs(dataset_id, epoch)
);
CREATE INDEX idx_knowledge_checkpoints_water
    ON knowledge_checkpoints(dataset_id, epoch, high_water, observed_at);
CREATE INDEX idx_knowledge_checkpoints_latest
    ON knowledge_checkpoints(dataset_id, epoch, observed_at DESC, id DESC);
"""

INGEST_EVIDENCE_SCHEMA_SQL = """
CREATE TABLE source_config_versions (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    version INTEGER NOT NULL CHECK(version > 0),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    available_at TEXT NOT NULL,
    UNIQUE(source_id, version),
    UNIQUE(source_id, config_hash)
);

CREATE TABLE ingest_runs (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    config_version_id TEXT NOT NULL REFERENCES source_config_versions(id),
    dataset_id TEXT NOT NULL,
    dataset_epoch TEXT NOT NULL,
    parent_run_id TEXT REFERENCES ingest_runs(id),
    scheduled_for TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'queued','running','succeeded','partial','failed','skipped'
    )),
    request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
    raw_count INTEGER NOT NULL DEFAULT 0 CHECK(raw_count >= 0),
    accepted_count INTEGER NOT NULL DEFAULT 0 CHECK(accepted_count >= 0),
    duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK(duplicate_count >= 0),
    rejected_count INTEGER NOT NULL DEFAULT 0 CHECK(rejected_count >= 0),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK(bytes >= 0),
    watermark_before TEXT,
    watermark_after TEXT,
    error_code TEXT,
    trace_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(dataset_id, dataset_epoch)
        REFERENCES dataset_epochs(dataset_id, epoch)
);
CREATE INDEX idx_ingest_runs_source_started
    ON ingest_runs(source_id, started_at DESC);
CREATE INDEX idx_ingest_runs_status_started
    ON ingest_runs(status, started_at);

CREATE TABLE raw_records (
    id TEXT PRIMARY KEY,
    first_ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    request_url TEXT,
    final_url TEXT,
    http_status INTEGER,
    selected_headers TEXT NOT NULL DEFAULT '{}',
    media_type TEXT NOT NULL,
    encoding TEXT,
    payload_sha256 TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    payload_kind TEXT NOT NULL CHECK(payload_kind IN (
        'feed_entry','api_record','html','pdf','legacy_excerpt','generated_metadata'
    )),
    truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0,1)),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    retention_class TEXT NOT NULL,
    UNIQUE(source_id, external_id, payload_sha256)
);
CREATE INDEX idx_raw_records_source_observed
    ON raw_records(source_id, observed_at DESC);
CREATE INDEX idx_raw_records_payload ON raw_records(payload_sha256);

CREATE TABLE raw_observations (
    id TEXT PRIMARY KEY,
    raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
    ingest_run_id TEXT NOT NULL REFERENCES ingest_runs(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    observed_at TEXT NOT NULL,
    UNIQUE(ingest_run_id, ordinal)
);
CREATE INDEX idx_raw_observations_record
    ON raw_observations(raw_record_id, observed_at DESC);

CREATE TRIGGER source_config_versions_no_update
BEFORE UPDATE ON source_config_versions
BEGIN SELECT RAISE(ABORT, 'source config versions are immutable'); END;
CREATE TRIGGER source_config_versions_no_delete
BEFORE DELETE ON source_config_versions
BEGIN SELECT RAISE(ABORT, 'source config versions are immutable'); END;

CREATE TRIGGER raw_records_no_update
BEFORE UPDATE ON raw_records
BEGIN SELECT RAISE(ABORT, 'raw records are immutable'); END;
CREATE TRIGGER raw_records_no_delete
BEFORE DELETE ON raw_records
BEGIN SELECT RAISE(ABORT, 'raw records are immutable'); END;
CREATE TRIGGER raw_observations_no_update
BEFORE UPDATE ON raw_observations
BEGIN SELECT RAISE(ABORT, 'raw observations are immutable'); END;
CREATE TRIGGER raw_observations_no_delete
BEFORE DELETE ON raw_observations
BEGIN SELECT RAISE(ABORT, 'raw observations are immutable'); END;

CREATE TRIGGER ingest_runs_valid_transition
BEFORE UPDATE ON ingest_runs
WHEN OLD.status <> 'running'
  OR NEW.id IS NOT OLD.id
  OR NEW.source_id IS NOT OLD.source_id
  OR NEW.config_version_id IS NOT OLD.config_version_id
  OR NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.dataset_epoch IS NOT OLD.dataset_epoch
  OR NEW.parent_run_id IS NOT OLD.parent_run_id
  OR NEW.scheduled_for IS NOT OLD.scheduled_for
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.watermark_before IS NOT OLD.watermark_before
  OR NEW.trace_id IS NOT OLD.trace_id
  OR NEW.status NOT IN ('succeeded','partial','failed','skipped')
  OR NEW.finished_at IS NULL
BEGIN SELECT RAISE(ABORT, 'invalid immutable ingest run transition'); END;
CREATE TRIGGER ingest_runs_no_delete
BEFORE DELETE ON ingest_runs
BEGIN SELECT RAISE(ABORT, 'ingest runs are immutable'); END;
"""

SOURCE_TIME_SCHEMA_SQL = """
CREATE TABLE source_time_values (
    id TEXT PRIMARY KEY,
    raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    field_path TEXT NOT NULL,
    raw_value TEXT,
    role TEXT NOT NULL CHECK(role IN (
        'published','updated','accepted','filing_date','report_period','other'
    )),
    source_timezone TEXT,
    utc TEXT CHECK(utc IS NULL OR (length(utc)=27 AND substr(utc,27,1)='Z')),
    range_start_utc TEXT CHECK(
        range_start_utc IS NULL OR (length(range_start_utc)=27 AND substr(range_start_utc,27,1)='Z')
    ),
    range_end_utc TEXT CHECK(
        range_end_utc IS NULL OR (length(range_end_utc)=27 AND substr(range_end_utc,27,1)='Z')
    ),
    precision TEXT NOT NULL CHECK(precision IN (
        'second','minute','date','month','unknown'
    )),
    interpretation TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'valid','missing','invalid','missing_timezone','ambiguous_local_time',
        'nonexistent_local_time','future_suspect'
    )),
    rule_version TEXT NOT NULL,
    tzdb_version TEXT NOT NULL,
    CHECK((range_start_utc IS NULL) = (range_end_utc IS NULL)),
    UNIQUE(raw_record_id, rule_version, tzdb_version, ordinal)
);
CREATE INDEX idx_source_time_record_role
    ON source_time_values(raw_record_id, role, ordinal);
CREATE TRIGGER source_time_values_no_update
BEFORE UPDATE ON source_time_values
BEGIN SELECT RAISE(ABORT, 'source time values are immutable'); END;
CREATE TRIGGER source_time_values_no_delete
BEFORE DELETE ON source_time_values
BEGIN SELECT RAISE(ABORT, 'source time values are immutable'); END;
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


def _durable_job_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, JOB_SCHEMA_SQL)


def _publication_ledger_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, PUBLICATION_SCHEMA_SQL)
    now = _utc_now()
    dataset_id = f"dataset_{uuid4().hex}"
    epoch = f"epoch_{uuid4().hex}"
    release_id = config.APP_VERSION.strip() or "unknown"
    db.execute(
        """INSERT INTO dataset_epochs(
               dataset_id,epoch,previous_epoch,reason,owner_environment_id,
               started_at,release_id
           ) VALUES(?,?,NULL,'initialization',?,?,?)""",
        (dataset_id, epoch, config.ENVIRONMENT_ID, now, release_id),
    )
    db.execute(
        """INSERT INTO dataset_state(
               singleton,dataset_id,current_epoch,owner_environment_id,created_at,updated_at
           ) VALUES(1,?,?,?,?,?)""",
        (dataset_id, epoch, config.ENVIRONMENT_ID, now, now),
    )
    # Version 2 jobs used a global idempotency namespace. Adopt them into the
    # initial epoch without rewriting their physical key or request hash, so
    # queued and leased work remains claimable and same-request retries still
    # resolve to the original row after migration.
    db.execute(
        """UPDATE jobs SET dataset_epoch=?,idempotency_scope=?,
                  logical_idempotency_key=COALESCE(logical_idempotency_key,idempotency_key)
           WHERE dataset_epoch IS NULL""",
        (epoch, epoch),
    )


def _ingest_evidence_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, INGEST_EVIDENCE_SCHEMA_SQL)


def _source_time_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, SOURCE_TIME_SCHEMA_SQL)


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
    Migration(
        2,
        "durable job foundation",
        JOB_SCHEMA_SQL,
        _durable_job_foundation,
    ),
    Migration(
        3,
        "atomic publication ledger",
        PUBLICATION_SCHEMA_SQL
        + "\ninitialize:dataset-and-epoch-uuid-v1"
        + "\nadopt-version-2-jobs-into-initial-epoch-v1",
        _publication_ledger_foundation,
    ),
    Migration(
        4,
        "immutable ingest evidence foundation",
        INGEST_EVIDENCE_SCHEMA_SQL,
        _ingest_evidence_foundation,
    ),
    Migration(
        5,
        "source time evidence foundation",
        SOURCE_TIME_SCHEMA_SQL,
        _source_time_foundation,
    ),
)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version
REQUIRED_MIGRATION_COLUMNS = {
    "version", "name", "checksum", "applied_at", "release_id",
}
LEGACY_ANCHORS = {"companies", "sources", "items"}
EXPECTED_TABLES = LEGACY_ANCHORS | {
    "clusters", "cluster_members", "daily_reports", "fetch_log", "item_companies", "item_discoveries",
    "topics", "item_topics", "stories", "story_items", "derived_dirty",
    "indexed_items", "schema_migrations", "jobs", "job_attempts", "schedules",
    "dataset_epochs", "dataset_state", "change_log", "clock_checks",
    "knowledge_checkpoints",
    "source_config_versions", "ingest_runs", "raw_records", "raw_observations",
    "source_time_values",
}
EXPECTED_ITEM_COLUMNS = {
    "id", "source_id", "url", "title", "title_zh", "summary", "raw_summary",
    "channel", "tmt", "reason", "ai_cat", "published_at", "fetched_at",
}
EXPECTED_JOB_COLUMNS = {
    "id", "kind", "subject_id", "input_version", "payload_json", "request_hash",
    "idempotency_key", "dataset_epoch", "state", "priority", "scheduled_for", "next_attempt_at",
    "idempotency_scope", "logical_idempotency_key",
    "lease_owner", "lease_token",
    "lease_generation", "lease_expires_at", "heartbeat_at", "attempt_count", "max_attempts",
    "error_code", "error_detail", "result_ref", "completed_lease_token", "created_at",
    "updated_at", "finished_at",
}
EXPECTED_JOB_ATTEMPT_COLUMNS = {
    "id", "job_id", "attempt_number", "worker_id", "lease_token", "started_at",
    "finished_at", "status", "error_code", "error_detail", "result_ref",
}
EXPECTED_SCHEDULE_COLUMNS = {
    "id", "kind", "subject_id", "payload_json", "config_hash", "interval_seconds",
    "priority", "max_attempts", "enabled", "next_due_at", "last_enqueued_at",
    "last_success_at", "created_at", "updated_at",
}
EXPECTED_DATASET_STATE_COLUMNS = {
    "singleton", "dataset_id", "current_epoch", "owner_environment_id", "created_at",
    "updated_at",
}
EXPECTED_DATASET_EPOCH_COLUMNS = {
    "dataset_id", "epoch", "previous_epoch", "reason", "owner_environment_id",
    "started_at", "release_id",
}
EXPECTED_CHANGE_COLUMNS = {
    "seq", "dataset_id", "epoch", "idempotency_key", "resource_type", "resource_id",
    "version_id", "operation", "available_at", "payload_json", "payload_sha256",
    "hash_algorithm", "job_id", "lease_token",
}
EXPECTED_CHECKPOINT_COLUMNS = {
    "id", "dataset_id", "epoch", "high_water", "observed_at", "clock_status",
    "clock_check_id",
}
EXPECTED_CLOCK_CHECK_COLUMNS = {
    "id", "environment_id", "measured_at", "recorded_at", "source", "offset_ms",
    "status", "detail_json",
}
EXPECTED_SOURCE_CONFIG_VERSION_COLUMNS = {
    "id", "source_id", "version", "config_json", "config_hash", "available_at",
}
EXPECTED_INGEST_RUN_COLUMNS = {
    "id", "source_id", "config_version_id", "dataset_id", "dataset_epoch",
    "parent_run_id", "scheduled_for",
    "started_at", "finished_at", "status", "request_count", "raw_count",
    "accepted_count", "duplicate_count", "rejected_count", "bytes",
    "watermark_before", "watermark_after", "error_code", "trace_id",
}
EXPECTED_RAW_RECORD_COLUMNS = {
    "id", "first_ingest_run_id", "source_id", "external_id", "observed_at",
    "ingested_at", "request_url", "final_url", "http_status", "selected_headers",
    "media_type", "encoding", "payload_sha256", "payload_ref", "payload_kind",
    "truncated", "size_bytes", "retention_class",
}
EXPECTED_RAW_OBSERVATION_COLUMNS = {
    "id", "raw_record_id", "ingest_run_id", "ordinal", "observed_at",
}
EXPECTED_SOURCE_TIME_COLUMNS = {
    "id", "raw_record_id", "ordinal", "field_path", "raw_value", "role",
    "source_timezone", "utc", "range_start_utc", "range_end_utc", "precision",
    "interpretation", "status", "rule_version", "tzdb_version",
}
EXPECTED_INGEST_TRIGGERS = {
    "source_config_versions_no_update", "source_config_versions_no_delete",
    "raw_records_no_update", "raw_records_no_delete",
    "raw_observations_no_update", "raw_observations_no_delete",
    "ingest_runs_valid_transition", "ingest_runs_no_delete",
    "source_time_values_no_update", "source_time_values_no_delete",
}


def _utc_now() -> str:
    return utc_now()


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
        "SELECT version,name,checksum,applied_at,release_id FROM schema_migrations ORDER BY version"
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
    ordered = tuple(migrations)
    tables = _table_names(db)
    if not tables:
        return "empty", 0
    if "schema_migrations" in tables:
        rows = _read_history(db, ordered)
        latest = max(_migration_map(ordered), default=0)
        return ("current" if rows[-1]["version"] == latest else "versioned"), rows[-1]["version"]
    if LEGACY_ANCHORS.issubset(tables):
        return "legacy_unversioned", 0
    visible = ", ".join(sorted(tables)[:8]) or "none"
    raise UnsupportedSchemaError(
        f"database is neither empty nor a recognized InfoHub legacy database (tables: {visible})"
    )


def apply_migrations(
    db: sqlite3.Connection,
    migrations: Iterable[Migration] = MIGRATIONS,
    release_id: str | None = None,
) -> tuple[int, ...]:
    """Apply all pending migrations in one explicit, rollback-safe transaction."""
    ordered = tuple(migrations)
    known = _migration_map(ordered)
    state, version = database_state(db, ordered)
    if state == "current":
        return ()
    pending = [known[number] for number in sorted(known) if number > version]
    applied_release = (release_id or config.APP_VERSION).strip() or "unknown"
    try:
        db.execute("BEGIN IMMEDIATE")
        if "schema_migrations" not in _table_names(db):
            db.execute(MIGRATION_TABLE_SQL)
        for migration in pending:
            migration.operation(db)
            db.execute(
                """INSERT INTO schema_migrations(
                       version,name,checksum,applied_at,release_id
                   ) VALUES(?,?,?,?,?)""",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    _utc_now(),
                    applied_release,
                ),
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
    job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
    missing_job_columns = EXPECTED_JOB_COLUMNS - job_columns
    attempt_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(job_attempts)")
    }
    missing_attempt_columns = EXPECTED_JOB_ATTEMPT_COLUMNS - attempt_columns
    schedule_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(schedules)")
    }
    missing_schedule_columns = EXPECTED_SCHEDULE_COLUMNS - schedule_columns
    dataset_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(dataset_state)")
    }
    missing_dataset_columns = EXPECTED_DATASET_STATE_COLUMNS - dataset_columns
    epoch_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(dataset_epochs)")
    }
    missing_epoch_columns = EXPECTED_DATASET_EPOCH_COLUMNS - epoch_columns
    change_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(change_log)")
    }
    missing_change_columns = EXPECTED_CHANGE_COLUMNS - change_columns
    checkpoint_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(knowledge_checkpoints)")
    }
    missing_checkpoint_columns = EXPECTED_CHECKPOINT_COLUMNS - checkpoint_columns
    clock_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(clock_checks)")
    }
    missing_clock_columns = EXPECTED_CLOCK_CHECK_COLUMNS - clock_columns
    source_config_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(source_config_versions)")
    }
    missing_source_config_columns = (
        EXPECTED_SOURCE_CONFIG_VERSION_COLUMNS - source_config_columns
    )
    ingest_run_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(ingest_runs)")
    }
    missing_ingest_run_columns = EXPECTED_INGEST_RUN_COLUMNS - ingest_run_columns
    raw_record_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(raw_records)")
    }
    missing_raw_record_columns = EXPECTED_RAW_RECORD_COLUMNS - raw_record_columns
    raw_observation_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(raw_observations)")
    }
    missing_raw_observation_columns = (
        EXPECTED_RAW_OBSERVATION_COLUMNS - raw_observation_columns
    )
    source_time_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(source_time_values)")
    }
    missing_source_time_columns = EXPECTED_SOURCE_TIME_COLUMNS - source_time_columns
    ingest_triggers = {
        row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    missing_ingest_triggers = EXPECTED_INGEST_TRIGGERS - ingest_triggers
    if (
        missing_tables
        or missing_columns
        or missing_job_columns
        or missing_attempt_columns
        or missing_schedule_columns
        or missing_dataset_columns
        or missing_epoch_columns
        or missing_change_columns
        or missing_checkpoint_columns
        or missing_clock_columns
        or missing_source_config_columns
        or missing_ingest_run_columns
        or missing_raw_record_columns
        or missing_raw_observation_columns
        or missing_source_time_columns
        or missing_ingest_triggers
        or "title_zh" not in fts_columns
    ):
        raise DatabaseVerificationError(
            "current schema is incomplete: "
            f"missing tables={sorted(missing_tables)}, columns={sorted(missing_columns)}, "
            f"job_columns={sorted(missing_job_columns)}, "
            f"attempt_columns={sorted(missing_attempt_columns)}, "
            f"schedule_columns={sorted(missing_schedule_columns)}, "
            f"dataset_columns={sorted(missing_dataset_columns)}, "
            f"epoch_columns={sorted(missing_epoch_columns)}, "
            f"change_columns={sorted(missing_change_columns)}, "
            f"checkpoint_columns={sorted(missing_checkpoint_columns)}, "
            f"clock_columns={sorted(missing_clock_columns)}, "
            f"source_config_columns={sorted(missing_source_config_columns)}, "
            f"ingest_run_columns={sorted(missing_ingest_run_columns)}, "
            f"raw_record_columns={sorted(missing_raw_record_columns)}, "
            f"raw_observation_columns={sorted(missing_raw_observation_columns)}, "
            f"source_time_columns={sorted(missing_source_time_columns)}, "
            f"ingest_triggers={sorted(missing_ingest_triggers)}, "
            f"fts_title_zh={'title_zh' in fts_columns}"
        )
    identity_rows = db.execute(
        """SELECT state.dataset_id,state.current_epoch,state.owner_environment_id,
                  epoch.owner_environment_id AS epoch_owner
           FROM dataset_state AS state
           JOIN dataset_epochs AS epoch
             ON epoch.dataset_id=state.dataset_id AND epoch.epoch=state.current_epoch"""
    ).fetchall()
    if len(identity_rows) != 1:
        raise DatabaseVerificationError(
            "current schema must contain exactly one valid dataset identity"
        )
    if identity_rows[0]["owner_environment_id"] != identity_rows[0]["epoch_owner"]:
        raise DatabaseVerificationError(
            "dataset state and current epoch have different environment owners"
        )
    invalid_jobs = db.execute(
        """SELECT COUNT(*) FROM jobs AS job
           LEFT JOIN dataset_epochs AS epoch ON epoch.epoch=job.dataset_epoch
           WHERE job.dataset_epoch IS NULL OR job.idempotency_scope IS NULL
              OR job.logical_idempotency_key IS NULL OR epoch.epoch IS NULL"""
    ).fetchone()[0]
    if invalid_jobs:
        raise DatabaseVerificationError(
            f"{invalid_jobs} durable job(s) are outside a valid dataset epoch"
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
    dataset_id = None
    dataset_epoch = None
    change_high_water = None
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
        if "dataset_state" in _table_names(db):
            identity = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            if identity:
                dataset_id = identity["dataset_id"]
                dataset_epoch = identity["current_epoch"]
                change_high_water = db.execute(
                    """SELECT COALESCE(MAX(seq),0) FROM change_log
                       WHERE dataset_id=? AND epoch=?""",
                    (dataset_id, dataset_epoch),
                ).fetchone()[0]
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
        dataset_id=dataset_id,
        dataset_epoch=dataset_epoch,
        change_high_water=change_high_water,
    )


def backup_database(
    source_path: Path | str | None = None, destination: Path | str | None = None
) -> VerificationReport:
    """Create, fsync, verify and atomically publish a SQLite backup."""
    source = Path(source_path or database.DB_PATH).expanduser().resolve(strict=True)
    if destination is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        configured_database = config.DB_PATH.expanduser().resolve()
        backup_root = config.BACKUP_PATH if source == configured_database else source.parent / "backups"
        environment_label = config.ENVIRONMENT_ID if source == configured_database else "isolated-copy"
        target = backup_root / f"{source.stem}.{environment_label}.{stamp}.db"
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
