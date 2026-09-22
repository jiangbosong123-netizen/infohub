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

DOCUMENT_VERSION_SCHEMA_SQL = """
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    legacy_item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
    kind TEXT NOT NULL CHECK(kind IN (
        'article','flash','filing','policy_release','research','commentary','transcript','other'
    )),
    first_seen_at TEXT NOT NULL CHECK(length(first_seen_at)=27 AND substr(first_seen_at,27,1)='Z'),
    current_version_id TEXT UNIQUE REFERENCES document_versions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN (
        'active','withdrawn','restricted','duplicate_alias'
    ))
);
CREATE INDEX idx_documents_dataset_seen ON documents(dataset_id, first_seen_at DESC);

CREATE TABLE document_versions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    version INTEGER NOT NULL CHECK(version > 0),
    previous_version_id TEXT REFERENCES document_versions(id),
    normalizer_version TEXT NOT NULL,
    normalized_at TEXT NOT NULL CHECK(length(normalized_at)=27 AND substr(normalized_at,27,1)='Z'),
    title_original TEXT NOT NULL,
    language TEXT NOT NULL,
    text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    version_sha256 TEXT NOT NULL CHECK(length(version_sha256)=64),
    canonical_url TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    publisher_id TEXT,
    published_at TEXT CHECK(
        published_at IS NULL OR (length(published_at)=27 AND substr(published_at,27,1)='Z')
    ),
    published_time_value_id TEXT REFERENCES source_time_values(id),
    published_precision TEXT NOT NULL CHECK(published_precision IN (
        'second','minute','date','month','unknown'
    )),
    time_status TEXT NOT NULL CHECK(time_status IN (
        'parsed','missing','invalid','missing_timezone','ambiguous_local_time',
        'nonexistent_local_time','future_suspect','legacy_unverified'
    )),
    time_rule_version TEXT NOT NULL,
    tzdb_version TEXT NOT NULL,
    content_origin TEXT NOT NULL CHECK(content_origin IN (
        'publisher_text','feed_excerpt','generated_metadata','legacy_unknown'
    )),
    content_extent TEXT NOT NULL CHECK(content_extent IN (
        'full','excerpt','title_only','none'
    )),
    truncated INTEGER NOT NULL CHECK(truncated IN (0,1)),
    extraction_status TEXT NOT NULL CHECK(extraction_status IN (
        'complete','partial','not_attempted','failed'
    )),
    correction_kind TEXT NOT NULL CHECK(correction_kind IN (
        'initial','content_change','metadata_change'
    )),
    available_at TEXT NOT NULL CHECK(length(available_at)=27 AND substr(available_at,27,1)='Z'),
    UNIQUE(document_id, version),
    CHECK((version=1 AND previous_version_id IS NULL)
       OR (version>1 AND previous_version_id IS NOT NULL))
);
CREATE INDEX idx_document_versions_document
    ON document_versions(document_id, version DESC);
CREATE INDEX idx_document_versions_content ON document_versions(content_sha256);

CREATE TABLE document_version_inputs (
    version_id TEXT NOT NULL REFERENCES document_versions(id),
    raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
    role TEXT NOT NULL CHECK(role IN ('primary','metadata','additional')),
    PRIMARY KEY(version_id, raw_record_id)
);
CREATE INDEX idx_document_inputs_raw ON document_version_inputs(raw_record_id);

CREATE TABLE document_locators (
    document_id TEXT NOT NULL REFERENCES documents(id),
    source_id INTEGER NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'canonical','mirror','redirect','source_alias'
    )),
    first_observed_at TEXT NOT NULL CHECK(
        length(first_observed_at)=27 AND substr(first_observed_at,27,1)='Z'
    ),
    last_observed_at TEXT NOT NULL CHECK(
        length(last_observed_at)=27 AND substr(last_observed_at,27,1)='Z'
    ),
    PRIMARY KEY(source_id, external_id),
    CHECK(last_observed_at >= first_observed_at)
);
CREATE INDEX idx_document_locators_document ON document_locators(document_id);
CREATE INDEX idx_document_locators_url ON document_locators(canonical_url);

CREATE TRIGGER document_versions_valid_append
BEFORE INSERT ON document_versions
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM document_versions WHERE document_id=NEW.document_id), 1
     )
  OR (NEW.version=1 AND NEW.previous_version_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_version_id IS NOT (
         SELECT id FROM document_versions
         WHERE document_id=NEW.document_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT, 'document versions must form a contiguous append-only chain'); END;

CREATE TRIGGER document_versions_no_update
BEFORE UPDATE ON document_versions
BEGIN SELECT RAISE(ABORT, 'document versions are immutable'); END;
CREATE TRIGGER document_versions_no_delete
BEFORE DELETE ON document_versions
BEGIN SELECT RAISE(ABORT, 'document versions are immutable'); END;
CREATE TRIGGER document_version_inputs_no_update
BEFORE UPDATE ON document_version_inputs
BEGIN SELECT RAISE(ABORT, 'document version inputs are immutable'); END;
CREATE TRIGGER document_version_inputs_no_delete
BEFORE DELETE ON document_version_inputs
BEGIN SELECT RAISE(ABORT, 'document version inputs are immutable'); END;

CREATE TRIGGER documents_identity_immutable
BEFORE UPDATE ON documents
WHEN NEW.id IS NOT OLD.id
  OR NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.legacy_item_id IS NOT OLD.legacy_item_id
  OR NEW.kind IS NOT OLD.kind
  OR NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN SELECT RAISE(ABORT, 'document identity is immutable'); END;

CREATE TRIGGER documents_current_version_valid
BEFORE UPDATE OF current_version_id ON documents
WHEN NEW.current_version_id IS NOT NULL
 AND NOT EXISTS(
     SELECT 1 FROM document_versions
     WHERE id=NEW.current_version_id AND document_id=NEW.id
 )
BEGIN SELECT RAISE(ABORT, 'current version must belong to the document'); END;
CREATE TRIGGER documents_current_version_required
BEFORE UPDATE OF current_version_id ON documents
WHEN NEW.current_version_id IS NULL
BEGIN SELECT RAISE(ABORT, 'current version cannot be cleared'); END;

CREATE TRIGGER documents_no_delete
BEFORE DELETE ON documents
BEGIN SELECT RAISE(ABORT, 'documents are stable identities'); END;

CREATE TRIGGER document_locators_identity_immutable
BEFORE UPDATE ON document_locators
WHEN NEW.document_id IS NOT OLD.document_id
  OR NEW.source_id IS NOT OLD.source_id
  OR NEW.external_id IS NOT OLD.external_id
  OR NEW.first_observed_at IS NOT OLD.first_observed_at
BEGIN SELECT RAISE(ABORT, 'document locator identity is immutable'); END;
CREATE TRIGGER document_locators_no_delete
BEFORE DELETE ON document_locators
BEGIN SELECT RAISE(ABORT, 'document locators are append-only'); END;
"""

LEGACY_BACKFILL_SCHEMA_SQL = """
ALTER TABLE document_versions ADD COLUMN availability_basis TEXT NOT NULL
    DEFAULT 'transaction_recorded' CHECK(availability_basis IN (
        'transaction_recorded','legacy_unknown'
    ));
ALTER TABLE document_versions ADD COLUMN point_in_time_eligible INTEGER NOT NULL
    DEFAULT 0 CHECK(point_in_time_eligible IN (0,1));

CREATE TABLE legacy_backfill_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    dataset_id TEXT NOT NULL,
    cutoff_item_id INTEGER NOT NULL CHECK(cutoff_item_id >= 0),
    cutoff_report_id INTEGER NOT NULL CHECK(cutoff_report_id >= 0),
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error_detail TEXT
);

CREATE TABLE legacy_backfill_sources (
    source_id INTEGER PRIMARY KEY REFERENCES sources(id),
    dataset_id TEXT NOT NULL,
    active_run_id TEXT REFERENCES ingest_runs(id),
    last_item_id INTEGER NOT NULL DEFAULT 0 CHECK(last_item_id >= 0),
    processed_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_count >= 0),
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    updated_at TEXT NOT NULL,
    error_detail TEXT
);
CREATE INDEX idx_legacy_backfill_sources_status
    ON legacy_backfill_sources(status, source_id);

CREATE TABLE legacy_object_mappings (
    dataset_id TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN (
        'item','item_discovery','daily_report'
    )),
    legacy_key TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN (
        'document','document_locator','legacy_report'
    )),
    target_id TEXT NOT NULL,
    legacy_sha256 TEXT NOT NULL CHECK(length(legacy_sha256)=64),
    mapping_status TEXT NOT NULL CHECK(mapping_status IN (
        'mapped','mapped_unverified','pending_domain_upgrade'
    )),
    detail_json TEXT NOT NULL DEFAULT '{}',
    available_at TEXT NOT NULL,
    PRIMARY KEY(dataset_id, resource_type, legacy_key)
);
CREATE INDEX idx_legacy_mappings_target
    ON legacy_object_mappings(dataset_id, target_type, target_id);

CREATE TABLE legacy_report_identities (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    legacy_report_id INTEGER NOT NULL,
    report_date TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    legacy_created_at TEXT,
    available_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status='legacy_unverified'),
    UNIQUE(dataset_id, legacy_report_id)
);

CREATE TRIGGER legacy_object_mappings_no_update
BEFORE UPDATE ON legacy_object_mappings
BEGIN SELECT RAISE(ABORT, 'legacy mappings are immutable'); END;
CREATE TRIGGER legacy_object_mappings_no_delete
BEFORE DELETE ON legacy_object_mappings
BEGIN SELECT RAISE(ABORT, 'legacy mappings are immutable'); END;
CREATE TRIGGER legacy_report_identities_no_update
BEFORE UPDATE ON legacy_report_identities
BEGIN SELECT RAISE(ABORT, 'legacy report identities are immutable'); END;
CREATE TRIGGER legacy_report_identities_no_delete
BEFORE DELETE ON legacy_report_identities
BEGIN SELECT RAISE(ABORT, 'legacy report identities are immutable'); END;
"""

IDENTITY_CATALOG_SCHEMA_SQL = """
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN (
        'organization','security','person','product','model','industry','region','macro_concept'
    )),
    current_version_id TEXT UNIQUE REFERENCES entity_versions(id),
    status TEXT NOT NULL CHECK(status IN ('active','inactive','merged','restricted')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_entities_dataset_type ON entities(dataset_id,type,status);

CREATE TABLE entity_versions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_version_id TEXT REFERENCES entity_versions(id),
    type TEXT NOT NULL CHECK(type IN (
        'organization','security','person','product','model','industry','region','macro_concept'
    )),
    canonical_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','inactive','merged','restricted')),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    version_sha256 TEXT NOT NULL CHECK(length(version_sha256)=64),
    available_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(entity_id,version),
    CHECK((version=1 AND previous_version_id IS NULL)
       OR (version>1 AND previous_version_id IS NOT NULL))
);

CREATE TABLE entity_identifiers (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    namespace TEXT NOT NULL,
    value TEXT NOT NULL,
    qualifier_json TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT,
    valid_to TEXT,
    evidence_id TEXT,
    verification_status TEXT NOT NULL CHECK(verification_status IN (
        'verified','legacy_unverified','candidate','rejected'
    )),
    assertion_sha256 TEXT NOT NULL CHECK(length(assertion_sha256)=64),
    available_at TEXT NOT NULL,
    UNIQUE(entity_id,assertion_sha256),
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);
CREATE INDEX idx_entity_identifiers_lookup
    ON entity_identifiers(namespace,value,verification_status);

CREATE TABLE entity_aliases (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'und',
    match_mode TEXT NOT NULL CHECK(match_mode IN ('exact','casefold','candidate_only')),
    ambiguity TEXT NOT NULL CHECK(ambiguity IN ('unique','ambiguous','unreviewed')),
    status TEXT NOT NULL CHECK(status IN ('active','deprecated','rejected')),
    evidence_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    assertion_sha256 TEXT NOT NULL CHECK(length(assertion_sha256)=64),
    available_at TEXT NOT NULL,
    UNIQUE(entity_id,assertion_sha256),
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);
CREATE INDEX idx_entity_aliases_lookup ON entity_aliases(alias_key,status);

CREATE TABLE entity_relations (
    id TEXT PRIMARY KEY,
    from_entity_id TEXT NOT NULL REFERENCES entities(id),
    to_entity_id TEXT NOT NULL REFERENCES entities(id),
    relation TEXT NOT NULL CHECK(relation IN (
        'issues','employed_by','subsidiary_of','developed_by','located_in','related_to'
    )),
    valid_from TEXT,
    valid_to TEXT,
    evidence_id TEXT,
    verification_status TEXT NOT NULL CHECK(verification_status IN (
        'verified','legacy_unverified','candidate','rejected'
    )),
    available_at TEXT NOT NULL,
    UNIQUE(from_entity_id,to_entity_id,relation,valid_from),
    CHECK(from_entity_id<>to_entity_id),
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);

CREATE TABLE security_listings (
    id TEXT PRIMARY KEY,
    security_entity_id TEXT NOT NULL REFERENCES entities(id),
    issuer_entity_id TEXT NOT NULL REFERENCES entities(id),
    exchange TEXT,
    ticker TEXT,
    listing_type TEXT NOT NULL CHECK(listing_type IN (
        'common_stock','adr','depositary_receipt','preferred','other','unknown'
    )),
    valid_from TEXT,
    valid_to TEXT,
    evidence_id TEXT,
    verification_status TEXT NOT NULL CHECK(verification_status IN (
        'verified','legacy_unverified','candidate','rejected'
    )),
    available_at TEXT NOT NULL,
    publication_seq INTEGER,
    CHECK(security_entity_id<>issuer_entity_id),
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from),
    CHECK(ticker IS NULL OR exchange IS NOT NULL)
);
CREATE INDEX idx_security_listings_lookup
    ON security_listings(exchange,ticker,valid_from,valid_to);

CREATE TABLE entity_mentions (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    entity_id TEXT REFERENCES entities(id),
    evidence_id TEXT,
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    raw_confidence REAL,
    calibration_version TEXT,
    status TEXT NOT NULL CHECK(status IN ('resolved','unresolved','ambiguous','rejected')),
    available_at TEXT NOT NULL,
    CHECK(raw_confidence IS NULL OR (raw_confidence>=0 AND raw_confidence<=1))
);
CREATE INDEX idx_entity_mentions_entity ON entity_mentions(entity_id,document_version_id);

CREATE TABLE legacy_company_entities (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    entity_id TEXT NOT NULL UNIQUE REFERENCES entities(id),
    legacy_sha256 TEXT NOT NULL CHECK(length(legacy_sha256)=64),
    available_at TEXT NOT NULL
);

CREATE TABLE publishers (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    organization_entity_id TEXT REFERENCES entities(id),
    current_version_id TEXT UNIQUE REFERENCES publisher_versions(id),
    status TEXT NOT NULL CHECK(status IN ('active','inactive','merged','restricted')),
    created_at TEXT NOT NULL
);

CREATE TABLE publisher_versions (
    id TEXT PRIMARY KEY,
    publisher_id TEXT NOT NULL REFERENCES publishers(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_version_id TEXT REFERENCES publisher_versions(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','inactive','merged','restricted')),
    version_sha256 TEXT NOT NULL CHECK(length(version_sha256)=64),
    available_at TEXT NOT NULL,
    UNIQUE(publisher_id,version),
    CHECK((version=1 AND previous_version_id IS NULL)
       OR (version>1 AND previous_version_id IS NOT NULL))
);

CREATE TABLE publisher_legacy_keys (
    legacy_key TEXT PRIMARY KEY,
    publisher_id TEXT NOT NULL REFERENCES publishers(id),
    available_at TEXT NOT NULL
);

CREATE TABLE publisher_names (
    id TEXT PRIMARY KEY,
    publisher_id TEXT NOT NULL REFERENCES publishers(id),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'und',
    status TEXT NOT NULL CHECK(status IN ('active','deprecated','rejected')),
    assertion_sha256 TEXT NOT NULL CHECK(length(assertion_sha256)=64),
    available_at TEXT NOT NULL,
    UNIQUE(publisher_id,assertion_sha256)
);
CREATE INDEX idx_publisher_names_lookup ON publisher_names(name_key,status);

CREATE TABLE publisher_domains (
    id TEXT PRIMARY KEY,
    publisher_id TEXT NOT NULL REFERENCES publishers(id),
    domain TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    evidence_id TEXT,
    verification_status TEXT NOT NULL CHECK(verification_status IN (
        'verified','legacy_unverified','candidate','rejected'
    )),
    assertion_sha256 TEXT NOT NULL CHECK(length(assertion_sha256)=64),
    available_at TEXT NOT NULL,
    UNIQUE(publisher_id,assertion_sha256),
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>=valid_from)
);
CREATE INDEX idx_publisher_domains_lookup
    ON publisher_domains(domain,verification_status,valid_from,valid_to);

CREATE TABLE document_attributions (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    publisher_id TEXT REFERENCES publishers(id),
    origin_document_id TEXT REFERENCES documents(id),
    relation TEXT NOT NULL CHECK(relation IN ('original','syndicated','cites','unknown')),
    evidence_id TEXT,
    method TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('asserted','verified','unknown','rejected')),
    available_at TEXT NOT NULL
);
CREATE INDEX idx_document_attributions_document
    ON document_attributions(document_version_id,publisher_id);

CREATE TABLE topic_catalog (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    current_version_id TEXT UNIQUE REFERENCES topic_versions(id),
    status TEXT NOT NULL CHECK(status IN ('active','inactive','merged','restricted')),
    created_at TEXT NOT NULL
);

CREATE TABLE topic_versions (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topic_catalog(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_version_id TEXT REFERENCES topic_versions(id),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    group_key TEXT NOT NULL CHECK(group_key IN (
        'company_model','technology','format','macro','research'
    )),
    description TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    rules_hash TEXT NOT NULL CHECK(length(rules_hash)=64),
    version_sha256 TEXT NOT NULL CHECK(length(version_sha256)=64),
    status TEXT NOT NULL CHECK(status IN ('active','inactive','merged','restricted')),
    available_at TEXT NOT NULL,
    UNIQUE(topic_id,version),
    CHECK((version=1 AND previous_version_id IS NULL)
       OR (version>1 AND previous_version_id IS NOT NULL))
);
CREATE INDEX idx_topic_versions_slug ON topic_versions(slug,available_at);

CREATE TABLE topic_slug_aliases (
    slug TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topic_catalog(id),
    available_at TEXT NOT NULL
);

CREATE TABLE document_topic_assignments (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    topic_version_id TEXT NOT NULL REFERENCES topic_versions(id),
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    analysis_result_id TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('candidate','accepted','rejected','superseded')),
    available_at TEXT NOT NULL
);
CREATE INDEX idx_document_topics_topic
    ON document_topic_assignments(topic_version_id,document_version_id);

CREATE TRIGGER entity_versions_valid_append BEFORE INSERT ON entity_versions
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM entity_versions WHERE entity_id=NEW.entity_id),1
     )
  OR (NEW.version=1 AND NEW.previous_version_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_version_id IS NOT (
         SELECT id FROM entity_versions
         WHERE entity_id=NEW.entity_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'entity versions must form a contiguous append-only chain'); END;
CREATE TRIGGER entity_versions_no_update BEFORE UPDATE ON entity_versions
BEGIN SELECT RAISE(ABORT,'entity versions are immutable'); END;
CREATE TRIGGER entity_versions_no_delete BEFORE DELETE ON entity_versions
BEGIN SELECT RAISE(ABORT,'entity versions are immutable'); END;
CREATE TRIGGER entities_identity_immutable BEFORE UPDATE ON entities
WHEN NEW.id IS NOT OLD.id OR NEW.dataset_id IS NOT OLD.dataset_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'entity identity is immutable'); END;
CREATE TRIGGER entities_current_version_valid BEFORE UPDATE OF current_version_id ON entities
WHEN NEW.current_version_id IS NULL OR NOT EXISTS(
    SELECT 1 FROM entity_versions WHERE id=NEW.current_version_id AND entity_id=NEW.id
)
BEGIN SELECT RAISE(ABORT,'entity current version must belong to the entity'); END;
CREATE TRIGGER entities_no_delete BEFORE DELETE ON entities
BEGIN SELECT RAISE(ABORT,'entities are stable identities'); END;

CREATE TRIGGER publisher_versions_valid_append BEFORE INSERT ON publisher_versions
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM publisher_versions WHERE publisher_id=NEW.publisher_id),1
     )
  OR (NEW.version=1 AND NEW.previous_version_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_version_id IS NOT (
         SELECT id FROM publisher_versions
         WHERE publisher_id=NEW.publisher_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'publisher versions must form a contiguous append-only chain'); END;
CREATE TRIGGER publisher_versions_no_update BEFORE UPDATE ON publisher_versions
BEGIN SELECT RAISE(ABORT,'publisher versions are immutable'); END;
CREATE TRIGGER publisher_versions_no_delete BEFORE DELETE ON publisher_versions
BEGIN SELECT RAISE(ABORT,'publisher versions are immutable'); END;
CREATE TRIGGER publishers_identity_immutable BEFORE UPDATE ON publishers
WHEN NEW.id IS NOT OLD.id OR NEW.dataset_id IS NOT OLD.dataset_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'publisher identity is immutable'); END;
CREATE TRIGGER publishers_current_version_valid BEFORE UPDATE OF current_version_id ON publishers
WHEN NEW.current_version_id IS NULL OR NOT EXISTS(
    SELECT 1 FROM publisher_versions WHERE id=NEW.current_version_id AND publisher_id=NEW.id
)
BEGIN SELECT RAISE(ABORT,'publisher current version must belong to the publisher'); END;
CREATE TRIGGER publishers_no_delete BEFORE DELETE ON publishers
BEGIN SELECT RAISE(ABORT,'publishers are stable identities'); END;

CREATE TRIGGER topic_versions_valid_append BEFORE INSERT ON topic_versions
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM topic_versions WHERE topic_id=NEW.topic_id),1
     )
  OR (NEW.version=1 AND NEW.previous_version_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_version_id IS NOT (
         SELECT id FROM topic_versions
         WHERE topic_id=NEW.topic_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'topic versions must form a contiguous append-only chain'); END;
CREATE TRIGGER topic_versions_no_update BEFORE UPDATE ON topic_versions
BEGIN SELECT RAISE(ABORT,'topic versions are immutable'); END;
CREATE TRIGGER topic_versions_no_delete BEFORE DELETE ON topic_versions
BEGIN SELECT RAISE(ABORT,'topic versions are immutable'); END;
CREATE TRIGGER topic_catalog_identity_immutable BEFORE UPDATE ON topic_catalog
WHEN NEW.id IS NOT OLD.id OR NEW.dataset_id IS NOT OLD.dataset_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'topic identity is immutable'); END;
CREATE TRIGGER topic_catalog_current_version_valid BEFORE UPDATE OF current_version_id ON topic_catalog
WHEN NEW.current_version_id IS NULL OR NOT EXISTS(
    SELECT 1 FROM topic_versions WHERE id=NEW.current_version_id AND topic_id=NEW.id
)
BEGIN SELECT RAISE(ABORT,'topic current version must belong to the topic'); END;
CREATE TRIGGER topic_catalog_no_delete BEFORE DELETE ON topic_catalog
BEGIN SELECT RAISE(ABORT,'topics are stable identities'); END;

CREATE TRIGGER entity_identifiers_no_update BEFORE UPDATE ON entity_identifiers
BEGIN SELECT RAISE(ABORT,'entity identifiers are immutable'); END;
CREATE TRIGGER entity_identifiers_no_delete BEFORE DELETE ON entity_identifiers
BEGIN SELECT RAISE(ABORT,'entity identifiers are immutable'); END;
CREATE TRIGGER entity_aliases_no_update BEFORE UPDATE ON entity_aliases
BEGIN SELECT RAISE(ABORT,'entity aliases are immutable'); END;
CREATE TRIGGER entity_aliases_no_delete BEFORE DELETE ON entity_aliases
BEGIN SELECT RAISE(ABORT,'entity aliases are immutable'); END;
CREATE TRIGGER entity_relations_no_update BEFORE UPDATE ON entity_relations
BEGIN SELECT RAISE(ABORT,'entity relations are immutable'); END;
CREATE TRIGGER entity_relations_no_delete BEFORE DELETE ON entity_relations
BEGIN SELECT RAISE(ABORT,'entity relations are immutable'); END;
CREATE TRIGGER security_listings_no_update BEFORE UPDATE ON security_listings
BEGIN SELECT RAISE(ABORT,'security listings are immutable'); END;
CREATE TRIGGER security_listings_no_delete BEFORE DELETE ON security_listings
BEGIN SELECT RAISE(ABORT,'security listings are immutable'); END;
CREATE TRIGGER entity_mentions_no_update BEFORE UPDATE ON entity_mentions
BEGIN SELECT RAISE(ABORT,'entity mentions are immutable'); END;
CREATE TRIGGER entity_mentions_no_delete BEFORE DELETE ON entity_mentions
BEGIN SELECT RAISE(ABORT,'entity mentions are immutable'); END;
CREATE TRIGGER legacy_company_entities_no_update BEFORE UPDATE ON legacy_company_entities
BEGIN SELECT RAISE(ABORT,'legacy company mappings are immutable'); END;
CREATE TRIGGER legacy_company_entities_no_delete BEFORE DELETE ON legacy_company_entities
BEGIN SELECT RAISE(ABORT,'legacy company mappings are immutable'); END;
CREATE TRIGGER publisher_legacy_keys_no_update BEFORE UPDATE ON publisher_legacy_keys
BEGIN SELECT RAISE(ABORT,'publisher keys are immutable'); END;
CREATE TRIGGER publisher_legacy_keys_no_delete BEFORE DELETE ON publisher_legacy_keys
BEGIN SELECT RAISE(ABORT,'publisher keys are immutable'); END;
CREATE TRIGGER publisher_names_no_update BEFORE UPDATE ON publisher_names
BEGIN SELECT RAISE(ABORT,'publisher names are immutable'); END;
CREATE TRIGGER publisher_names_no_delete BEFORE DELETE ON publisher_names
BEGIN SELECT RAISE(ABORT,'publisher names are immutable'); END;
CREATE TRIGGER publisher_domains_no_update BEFORE UPDATE ON publisher_domains
BEGIN SELECT RAISE(ABORT,'publisher domains are immutable'); END;
CREATE TRIGGER publisher_domains_no_delete BEFORE DELETE ON publisher_domains
BEGIN SELECT RAISE(ABORT,'publisher domains are immutable'); END;
CREATE TRIGGER document_attributions_no_update BEFORE UPDATE ON document_attributions
BEGIN SELECT RAISE(ABORT,'document attributions are immutable'); END;
CREATE TRIGGER document_attributions_no_delete BEFORE DELETE ON document_attributions
BEGIN SELECT RAISE(ABORT,'document attributions are immutable'); END;
CREATE TRIGGER topic_slug_aliases_no_update BEFORE UPDATE ON topic_slug_aliases
BEGIN SELECT RAISE(ABORT,'topic slug aliases are immutable'); END;
CREATE TRIGGER topic_slug_aliases_no_delete BEFORE DELETE ON topic_slug_aliases
BEGIN SELECT RAISE(ABORT,'topic slug aliases are immutable'); END;
CREATE TRIGGER document_topic_assignments_no_update BEFORE UPDATE ON document_topic_assignments
BEGIN SELECT RAISE(ABORT,'topic assignments are immutable'); END;
CREATE TRIGGER document_topic_assignments_no_delete BEFORE DELETE ON document_topic_assignments
BEGIN SELECT RAISE(ABORT,'topic assignments are immutable'); END;
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


def _document_version_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, DOCUMENT_VERSION_SCHEMA_SQL)


def _legacy_backfill_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, LEGACY_BACKFILL_SCHEMA_SQL)


def _identity_catalog_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, IDENTITY_CATALOG_SCHEMA_SQL)


SEC_IDENTITY_SCHEMA_SQL = """
CREATE TABLE sec_security_keys (
    cik TEXT NOT NULL,
    exchange TEXT NOT NULL,
    ticker TEXT NOT NULL,
    security_entity_id TEXT NOT NULL UNIQUE REFERENCES entities(id),
    first_evidence_id TEXT NOT NULL REFERENCES raw_records(id),
    available_at TEXT NOT NULL,
    PRIMARY KEY(cik,exchange,ticker)
);

CREATE TABLE sec_filings (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    cik TEXT NOT NULL,
    accession_number TEXT NOT NULL,
    issuer_entity_id TEXT NOT NULL REFERENCES entities(id),
    current_version_id TEXT UNIQUE REFERENCES sec_filing_versions(id),
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('observed','withdrawn')),
    UNIQUE(dataset_id,cik,accession_number)
);
CREATE INDEX idx_sec_filings_issuer ON sec_filings(issuer_entity_id,cik,accession_number);

CREATE TABLE sec_filing_versions (
    id TEXT PRIMARY KEY,
    filing_id TEXT NOT NULL REFERENCES sec_filings(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_version_id TEXT REFERENCES sec_filing_versions(id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
    form TEXT NOT NULL,
    base_form TEXT NOT NULL,
    is_amendment INTEGER NOT NULL CHECK(is_amendment IN (0,1)),
    amends_filing_id TEXT REFERENCES sec_filings(id),
    amendment_status TEXT NOT NULL CHECK(amendment_status IN (
        'not_amendment','linked','unresolved','ambiguous'
    )),
    primary_document TEXT NOT NULL,
    filing_date TEXT,
    report_period_end TEXT,
    accepted_at TEXT,
    items_json TEXT NOT NULL DEFAULT '[]',
    metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64),
    available_at TEXT NOT NULL,
    UNIQUE(filing_id,version),
    CHECK((version=1 AND previous_version_id IS NULL)
       OR (version>1 AND previous_version_id IS NOT NULL)),
    CHECK((is_amendment=0 AND amends_filing_id IS NULL AND amendment_status='not_amendment')
       OR (is_amendment=1 AND amendment_status IN ('linked','unresolved','ambiguous'))),
    CHECK(amendment_status!='linked' OR amends_filing_id IS NOT NULL)
);
CREATE INDEX idx_sec_filing_match
    ON sec_filing_versions(base_form,report_period_end,filing_date,is_amendment);

CREATE TRIGGER sec_security_keys_no_update BEFORE UPDATE ON sec_security_keys
BEGIN SELECT RAISE(ABORT,'SEC security keys are immutable'); END;
CREATE TRIGGER sec_security_keys_no_delete BEFORE DELETE ON sec_security_keys
BEGIN SELECT RAISE(ABORT,'SEC security keys are immutable'); END;
CREATE TRIGGER sec_filing_versions_valid_append BEFORE INSERT ON sec_filing_versions
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM sec_filing_versions WHERE filing_id=NEW.filing_id),1
     )
  OR (NEW.version=1 AND NEW.previous_version_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_version_id IS NOT (
         SELECT id FROM sec_filing_versions
         WHERE filing_id=NEW.filing_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'SEC filing versions must form a contiguous append-only chain'); END;
CREATE TRIGGER sec_filing_versions_no_update BEFORE UPDATE ON sec_filing_versions
BEGIN SELECT RAISE(ABORT,'SEC filing versions are immutable'); END;
CREATE TRIGGER sec_filing_versions_no_delete BEFORE DELETE ON sec_filing_versions
BEGIN SELECT RAISE(ABORT,'SEC filing versions are immutable'); END;
CREATE TRIGGER sec_filings_identity_immutable BEFORE UPDATE ON sec_filings
WHEN NEW.id IS NOT OLD.id OR NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.cik IS NOT OLD.cik OR NEW.accession_number IS NOT OLD.accession_number
  OR NEW.issuer_entity_id IS NOT OLD.issuer_entity_id OR NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN SELECT RAISE(ABORT,'SEC filing identity is immutable'); END;
CREATE TRIGGER sec_filings_current_version_valid BEFORE UPDATE OF current_version_id ON sec_filings
WHEN NEW.current_version_id IS NULL OR NOT EXISTS(
    SELECT 1 FROM sec_filing_versions
    WHERE id=NEW.current_version_id AND filing_id=NEW.id
)
BEGIN SELECT RAISE(ABORT,'SEC filing current version must belong to the filing'); END;
CREATE TRIGGER sec_filings_no_delete BEFORE DELETE ON sec_filings
BEGIN SELECT RAISE(ABORT,'SEC filings are stable identities'); END;
"""


def _sec_identity_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, SEC_IDENTITY_SCHEMA_SQL)


EVENT_FOUNDATION_SCHEMA_SQL = """
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    latest_report_at TEXT NOT NULL,
    last_fact_change_at TEXT,
    current_version_id TEXT UNIQUE REFERENCES event_versions(id),
    status TEXT NOT NULL CHECK(status IN (
        'candidate','active','resolved','retracted','merged','split'
    )),
    CHECK(latest_report_at>=first_seen_at),
    CHECK(last_fact_change_at IS NULL OR last_fact_change_at>=first_seen_at)
);
CREATE INDEX idx_events_recent ON events(latest_report_at,status);

CREATE TABLE event_versions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_version_id TEXT REFERENCES event_versions(id),
    schema_version TEXT NOT NULL,
    title TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'model_release','product_update','research_result','earnings','financing',
        'ma','personnel','buyback','regulation','litigation','macro_release',
        'monetary_policy','other'
    )),
    event_time_start TEXT,
    event_time_end TEXT,
    time_precision TEXT NOT NULL CHECK(time_precision IN (
        'unknown','year','month','day','minute','second','range'
    )),
    primary_entities_json TEXT NOT NULL DEFAULT '[]',
    object_entities_json TEXT NOT NULL DEFAULT '[]',
    facts_json TEXT NOT NULL DEFAULT '[]',
    topics_json TEXT NOT NULL DEFAULT '[]',
    knowledge_status TEXT NOT NULL CHECK(knowledge_status IN (
        'reported','corroborated','disputed','confirmed_by_primary',
        'retracted','unknown'
    )),
    version_sha256 TEXT NOT NULL CHECK(length(version_sha256)=64),
    available_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    method_version TEXT NOT NULL,
    UNIQUE(event_id,version),
    CHECK((version=1 AND previous_version_id IS NULL)
       OR (version>1 AND previous_version_id IS NOT NULL)),
    CHECK(event_time_end IS NULL OR event_time_start IS NOT NULL),
    CHECK(event_time_end IS NULL OR event_time_end>=event_time_start)
);

CREATE TABLE match_decisions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    decision_key TEXT NOT NULL UNIQUE,
    input_versions_json TEXT NOT NULL,
    candidate_event_versions_json TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    features_json TEXT NOT NULL,
    score REAL,
    decision TEXT NOT NULL CHECK(decision IN (
        'new_candidate','candidate_link','no_match','needs_review'
    )),
    reason TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK(review_status IN (
        'pending','accepted','rejected','superseded'
    )),
    available_at TEXT NOT NULL,
    CHECK(score IS NULL OR (score>=0 AND score<=1))
);

CREATE TABLE event_evidence (
    id TEXT PRIMARY KEY,
    event_version_id TEXT NOT NULL REFERENCES event_versions(id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    evidence_id TEXT NOT NULL REFERENCES raw_records(id),
    fact_id TEXT,
    role TEXT NOT NULL CHECK(role IN ('supports','contradicts','context')),
    available_at TEXT NOT NULL,
    UNIQUE(event_version_id,document_version_id,evidence_id,fact_id,role)
);
CREATE INDEX idx_event_evidence_document
    ON event_evidence(document_version_id,event_version_id);

CREATE TABLE document_event_links (
    id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    event_version_id TEXT NOT NULL REFERENCES event_versions(id),
    role TEXT NOT NULL CHECK(role IN ('primary','supporting','context','candidate')),
    decision_id TEXT NOT NULL REFERENCES match_decisions(id),
    available_at TEXT NOT NULL,
    supersedes_link_id TEXT REFERENCES document_event_links(id),
    UNIQUE(document_version_id,event_id,event_version_id,role)
);
CREATE INDEX idx_document_event_links_event
    ON document_event_links(event_id,event_version_id,document_version_id);

CREATE TABLE legacy_story_events (
    story_id TEXT PRIMARY KEY REFERENCES stories(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    canonical_story_id TEXT NOT NULL REFERENCES stories(id),
    mapping_status TEXT NOT NULL CHECK(mapping_status='candidate'),
    available_at TEXT NOT NULL
);
CREATE INDEX idx_legacy_story_events_event ON legacy_story_events(event_id,story_id);

CREATE TRIGGER event_versions_valid_append BEFORE INSERT ON event_versions
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM event_versions WHERE event_id=NEW.event_id),1
     )
  OR (NEW.version=1 AND NEW.previous_version_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_version_id IS NOT (
         SELECT id FROM event_versions
         WHERE event_id=NEW.event_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'event versions must form a contiguous append-only chain'); END;
CREATE TRIGGER event_versions_no_update BEFORE UPDATE ON event_versions
BEGIN SELECT RAISE(ABORT,'event versions are immutable'); END;
CREATE TRIGGER event_versions_no_delete BEFORE DELETE ON event_versions
BEGIN SELECT RAISE(ABORT,'event versions are immutable'); END;
CREATE TRIGGER events_identity_immutable BEFORE UPDATE ON events
WHEN NEW.id IS NOT OLD.id OR NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.first_seen_at IS NOT OLD.first_seen_at
BEGIN SELECT RAISE(ABORT,'event identity is immutable'); END;
CREATE TRIGGER events_current_version_valid BEFORE UPDATE OF current_version_id ON events
WHEN NEW.current_version_id IS NULL OR NOT EXISTS(
    SELECT 1 FROM event_versions WHERE id=NEW.current_version_id AND event_id=NEW.id
)
BEGIN SELECT RAISE(ABORT,'event current version must belong to the event'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT,'events are stable identities'); END;
CREATE TRIGGER match_decisions_no_update BEFORE UPDATE ON match_decisions
BEGIN SELECT RAISE(ABORT,'match decisions are immutable'); END;
CREATE TRIGGER match_decisions_no_delete BEFORE DELETE ON match_decisions
BEGIN SELECT RAISE(ABORT,'match decisions are immutable'); END;
CREATE TRIGGER event_evidence_no_update BEFORE UPDATE ON event_evidence
BEGIN SELECT RAISE(ABORT,'event evidence is immutable'); END;
CREATE TRIGGER event_evidence_no_delete BEFORE DELETE ON event_evidence
BEGIN SELECT RAISE(ABORT,'event evidence is immutable'); END;
CREATE TRIGGER document_event_links_no_update BEFORE UPDATE ON document_event_links
BEGIN SELECT RAISE(ABORT,'document event links are immutable'); END;
CREATE TRIGGER document_event_links_no_delete BEFORE DELETE ON document_event_links
BEGIN SELECT RAISE(ABORT,'document event links are immutable'); END;
CREATE TRIGGER legacy_story_events_no_update BEFORE UPDATE ON legacy_story_events
BEGIN SELECT RAISE(ABORT,'legacy story mappings are immutable'); END;
CREATE TRIGGER legacy_story_events_no_delete BEFORE DELETE ON legacy_story_events
BEGIN SELECT RAISE(ABORT,'legacy story mappings are immutable'); END;
"""


def _event_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_FOUNDATION_SCHEMA_SQL)


EVENT_RELATION_SCHEMA_SQL = """
CREATE TABLE event_relations (
    id TEXT PRIMARY KEY,
    from_event_id TEXT NOT NULL REFERENCES events(id),
    to_event_id TEXT NOT NULL REFERENCES events(id),
    relation TEXT NOT NULL CHECK(relation IN (
        'follows','implements','corrects','denies','related_to'
    )),
    evidence_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    available_at TEXT NOT NULL,
    supersedes_relation_id TEXT UNIQUE REFERENCES event_relations(id),
    publication_seq INTEGER NOT NULL UNIQUE REFERENCES change_log(seq),
    CHECK(from_event_id<>to_event_id)
);
CREATE INDEX idx_event_relations_from
    ON event_relations(from_event_id,to_event_id,available_at);
CREATE INDEX idx_event_relations_to
    ON event_relations(to_event_id,from_event_id,available_at);

CREATE TABLE event_merges (
    id TEXT PRIMARY KEY,
    absorbed_event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
    survivor_event_id TEXT NOT NULL REFERENCES events(id),
    evidence_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    available_at TEXT NOT NULL,
    publication_seq INTEGER NOT NULL UNIQUE REFERENCES change_log(seq),
    CHECK(absorbed_event_id<>survivor_event_id)
);
CREATE INDEX idx_event_merges_survivor
    ON event_merges(survivor_event_id,absorbed_event_id);

CREATE TRIGGER event_relations_supersedes_valid BEFORE INSERT ON event_relations
WHEN NEW.supersedes_relation_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM event_relations AS previous
    WHERE previous.id=NEW.supersedes_relation_id
      AND previous.from_event_id=NEW.from_event_id
      AND previous.to_event_id=NEW.to_event_id
) BEGIN
    SELECT RAISE(ABORT,'superseded event relation must have the same endpoints');
END;
CREATE TRIGGER event_relations_no_update BEFORE UPDATE ON event_relations
BEGIN SELECT RAISE(ABORT,'event relations are immutable'); END;
CREATE TRIGGER event_relations_no_delete BEFORE DELETE ON event_relations
BEGIN SELECT RAISE(ABORT,'event relations are immutable'); END;

CREATE TRIGGER event_merges_no_cycle BEFORE INSERT ON event_merges
WHEN EXISTS(
    WITH RECURSIVE successors(event_id) AS (
        SELECT NEW.survivor_event_id
        UNION
        SELECT merge.survivor_event_id
        FROM event_merges AS merge
        JOIN successors ON merge.absorbed_event_id=successors.event_id
    )
    SELECT 1 FROM successors WHERE event_id=NEW.absorbed_event_id
) BEGIN
    SELECT RAISE(ABORT,'event merge would create a cycle');
END;
CREATE TRIGGER event_merges_status_guard BEFORE INSERT ON event_merges
WHEN (SELECT status FROM events WHERE id=NEW.absorbed_event_id) IN ('merged','split','retracted')
  OR (SELECT status FROM events WHERE id=NEW.survivor_event_id) IN ('merged','split','retracted')
BEGIN SELECT RAISE(ABORT,'event merge endpoints are not mergeable'); END;
CREATE TRIGGER event_merges_no_update BEFORE UPDATE ON event_merges
BEGIN SELECT RAISE(ABORT,'event merges are immutable'); END;
CREATE TRIGGER event_merges_no_delete BEFORE DELETE ON event_merges
BEGIN SELECT RAISE(ABORT,'event merges are immutable'); END;
"""


def _event_relation_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_RELATION_SCHEMA_SQL)


EVENT_TERMINAL_SCHEMA_SQL = """
ALTER TABLE event_merges ADD COLUMN previous_status TEXT
    CHECK(previous_status IS NULL OR previous_status IN ('candidate','active','resolved'));

CREATE TABLE event_splits (
    id TEXT PRIMARY KEY,
    original_event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
    previous_status TEXT NOT NULL CHECK(previous_status IN ('candidate','active','resolved')),
    evidence_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    available_at TEXT NOT NULL,
    publication_seq INTEGER NOT NULL UNIQUE REFERENCES change_log(seq)
);
CREATE TABLE event_split_replacements (
    split_id TEXT NOT NULL REFERENCES event_splits(id),
    replacement_event_id TEXT NOT NULL REFERENCES events(id),
    replacement_event_version_id TEXT NOT NULL REFERENCES event_versions(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    PRIMARY KEY(split_id,replacement_event_id),
    UNIQUE(split_id,ordinal)
);
CREATE TABLE event_split_assignments (
    split_id TEXT NOT NULL REFERENCES event_splits(id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    evidence_id TEXT NOT NULL REFERENCES raw_records(id),
    replacement_event_id TEXT NOT NULL REFERENCES events(id),
    replacement_event_version_id TEXT NOT NULL REFERENCES event_versions(id),
    available_at TEXT NOT NULL,
    PRIMARY KEY(split_id,document_version_id,evidence_id,replacement_event_id)
);
CREATE INDEX idx_event_split_assignments_replacement
    ON event_split_assignments(replacement_event_id,document_version_id);

CREATE TABLE event_retractions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
    previous_status TEXT NOT NULL CHECK(previous_status IN ('candidate','active','resolved')),
    retracted_event_version_id TEXT NOT NULL UNIQUE REFERENCES event_versions(id),
    evidence_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    available_at TEXT NOT NULL,
    publication_seq INTEGER NOT NULL UNIQUE REFERENCES change_log(seq)
);

CREATE TRIGGER event_splits_no_update BEFORE UPDATE ON event_splits
BEGIN SELECT RAISE(ABORT,'event splits are immutable'); END;
CREATE TRIGGER event_splits_no_delete BEFORE DELETE ON event_splits
BEGIN SELECT RAISE(ABORT,'event splits are immutable'); END;
CREATE TRIGGER event_split_replacements_no_update BEFORE UPDATE ON event_split_replacements
BEGIN SELECT RAISE(ABORT,'event split replacements are immutable'); END;
CREATE TRIGGER event_split_replacements_no_delete BEFORE DELETE ON event_split_replacements
BEGIN SELECT RAISE(ABORT,'event split replacements are immutable'); END;
CREATE TRIGGER event_split_assignments_no_update BEFORE UPDATE ON event_split_assignments
BEGIN SELECT RAISE(ABORT,'event split assignments are immutable'); END;
CREATE TRIGGER event_split_assignments_no_delete BEFORE DELETE ON event_split_assignments
BEGIN SELECT RAISE(ABORT,'event split assignments are immutable'); END;
CREATE TRIGGER event_retractions_no_update BEFORE UPDATE ON event_retractions
BEGIN SELECT RAISE(ABORT,'event retractions are immutable'); END;
CREATE TRIGGER event_retractions_no_delete BEFORE DELETE ON event_retractions
BEGIN SELECT RAISE(ABORT,'event retractions are immutable'); END;
"""


def _event_terminal_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_TERMINAL_SCHEMA_SQL)


EVENT_REVISION_SCHEMA_SQL = """
CREATE TABLE event_revisions (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id),
    previous_version_id TEXT NOT NULL REFERENCES event_versions(id),
    revised_version_id TEXT NOT NULL UNIQUE REFERENCES event_versions(id),
    revision_kind TEXT NOT NULL CHECK(revision_kind IN (
        'fact_update','correction','knowledge_update'
    )),
    changed_fields_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    available_at TEXT NOT NULL,
    publication_seq INTEGER NOT NULL UNIQUE REFERENCES change_log(seq)
);
CREATE INDEX idx_event_revisions_event ON event_revisions(event_id,publication_seq);
CREATE TRIGGER event_revisions_no_update BEFORE UPDATE ON event_revisions
BEGIN SELECT RAISE(ABORT,'event revisions are immutable'); END;
CREATE TRIGGER event_revisions_no_delete BEFORE DELETE ON event_revisions
BEGIN SELECT RAISE(ABORT,'event revisions are immutable'); END;
"""


def _event_revision_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_REVISION_SCHEMA_SQL)


ANALYSIS_INPUT_SCHEMA_SQL = """
CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    subject_type TEXT NOT NULL CHECK(subject_type IN ('document','event')),
    subject_version_id TEXT NOT NULL,
    task_type TEXT NOT NULL CHECK(task_type IN (
        'language','translation','relevance','entity_linking','summarization','importance',
        'event_extraction','event_linking','tone','impact','macro_mapping','report'
    )),
    output_schema_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    prompt_template_id TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
    rendered_input_ref TEXT NOT NULL,
    rendered_input_sha256 TEXT NOT NULL CHECK(length(rendered_input_sha256)=64),
    pipeline_version TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    input_manifest_json TEXT NOT NULL,
    input_manifest_sha256 TEXT NOT NULL CHECK(length(input_manifest_sha256)=64),
    prepared_at TEXT NOT NULL
);
CREATE INDEX idx_analysis_runs_subject
    ON analysis_runs(subject_type,subject_version_id,task_type,prepared_at);

CREATE TABLE analysis_inputs (
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    document_version_id TEXT REFERENCES document_versions(id),
    event_version_id TEXT REFERENCES event_versions(id),
    evidence_id TEXT REFERENCES raw_records(id),
    role TEXT NOT NULL CHECK(role IN ('primary','supporting','context','contradicting')),
    PRIMARY KEY(run_id,ordinal),
    CHECK((document_version_id IS NOT NULL) != (event_version_id IS NOT NULL)),
    CHECK(evidence_id IS NULL OR document_version_id IS NOT NULL)
);
CREATE INDEX idx_analysis_inputs_document ON analysis_inputs(document_version_id,run_id);
CREATE INDEX idx_analysis_inputs_event ON analysis_inputs(event_version_id,run_id);

CREATE TRIGGER analysis_runs_no_update BEFORE UPDATE ON analysis_runs
BEGIN SELECT RAISE(ABORT,'analysis runs are immutable'); END;
CREATE TRIGGER analysis_runs_no_delete BEFORE DELETE ON analysis_runs
BEGIN SELECT RAISE(ABORT,'analysis runs are immutable'); END;
CREATE TRIGGER analysis_inputs_no_update BEFORE UPDATE ON analysis_inputs
BEGIN SELECT RAISE(ABORT,'analysis inputs are immutable'); END;
CREATE TRIGGER analysis_inputs_no_delete BEFORE DELETE ON analysis_inputs
BEGIN SELECT RAISE(ABORT,'analysis inputs are immutable'); END;
"""


def _analysis_input_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, ANALYSIS_INPUT_SCHEMA_SQL)


ANALYSIS_ATTEMPT_SCHEMA_SQL = """
CREATE TABLE analysis_budget_policies (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    daily_limit_microusd INTEGER NOT NULL CHECK(daily_limit_microusd>=0),
    per_attempt_limit_microusd INTEGER NOT NULL CHECK(per_attempt_limit_microusd>=0),
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_policy_id TEXT UNIQUE REFERENCES analysis_budget_policies(id)
);
CREATE INDEX idx_analysis_budget_provider ON analysis_budget_policies(provider,effective_from);

CREATE TABLE analysis_attempt_authorizations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 4),
    attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ('primary','retry','repair')),
    provider TEXT NOT NULL,
    budget_policy_id TEXT NOT NULL REFERENCES analysis_budget_policies(id),
    budget_day TEXT NOT NULL,
    reserved_cost_microusd INTEGER NOT NULL CHECK(reserved_cost_microusd>=0),
    decision TEXT NOT NULL CHECK(decision IN ('allowed','blocked')),
    reason TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    UNIQUE(run_id,attempt_number)
);

CREATE TABLE analysis_attempts (
    id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL UNIQUE REFERENCES analysis_attempt_authorizations(id),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    attempt_number INTEGER NOT NULL,
    attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ('primary','retry','repair')),
    resolved_model TEXT,
    provider_request_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'succeeded','failed','refused','invalid_output','blocked'
    )),
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens>=0),
    usage_status TEXT NOT NULL CHECK(usage_status IN ('reported','estimated','unknown')),
    cost_microusd INTEGER CHECK(cost_microusd IS NULL OR cost_microusd>=0),
    pricing_version TEXT,
    raw_response_ref TEXT,
    raw_response_sha256 TEXT CHECK(raw_response_sha256 IS NULL OR length(raw_response_sha256)=64),
    error_type TEXT,
    error_detail TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(run_id,attempt_number),
    CHECK(finished_at>=started_at)
);
CREATE INDEX idx_analysis_attempts_run ON analysis_attempts(run_id,attempt_number);

CREATE TRIGGER analysis_budget_policies_no_update BEFORE UPDATE ON analysis_budget_policies
BEGIN SELECT RAISE(ABORT,'analysis budget policies are immutable'); END;
CREATE TRIGGER analysis_budget_policies_no_delete BEFORE DELETE ON analysis_budget_policies
BEGIN SELECT RAISE(ABORT,'analysis budget policies are immutable'); END;
CREATE TRIGGER analysis_attempt_authorizations_no_update BEFORE UPDATE ON analysis_attempt_authorizations
BEGIN SELECT RAISE(ABORT,'analysis attempt authorizations are immutable'); END;
CREATE TRIGGER analysis_attempt_authorizations_no_delete BEFORE DELETE ON analysis_attempt_authorizations
BEGIN SELECT RAISE(ABORT,'analysis attempt authorizations are immutable'); END;
CREATE TRIGGER analysis_attempts_no_update BEFORE UPDATE ON analysis_attempts
BEGIN SELECT RAISE(ABORT,'analysis attempts are immutable'); END;
CREATE TRIGGER analysis_attempts_no_delete BEFORE DELETE ON analysis_attempts
BEGIN SELECT RAISE(ABORT,'analysis attempts are immutable'); END;
"""


def _analysis_attempt_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, ANALYSIS_ATTEMPT_SCHEMA_SQL)


ANALYSIS_RESULT_SCHEMA_SQL = """
CREATE TABLE analysis_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES analysis_runs(id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES analysis_attempts(id),
    schema_version TEXT NOT NULL,
    raw_output_ref TEXT,
    raw_output_sha256 TEXT CHECK(raw_output_sha256 IS NULL OR length(raw_output_sha256)=64),
    validated_output_json TEXT NOT NULL,
    validation_report_json TEXT NOT NULL,
    result_status TEXT NOT NULL CHECK(result_status IN (
        'valid','needs_review','insufficient_evidence','refused'
    )),
    created_at TEXT NOT NULL,
    available_at TEXT NOT NULL
);

CREATE TABLE analysis_publication_versions (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('document','event')),
    subject_version_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    result_id TEXT NOT NULL REFERENCES analysis_results(id),
    version INTEGER NOT NULL CHECK(version>0),
    review_status TEXT NOT NULL CHECK(review_status IN (
        'unreviewed','accepted','rejected','corrected'
    )),
    evidence_status TEXT NOT NULL CHECK(evidence_status IN (
        'supported','partial','insufficient','refused'
    )),
    available_at TEXT NOT NULL,
    supersedes_id TEXT UNIQUE REFERENCES analysis_publication_versions(id),
    publication_seq INTEGER NOT NULL UNIQUE REFERENCES change_log(seq),
    UNIQUE(subject_type,subject_version_id,task_type,version)
);
CREATE INDEX idx_analysis_publication_subject
    ON analysis_publication_versions(subject_type,subject_version_id,task_type,version);

CREATE TABLE analysis_publications (
    subject_type TEXT NOT NULL,
    subject_version_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    current_publication_id TEXT NOT NULL UNIQUE REFERENCES analysis_publication_versions(id),
    PRIMARY KEY(subject_type,subject_version_id,task_type)
);

CREATE TRIGGER analysis_results_no_update BEFORE UPDATE ON analysis_results
BEGIN SELECT RAISE(ABORT,'analysis results are immutable'); END;
CREATE TRIGGER analysis_results_no_delete BEFORE DELETE ON analysis_results
BEGIN SELECT RAISE(ABORT,'analysis results are immutable'); END;
CREATE TRIGGER analysis_publication_versions_no_update BEFORE UPDATE ON analysis_publication_versions
BEGIN SELECT RAISE(ABORT,'analysis publication versions are immutable'); END;
CREATE TRIGGER analysis_publication_versions_no_delete BEFORE DELETE ON analysis_publication_versions
BEGIN SELECT RAISE(ABORT,'analysis publication versions are immutable'); END;
CREATE TRIGGER analysis_publications_no_delete BEFORE DELETE ON analysis_publications
BEGIN SELECT RAISE(ABORT,'analysis publication pointers cannot be deleted'); END;
"""


def _analysis_result_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, ANALYSIS_RESULT_SCHEMA_SQL)


CURATION_SEARCH_SCHEMA_SQL = """
CREATE TABLE curation_search_documents (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    document_version_id TEXT,
    translation_publication_id TEXT,
    summary_publication_id TEXT,
    index_schema_version TEXT NOT NULL CHECK(index_schema_version='curation-search-v1'),
    title_original TEXT NOT NULL,
    title_display TEXT NOT NULL,
    summary_display TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE curation_search_fts USING fts5(
    title_original, title_display, summary_display,
    content='curation_search_documents', content_rowid='item_id', tokenize='trigram'
);
CREATE TRIGGER curation_search_ai AFTER INSERT ON curation_search_documents BEGIN
    INSERT INTO curation_search_fts(rowid,title_original,title_display,summary_display)
    VALUES(new.item_id,new.title_original,new.title_display,new.summary_display);
END;
CREATE TRIGGER curation_search_ad AFTER DELETE ON curation_search_documents BEGIN
    INSERT INTO curation_search_fts(curation_search_fts,rowid,title_original,title_display,summary_display)
    VALUES('delete',old.item_id,old.title_original,old.title_display,old.summary_display);
END;
CREATE TRIGGER curation_search_au AFTER UPDATE ON curation_search_documents BEGIN
    INSERT INTO curation_search_fts(curation_search_fts,rowid,title_original,title_display,summary_display)
    VALUES('delete',old.item_id,old.title_original,old.title_display,old.summary_display);
    INSERT INTO curation_search_fts(rowid,title_original,title_display,summary_display)
    VALUES(new.item_id,new.title_original,new.title_display,new.summary_display);
END;
CREATE TABLE curation_search_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    generation INTEGER NOT NULL CHECK(generation>=0),
    status TEXT NOT NULL CHECK(status IN ('empty','building','ready')),
    last_item_id INTEGER NOT NULL CHECK(last_item_id>=0),
    indexed_count INTEGER NOT NULL CHECK(indexed_count>=0),
    updated_at TEXT NOT NULL
);
CREATE TABLE curation_search_dirty (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL
);
CREATE TRIGGER curation_search_item_ai AFTER INSERT ON items BEGIN
    INSERT OR REPLACE INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.id,'item_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER curation_search_item_au AFTER UPDATE OF title,title_zh,summary,raw_summary,tmt ON items BEGIN
    INSERT OR REPLACE INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.id,'item_update',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER curation_search_document_ai AFTER INSERT ON documents BEGIN
    INSERT OR REPLACE INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.legacy_item_id,'document_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER curation_search_document_au AFTER UPDATE OF current_version_id,status ON documents BEGIN
    INSERT OR REPLACE INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.legacy_item_id,'document_update',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER curation_search_publication_ai AFTER INSERT ON analysis_publications
WHEN new.subject_type='document' AND new.task_type IN ('translation','summarization','relevance') BEGIN
    INSERT OR REPLACE INTO curation_search_dirty(item_id,reason,queued_at)
    SELECT d.legacy_item_id,'publication_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM documents d WHERE d.current_version_id=new.subject_version_id;
END;
CREATE TRIGGER curation_search_publication_au AFTER UPDATE OF current_publication_id ON analysis_publications
WHEN new.subject_type='document' AND new.task_type IN ('translation','summarization','relevance') BEGIN
    INSERT OR REPLACE INTO curation_search_dirty(item_id,reason,queued_at)
    SELECT d.legacy_item_id,'publication_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM documents d WHERE d.current_version_id=new.subject_version_id;
END;
"""


def _curation_search_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, CURATION_SEARCH_SCHEMA_SQL)
    db.execute("""INSERT INTO curation_search_state(
        singleton,generation,status,last_item_id,indexed_count,updated_at)
        VALUES(1,0,'empty',0,0,?)""", (_utc_now(),))


CURATION_STORY_METRICS_SCHEMA_SQL = """
CREATE TABLE curation_story_metrics (
    story_id TEXT PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    metric_schema_version TEXT NOT NULL CHECK(metric_schema_version='curation-story-metrics-v1'),
    visible_item_count INTEGER NOT NULL CHECK(visible_item_count>=0),
    publisher_count INTEGER NOT NULL CHECK(publisher_count>=0),
    heat REAL NOT NULL CHECK(heat>=0),
    representative_item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    title_display TEXT NOT NULL,
    url_display TEXT NOT NULL,
    company_slugs TEXT NOT NULL DEFAULT '[]',
    last_visible_at TEXT,
    computed_at TEXT NOT NULL
);
CREATE TABLE curation_story_metrics_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    generation INTEGER NOT NULL CHECK(generation>=0),
    status TEXT NOT NULL CHECK(status IN ('empty','building','ready')),
    last_story_id TEXT NOT NULL DEFAULT '',
    indexed_count INTEGER NOT NULL CHECK(indexed_count>=0),
    updated_at TEXT NOT NULL
);
CREATE TABLE curation_story_metrics_dirty (
    story_id TEXT PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL
);
CREATE TRIGGER curation_story_insert AFTER INSERT ON stories BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    VALUES(new.id,'story_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_update
AFTER UPDATE OF anchor_item_id,title,url,channel,first_at,last_at,redirect_to ON stories BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    VALUES(new.id,'story_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_member_insert AFTER INSERT ON story_items BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    VALUES(new.story_id,'member_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_member_delete AFTER DELETE ON story_items BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    SELECT old.story_id,'member_delete',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE EXISTS(SELECT 1 FROM stories WHERE id=old.story_id) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_member_move AFTER UPDATE OF story_id ON story_items BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    VALUES(old.story_id,'member_move',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    VALUES(new.story_id,'member_move',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_item_update
AFTER UPDATE OF title,title_zh,score,tmt,official,published_at,url,extra,companies,event_type ON items BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    SELECT si.story_id,'item_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM story_items si WHERE si.item_id=new.id ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_document_insert AFTER INSERT ON documents BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    SELECT si.story_id,'document_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM story_items si WHERE si.item_id=new.legacy_item_id ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_document_update
AFTER UPDATE OF current_version_id,status ON documents BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    SELECT si.story_id,'document_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM story_items si WHERE si.item_id=new.legacy_item_id ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_publication_insert AFTER INSERT ON analysis_publications
WHEN new.subject_type='document' AND new.task_type IN ('translation','relevance','importance') BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    SELECT si.story_id,'publication_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM documents d JOIN story_items si ON si.item_id=d.legacy_item_id
    WHERE d.current_version_id=new.subject_version_id ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
CREATE TRIGGER curation_story_publication_update
AFTER UPDATE OF current_publication_id ON analysis_publications
WHEN new.subject_type='document' AND new.task_type IN ('translation','relevance','importance') BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    SELECT si.story_id,'publication_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM documents d JOIN story_items si ON si.item_id=d.legacy_item_id
    WHERE d.current_version_id=new.subject_version_id ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
"""


def _curation_story_metrics_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, CURATION_STORY_METRICS_SCHEMA_SQL)
    db.execute("""INSERT INTO curation_story_metrics_state(
        singleton,generation,status,last_story_id,indexed_count,updated_at)
        VALUES(1,0,'empty','',0,?)""", (_utc_now(),))


REPORT_VERSION_SCHEMA_SQL = """
CREATE TABLE report_input_snapshots (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    report_key TEXT NOT NULL,
    report_type TEXT NOT NULL CHECK(report_type IN ('calendar_daily','us_market_daily','other')),
    report_date TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    window_basis TEXT NOT NULL CHECK(window_basis IN ('calendar_day','market_session','custom')),
    timezone TEXT NOT NULL,
    calendar_id TEXT,
    calendar_version TEXT,
    as_of TEXT NOT NULL,
    knowledge_checkpoint_id TEXT REFERENCES knowledge_checkpoints(id),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    input_count INTEGER NOT NULL CHECK(input_count>=0),
    created_at TEXT NOT NULL,
    CHECK(window_start<window_end),
    CHECK((calendar_id IS NULL)=(calendar_version IS NULL)),
    CHECK(window_basis!='market_session' OR calendar_id IS NOT NULL),
    UNIQUE(dataset_id,report_key,manifest_sha256)
);
CREATE INDEX idx_report_inputs_key ON report_input_snapshots(dataset_id,report_key,created_at);

CREATE TABLE report_input_members (
    snapshot_id TEXT NOT NULL REFERENCES report_input_snapshots(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    legacy_item_id INTEGER REFERENCES items(id),
    document_version_id TEXT REFERENCES document_versions(id),
    material_sha256 TEXT NOT NULL CHECK(length(material_sha256)=64),
    PRIMARY KEY(snapshot_id,ordinal),
    CHECK(legacy_item_id IS NOT NULL OR document_version_id IS NOT NULL)
);
CREATE INDEX idx_report_input_members_document ON report_input_members(document_version_id);

CREATE TABLE report_versions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    report_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version>=1),
    input_snapshot_id TEXT NOT NULL REFERENCES report_input_snapshots(id),
    mode TEXT NOT NULL CHECK(mode IN ('llm','structured_fallback','legacy_unknown')),
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    citations_json TEXT NOT NULL CHECK(json_valid(citations_json)),
    coverage_json TEXT NOT NULL CHECK(json_valid(coverage_json)),
    provider TEXT,
    model TEXT,
    prompt_template_id TEXT,
    prompt_sha256 TEXT CHECK(prompt_sha256 IS NULL OR length(prompt_sha256)=64),
    generated_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    supersedes_version_id TEXT REFERENCES report_versions(id),
    UNIQUE(dataset_id,report_key,version),
    CHECK(mode!='llm' OR (provider IS NOT NULL AND model IS NOT NULL
                            AND prompt_template_id IS NOT NULL AND prompt_sha256 IS NOT NULL))
);
CREATE INDEX idx_report_versions_key ON report_versions(dataset_id,report_key,version DESC);

CREATE TRIGGER report_versions_valid_append BEFORE INSERT ON report_versions
WHEN NOT EXISTS(SELECT 1 FROM report_input_snapshots AS s
                WHERE s.id=NEW.input_snapshot_id AND s.dataset_id=NEW.dataset_id
                  AND s.report_key=NEW.report_key
                  AND s.input_count=(SELECT COUNT(*) FROM report_input_members AS m
                                     WHERE m.snapshot_id=s.id))
  OR (NEW.version=1 AND NEW.supersedes_version_id IS NOT NULL)
  OR (NEW.version>1 AND NOT EXISTS(
        SELECT 1 FROM report_versions AS prior
        WHERE prior.id=NEW.supersedes_version_id AND prior.dataset_id=NEW.dataset_id
          AND prior.report_key=NEW.report_key AND prior.version=NEW.version-1))
BEGIN SELECT RAISE(ABORT,'report version must follow a complete matching input snapshot and prior version'); END;

CREATE TABLE report_publications (
    dataset_id TEXT NOT NULL,
    report_key TEXT NOT NULL,
    current_version_id TEXT NOT NULL REFERENCES report_versions(id),
    published_at TEXT NOT NULL,
    PRIMARY KEY(dataset_id,report_key)
);

CREATE TRIGGER report_input_snapshots_no_update BEFORE UPDATE ON report_input_snapshots
BEGIN SELECT RAISE(ABORT,'report input snapshots are immutable'); END;
CREATE TRIGGER report_input_snapshots_no_delete BEFORE DELETE ON report_input_snapshots
BEGIN SELECT RAISE(ABORT,'report input snapshots are immutable'); END;
CREATE TRIGGER report_input_members_no_update BEFORE UPDATE ON report_input_members
BEGIN SELECT RAISE(ABORT,'report input members are immutable'); END;
CREATE TRIGGER report_input_members_no_delete BEFORE DELETE ON report_input_members
BEGIN SELECT RAISE(ABORT,'report input members are immutable'); END;
CREATE TRIGGER report_input_members_no_late_insert BEFORE INSERT ON report_input_members
WHEN EXISTS(SELECT 1 FROM report_versions WHERE input_snapshot_id=NEW.snapshot_id)
BEGIN SELECT RAISE(ABORT,'published report input is closed'); END;
CREATE TRIGGER report_versions_no_update BEFORE UPDATE ON report_versions
BEGIN SELECT RAISE(ABORT,'report versions are immutable'); END;
CREATE TRIGGER report_versions_no_delete BEFORE DELETE ON report_versions
BEGIN SELECT RAISE(ABORT,'report versions are immutable'); END;
CREATE TRIGGER report_publications_match_insert BEFORE INSERT ON report_publications
WHEN NOT EXISTS(SELECT 1 FROM report_versions AS v WHERE v.id=NEW.current_version_id
                AND v.dataset_id=NEW.dataset_id AND v.report_key=NEW.report_key)
BEGIN SELECT RAISE(ABORT,'report publication identity mismatch'); END;
CREATE TRIGGER report_publications_match_update BEFORE UPDATE ON report_publications
WHEN NOT EXISTS(SELECT 1 FROM report_versions AS v WHERE v.id=NEW.current_version_id
                AND v.dataset_id=NEW.dataset_id AND v.report_key=NEW.report_key)
BEGIN SELECT RAISE(ABORT,'report publication identity mismatch'); END;
"""


def _report_version_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, REPORT_VERSION_SCHEMA_SQL)


REPORT_GENERATION_SCHEMA_SQL = """
CREATE TABLE report_generation_runs (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    input_snapshot_id TEXT NOT NULL REFERENCES report_input_snapshots(id),
    provider TEXT NOT NULL CHECK(length(trim(provider))>0),
    requested_model TEXT NOT NULL CHECK(length(trim(requested_model))>0),
    prompt_template_id TEXT NOT NULL CHECK(length(trim(prompt_template_id))>0),
    prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
    rendered_prompt_ref TEXT NOT NULL CHECK(length(trim(rendered_prompt_ref))>0),
    rendered_prompt_sha256 TEXT NOT NULL CHECK(length(rendered_prompt_sha256)=64),
    parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json)),
    prepared_at TEXT NOT NULL,
    UNIQUE(input_snapshot_id,provider,requested_model,prompt_template_id,
           prompt_sha256,rendered_prompt_sha256,parameters_json)
);
CREATE INDEX idx_report_generation_runs_input ON report_generation_runs(input_snapshot_id,prepared_at);
CREATE TRIGGER report_generation_runs_match BEFORE INSERT ON report_generation_runs
WHEN NOT EXISTS(SELECT 1 FROM report_input_snapshots s
                WHERE s.id=NEW.input_snapshot_id AND s.dataset_id=NEW.dataset_id)
BEGIN SELECT RAISE(ABORT,'report generation input identity mismatch'); END;
CREATE TRIGGER report_generation_runs_no_update BEFORE UPDATE ON report_generation_runs
BEGIN SELECT RAISE(ABORT,'report generation runs are immutable'); END;
CREATE TRIGGER report_generation_runs_no_delete BEFORE DELETE ON report_generation_runs
BEGIN SELECT RAISE(ABORT,'report generation runs are immutable'); END;

CREATE TABLE report_generation_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES report_generation_runs(id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
    status TEXT NOT NULL CHECK(status IN ('valid_draft','invalid_draft','failed','refused')),
    resolved_model TEXT NOT NULL CHECK(length(trim(resolved_model))>0),
    provider_request_id TEXT,
    raw_response_ref TEXT,
    raw_response_sha256 TEXT CHECK(raw_response_sha256 IS NULL OR length(raw_response_sha256)=64),
    validated_draft_json TEXT CHECK(validated_draft_json IS NULL OR json_valid(validated_draft_json)),
    validation_report_json TEXT NOT NULL CHECK(json_valid(validation_report_json)),
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),
    output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens>=0),
    cost_microusd INTEGER CHECK(cost_microusd IS NULL OR cost_microusd>=0),
    usage_status TEXT NOT NULL CHECK(usage_status IN ('reported','estimated','unknown')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(run_id,attempt_number),
    CHECK((raw_response_ref IS NULL)=(raw_response_sha256 IS NULL)),
    CHECK(status NOT IN ('valid_draft','invalid_draft') OR raw_response_ref IS NOT NULL),
    CHECK((status='valid_draft')=(validated_draft_json IS NOT NULL)),
    CHECK(usage_status!='unknown' OR (input_tokens IS NULL AND output_tokens IS NULL
                                     AND cost_microusd IS NULL))
);
CREATE INDEX idx_report_generation_attempts_run ON report_generation_attempts(run_id,attempt_number);
CREATE TRIGGER report_generation_attempts_no_update BEFORE UPDATE ON report_generation_attempts
BEGIN SELECT RAISE(ABORT,'report generation attempts are immutable'); END;
CREATE TRIGGER report_generation_attempts_no_delete BEFORE DELETE ON report_generation_attempts
BEGIN SELECT RAISE(ABORT,'report generation attempts are immutable'); END;

ALTER TABLE report_versions ADD COLUMN generation_attempt_id TEXT REFERENCES report_generation_attempts(id);
CREATE TRIGGER report_versions_generation_provenance BEFORE INSERT ON report_versions
WHEN (NEW.mode='llm' AND NOT EXISTS(
    SELECT 1 FROM report_generation_attempts a
    JOIN report_generation_runs r ON r.id=a.run_id
    WHERE a.id=NEW.generation_attempt_id AND a.status='valid_draft'
      AND r.input_snapshot_id=NEW.input_snapshot_id AND r.dataset_id=NEW.dataset_id
      AND r.provider=NEW.provider AND r.requested_model=NEW.model
      AND r.prompt_template_id=NEW.prompt_template_id AND r.prompt_sha256=NEW.prompt_sha256
)) OR (NEW.mode!='llm' AND NEW.generation_attempt_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'report version generation provenance mismatch'); END;
"""


def _report_generation_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, REPORT_GENERATION_SCHEMA_SQL)


REPORT_REVIEW_SCHEMA_SQL = """
CREATE TABLE report_generation_reviews (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES report_generation_attempts(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    review_type TEXT NOT NULL CHECK(review_type='manual_source_check'),
    reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id))>0),
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    draft_sha256 TEXT NOT NULL CHECK(length(draft_sha256)=64),
    reviewed_at TEXT NOT NULL
);
CREATE INDEX idx_report_generation_reviews_attempt ON report_generation_reviews(attempt_id);
CREATE TRIGGER report_generation_reviews_valid_approval BEFORE INSERT ON report_generation_reviews
WHEN NEW.decision='approved' AND NOT EXISTS(
    SELECT 1 FROM report_generation_attempts a
    WHERE a.id=NEW.attempt_id AND a.status='valid_draft'
      AND a.validated_draft_json IS NOT NULL
)
BEGIN SELECT RAISE(ABORT,'only a valid report draft can be approved'); END;
CREATE TRIGGER report_generation_reviews_no_update BEFORE UPDATE ON report_generation_reviews
BEGIN SELECT RAISE(ABORT,'report generation reviews are immutable'); END;
CREATE TRIGGER report_generation_reviews_no_delete BEFORE DELETE ON report_generation_reviews
BEGIN SELECT RAISE(ABORT,'report generation reviews are immutable'); END;

DROP TRIGGER report_versions_generation_provenance;
CREATE TRIGGER report_versions_generation_provenance BEFORE INSERT ON report_versions
WHEN (NEW.mode='llm' AND NOT EXISTS(
    SELECT 1 FROM report_generation_attempts a
    JOIN report_generation_runs r ON r.id=a.run_id
    JOIN report_generation_reviews review ON review.attempt_id=a.id
    WHERE a.id=NEW.generation_attempt_id AND a.status='valid_draft'
      AND review.decision='approved' AND review.review_type='manual_source_check'
      AND r.input_snapshot_id=NEW.input_snapshot_id AND r.dataset_id=NEW.dataset_id
      AND r.provider=NEW.provider AND r.requested_model=NEW.model
      AND r.prompt_template_id=NEW.prompt_template_id AND r.prompt_sha256=NEW.prompt_sha256
)) OR (NEW.mode!='llm' AND NEW.generation_attempt_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'report version generation or review provenance mismatch'); END;
"""


def _report_review_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, REPORT_REVIEW_SCHEMA_SQL)


API_AUTH_SCHEMA_SQL = """
CREATE TABLE api_consumers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK(length(trim(name))>0),
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    authz_version INTEGER NOT NULL DEFAULT 1 CHECK(authz_version>=1),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK((status='active' AND revoked_at IS NULL)
       OR (status='revoked' AND revoked_at IS NOT NULL))
);
CREATE TABLE api_keys (
    key_id TEXT PRIMARY KEY,
    consumer_id TEXT NOT NULL REFERENCES api_consumers(id),
    token_sha256 TEXT NOT NULL UNIQUE CHECK(length(token_sha256)=64),
    scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json)),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX idx_api_keys_consumer ON api_keys(consumer_id,revoked_at);
"""


def _api_auth_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, API_AUTH_SCHEMA_SQL)


API_KEY_AUDIT_SCHEMA_SQL = """
CREATE TABLE api_key_audit (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL CHECK(action IN ('consumer_created','key_issued','key_revoked','consumer_revoked')),
    actor TEXT NOT NULL CHECK(length(trim(actor)) BETWEEN 1 AND 120),
    consumer_id TEXT NOT NULL REFERENCES api_consumers(id),
    key_id TEXT REFERENCES api_keys(key_id),
    details_json TEXT NOT NULL CHECK(json_valid(details_json)),
    occurred_at TEXT NOT NULL
);
CREATE INDEX idx_api_key_audit_consumer ON api_key_audit(consumer_id,id);
CREATE TRIGGER api_key_audit_no_update BEFORE UPDATE ON api_key_audit
BEGIN SELECT RAISE(ABORT,'API key audit is append-only'); END;
CREATE TRIGGER api_key_audit_no_delete BEFORE DELETE ON api_key_audit
BEGIN SELECT RAISE(ABORT,'API key audit is append-only'); END;
"""


def _api_key_audit_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, API_KEY_AUDIT_SCHEMA_SQL)


API_REQUEST_LIMIT_SCHEMA_SQL = """
CREATE TABLE api_rate_buckets (
    key_id TEXT NOT NULL REFERENCES api_keys(key_id),
    window_start INTEGER NOT NULL,
    used_count INTEGER NOT NULL CHECK(used_count>=1),
    PRIMARY KEY(key_id,window_start)
);
CREATE INDEX idx_api_rate_buckets_window ON api_rate_buckets(window_start);
CREATE TABLE api_request_leases (
    lease_id TEXT PRIMARY KEY,
    consumer_id TEXT NOT NULL REFERENCES api_consumers(id),
    key_id TEXT NOT NULL REFERENCES api_keys(key_id),
    acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL CHECK(expires_at>acquired_at)
);
CREATE INDEX idx_api_request_leases_consumer
    ON api_request_leases(consumer_id,expires_at);
CREATE INDEX idx_api_request_leases_expiry ON api_request_leases(expires_at);
"""


def _api_request_limit_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, API_REQUEST_LIMIT_SCHEMA_SQL)


API_REQUEST_AUDIT_SCHEMA_SQL = """
CREATE TABLE api_request_audit (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('admitted','completed','denied','handler_error')),
    consumer_id TEXT NOT NULL REFERENCES api_consumers(id),
    key_id TEXT NOT NULL REFERENCES api_keys(key_id),
    method TEXT NOT NULL CHECK(method IN ('GET','POST')),
    resource TEXT NOT NULL CHECK(length(resource) BETWEEN 1 AND 40),
    required_scope TEXT NOT NULL CHECK(length(required_scope) BETWEEN 1 AND 40),
    status_code INTEGER CHECK(status_code BETWEEN 100 AND 599),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 80),
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
    occurred_at TEXT NOT NULL,
    UNIQUE(request_id,event)
);
CREATE INDEX idx_api_request_audit_consumer
    ON api_request_audit(consumer_id,id);
CREATE INDEX idx_api_request_audit_occurred
    ON api_request_audit(occurred_at,id);
CREATE TRIGGER api_request_audit_no_update BEFORE UPDATE ON api_request_audit
BEGIN SELECT RAISE(ABORT,'API request audit is append-only'); END;
CREATE TRIGGER api_request_audit_no_delete BEFORE DELETE ON api_request_audit
BEGIN SELECT RAISE(ABORT,'API request audit is append-only'); END;
"""


def _api_request_audit_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, API_REQUEST_AUDIT_SCHEMA_SQL)


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
    Migration(
        6,
        "stable document version foundation",
        DOCUMENT_VERSION_SCHEMA_SQL,
        _document_version_foundation,
    ),
    Migration(
        7,
        "resumable legacy backfill foundation",
        LEGACY_BACKFILL_SCHEMA_SQL,
        _legacy_backfill_foundation,
    ),
    Migration(
        8,
        "versioned identity catalog foundation",
        IDENTITY_CATALOG_SCHEMA_SQL,
        _identity_catalog_foundation,
    ),
    Migration(
        9,
        "SEC identity and filing semantics",
        SEC_IDENTITY_SCHEMA_SQL,
        _sec_identity_foundation,
    ),
    Migration(
        10,
        "stable event candidate foundation",
        EVENT_FOUNDATION_SCHEMA_SQL,
        _event_foundation,
    ),
    Migration(
        11,
        "event relation and merge foundation",
        EVENT_RELATION_SCHEMA_SQL,
        _event_relation_foundation,
    ),
    Migration(
        12,
        "event split and retraction foundation",
        EVENT_TERMINAL_SCHEMA_SQL,
        _event_terminal_foundation,
    ),
    Migration(
        13,
        "auditable event fact revisions",
        EVENT_REVISION_SCHEMA_SQL,
        _event_revision_foundation,
    ),
    Migration(
        14,
        "immutable analysis input manifests",
        ANALYSIS_INPUT_SCHEMA_SQL,
        _analysis_input_foundation,
    ),
    Migration(
        15,
        "analysis attempt audit and budget gates",
        ANALYSIS_ATTEMPT_SCHEMA_SQL,
        _analysis_attempt_foundation,
    ),
    Migration(
        16,
        "validated analysis result publication",
        ANALYSIS_RESULT_SCHEMA_SQL,
        _analysis_result_foundation,
    ),
    Migration(
        17,
        "versioned curation search index foundation",
        CURATION_SEARCH_SCHEMA_SQL + "\ninitialize:empty-index-v1",
        _curation_search_foundation,
    ),
    Migration(
        18,
        "versioned curation story metrics foundation",
        CURATION_STORY_METRICS_SCHEMA_SQL + "\ninitialize:empty-story-metrics-v1",
        _curation_story_metrics_foundation,
    ),
    Migration(19, "versioned report input and publication foundation",
              REPORT_VERSION_SCHEMA_SQL, _report_version_foundation),
    Migration(20, "immutable report model generation ledger",
              REPORT_GENERATION_SCHEMA_SQL, _report_generation_foundation),
    Migration(21, "manual report draft review gate",
              REPORT_REVIEW_SCHEMA_SQL, _report_review_foundation),
    Migration(22, "API consumer and hashed key foundation",
              API_AUTH_SCHEMA_SQL, _api_auth_foundation),
    Migration(23, "append-only API key lifecycle audit",
              API_KEY_AUDIT_SCHEMA_SQL, _api_key_audit_foundation),
    Migration(24, "shared API request rate and concurrency limits",
              API_REQUEST_LIMIT_SCHEMA_SQL, _api_request_limit_foundation),
    Migration(25, "allowlisted append-only API request audit",
              API_REQUEST_AUDIT_SCHEMA_SQL, _api_request_audit_foundation),
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
    "documents", "document_versions", "document_version_inputs", "document_locators",
    "legacy_backfill_state", "legacy_backfill_sources", "legacy_object_mappings",
    "legacy_report_identities",
    "entities", "entity_versions", "entity_identifiers", "entity_aliases",
    "entity_relations", "security_listings", "entity_mentions",
    "legacy_company_entities", "publishers", "publisher_versions",
    "publisher_legacy_keys", "publisher_names", "publisher_domains",
    "document_attributions", "topic_catalog", "topic_versions",
    "topic_slug_aliases", "document_topic_assignments",
    "sec_security_keys", "sec_filings", "sec_filing_versions",
    "events", "event_versions", "match_decisions", "event_evidence",
    "document_event_links", "legacy_story_events", "event_relations", "event_merges",
    "event_splits", "event_split_replacements", "event_split_assignments",
    "event_retractions", "event_revisions", "analysis_runs", "analysis_inputs",
    "analysis_budget_policies", "analysis_attempt_authorizations", "analysis_attempts",
    "analysis_results", "analysis_publication_versions", "analysis_publications",
    "curation_search_documents", "curation_search_fts", "curation_search_state",
    "curation_search_dirty",
    "curation_story_metrics", "curation_story_metrics_state", "curation_story_metrics_dirty",
    "report_input_snapshots", "report_input_members", "report_versions", "report_publications",
    "report_generation_runs", "report_generation_attempts",
    "report_generation_reviews", "api_consumers", "api_keys", "api_key_audit",
    "api_rate_buckets", "api_request_leases",
    "api_request_audit",
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
EXPECTED_DOCUMENT_COLUMNS = {
    "id", "dataset_id", "legacy_item_id", "kind", "first_seen_at",
    "current_version_id", "status",
}
EXPECTED_DOCUMENT_VERSION_COLUMNS = {
    "id", "document_id", "version", "previous_version_id", "normalizer_version",
    "normalized_at",
    "title_original", "language", "text", "content_sha256", "version_sha256",
    "canonical_url", "source_id", "publisher_id", "published_at",
    "published_time_value_id", "published_precision", "time_status",
    "time_rule_version", "tzdb_version",
    "content_origin", "content_extent", "truncated", "extraction_status",
    "correction_kind", "available_at", "availability_basis", "point_in_time_eligible",
}
EXPECTED_LEGACY_BACKFILL_STATE_COLUMNS = {
    "singleton", "dataset_id", "cutoff_item_id", "cutoff_report_id", "status",
    "started_at", "updated_at", "finished_at", "error_detail",
}
EXPECTED_LEGACY_BACKFILL_SOURCE_COLUMNS = {
    "source_id", "dataset_id", "active_run_id", "last_item_id",
    "processed_count", "status", "updated_at", "error_detail",
}
EXPECTED_LEGACY_MAPPING_COLUMNS = {
    "dataset_id", "resource_type", "legacy_key", "target_type", "target_id",
    "legacy_sha256", "mapping_status", "detail_json", "available_at",
}
EXPECTED_LEGACY_REPORT_COLUMNS = {
    "id", "dataset_id", "legacy_report_id", "report_date", "content_sha256",
    "legacy_created_at", "available_at", "status",
}
EXPECTED_ENTITY_COLUMNS = {
    "id", "dataset_id", "type", "current_version_id", "status", "created_at",
}
EXPECTED_ENTITY_VERSION_COLUMNS = {
    "id", "entity_id", "version", "previous_version_id", "type",
    "canonical_name", "status", "attributes_json", "version_sha256",
    "available_at", "created_by",
}
EXPECTED_ENTITY_IDENTIFIER_COLUMNS = {
    "id", "entity_id", "namespace", "value", "qualifier_json", "valid_from",
    "valid_to", "evidence_id", "verification_status", "assertion_sha256", "available_at",
}
EXPECTED_ENTITY_ALIAS_COLUMNS = {
    "id", "entity_id", "alias", "alias_key", "language", "match_mode",
    "ambiguity", "status", "evidence_id", "valid_from", "valid_to",
    "assertion_sha256", "available_at",
}
EXPECTED_TOPIC_CATALOG_COLUMNS = {
    "id", "dataset_id", "current_version_id", "status", "created_at",
}
EXPECTED_TOPIC_VERSION_COLUMNS = {
    "id", "topic_id", "version", "previous_version_id", "slug", "name",
    "group_key", "description", "rules_json", "rules_hash", "version_sha256",
    "status", "available_at",
}
EXPECTED_PUBLISHER_COLUMNS = {
    "id", "dataset_id", "organization_entity_id", "current_version_id",
    "status", "created_at",
}
EXPECTED_PUBLISHER_VERSION_COLUMNS = {
    "id", "publisher_id", "version", "previous_version_id", "name", "status",
    "version_sha256", "available_at",
}
EXPECTED_IDENTITY_AUXILIARY_COLUMNS = {
    "entity_relations": {
        "id", "from_entity_id", "to_entity_id", "relation", "valid_from",
        "valid_to", "evidence_id", "verification_status", "available_at",
    },
    "security_listings": {
        "id", "security_entity_id", "issuer_entity_id", "exchange", "ticker",
        "listing_type", "valid_from", "valid_to", "evidence_id",
        "verification_status", "available_at", "publication_seq",
    },
    "entity_mentions": {
        "id", "document_version_id", "entity_id", "evidence_id", "method",
        "method_version", "raw_confidence", "calibration_version", "status",
        "available_at",
    },
    "legacy_company_entities": {
        "company_id", "entity_id", "legacy_sha256", "available_at",
    },
    "publisher_legacy_keys": {"legacy_key", "publisher_id", "available_at"},
    "publisher_names": {
        "id", "publisher_id", "name", "name_key", "language", "status",
        "assertion_sha256", "available_at",
    },
    "publisher_domains": {
        "id", "publisher_id", "domain", "valid_from", "valid_to", "evidence_id",
        "verification_status", "assertion_sha256", "available_at",
    },
    "document_attributions": {
        "id", "document_version_id", "publisher_id", "origin_document_id",
        "relation", "evidence_id", "method", "status", "available_at",
    },
    "topic_slug_aliases": {"slug", "topic_id", "available_at"},
    "document_topic_assignments": {
        "id", "document_version_id", "topic_version_id", "method", "method_version",
        "analysis_result_id", "evidence_ids_json", "status", "available_at",
    },
    "sec_security_keys": {
        "cik", "exchange", "ticker", "security_entity_id", "first_evidence_id",
        "available_at",
    },
    "sec_filings": {
        "id", "dataset_id", "cik", "accession_number", "issuer_entity_id",
        "current_version_id", "first_seen_at", "status",
    },
    "sec_filing_versions": {
        "id", "filing_id", "version", "previous_version_id", "document_version_id",
        "raw_record_id", "form", "base_form", "is_amendment", "amends_filing_id",
        "amendment_status", "primary_document", "filing_date", "report_period_end",
        "accepted_at", "items_json", "metadata_sha256", "available_at",
    },
    "events": {
        "id", "dataset_id", "first_seen_at", "latest_report_at",
        "last_fact_change_at", "current_version_id", "status",
    },
    "event_versions": {
        "id", "event_id", "version", "previous_version_id", "schema_version",
        "title", "event_type", "event_time_start", "event_time_end",
        "time_precision", "primary_entities_json", "object_entities_json",
        "facts_json", "topics_json", "knowledge_status", "version_sha256",
        "available_at", "created_by", "method_version",
    },
    "match_decisions": {
        "id", "dataset_id", "decision_key", "input_versions_json",
        "candidate_event_versions_json", "matcher_version", "features_json",
        "score", "decision", "reason", "review_status", "available_at",
    },
    "event_evidence": {
        "id", "event_version_id", "document_version_id", "evidence_id",
        "fact_id", "role", "available_at",
    },
    "document_event_links": {
        "id", "document_version_id", "event_id", "event_version_id", "role",
        "decision_id", "available_at", "supersedes_link_id",
    },
    "legacy_story_events": {
        "story_id", "event_id", "canonical_story_id", "mapping_status", "available_at",
    },
    "event_relations": {
        "id", "from_event_id", "to_event_id", "relation", "evidence_ids_json",
        "reason", "available_at", "supersedes_relation_id", "publication_seq",
    },
    "event_merges": {
        "id", "absorbed_event_id", "survivor_event_id", "evidence_ids_json",
        "reason", "available_at", "publication_seq", "previous_status",
    },
    "event_splits": {
        "id", "original_event_id", "previous_status", "evidence_ids_json",
        "reason", "available_at", "publication_seq",
    },
    "event_split_replacements": {
        "split_id", "replacement_event_id", "replacement_event_version_id", "ordinal",
    },
    "event_split_assignments": {
        "split_id", "document_version_id", "evidence_id", "replacement_event_id",
        "replacement_event_version_id", "available_at",
    },
    "event_retractions": {
        "id", "event_id", "previous_status", "retracted_event_version_id",
        "evidence_ids_json", "reason", "available_at", "publication_seq",
    },
    "event_revisions": {
        "id", "event_id", "previous_version_id", "revised_version_id",
        "revision_kind", "changed_fields_json", "evidence_ids_json", "reason",
        "available_at", "publication_seq",
    },
    "analysis_runs": {
        "id", "idempotency_key", "request_sha256", "job_id", "subject_type",
        "subject_version_id", "task_type", "output_schema_version", "provider",
        "requested_model", "prompt_template_id", "prompt_sha256",
        "rendered_input_ref", "rendered_input_sha256", "pipeline_version",
        "parameters_json", "input_manifest_json", "input_manifest_sha256", "prepared_at",
    },
    "analysis_inputs": {
        "run_id", "ordinal", "document_version_id", "event_version_id",
        "evidence_id", "role",
    },
    "analysis_budget_policies": {
        "id", "provider", "daily_limit_microusd", "per_attempt_limit_microusd",
        "effective_from", "created_at", "supersedes_policy_id",
    },
    "analysis_attempt_authorizations": {
        "id", "run_id", "attempt_number", "attempt_kind", "provider",
        "budget_policy_id", "budget_day", "reserved_cost_microusd", "decision",
        "reason", "authorized_at",
    },
    "analysis_attempts": {
        "id", "authorization_id", "run_id", "attempt_number", "attempt_kind",
        "resolved_model", "provider_request_id", "started_at", "finished_at", "status",
        "input_tokens", "output_tokens", "usage_status", "cost_microusd",
        "pricing_version", "raw_response_ref", "raw_response_sha256", "error_type",
        "error_detail", "recorded_at",
    },
    "analysis_results": {
        "id", "run_id", "attempt_id", "schema_version", "raw_output_ref",
        "raw_output_sha256", "validated_output_json", "validation_report_json",
        "result_status", "created_at", "available_at",
    },
    "analysis_publication_versions": {
        "id", "subject_type", "subject_version_id", "task_type", "result_id",
        "version", "review_status", "evidence_status", "available_at",
        "supersedes_id", "publication_seq",
    },
    "analysis_publications": {
        "subject_type", "subject_version_id", "task_type", "current_publication_id",
    },
}
EXPECTED_DOCUMENT_INPUT_COLUMNS = {"version_id", "raw_record_id", "role"}
EXPECTED_DOCUMENT_LOCATOR_COLUMNS = {
    "document_id", "source_id", "external_id", "canonical_url", "relation",
    "first_observed_at", "last_observed_at",
}
EXPECTED_CURATION_SEARCH_COLUMNS = {
    "curation_search_documents": {
        "item_id", "document_version_id", "translation_publication_id",
        "summary_publication_id", "index_schema_version", "title_original",
        "title_display", "summary_display", "indexed_at",
    },
    "curation_search_state": {
        "singleton", "generation", "status", "last_item_id", "indexed_count", "updated_at",
    },
    "curation_search_dirty": {"item_id", "reason", "queued_at"},
    "curation_search_fts": {"title_original", "title_display", "summary_display"},
}
EXPECTED_CURATION_STORY_METRICS_COLUMNS = {
    "curation_story_metrics": {
        "story_id", "metric_schema_version", "visible_item_count", "publisher_count",
        "heat", "representative_item_id", "title_display", "url_display",
        "company_slugs", "last_visible_at", "computed_at",
    },
    "curation_story_metrics_state": {
        "singleton", "generation", "status", "last_story_id", "indexed_count", "updated_at",
    },
    "curation_story_metrics_dirty": {"story_id", "reason", "queued_at"},
}
EXPECTED_REPORT_VERSION_COLUMNS = {
    "report_input_snapshots": {
        "id", "dataset_id", "report_key", "report_type", "report_date",
        "window_start", "window_end", "window_basis", "timezone", "calendar_id",
        "calendar_version", "as_of", "knowledge_checkpoint_id", "manifest_json",
        "manifest_sha256", "input_count", "created_at",
    },
    "report_input_members": {
        "snapshot_id", "ordinal", "legacy_item_id", "document_version_id",
        "material_sha256",
    },
    "report_versions": {
        "id", "dataset_id", "report_key", "version", "input_snapshot_id", "mode",
        "content", "content_sha256", "citations_json", "coverage_json", "provider",
        "model", "prompt_template_id", "prompt_sha256", "generated_at",
        "available_at", "supersedes_version_id", "generation_attempt_id",
    },
    "report_publications": {
        "dataset_id", "report_key", "current_version_id", "published_at",
    },
    "report_generation_runs": {
        "id", "dataset_id", "input_snapshot_id", "provider", "requested_model",
        "prompt_template_id", "prompt_sha256", "rendered_prompt_ref",
        "rendered_prompt_sha256", "parameters_json", "prepared_at",
    },
    "report_generation_attempts": {
        "id", "run_id", "attempt_number", "status", "resolved_model",
        "provider_request_id", "raw_response_ref", "raw_response_sha256",
        "validated_draft_json", "validation_report_json", "input_tokens",
        "output_tokens", "cost_microusd", "usage_status", "started_at",
        "finished_at", "recorded_at",
    },
    "report_generation_reviews": {
        "id", "attempt_id", "decision", "review_type", "reviewer_id",
        "reason", "draft_sha256", "reviewed_at",
    },
}
EXPECTED_API_AUTH_COLUMNS = {
    "api_consumers": {"id", "name", "status", "authz_version", "created_at", "revoked_at"},
    "api_keys": {"key_id", "consumer_id", "token_sha256", "scopes_json",
                 "issued_at", "expires_at", "revoked_at"},
    "api_key_audit": {"id", "action", "actor", "consumer_id", "key_id",
                      "details_json", "occurred_at"},
    "api_rate_buckets": {"key_id", "window_start", "used_count"},
    "api_request_leases": {"lease_id", "consumer_id", "key_id", "acquired_at", "expires_at"},
    "api_request_audit": {"id", "request_id", "event", "consumer_id", "key_id",
                          "method", "resource", "required_scope", "status_code",
                          "error_code", "duration_ms", "occurred_at"},
}
EXPECTED_INGEST_TRIGGERS = {
    "api_request_audit_no_update", "api_request_audit_no_delete",
    "api_key_audit_no_update", "api_key_audit_no_delete",
    "report_generation_reviews_valid_approval", "report_generation_reviews_no_update",
    "report_generation_reviews_no_delete",
    "report_generation_runs_match", "report_generation_runs_no_update",
    "report_generation_runs_no_delete", "report_generation_attempts_no_update",
    "report_generation_attempts_no_delete", "report_versions_generation_provenance",
    "report_input_snapshots_no_update", "report_input_snapshots_no_delete",
    "report_input_members_no_update", "report_input_members_no_delete",
    "report_input_members_no_late_insert",
    "report_versions_valid_append", "report_versions_no_update", "report_versions_no_delete",
    "report_publications_match_insert", "report_publications_match_update",
    "curation_search_ai", "curation_search_ad", "curation_search_au",
    "curation_search_item_ai", "curation_search_item_au",
    "curation_search_document_ai", "curation_search_document_au",
    "curation_search_publication_ai", "curation_search_publication_au",
    "curation_story_insert", "curation_story_update",
    "curation_story_member_insert", "curation_story_member_delete",
    "curation_story_member_move", "curation_story_item_update",
    "curation_story_document_insert", "curation_story_document_update",
    "curation_story_publication_insert", "curation_story_publication_update",
    "source_config_versions_no_update", "source_config_versions_no_delete",
    "raw_records_no_update", "raw_records_no_delete",
    "raw_observations_no_update", "raw_observations_no_delete",
    "ingest_runs_valid_transition", "ingest_runs_no_delete",
    "source_time_values_no_update", "source_time_values_no_delete",
    "document_versions_valid_append", "document_versions_no_update",
    "document_versions_no_delete", "document_version_inputs_no_update",
    "document_version_inputs_no_delete", "documents_identity_immutable",
    "documents_current_version_valid", "documents_current_version_required",
    "documents_no_delete",
    "document_locators_identity_immutable", "document_locators_no_delete",
    "legacy_object_mappings_no_update", "legacy_object_mappings_no_delete",
    "legacy_report_identities_no_update", "legacy_report_identities_no_delete",
    "entity_versions_valid_append", "entity_versions_no_update", "entity_versions_no_delete",
    "entities_identity_immutable", "entities_current_version_valid", "entities_no_delete",
    "publisher_versions_valid_append", "publisher_versions_no_update",
    "publisher_versions_no_delete", "publishers_identity_immutable",
    "publishers_current_version_valid", "publishers_no_delete",
    "topic_versions_valid_append", "topic_versions_no_update", "topic_versions_no_delete",
    "topic_catalog_identity_immutable", "topic_catalog_current_version_valid",
    "topic_catalog_no_delete", "entity_identifiers_no_update",
    "entity_identifiers_no_delete", "entity_aliases_no_update", "entity_aliases_no_delete",
    "entity_relations_no_update", "entity_relations_no_delete",
    "security_listings_no_update", "security_listings_no_delete",
    "entity_mentions_no_update", "entity_mentions_no_delete",
    "legacy_company_entities_no_update", "legacy_company_entities_no_delete",
    "publisher_legacy_keys_no_update", "publisher_legacy_keys_no_delete",
    "publisher_names_no_update", "publisher_names_no_delete",
    "publisher_domains_no_update", "publisher_domains_no_delete",
    "document_attributions_no_update", "document_attributions_no_delete",
    "topic_slug_aliases_no_update", "topic_slug_aliases_no_delete",
    "document_topic_assignments_no_update", "document_topic_assignments_no_delete",
    "sec_security_keys_no_update", "sec_security_keys_no_delete",
    "sec_filing_versions_valid_append", "sec_filing_versions_no_update",
    "sec_filing_versions_no_delete", "sec_filings_identity_immutable",
    "sec_filings_current_version_valid", "sec_filings_no_delete",
    "event_versions_valid_append", "event_versions_no_update", "event_versions_no_delete",
    "events_identity_immutable", "events_current_version_valid", "events_no_delete",
    "match_decisions_no_update", "match_decisions_no_delete",
    "event_evidence_no_update", "event_evidence_no_delete",
    "document_event_links_no_update", "document_event_links_no_delete",
    "legacy_story_events_no_update", "legacy_story_events_no_delete",
    "event_relations_supersedes_valid", "event_relations_no_update",
    "event_relations_no_delete", "event_merges_no_cycle",
    "event_merges_status_guard", "event_merges_no_update", "event_merges_no_delete",
    "event_splits_no_update", "event_splits_no_delete",
    "event_split_replacements_no_update", "event_split_replacements_no_delete",
    "event_split_assignments_no_update", "event_split_assignments_no_delete",
    "event_retractions_no_update", "event_retractions_no_delete",
    "event_revisions_no_update", "event_revisions_no_delete",
    "analysis_runs_no_update", "analysis_runs_no_delete",
    "analysis_inputs_no_update", "analysis_inputs_no_delete",
    "analysis_budget_policies_no_update", "analysis_budget_policies_no_delete",
    "analysis_attempt_authorizations_no_update", "analysis_attempt_authorizations_no_delete",
    "analysis_attempts_no_update", "analysis_attempts_no_delete",
    "analysis_results_no_update", "analysis_results_no_delete",
    "analysis_publication_versions_no_update", "analysis_publication_versions_no_delete",
    "analysis_publications_no_delete",
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
    document_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(documents)")
    }
    missing_document_columns = EXPECTED_DOCUMENT_COLUMNS - document_columns
    document_version_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(document_versions)")
    }
    missing_document_version_columns = (
        EXPECTED_DOCUMENT_VERSION_COLUMNS - document_version_columns
    )
    document_input_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(document_version_inputs)")
    }
    missing_document_input_columns = (
        EXPECTED_DOCUMENT_INPUT_COLUMNS - document_input_columns
    )
    document_locator_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(document_locators)")
    }
    missing_document_locator_columns = (
        EXPECTED_DOCUMENT_LOCATOR_COLUMNS - document_locator_columns
    )
    legacy_backfill_state_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(legacy_backfill_state)")
    }
    missing_legacy_backfill_state_columns = (
        EXPECTED_LEGACY_BACKFILL_STATE_COLUMNS - legacy_backfill_state_columns
    )
    legacy_backfill_source_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(legacy_backfill_sources)")
    }
    missing_legacy_backfill_source_columns = (
        EXPECTED_LEGACY_BACKFILL_SOURCE_COLUMNS - legacy_backfill_source_columns
    )
    legacy_mapping_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(legacy_object_mappings)")
    }
    missing_legacy_mapping_columns = EXPECTED_LEGACY_MAPPING_COLUMNS - legacy_mapping_columns
    legacy_report_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(legacy_report_identities)")
    }
    missing_legacy_report_columns = EXPECTED_LEGACY_REPORT_COLUMNS - legacy_report_columns
    entity_columns = {row["name"] for row in db.execute("PRAGMA table_info(entities)")}
    missing_entity_columns = EXPECTED_ENTITY_COLUMNS - entity_columns
    entity_version_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(entity_versions)")
    }
    missing_entity_version_columns = EXPECTED_ENTITY_VERSION_COLUMNS - entity_version_columns
    entity_identifier_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(entity_identifiers)")
    }
    missing_entity_identifier_columns = (
        EXPECTED_ENTITY_IDENTIFIER_COLUMNS - entity_identifier_columns
    )
    entity_alias_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(entity_aliases)")
    }
    missing_entity_alias_columns = EXPECTED_ENTITY_ALIAS_COLUMNS - entity_alias_columns
    topic_catalog_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(topic_catalog)")
    }
    missing_topic_catalog_columns = EXPECTED_TOPIC_CATALOG_COLUMNS - topic_catalog_columns
    topic_version_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(topic_versions)")
    }
    missing_topic_version_columns = EXPECTED_TOPIC_VERSION_COLUMNS - topic_version_columns
    publisher_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(publishers)")
    }
    missing_publisher_columns = EXPECTED_PUBLISHER_COLUMNS - publisher_columns
    publisher_version_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(publisher_versions)")
    }
    missing_publisher_version_columns = (
        EXPECTED_PUBLISHER_VERSION_COLUMNS - publisher_version_columns
    )
    missing_identity_auxiliary_columns = {
        table: sorted(required - {
            row["name"] for row in db.execute(f"PRAGMA table_info({table})")
        })
        for table, required in EXPECTED_IDENTITY_AUXILIARY_COLUMNS.items()
    }
    missing_identity_auxiliary_columns = {
        table: columns
        for table, columns in missing_identity_auxiliary_columns.items()
        if columns
    }
    missing_search_columns = {
        table: sorted(required - {
            row["name"] for row in db.execute(f"PRAGMA table_info({table})")
        })
        for table, required in (
            EXPECTED_CURATION_SEARCH_COLUMNS | EXPECTED_CURATION_STORY_METRICS_COLUMNS
            | EXPECTED_REPORT_VERSION_COLUMNS | EXPECTED_API_AUTH_COLUMNS
        ).items()
    }
    missing_search_columns = {
        table: columns for table, columns in missing_search_columns.items() if columns
    }
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
        or missing_document_columns
        or missing_document_version_columns
        or missing_document_input_columns
        or missing_document_locator_columns
        or missing_legacy_backfill_state_columns
        or missing_legacy_backfill_source_columns
        or missing_legacy_mapping_columns
        or missing_legacy_report_columns
        or missing_entity_columns
        or missing_entity_version_columns
        or missing_entity_identifier_columns
        or missing_entity_alias_columns
        or missing_topic_catalog_columns
        or missing_topic_version_columns
        or missing_publisher_columns
        or missing_publisher_version_columns
        or missing_identity_auxiliary_columns
        or missing_search_columns
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
            f"document_columns={sorted(missing_document_columns)}, "
            f"document_version_columns={sorted(missing_document_version_columns)}, "
            f"document_input_columns={sorted(missing_document_input_columns)}, "
            f"document_locator_columns={sorted(missing_document_locator_columns)}, "
            f"legacy_backfill_state_columns={sorted(missing_legacy_backfill_state_columns)}, "
            f"legacy_backfill_source_columns={sorted(missing_legacy_backfill_source_columns)}, "
            f"legacy_mapping_columns={sorted(missing_legacy_mapping_columns)}, "
            f"legacy_report_columns={sorted(missing_legacy_report_columns)}, "
            f"entity_columns={sorted(missing_entity_columns)}, "
            f"entity_version_columns={sorted(missing_entity_version_columns)}, "
            f"entity_identifier_columns={sorted(missing_entity_identifier_columns)}, "
            f"entity_alias_columns={sorted(missing_entity_alias_columns)}, "
            f"topic_catalog_columns={sorted(missing_topic_catalog_columns)}, "
            f"topic_version_columns={sorted(missing_topic_version_columns)}, "
            f"publisher_columns={sorted(missing_publisher_columns)}, "
            f"publisher_version_columns={sorted(missing_publisher_version_columns)}, "
            f"identity_auxiliary_columns={missing_identity_auxiliary_columns}, "
            f"search_columns={missing_search_columns}, "
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
    search_state = db.execute(
        "SELECT singleton,status FROM curation_search_state"
    ).fetchall()
    if len(search_state) != 1 or search_state[0]["singleton"] != 1:
        raise DatabaseVerificationError("curation search index must have one state row")
    story_metrics_state = db.execute(
        "SELECT singleton,status FROM curation_story_metrics_state"
    ).fetchall()
    if len(story_metrics_state) != 1 or story_metrics_state[0]["singleton"] != 1:
        raise DatabaseVerificationError("curation story metrics must have one state row")
    invalid_documents = db.execute(
        """SELECT COUNT(*) FROM documents AS document
           LEFT JOIN document_versions AS version
             ON version.id=document.current_version_id
           JOIN dataset_state AS state ON state.singleton=1
           WHERE document.dataset_id<>state.dataset_id
              OR document.current_version_id IS NULL
              OR version.id IS NULL
              OR version.document_id<>document.id"""
    ).fetchone()[0]
    if invalid_documents:
        raise DatabaseVerificationError(
            f"{invalid_documents} document(s) have an invalid dataset or current version"
        )
    for table, version_table, owner_column in (
        ("entities", "entity_versions", "entity_id"),
        ("publishers", "publisher_versions", "publisher_id"),
        ("topic_catalog", "topic_versions", "topic_id"),
    ):
        invalid = db.execute(
            f"""SELECT COUNT(*) FROM {table} AS identity
                LEFT JOIN {version_table} AS version
                  ON version.id=identity.current_version_id
                JOIN dataset_state AS state ON state.singleton=1
                WHERE identity.dataset_id<>state.dataset_id
                   OR identity.current_version_id IS NULL
                   OR version.id IS NULL
                   OR version.{owner_column}<>identity.id"""
        ).fetchone()[0]
        if invalid:
            raise DatabaseVerificationError(
                f"{invalid} {table} row(s) have an invalid dataset or current version"
            )
    mismatched_entities = db.execute(
        """SELECT COUNT(*) FROM entities AS identity
           JOIN entity_versions AS version ON version.id=identity.current_version_id
           WHERE identity.type<>version.type OR identity.status<>version.status"""
    ).fetchone()[0]
    mismatched_publishers = db.execute(
        """SELECT COUNT(*) FROM publishers AS identity
           JOIN publisher_versions AS version ON version.id=identity.current_version_id
           WHERE identity.status<>version.status"""
    ).fetchone()[0]
    mismatched_topics = db.execute(
        """SELECT COUNT(*) FROM topic_catalog AS identity
           JOIN topic_versions AS version ON version.id=identity.current_version_id
           WHERE identity.status<>version.status"""
    ).fetchone()[0]
    if mismatched_entities or mismatched_publishers or mismatched_topics:
        raise DatabaseVerificationError(
            "catalog current projections disagree with their versions: "
            f"entities={mismatched_entities}, publishers={mismatched_publishers}, "
            f"topics={mismatched_topics}"
        )
    invalid_sec_filings = db.execute(
        """SELECT COUNT(*) FROM sec_filings AS filing
           LEFT JOIN sec_filing_versions AS version
             ON version.id=filing.current_version_id
           JOIN dataset_state AS state ON state.singleton=1
           WHERE filing.dataset_id<>state.dataset_id
              OR filing.current_version_id IS NULL
              OR version.id IS NULL
              OR version.filing_id<>filing.id"""
    ).fetchone()[0]
    invalid_sec_links = db.execute(
        """SELECT COUNT(*) FROM sec_filing_versions AS version
           LEFT JOIN sec_filings AS target ON target.id=version.amends_filing_id
           WHERE (version.amendment_status='linked' AND target.id IS NULL)
              OR (version.amendment_status!='linked' AND version.amends_filing_id IS NOT NULL)"""
    ).fetchone()[0]
    if invalid_sec_filings or invalid_sec_links:
        raise DatabaseVerificationError(
            "SEC filing projections are invalid: "
            f"filings={invalid_sec_filings}, amendment_links={invalid_sec_links}"
        )
    invalid_events = db.execute(
        """SELECT COUNT(*) FROM events AS event
           LEFT JOIN event_versions AS version ON version.id=event.current_version_id
           JOIN dataset_state AS state ON state.singleton=1
           WHERE event.dataset_id<>state.dataset_id
              OR event.current_version_id IS NULL
              OR version.id IS NULL OR version.event_id<>event.id"""
    ).fetchone()[0]
    invalid_event_links = db.execute(
        """SELECT COUNT(*) FROM document_event_links AS link
           LEFT JOIN event_versions AS version ON version.id=link.event_version_id
           WHERE version.id IS NULL OR version.event_id<>link.event_id"""
    ).fetchone()[0]
    invalid_event_evidence = db.execute(
        """SELECT COUNT(*) FROM event_evidence AS evidence
           WHERE NOT EXISTS(
               SELECT 1 FROM document_version_inputs AS input
               WHERE input.version_id=evidence.document_version_id
                 AND input.raw_record_id=evidence.evidence_id
           )"""
    ).fetchone()[0]
    document_version_ids = {
        row[0] for row in db.execute("SELECT id FROM document_versions")
    }
    event_version_ids = {row[0] for row in db.execute("SELECT id FROM event_versions")}
    invalid_match_decisions = 0
    match_references: dict[str, tuple[set[str], set[str], str]] = {}
    for row in db.execute(
        """SELECT id,input_versions_json,candidate_event_versions_json,decision
           FROM match_decisions"""
    ):
        try:
            inputs = json.loads(row["input_versions_json"])
            candidates = json.loads(row["candidate_event_versions_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_match_decisions += 1
            continue
        if (
            not isinstance(inputs, list)
            or not inputs
            or not all(isinstance(item, str) and item in document_version_ids for item in inputs)
            or not isinstance(candidates, list)
            or not all(isinstance(item, str) and item in event_version_ids for item in candidates)
            or (row["decision"] != "new_candidate" and not candidates)
        ):
            invalid_match_decisions += 1
            continue
        match_references[row["id"]] = (set(inputs), set(candidates), row["decision"])
    invalid_link_decisions = 0
    for row in db.execute(
        """SELECT document_version_id,event_version_id,decision_id
           FROM document_event_links"""
    ):
        references = match_references.get(row["decision_id"])
        if (
            not references
            or references[2] != "candidate_link"
            or row["document_version_id"] not in references[0]
            or row["event_version_id"] not in references[1]
        ):
            invalid_link_decisions += 1
    raw_record_ids = {row[0] for row in db.execute("SELECT id FROM raw_records")}
    change_rows = {
        row["seq"]: row for row in db.execute(
            "SELECT seq,resource_type,resource_id,version_id,operation FROM change_log"
        )
    }
    invalid_event_relations = 0
    relation_rows = {
        row["id"]: row for row in db.execute("SELECT * FROM event_relations")
    }
    for row in relation_rows.values():
        try:
            evidence_ids = json.loads(row["evidence_ids_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_event_relations += 1
            continue
        previous = (
            relation_rows.get(row["supersedes_relation_id"])
            if row["supersedes_relation_id"]
            else None
        )
        change = change_rows.get(row["publication_seq"])
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(item, str) and item in raw_record_ids for item in evidence_ids)
            or (previous is not None and (
                previous["from_event_id"] != row["from_event_id"]
                or previous["to_event_id"] != row["to_event_id"]
            ))
            or (row["supersedes_relation_id"] and previous is None)
            or change is None
            or change["resource_type"] != "event"
            or change["resource_id"] != row["from_event_id"]
            or change["version_id"] != row["id"]
            or change["operation"] != "update"
        ):
            invalid_event_relations += 1
    event_statuses = {
        row["id"]: row["status"] for row in db.execute("SELECT id,status FROM events")
    }
    merge_rows = {
        row["absorbed_event_id"]: row for row in db.execute("SELECT * FROM event_merges")
    }
    invalid_event_merges = 0
    for absorbed_id, row in merge_rows.items():
        try:
            evidence_ids = json.loads(row["evidence_ids_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_event_merges += 1
            continue
        change = change_rows.get(row["publication_seq"])
        if (
            event_statuses.get(absorbed_id) != "merged"
            or row["survivor_event_id"] not in event_statuses
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(item, str) and item in raw_record_ids for item in evidence_ids)
            or change is None
            or change["resource_type"] != "event"
            or change["resource_id"] != absorbed_id
            or change["version_id"] != row["id"]
            or change["operation"] != "merge"
        ):
            invalid_event_merges += 1
            continue
        seen = {absorbed_id}
        current = row["survivor_event_id"]
        while current in merge_rows:
            if current in seen:
                invalid_event_merges += 1
                break
            seen.add(current)
            current = merge_rows[current]["survivor_event_id"]
        if current in seen or event_statuses.get(current) == "merged":
            invalid_event_merges += 1
    invalid_event_merges += sum(
        1 for event_id, status in event_statuses.items()
        if status == "merged" and event_id not in merge_rows
    )
    split_rows = {
        row["original_event_id"]: row for row in db.execute("SELECT * FROM event_splits")
    }
    invalid_event_splits = 0
    for original_id, row in split_rows.items():
        try:
            evidence_ids = json.loads(row["evidence_ids_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_event_splits += 1
            continue
        change = change_rows.get(row["publication_seq"])
        replacements = db.execute(
            """SELECT replacement_event_id,replacement_event_version_id
               FROM event_split_replacements WHERE split_id=?""",
            (row["id"],),
        ).fetchall()
        replacement_pairs = {
            (item["replacement_event_id"], item["replacement_event_version_id"])
            for item in replacements
        }
        assignments = db.execute(
            """SELECT document_version_id,evidence_id,replacement_event_id,
                      replacement_event_version_id
               FROM event_split_assignments WHERE split_id=?""",
            (row["id"],),
        ).fetchall()
        original_version = db.execute(
            "SELECT current_version_id FROM events WHERE id=?", (original_id,)
        ).fetchone()
        original_evidence = set()
        if original_version:
            original_evidence = {
                (item["document_version_id"], item["evidence_id"])
                for item in db.execute(
                    """SELECT document_version_id,evidence_id FROM event_evidence
                       WHERE event_version_id=?""",
                    (original_version["current_version_id"],),
                )
            }
        assigned_evidence = {
            (item["document_version_id"], item["evidence_id"])
            for item in assignments
        }
        assigned_replacements = {
            (item["replacement_event_id"], item["replacement_event_version_id"])
            for item in assignments
        }
        if (
            event_statuses.get(original_id) != "split"
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or len(replacement_pairs) < 2
            or not assignments
            or assigned_evidence != original_evidence
            or assigned_replacements != replacement_pairs
            or set(evidence_ids) != {item[1] for item in assigned_evidence}
            or change is None
            or change["resource_type"] != "event"
            or change["resource_id"] != original_id
            or change["version_id"] != row["id"]
            or change["operation"] != "split"
        ):
            invalid_event_splits += 1
            continue
        for event_id, version_id in replacement_pairs:
            owner = db.execute(
                "SELECT event_id FROM event_versions WHERE id=?", (version_id,)
            ).fetchone()
            if not owner or owner["event_id"] != event_id:
                invalid_event_splits += 1
        for assignment in assignments:
            pair = (
                assignment["replacement_event_id"],
                assignment["replacement_event_version_id"],
            )
            if (
                pair not in replacement_pairs
                or assignment["evidence_id"] not in evidence_ids
                or not db.execute(
                    """SELECT 1 FROM document_version_inputs
                       WHERE version_id=? AND raw_record_id=?""",
                    (assignment["document_version_id"], assignment["evidence_id"]),
                ).fetchone()
                or not original_version
                or not db.execute(
                    """SELECT 1 FROM event_evidence
                       WHERE event_version_id=? AND document_version_id=? AND evidence_id=?""",
                    (
                        original_version["current_version_id"],
                        assignment["document_version_id"], assignment["evidence_id"],
                    ),
                ).fetchone()
            ):
                invalid_event_splits += 1
    retraction_rows = {
        row["event_id"]: row for row in db.execute("SELECT * FROM event_retractions")
    }
    invalid_event_retractions = 0
    for event_id, row in retraction_rows.items():
        try:
            evidence_ids = json.loads(row["evidence_ids_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_event_retractions += 1
            continue
        change = change_rows.get(row["publication_seq"])
        current = db.execute(
            """SELECT event.current_version_id,version.event_id,version.knowledge_status
               FROM events AS event
               LEFT JOIN event_versions AS version
                 ON version.id=event.current_version_id
               WHERE event.id=?""",
            (event_id,),
        ).fetchone()
        linked_evidence_ids = {
            item["evidence_id"] for item in db.execute(
                """SELECT evidence_id FROM event_evidence
                   WHERE event_version_id=?""",
                (row["retracted_event_version_id"],),
            )
        }
        if (
            event_statuses.get(event_id) != "retracted"
            or not current
            or current["current_version_id"] != row["retracted_event_version_id"]
            or current["event_id"] != event_id
            or current["knowledge_status"] != "retracted"
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(item in raw_record_ids for item in evidence_ids)
            or set(evidence_ids) != linked_evidence_ids
            or change is None
            or change["resource_type"] != "event"
            or change["resource_id"] != event_id
            or change["version_id"] != row["retracted_event_version_id"]
            or change["operation"] != "withdraw"
        ):
            invalid_event_retractions += 1
    terminal_ids = list(merge_rows) + list(split_rows) + list(retraction_rows)
    invalid_terminal_overlap = len(terminal_ids) - len(set(terminal_ids))
    invalid_event_splits += sum(
        1 for event_id, status in event_statuses.items()
        if status == "split" and event_id not in split_rows
    )
    invalid_event_retractions += sum(
        1 for event_id, status in event_statuses.items()
        if status == "retracted" and event_id not in retraction_rows
    )
    revision_fields = {
        "schema_version": "schema_version", "title": "title", "event_type": "event_type",
        "event_time_start": "event_time_start", "event_time_end": "event_time_end",
        "time_precision": "time_precision", "primary_entities": "primary_entities_json",
        "object_entities": "object_entities_json", "facts": "facts_json",
        "topics": "topics_json", "knowledge_status": "knowledge_status",
    }
    invalid_event_revisions = 0
    for row in db.execute("SELECT * FROM event_revisions"):
        try:
            changed_fields = json.loads(row["changed_fields_json"])
            evidence_ids = json.loads(row["evidence_ids_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_event_revisions += 1
            continue
        previous = db.execute(
            "SELECT * FROM event_versions WHERE id=?", (row["previous_version_id"],)
        ).fetchone()
        revised = db.execute(
            "SELECT * FROM event_versions WHERE id=?", (row["revised_version_id"],)
        ).fetchone()
        change = change_rows.get(row["publication_seq"])
        actual_changes = []
        if previous and revised:
            for public_name, column in revision_fields.items():
                before_value = previous[column]
                after_value = revised[column]
                if column.endswith("_json"):
                    try:
                        before_value = json.loads(before_value)
                        after_value = json.loads(after_value)
                    except (TypeError, json.JSONDecodeError):
                        invalid_event_revisions += 1
                if before_value != after_value:
                    actual_changes.append(public_name)
        linked_evidence = {
            item["evidence_id"] for item in db.execute(
                "SELECT evidence_id FROM event_evidence WHERE event_version_id=?",
                (row["revised_version_id"],),
            )
        }
        if (
            not previous or not revised
            or previous["event_id"] != row["event_id"]
            or revised["event_id"] != row["event_id"]
            or revised["previous_version_id"] != previous["id"]
            or revised["version"] != previous["version"] + 1
            or changed_fields != sorted(actual_changes)
            or not actual_changes
            or not isinstance(evidence_ids, list) or not evidence_ids
            or set(evidence_ids) != linked_evidence
            or change is None
            or change["resource_type"] != "event"
            or change["resource_id"] != row["event_id"]
            or change["version_id"] != row["revised_version_id"]
            or change["operation"] != "update"
        ):
            invalid_event_revisions += 1
    invalid_analysis_runs = 0
    for run in db.execute("SELECT * FROM analysis_runs"):
        try:
            manifest = json.loads(run["input_manifest_json"])
            parameters = json.loads(run["parameters_json"])
        except (TypeError, json.JSONDecodeError):
            invalid_analysis_runs += 1
    invalid_analysis_attempts = 0
    for authorization in db.execute("SELECT * FROM analysis_attempt_authorizations"):
        run = db.execute(
            "SELECT provider FROM analysis_runs WHERE id=?", (authorization["run_id"],)
        ).fetchone()
        policy = db.execute(
            "SELECT * FROM analysis_budget_policies WHERE id=?",
            (authorization["budget_policy_id"],),
        ).fetchone()
        attempt = db.execute(
            "SELECT * FROM analysis_attempts WHERE authorization_id=?", (authorization["id"],)
        ).fetchone()
        should_allow = bool(policy) and (
            authorization["reserved_cost_microusd"] <= policy["per_attempt_limit_microusd"]
        )
        if (
            not run or not policy or run["provider"] != authorization["provider"]
            or policy["provider"] != authorization["provider"]
            or (authorization["decision"] == "allowed" and not should_allow)
            or (attempt is not None and authorization["decision"] != "allowed")
            or (attempt is not None and (
                attempt["run_id"] != authorization["run_id"]
                or attempt["attempt_number"] != authorization["attempt_number"]
                or attempt["attempt_kind"] != authorization["attempt_kind"]
            ))
        ):
            invalid_analysis_attempts += 1
    invalid_analysis_results = 0
    for result in db.execute("SELECT * FROM analysis_results"):
        run = db.execute("SELECT * FROM analysis_runs WHERE id=?", (result["run_id"],)).fetchone()
        attempt = db.execute(
            "SELECT * FROM analysis_attempts WHERE id=? AND run_id=?",
            (result["attempt_id"], result["run_id"]),
        ).fetchone()
        try:
            output = json.loads(result["validated_output_json"])
            report = json.loads(result["validation_report_json"])
        except (TypeError, json.JSONDecodeError):
            output = report = None
        if (
            not run or not attempt or attempt["status"] not in {"succeeded", "refused"}
            or not isinstance(output, dict) or not isinstance(report, dict)
            or result["schema_version"] != run["output_schema_version"]
            or output.get("schema_version") != run["output_schema_version"]
            or output.get("subject") != {
                "type": run["subject_type"], "version_id": run["subject_version_id"]
            }
            or report.get("status") != "passed"
        ):
            invalid_analysis_results += 1
    for pointer in db.execute("SELECT * FROM analysis_publications"):
        publication = db.execute(
            "SELECT * FROM analysis_publication_versions WHERE id=?",
            (pointer["current_publication_id"],),
        ).fetchone()
        if (
            not publication
            or publication["subject_type"] != pointer["subject_type"]
            or publication["subject_version_id"] != pointer["subject_version_id"]
            or publication["task_type"] != pointer["task_type"]
        ):
            invalid_analysis_results += 1
            continue
        inputs = db.execute(
            "SELECT * FROM analysis_inputs WHERE run_id=? ORDER BY ordinal", (run["id"],)
        ).fetchall()
        subject_exists = (
            run["subject_version_id"] in document_version_ids
            if run["subject_type"] == "document"
            else run["subject_version_id"] in event_version_ids
        )
        includes_subject = any(
            item["document_version_id"] == run["subject_version_id"]
            if run["subject_type"] == "document"
            else item["event_version_id"] == run["subject_version_id"]
            for item in inputs
        )
        valid_inputs = bool(inputs)
        for ordinal, item in enumerate(inputs):
            valid_inputs = valid_inputs and item["ordinal"] == ordinal
            if item["document_version_id"] is not None:
                valid_inputs = valid_inputs and item["document_version_id"] in document_version_ids
                if item["evidence_id"] is not None:
                    valid_inputs = valid_inputs and bool(db.execute(
                        """SELECT 1 FROM document_version_inputs
                           WHERE version_id=? AND raw_record_id=?""",
                        (item["document_version_id"], item["evidence_id"]),
                    ).fetchone())
            else:
                valid_inputs = (
                    valid_inputs and item["event_version_id"] in event_version_ids
                    and item["evidence_id"] is None
                )
        manifest_hash = hashlib.sha256(
            run["input_manifest_json"].encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(manifest, dict) or not isinstance(parameters, dict)
            or manifest_hash != run["input_manifest_sha256"]
            or manifest.get("parameters") != parameters
            or not subject_exists or not includes_subject or not valid_inputs
        ):
            invalid_analysis_runs += 1
    if (
        invalid_events
        or invalid_event_links
        or invalid_event_evidence
        or invalid_match_decisions
        or invalid_link_decisions
        or invalid_event_relations
        or invalid_event_merges
        or invalid_event_splits
        or invalid_event_retractions
        or invalid_terminal_overlap
        or invalid_event_revisions
        or invalid_analysis_runs
        or invalid_analysis_attempts
        or invalid_analysis_results
    ):
        raise DatabaseVerificationError(
            "event projections are invalid: "
            f"events={invalid_events}, links={invalid_event_links}, "
            f"evidence={invalid_event_evidence}, "
            f"match_decisions={invalid_match_decisions}, "
            f"link_decisions={invalid_link_decisions}, "
            f"relations={invalid_event_relations}, merges={invalid_event_merges}, "
            f"splits={invalid_event_splits}, retractions={invalid_event_retractions}, "
            f"terminal_overlap={invalid_terminal_overlap}"
            f", revisions={invalid_event_revisions}"
            f", analysis_runs={invalid_analysis_runs}"
            f", analysis_attempts={invalid_analysis_attempts}"
            f", analysis_results={invalid_analysis_results}"
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
