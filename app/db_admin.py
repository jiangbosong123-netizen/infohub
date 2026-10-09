from __future__ import annotations

"""Versioned SQLite migration, verification and consistent backup tools."""

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from . import config, database
from .timeutil import format_utc, parse_utc, utc_now

_LOG = logging.getLogger(__name__)


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


LEGACY_TOPIC_BACKFILL_SCHEMA_SQL = """
CREATE TABLE legacy_topic_backfill_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    dataset_id TEXT NOT NULL,
    cutoff_item_id INTEGER NOT NULL CHECK(cutoff_item_id>=0),
    source_count INTEGER NOT NULL CHECK(source_count>=0),
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    last_item_id INTEGER NOT NULL DEFAULT 0 CHECK(last_item_id>=0),
    last_topic_slug TEXT NOT NULL DEFAULT '',
    processed_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_count>=0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error_detail TEXT
);
CREATE TABLE legacy_topic_assignment_snapshot (
    item_id INTEGER NOT NULL CHECK(item_id>0),
    topic_slug TEXT NOT NULL CHECK(length(topic_slug)>0),
    evidence_text TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
    document_version_id TEXT NOT NULL REFERENCES document_versions(id),
    topic_version_id TEXT NOT NULL REFERENCES topic_versions(id),
    captured_at TEXT NOT NULL,
    PRIMARY KEY(item_id,topic_slug)
);
CREATE TABLE legacy_topic_assignment_mappings (
    item_id INTEGER NOT NULL,
    topic_slug TEXT NOT NULL,
    assignment_id TEXT NOT NULL UNIQUE REFERENCES document_topic_assignments(id),
    evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
    available_at TEXT NOT NULL,
    PRIMARY KEY(item_id,topic_slug),
    FOREIGN KEY(item_id,topic_slug)
        REFERENCES legacy_topic_assignment_snapshot(item_id,topic_slug)
);
CREATE INDEX idx_legacy_topic_snapshot_topic
    ON legacy_topic_assignment_snapshot(topic_version_id,document_version_id);
CREATE TRIGGER legacy_topic_backfill_state_identity_immutable
BEFORE UPDATE ON legacy_topic_backfill_state
WHEN NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.cutoff_item_id IS NOT OLD.cutoff_item_id
  OR NEW.source_count IS NOT OLD.source_count
  OR NEW.source_sha256 IS NOT OLD.source_sha256
  OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT,'legacy topic backfill manifest is immutable'); END;
CREATE TRIGGER legacy_topic_backfill_state_no_delete
BEFORE DELETE ON legacy_topic_backfill_state
BEGIN SELECT RAISE(ABORT,'legacy topic backfill state cannot be deleted'); END;
CREATE TRIGGER legacy_topic_assignment_snapshot_no_update
BEFORE UPDATE ON legacy_topic_assignment_snapshot
BEGIN SELECT RAISE(ABORT,'legacy topic assignment snapshot is immutable'); END;
CREATE TRIGGER legacy_topic_assignment_snapshot_no_delete
BEFORE DELETE ON legacy_topic_assignment_snapshot
BEGIN SELECT RAISE(ABORT,'legacy topic assignment snapshot is immutable'); END;
CREATE TRIGGER legacy_topic_assignment_snapshot_no_late_insert
BEFORE INSERT ON legacy_topic_assignment_snapshot
WHEN EXISTS(SELECT 1 FROM legacy_topic_backfill_state WHERE singleton=1)
BEGIN SELECT RAISE(ABORT,'legacy topic assignment snapshot is already frozen'); END;
CREATE TRIGGER legacy_topic_assignment_mappings_no_update
BEFORE UPDATE ON legacy_topic_assignment_mappings
BEGIN SELECT RAISE(ABORT,'legacy topic assignment mappings are immutable'); END;
CREATE TRIGGER legacy_topic_assignment_mappings_no_delete
BEFORE DELETE ON legacy_topic_assignment_mappings
BEGIN SELECT RAISE(ABORT,'legacy topic assignment mappings are immutable'); END;
"""


def _legacy_topic_backfill_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, LEGACY_TOPIC_BACKFILL_SCHEMA_SQL)


TOPIC_ASSIGNMENT_REVIEW_SCHEMA_SQL = """
CREATE TABLE topic_assignment_reviews (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES document_topic_assignments(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_review_id TEXT REFERENCES topic_assignment_reviews(id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids_json)),
    reviewed_at TEXT NOT NULL,
    UNIQUE(assignment_id,version),
    CHECK((version=1 AND previous_review_id IS NULL)
       OR (version>1 AND previous_review_id IS NOT NULL))
);
CREATE INDEX idx_topic_assignment_reviews_current
    ON topic_assignment_reviews(assignment_id,version DESC);
CREATE TRIGGER topic_assignment_reviews_valid_append
BEFORE INSERT ON topic_assignment_reviews
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM topic_assignment_reviews
          WHERE assignment_id=NEW.assignment_id),1
     )
  OR (NEW.version=1 AND NEW.previous_review_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_review_id IS NOT (
         SELECT id FROM topic_assignment_reviews
         WHERE assignment_id=NEW.assignment_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'topic assignment reviews must form a contiguous append-only chain'); END;
CREATE TRIGGER topic_assignment_reviews_no_update
BEFORE UPDATE ON topic_assignment_reviews
BEGIN SELECT RAISE(ABORT,'topic assignment reviews are immutable'); END;
CREATE TRIGGER topic_assignment_reviews_no_delete
BEFORE DELETE ON topic_assignment_reviews
BEGIN SELECT RAISE(ABORT,'topic assignment reviews are immutable'); END;
"""


def _topic_assignment_review_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_ASSIGNMENT_REVIEW_SCHEMA_SQL)


TOPIC_STATISTICS_SCHEMA_SQL = """
CREATE TABLE topic_statistics_builds (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('building','ready','failed')),
    assignment_policy_version TEXT NOT NULL,
    event_policy_version TEXT NOT NULL,
    last_topic_id TEXT NOT NULL DEFAULT '',
    topic_count INTEGER NOT NULL DEFAULT 0 CHECK(topic_count>=0),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    error_detail TEXT
);
CREATE TABLE topic_statistics_versions (
    id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL REFERENCES topic_statistics_builds(id),
    topic_version_id TEXT NOT NULL REFERENCES topic_versions(id),
    document_count INTEGER NOT NULL CHECK(document_count>=0),
    event_count INTEGER NOT NULL CHECK(event_count>=0),
    input_manifest_sha256 TEXT NOT NULL CHECK(length(input_manifest_sha256)=64),
    counted_at TEXT NOT NULL,
    UNIQUE(build_id,topic_version_id)
);
CREATE TABLE topic_statistics_members (
    statistics_id TEXT NOT NULL REFERENCES topic_statistics_versions(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    member_type TEXT NOT NULL CHECK(member_type IN ('document','event')),
    resource_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
    member_sha256 TEXT NOT NULL CHECK(length(member_sha256)=64),
    PRIMARY KEY(statistics_id,ordinal),
    UNIQUE(statistics_id,member_type,resource_id)
);
CREATE INDEX idx_topic_statistics_versions_topic
    ON topic_statistics_versions(topic_version_id,build_id);
CREATE TABLE topic_statistics_publications (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE CHECK(version>0),
    build_id TEXT NOT NULL UNIQUE REFERENCES topic_statistics_builds(id),
    previous_publication_id TEXT REFERENCES topic_statistics_publications(id),
    published_at TEXT NOT NULL,
    CHECK((version=1 AND previous_publication_id IS NULL)
       OR (version>1 AND previous_publication_id IS NOT NULL))
);
CREATE TABLE topic_statistics_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    status TEXT NOT NULL CHECK(status IN ('empty','ready')),
    current_build_id TEXT REFERENCES topic_statistics_builds(id),
    current_publication_id TEXT REFERENCES topic_statistics_publications(id),
    updated_at TEXT NOT NULL,
    CHECK((status='empty' AND current_build_id IS NULL AND current_publication_id IS NULL)
       OR (status='ready' AND current_build_id IS NOT NULL AND current_publication_id IS NOT NULL))
);
CREATE TABLE topic_statistics_dirty (
    topic_id TEXT PRIMARY KEY REFERENCES topic_catalog(id),
    reason TEXT NOT NULL,
    queued_at TEXT NOT NULL
);
INSERT INTO topic_statistics_state(singleton,status,updated_at)
VALUES(1,'empty',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at)
SELECT id,'schema_initialize',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM topic_catalog;

CREATE TRIGGER topic_statistics_builds_identity_immutable BEFORE UPDATE ON topic_statistics_builds
WHEN NEW.id IS NOT OLD.id OR NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.assignment_policy_version IS NOT OLD.assignment_policy_version
  OR NEW.event_policy_version IS NOT OLD.event_policy_version
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT,'topic statistics build identity is immutable'); END;
CREATE TRIGGER topic_statistics_builds_valid_transition BEFORE UPDATE OF status ON topic_statistics_builds
WHEN NOT (OLD.status='building' AND NEW.status IN ('building','ready','failed'))
BEGIN SELECT RAISE(ABORT,'invalid topic statistics build transition'); END;
CREATE TRIGGER topic_statistics_builds_no_delete BEFORE DELETE ON topic_statistics_builds
BEGIN SELECT RAISE(ABORT,'topic statistics builds cannot be deleted'); END;
CREATE TRIGGER topic_statistics_versions_building_only BEFORE INSERT ON topic_statistics_versions
WHEN NOT EXISTS(SELECT 1 FROM topic_statistics_builds WHERE id=NEW.build_id AND status='building')
BEGIN SELECT RAISE(ABORT,'topic statistics can only be appended to a building run'); END;
CREATE TRIGGER topic_statistics_versions_no_update BEFORE UPDATE ON topic_statistics_versions
BEGIN SELECT RAISE(ABORT,'topic statistics versions are immutable'); END;
CREATE TRIGGER topic_statistics_versions_no_delete BEFORE DELETE ON topic_statistics_versions
BEGIN SELECT RAISE(ABORT,'topic statistics versions are immutable'); END;
CREATE TRIGGER topic_statistics_members_building_only BEFORE INSERT ON topic_statistics_members
WHEN NOT EXISTS(
    SELECT 1 FROM topic_statistics_versions AS version
    JOIN topic_statistics_builds AS build ON build.id=version.build_id
    WHERE version.id=NEW.statistics_id AND build.status='building'
)
BEGIN SELECT RAISE(ABORT,'topic statistic members can only be appended to a building run'); END;
CREATE TRIGGER topic_statistics_members_no_update BEFORE UPDATE ON topic_statistics_members
BEGIN SELECT RAISE(ABORT,'topic statistic members are immutable'); END;
CREATE TRIGGER topic_statistics_members_no_delete BEFORE DELETE ON topic_statistics_members
BEGIN SELECT RAISE(ABORT,'topic statistic members are immutable'); END;
CREATE TRIGGER topic_statistics_publications_valid_append BEFORE INSERT ON topic_statistics_publications
WHEN NEW.version != COALESCE((SELECT MAX(version)+1 FROM topic_statistics_publications),1)
  OR (NEW.version=1 AND NEW.previous_publication_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_publication_id IS NOT (
      SELECT id FROM topic_statistics_publications WHERE version=NEW.version-1))
  OR NOT EXISTS(SELECT 1 FROM topic_statistics_builds WHERE id=NEW.build_id AND status='ready')
BEGIN SELECT RAISE(ABORT,'topic statistics publications must append a ready build'); END;
CREATE TRIGGER topic_statistics_publications_no_update BEFORE UPDATE ON topic_statistics_publications
BEGIN SELECT RAISE(ABORT,'topic statistics publications are immutable'); END;
CREATE TRIGGER topic_statistics_publications_no_delete BEFORE DELETE ON topic_statistics_publications
BEGIN SELECT RAISE(ABORT,'topic statistics publications are immutable'); END;
CREATE TRIGGER topic_statistics_state_ready_build BEFORE UPDATE OF status,current_build_id,current_publication_id
ON topic_statistics_state WHEN NEW.status='ready' AND NOT EXISTS(
    SELECT 1 FROM topic_statistics_publications AS publication
    JOIN topic_statistics_builds AS build ON build.id=publication.build_id
    WHERE publication.id=NEW.current_publication_id
      AND publication.build_id=NEW.current_build_id AND build.status='ready'
)
BEGIN SELECT RAISE(ABORT,'current topic statistics publication must reference the ready build'); END;

CREATE TRIGGER topic_statistics_topic_insert AFTER INSERT ON topic_catalog BEGIN
  INSERT OR REPLACE INTO topic_statistics_dirty(topic_id,reason,queued_at)
  VALUES(NEW.id,'topic_insert',NEW.created_at);
END;
CREATE TRIGGER topic_statistics_topic_update AFTER UPDATE OF current_version_id,status ON topic_catalog BEGIN
  INSERT OR REPLACE INTO topic_statistics_dirty(topic_id,reason,queued_at)
  VALUES(NEW.id,'topic_update',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END;
CREATE TRIGGER topic_statistics_assignment_insert AFTER INSERT ON document_topic_assignments BEGIN
  INSERT OR REPLACE INTO topic_statistics_dirty(topic_id,reason,queued_at)
  SELECT topic_id,'assignment_insert',NEW.available_at FROM topic_versions WHERE id=NEW.topic_version_id;
END;
CREATE TRIGGER topic_statistics_review_insert AFTER INSERT ON topic_assignment_reviews BEGIN
  INSERT OR REPLACE INTO topic_statistics_dirty(topic_id,reason,queued_at)
  SELECT version.topic_id,'review_insert',NEW.reviewed_at
  FROM document_topic_assignments AS assignment
  JOIN topic_versions AS version ON version.id=assignment.topic_version_id
  WHERE assignment.id=NEW.assignment_id;
END;
CREATE TRIGGER topic_statistics_event_update AFTER UPDATE OF current_version_id,status ON events BEGIN
  INSERT OR REPLACE INTO topic_statistics_dirty(topic_id,reason,queued_at)
  SELECT DISTINCT version.topic_id,'event_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM topic_versions AS version
  WHERE version.id IN (
    SELECT value FROM event_versions AS event_version,json_each(event_version.topics_json)
    WHERE event_version.id IN (OLD.current_version_id,NEW.current_version_id)
  );
END;
"""


def _topic_statistics_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_STATISTICS_SCHEMA_SQL)


TOPIC_STATISTICS_ADMISSION_SCHEMA_SQL = """
CREATE TABLE topic_statistics_admission_reviews (
    id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES topic_statistics_publications(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_review_id TEXT REFERENCES topic_statistics_admission_reviews(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    minimum_decided_assignment_bps INTEGER NOT NULL
        CHECK(minimum_decided_assignment_bps BETWEEN 0 AND 10000),
    allow_zero_members INTEGER NOT NULL CHECK(allow_zero_members IN (0,1)),
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
    reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    reviewed_at TEXT NOT NULL,
    UNIQUE(publication_id,version),
    CHECK((version=1 AND previous_review_id IS NULL)
       OR (version>1 AND previous_review_id IS NOT NULL))
);
CREATE INDEX idx_topic_statistics_admission_current
    ON topic_statistics_admission_reviews(publication_id,version DESC);
CREATE TRIGGER topic_statistics_admission_valid_append
BEFORE INSERT ON topic_statistics_admission_reviews
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM topic_statistics_admission_reviews
          WHERE publication_id=NEW.publication_id),1
     )
  OR (NEW.version=1 AND NEW.previous_review_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_review_id IS NOT (
         SELECT id FROM topic_statistics_admission_reviews
         WHERE publication_id=NEW.publication_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'topic statistics admission reviews must form a contiguous append-only chain'); END;
CREATE TRIGGER topic_statistics_admission_no_update
BEFORE UPDATE ON topic_statistics_admission_reviews
BEGIN SELECT RAISE(ABORT,'topic statistics admission reviews are immutable'); END;
CREATE TRIGGER topic_statistics_admission_no_delete
BEFORE DELETE ON topic_statistics_admission_reviews
BEGIN SELECT RAISE(ABORT,'topic statistics admission reviews are immutable'); END;
"""


def _topic_statistics_admission_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_STATISTICS_ADMISSION_SCHEMA_SQL)


TOPIC_REVIEW_QUEUE_SCHEMA_SQL = """
CREATE TABLE topic_assignment_review_queue (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL UNIQUE REFERENCES document_topic_assignments(id)
);
INSERT INTO topic_assignment_review_queue(assignment_id)
SELECT id FROM document_topic_assignments ORDER BY rowid;
CREATE TRIGGER topic_assignment_review_queue_assignment_insert
AFTER INSERT ON document_topic_assignments
BEGIN
  INSERT INTO topic_assignment_review_queue(assignment_id) VALUES(NEW.id);
END;
CREATE TRIGGER topic_assignment_review_queue_no_update
BEFORE UPDATE ON topic_assignment_review_queue
BEGIN SELECT RAISE(ABORT,'topic assignment review queue is immutable'); END;
CREATE TRIGGER topic_assignment_review_queue_no_delete
BEFORE DELETE ON topic_assignment_review_queue
BEGIN SELECT RAISE(ABORT,'topic assignment review queue is immutable'); END;
"""


def _topic_review_queue_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_REVIEW_QUEUE_SCHEMA_SQL)


TOPIC_REVIEW_SAMPLING_SCHEMA_SQL = """
CREATE TABLE topic_assignment_review_order (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL UNIQUE REFERENCES topic_assignment_reviews(id)
);
INSERT INTO topic_assignment_review_order(review_id)
SELECT id FROM topic_assignment_reviews ORDER BY rowid;
CREATE TRIGGER topic_assignment_review_order_review_insert
AFTER INSERT ON topic_assignment_reviews
BEGIN
  INSERT INTO topic_assignment_review_order(review_id) VALUES(NEW.id);
END;
CREATE TRIGGER topic_assignment_review_order_no_update
BEFORE UPDATE ON topic_assignment_review_order
BEGIN SELECT RAISE(ABORT,'topic assignment review order is immutable'); END;
CREATE TRIGGER topic_assignment_review_order_no_delete
BEFORE DELETE ON topic_assignment_review_order
BEGIN SELECT RAISE(ABORT,'topic assignment review order is immutable'); END;

CREATE TABLE topic_review_sampling_batches (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    seed TEXT NOT NULL CHECK(length(trim(seed)) BETWEEN 1 AND 120),
    per_topic_limit INTEGER NOT NULL CHECK(per_topic_limit BETWEEN 1 AND 250),
    assignment_cutoff_sequence INTEGER NOT NULL CHECK(assignment_cutoff_sequence>=0),
    review_cutoff_sequence INTEGER NOT NULL CHECK(review_cutoff_sequence>=0),
    candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),
    topic_count INTEGER NOT NULL CHECK(topic_count>=0),
    member_count INTEGER NOT NULL CHECK(member_count>=0),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    created_by TEXT NOT NULL CHECK(length(trim(created_by)) BETWEEN 1 AND 120),
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id,seed,per_topic_limit,assignment_cutoff_sequence,
           review_cutoff_sequence,manifest_sha256)
);
CREATE TABLE topic_review_sampling_members (
    batch_id TEXT NOT NULL REFERENCES topic_review_sampling_batches(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    assignment_id TEXT NOT NULL REFERENCES document_topic_assignments(id),
    topic_id TEXT NOT NULL REFERENCES topic_catalog(id),
    queue_sequence INTEGER NOT NULL CHECK(queue_sequence>0),
    selection_sha256 TEXT NOT NULL CHECK(length(selection_sha256)=64),
    PRIMARY KEY(batch_id,ordinal),
    UNIQUE(batch_id,assignment_id)
);
CREATE INDEX idx_topic_review_sampling_members_topic
    ON topic_review_sampling_members(batch_id,topic_id,ordinal);
CREATE TRIGGER topic_review_sampling_batches_no_update
BEFORE UPDATE ON topic_review_sampling_batches
BEGIN SELECT RAISE(ABORT,'topic review sampling batches are immutable'); END;
CREATE TRIGGER topic_review_sampling_batches_no_delete
BEFORE DELETE ON topic_review_sampling_batches
BEGIN SELECT RAISE(ABORT,'topic review sampling batches are immutable'); END;
CREATE TRIGGER topic_review_sampling_members_no_update
BEFORE UPDATE ON topic_review_sampling_members
BEGIN SELECT RAISE(ABORT,'topic review sampling members are immutable'); END;
CREATE TRIGGER topic_review_sampling_members_no_delete
BEFORE DELETE ON topic_review_sampling_members
BEGIN SELECT RAISE(ABORT,'topic review sampling members are immutable'); END;
"""


def _topic_review_sampling_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_REVIEW_SAMPLING_SCHEMA_SQL)


TOPIC_REVIEW_SAMPLE_GATE_SCHEMA_SQL = """
CREATE TABLE topic_review_sample_evaluations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES topic_review_sampling_batches(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_evaluation_id TEXT REFERENCES topic_review_sample_evaluations(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    review_cutoff_sequence INTEGER NOT NULL CHECK(review_cutoff_sequence>=0),
    minimum_decided_bps INTEGER NOT NULL CHECK(minimum_decided_bps BETWEEN 1 AND 10000),
    minimum_topic_decided_bps INTEGER NOT NULL CHECK(minimum_topic_decided_bps BETWEEN 1 AND 10000),
    minimum_acceptance_bps INTEGER NOT NULL CHECK(minimum_acceptance_bps BETWEEN 1 AND 10000),
    minimum_topic_acceptance_bps INTEGER NOT NULL CHECK(minimum_topic_acceptance_bps BETWEEN 1 AND 10000),
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
    evaluator_id TEXT NOT NULL CHECK(length(trim(evaluator_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    evaluated_at TEXT NOT NULL,
    UNIQUE(batch_id,version),
    CHECK((version=1 AND previous_evaluation_id IS NULL)
       OR (version>1 AND previous_evaluation_id IS NOT NULL))
);
CREATE INDEX idx_topic_review_sample_evaluations_current
    ON topic_review_sample_evaluations(batch_id,version DESC);
CREATE TRIGGER topic_review_sample_evaluations_valid_append
BEFORE INSERT ON topic_review_sample_evaluations
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM topic_review_sample_evaluations
          WHERE batch_id=NEW.batch_id),1
     )
  OR (NEW.version=1 AND NEW.previous_evaluation_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_evaluation_id IS NOT (
         SELECT id FROM topic_review_sample_evaluations
         WHERE batch_id=NEW.batch_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'topic review sample evaluations must form a contiguous append-only chain'); END;
CREATE TRIGGER topic_review_sample_evaluations_no_update
BEFORE UPDATE ON topic_review_sample_evaluations
BEGIN SELECT RAISE(ABORT,'topic review sample evaluations are immutable'); END;
CREATE TRIGGER topic_review_sample_evaluations_no_delete
BEFORE DELETE ON topic_review_sample_evaluations
BEGIN SELECT RAISE(ABORT,'topic review sample evaluations are immutable'); END;
"""


def _topic_review_sample_gate_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_REVIEW_SAMPLE_GATE_SCHEMA_SQL)


TOPIC_STATISTICS_QUALITY_GATE_SCHEMA_SQL = """
ALTER TABLE topic_statistics_admission_reviews
ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'coverage-v1'
CHECK(policy_version IN ('coverage-v1','sample-gated-v2'));
ALTER TABLE topic_statistics_admission_reviews
ADD COLUMN sample_evaluation_id TEXT REFERENCES topic_review_sample_evaluations(id);
ALTER TABLE topic_statistics_admission_reviews
ADD COLUMN sample_metrics_sha256 TEXT
CHECK(sample_metrics_sha256 IS NULL OR length(sample_metrics_sha256)=64);
"""


def _topic_statistics_quality_gate_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TOPIC_STATISTICS_QUALITY_GATE_SCHEMA_SQL)


EVENT_ADMISSION_SCHEMA_SQL = """
CREATE TABLE event_admission_reviews (
    id TEXT PRIMARY KEY,
    event_version_id TEXT NOT NULL REFERENCES event_versions(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_review_id TEXT REFERENCES event_admission_reviews(id),
    decision TEXT NOT NULL CHECK(decision IN (
        'reported','corroborated','confirmed','rejected'
    )),
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
    reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    reviewed_at TEXT NOT NULL,
    policy_version TEXT NOT NULL CHECK(policy_version='event-admission-v1'),
    UNIQUE(event_version_id,version),
    CHECK((version=1 AND previous_review_id IS NULL)
       OR (version>1 AND previous_review_id IS NOT NULL))
);
CREATE INDEX idx_event_admission_reviews_current
    ON event_admission_reviews(event_version_id,version DESC);
CREATE TRIGGER event_admission_reviews_valid_append
BEFORE INSERT ON event_admission_reviews
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM event_admission_reviews
          WHERE event_version_id=NEW.event_version_id),1
     )
  OR (NEW.version=1 AND NEW.previous_review_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_review_id IS NOT (
         SELECT id FROM event_admission_reviews
         WHERE event_version_id=NEW.event_version_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'event admission reviews must form a contiguous append-only chain'); END;
CREATE TRIGGER event_admission_reviews_no_update
BEFORE UPDATE ON event_admission_reviews
BEGIN SELECT RAISE(ABORT,'event admission reviews are immutable'); END;
CREATE TRIGGER event_admission_reviews_no_delete
BEFORE DELETE ON event_admission_reviews
BEGIN SELECT RAISE(ABORT,'event admission reviews are immutable'); END;
"""


def _event_admission_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_ADMISSION_SCHEMA_SQL)


EVENT_MATCH_REVIEW_SCHEMA_SQL = """
CREATE TABLE event_match_review_queue (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE REFERENCES match_decisions(id)
);
INSERT INTO event_match_review_queue(decision_id)
SELECT id FROM match_decisions ORDER BY rowid;
CREATE TRIGGER event_match_review_queue_decision_insert
AFTER INSERT ON match_decisions
BEGIN
  INSERT INTO event_match_review_queue(decision_id) VALUES(NEW.id);
END;
CREATE TRIGGER event_match_review_queue_no_update
BEFORE UPDATE ON event_match_review_queue
BEGIN SELECT RAISE(ABORT,'event match review queue is immutable'); END;
CREATE TRIGGER event_match_review_queue_no_delete
BEFORE DELETE ON event_match_review_queue
BEGIN SELECT RAISE(ABORT,'event match review queue is immutable'); END;

CREATE TABLE event_match_reviews (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES match_decisions(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_review_id TEXT REFERENCES event_match_reviews(id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json)),
    reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    reviewed_at TEXT NOT NULL,
    UNIQUE(decision_id,version),
    CHECK((version=1 AND previous_review_id IS NULL)
       OR (version>1 AND previous_review_id IS NOT NULL))
);
CREATE INDEX idx_event_match_reviews_current
    ON event_match_reviews(decision_id,version DESC);
CREATE TRIGGER event_match_reviews_valid_append
BEFORE INSERT ON event_match_reviews
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM event_match_reviews
          WHERE decision_id=NEW.decision_id),1
     )
  OR (NEW.version=1 AND NEW.previous_review_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_review_id IS NOT (
         SELECT id FROM event_match_reviews
         WHERE decision_id=NEW.decision_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'event match reviews must form a contiguous append-only chain'); END;
CREATE TRIGGER event_match_reviews_no_update
BEFORE UPDATE ON event_match_reviews
BEGIN SELECT RAISE(ABORT,'event match reviews are immutable'); END;
CREATE TRIGGER event_match_reviews_no_delete
BEFORE DELETE ON event_match_reviews
BEGIN SELECT RAISE(ABORT,'event match reviews are immutable'); END;
"""


def _event_match_review_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_MATCH_REVIEW_SCHEMA_SQL)


EVENT_REVIEW_SAMPLING_SCHEMA_SQL = """
CREATE TABLE event_match_review_order (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL UNIQUE REFERENCES event_match_reviews(id)
);
INSERT INTO event_match_review_order(review_id)
SELECT id FROM event_match_reviews ORDER BY rowid;
CREATE TRIGGER event_match_review_order_review_insert
AFTER INSERT ON event_match_reviews
BEGIN
  INSERT INTO event_match_review_order(review_id) VALUES(NEW.id);
END;
CREATE TRIGGER event_match_review_order_no_update
BEFORE UPDATE ON event_match_review_order
BEGIN SELECT RAISE(ABORT,'event match review order is immutable'); END;
CREATE TRIGGER event_match_review_order_no_delete
BEFORE DELETE ON event_match_review_order
BEGIN SELECT RAISE(ABORT,'event match review order is immutable'); END;

CREATE TABLE event_review_sampling_batches (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    seed TEXT NOT NULL CHECK(length(trim(seed)) BETWEEN 1 AND 120),
    per_stratum_limit INTEGER NOT NULL CHECK(per_stratum_limit BETWEEN 1 AND 250),
    decision_cutoff_sequence INTEGER NOT NULL CHECK(decision_cutoff_sequence>=0),
    review_cutoff_sequence INTEGER NOT NULL CHECK(review_cutoff_sequence>=0),
    candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),
    stratum_count INTEGER NOT NULL CHECK(stratum_count>=0),
    member_count INTEGER NOT NULL CHECK(member_count>=0),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    created_by TEXT NOT NULL CHECK(length(trim(created_by)) BETWEEN 1 AND 120),
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id,seed,per_stratum_limit,decision_cutoff_sequence,
           review_cutoff_sequence,manifest_sha256)
);
CREATE TABLE event_review_sampling_members (
    batch_id TEXT NOT NULL REFERENCES event_review_sampling_batches(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    decision_id TEXT NOT NULL REFERENCES match_decisions(id),
    stratum_key TEXT NOT NULL,
    queue_sequence INTEGER NOT NULL CHECK(queue_sequence>0),
    selection_sha256 TEXT NOT NULL CHECK(length(selection_sha256)=64),
    PRIMARY KEY(batch_id,ordinal),
    UNIQUE(batch_id,decision_id)
);
CREATE INDEX idx_event_review_sampling_members_stratum
    ON event_review_sampling_members(batch_id,stratum_key,ordinal);
CREATE TRIGGER event_review_sampling_batches_no_update
BEFORE UPDATE ON event_review_sampling_batches
BEGIN SELECT RAISE(ABORT,'event review sampling batches are immutable'); END;
CREATE TRIGGER event_review_sampling_batches_no_delete
BEFORE DELETE ON event_review_sampling_batches
BEGIN SELECT RAISE(ABORT,'event review sampling batches are immutable'); END;
CREATE TRIGGER event_review_sampling_members_no_update
BEFORE UPDATE ON event_review_sampling_members
BEGIN SELECT RAISE(ABORT,'event review sampling members are immutable'); END;
CREATE TRIGGER event_review_sampling_members_no_delete
BEFORE DELETE ON event_review_sampling_members
BEGIN SELECT RAISE(ABORT,'event review sampling members are immutable'); END;
"""


def _event_review_sampling_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_REVIEW_SAMPLING_SCHEMA_SQL)


EVENT_REVIEW_SAMPLE_GATE_SCHEMA_SQL = """
CREATE TABLE event_review_sample_evaluations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES event_review_sampling_batches(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_evaluation_id TEXT REFERENCES event_review_sample_evaluations(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    review_cutoff_sequence INTEGER NOT NULL CHECK(review_cutoff_sequence>=0),
    minimum_decided_bps INTEGER NOT NULL CHECK(minimum_decided_bps BETWEEN 1 AND 10000),
    minimum_stratum_decided_bps INTEGER NOT NULL
        CHECK(minimum_stratum_decided_bps BETWEEN 1 AND 10000),
    minimum_acceptance_bps INTEGER NOT NULL
        CHECK(minimum_acceptance_bps BETWEEN 1 AND 10000),
    minimum_stratum_acceptance_bps INTEGER NOT NULL
        CHECK(minimum_stratum_acceptance_bps BETWEEN 1 AND 10000),
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
    evaluator_id TEXT NOT NULL CHECK(length(trim(evaluator_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    evaluated_at TEXT NOT NULL,
    UNIQUE(batch_id,version),
    CHECK((version=1 AND previous_evaluation_id IS NULL)
       OR (version>1 AND previous_evaluation_id IS NOT NULL))
);
CREATE INDEX idx_event_review_sample_evaluations_current
    ON event_review_sample_evaluations(batch_id,version DESC);
CREATE TRIGGER event_review_sample_evaluations_valid_append
BEFORE INSERT ON event_review_sample_evaluations
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM event_review_sample_evaluations
          WHERE batch_id=NEW.batch_id),1
     )
  OR (NEW.version=1 AND NEW.previous_evaluation_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_evaluation_id IS NOT (
         SELECT id FROM event_review_sample_evaluations
         WHERE batch_id=NEW.batch_id AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'event review sample evaluations must form a contiguous append-only chain'); END;
CREATE TRIGGER event_review_sample_evaluations_no_update
BEFORE UPDATE ON event_review_sample_evaluations
BEGIN SELECT RAISE(ABORT,'event review sample evaluations are immutable'); END;
CREATE TRIGGER event_review_sample_evaluations_no_delete
BEFORE DELETE ON event_review_sample_evaluations
BEGIN SELECT RAISE(ABORT,'event review sample evaluations are immutable'); END;
"""


def _event_review_sample_gate_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_REVIEW_SAMPLE_GATE_SCHEMA_SQL)


EVENT_DATASET_RELEASE_SCHEMA_SQL = """
CREATE TABLE event_dataset_release_reviews (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_epoch TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version>0),
    previous_review_id TEXT REFERENCES event_dataset_release_reviews(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    sample_evaluation_id TEXT NOT NULL REFERENCES event_review_sample_evaluations(id),
    sample_metrics_sha256 TEXT NOT NULL CHECK(length(sample_metrics_sha256)=64),
    event_manifest_json TEXT NOT NULL CHECK(json_valid(event_manifest_json)),
    event_manifest_sha256 TEXT NOT NULL CHECK(length(event_manifest_sha256)=64),
    metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
    reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id)) BETWEEN 1 AND 120),
    reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
    reviewed_at TEXT NOT NULL,
    policy_version TEXT NOT NULL CHECK(policy_version='event-release-v1'),
    UNIQUE(dataset_id,dataset_epoch,version),
    FOREIGN KEY(dataset_id,dataset_epoch)
        REFERENCES dataset_epochs(dataset_id,epoch),
    CHECK((version=1 AND previous_review_id IS NULL)
       OR (version>1 AND previous_review_id IS NOT NULL))
);
CREATE INDEX idx_event_dataset_release_reviews_current
    ON event_dataset_release_reviews(dataset_id,dataset_epoch,version DESC);
CREATE TRIGGER event_dataset_release_reviews_valid_append
BEFORE INSERT ON event_dataset_release_reviews
WHEN NEW.version != COALESCE(
         (SELECT MAX(version)+1 FROM event_dataset_release_reviews
          WHERE dataset_id=NEW.dataset_id AND dataset_epoch=NEW.dataset_epoch),1
     )
  OR (NEW.version=1 AND NEW.previous_review_id IS NOT NULL)
  OR (NEW.version>1 AND NEW.previous_review_id IS NOT (
         SELECT id FROM event_dataset_release_reviews
         WHERE dataset_id=NEW.dataset_id AND dataset_epoch=NEW.dataset_epoch
           AND version=NEW.version-1
     ))
BEGIN SELECT RAISE(ABORT,'event dataset release reviews must form a contiguous append-only chain'); END;
CREATE TRIGGER event_dataset_release_reviews_no_update
BEFORE UPDATE ON event_dataset_release_reviews
BEGIN SELECT RAISE(ABORT,'event dataset release reviews are immutable'); END;
CREATE TRIGGER event_dataset_release_reviews_no_delete
BEFORE DELETE ON event_dataset_release_reviews
BEGIN SELECT RAISE(ABORT,'event dataset release reviews are immutable'); END;
"""


def _event_dataset_release_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, EVENT_DATASET_RELEASE_SCHEMA_SQL)


SYNC_SNAPSHOT_SCHEMA_SQL = """
CREATE TABLE sync_snapshot_requests (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_epoch TEXT NOT NULL,
    consumer_id TEXT NOT NULL REFERENCES api_consumers(id),
    key_id TEXT NOT NULL REFERENCES api_keys(key_id),
    authz_version INTEGER NOT NULL CHECK(authz_version>0),
    idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) BETWEEN 1 AND 500),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
    resources_json TEXT NOT NULL CHECK(json_valid(resources_json)
        AND json_type(resources_json)='array'),
    scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json)
        AND json_type(scopes_json)='array'),
    projection_scope TEXT NOT NULL CHECK(projection_scope IN ('research','selected')),
    state TEXT NOT NULL CHECK(state IN ('pending','running','ready','failed')),
    job_id TEXT UNIQUE REFERENCES jobs(id),
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    expires_at TEXT NOT NULL,
    UNIQUE(key_id,authz_version,idempotency_key),
    FOREIGN KEY(dataset_id,dataset_epoch) REFERENCES dataset_epochs(dataset_id,epoch),
    CHECK((state='pending' AND started_at IS NULL AND finished_at IS NULL AND error_code IS NULL)
       OR (state='running' AND started_at IS NOT NULL AND finished_at IS NULL AND error_code IS NULL)
       OR (state='ready' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND error_code IS NULL)
       OR (state='failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL))
);
CREATE INDEX idx_sync_snapshot_requests_owner
    ON sync_snapshot_requests(key_id,authz_version,created_at DESC);
CREATE INDEX idx_sync_snapshot_requests_state
    ON sync_snapshot_requests(state,created_at);
CREATE TRIGGER sync_snapshot_requests_valid_manifest
BEFORE INSERT ON sync_snapshot_requests
WHEN json_array_length(NEW.resources_json)=0
  OR (SELECT COUNT(*) FROM json_each(NEW.resources_json))<>(
       SELECT COUNT(DISTINCT value) FROM json_each(NEW.resources_json))
  OR EXISTS(SELECT 1 FROM json_each(NEW.resources_json)
            WHERE type<>'text' OR value NOT IN (
                'items','events','entities','topics','sources','analyses','signals','reports','evidence'
            ))
  OR json_array_length(NEW.scopes_json)=0
  OR (SELECT COUNT(*) FROM json_each(NEW.scopes_json))<>(
       SELECT COUNT(DISTINCT value) FROM json_each(NEW.scopes_json))
  OR EXISTS(SELECT 1 FROM json_each(NEW.scopes_json)
            WHERE type<>'text' OR value NOT IN (
                'read:catalog','read:items','read:events','read:analyses','read:evidence',
                'read:signals','read:reports','read:sync','read:ops'
            ))
BEGIN SELECT RAISE(ABORT,'sync snapshot resource and scope manifests must be unique allowlists'); END;

CREATE TABLE sync_snapshots (
    id TEXT PRIMARY KEY REFERENCES sync_snapshot_requests(id),
    dataset_id TEXT NOT NULL,
    dataset_epoch TEXT NOT NULL,
    consumer_id TEXT NOT NULL REFERENCES api_consumers(id),
    key_id TEXT NOT NULL REFERENCES api_keys(key_id),
    authz_version INTEGER NOT NULL CHECK(authz_version>0),
    projection_scope TEXT NOT NULL CHECK(projection_scope IN ('research','selected')),
    high_water INTEGER NOT NULL CHECK(high_water>=0),
    knowledge_checkpoint_id TEXT NOT NULL REFERENCES knowledge_checkpoints(id),
    backup_sha256 TEXT NOT NULL CHECK(length(backup_sha256)=64),
    source_schema_version INTEGER NOT NULL CHECK(source_schema_version>0),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
    resource_count INTEGER NOT NULL CHECK(resource_count>0),
    record_count INTEGER NOT NULL CHECK(record_count>=0),
    snapshot_schema_version TEXT NOT NULL CHECK(snapshot_schema_version='sync-snapshot-v1'),
    created_at TEXT NOT NULL,
    ready_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(dataset_id,dataset_epoch) REFERENCES dataset_epochs(dataset_id,epoch)
);

CREATE TABLE sync_snapshot_resources (
    snapshot_id TEXT NOT NULL REFERENCES sync_snapshot_requests(id),
    resource TEXT NOT NULL CHECK(resource IN (
        'items','events','entities','topics','sources','analyses','signals','reports','evidence'
    )),
    record_count INTEGER NOT NULL CHECK(record_count>=0),
    page_count INTEGER NOT NULL CHECK(page_count>=0),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
    PRIMARY KEY(snapshot_id,resource),
    CHECK((record_count=0 AND page_count=0) OR (record_count>0 AND page_count>0))
);

CREATE TABLE sync_snapshot_pages (
    snapshot_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK(page_number>0),
    first_resource_id TEXT NOT NULL CHECK(length(first_resource_id)>0),
    last_resource_id TEXT NOT NULL CHECK(length(last_resource_id)>0),
    record_count INTEGER NOT NULL CHECK(record_count>0),
    payload_ref TEXT NOT NULL CHECK(length(payload_ref)>0),
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
    PRIMARY KEY(snapshot_id,resource,page_number),
    FOREIGN KEY(snapshot_id,resource)
        REFERENCES sync_snapshot_resources(snapshot_id,resource),
    CHECK(first_resource_id<=last_resource_id)
);

CREATE TRIGGER sync_snapshot_requests_valid_transition
BEFORE UPDATE ON sync_snapshot_requests
WHEN NEW.id IS NOT OLD.id
  OR NEW.dataset_id IS NOT OLD.dataset_id
  OR NEW.dataset_epoch IS NOT OLD.dataset_epoch
  OR NEW.consumer_id IS NOT OLD.consumer_id
  OR NEW.key_id IS NOT OLD.key_id
  OR NEW.authz_version IS NOT OLD.authz_version
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.request_json IS NOT OLD.request_json
  OR NEW.request_sha256 IS NOT OLD.request_sha256
  OR NEW.resources_json IS NOT OLD.resources_json
  OR NEW.scopes_json IS NOT OLD.scopes_json
  OR NEW.projection_scope IS NOT OLD.projection_scope
  OR NEW.job_id IS NOT OLD.job_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.expires_at IS NOT OLD.expires_at
  OR NOT ((OLD.state='pending' AND NEW.state IN ('running','failed'))
       OR (OLD.state='running' AND NEW.state IN ('ready','failed')))
BEGIN SELECT RAISE(ABORT,'invalid sync snapshot request transition'); END;
CREATE TRIGGER sync_snapshot_requests_ready_complete
BEFORE UPDATE OF state ON sync_snapshot_requests
WHEN NEW.state='ready' AND NOT EXISTS(
    SELECT 1 FROM sync_snapshots AS snapshot
    WHERE snapshot.id=NEW.id
      AND snapshot.dataset_id=NEW.dataset_id
      AND snapshot.dataset_epoch=NEW.dataset_epoch
      AND snapshot.consumer_id=NEW.consumer_id
      AND snapshot.key_id=NEW.key_id
      AND snapshot.authz_version=NEW.authz_version
      AND snapshot.projection_scope=NEW.projection_scope
      AND snapshot.expires_at=NEW.expires_at
      AND snapshot.resource_count=(
          SELECT COUNT(*) FROM sync_snapshot_resources WHERE snapshot_id=NEW.id)
      AND snapshot.record_count=COALESCE((
          SELECT SUM(record_count) FROM sync_snapshot_resources WHERE snapshot_id=NEW.id),0)
      AND NOT EXISTS(
          SELECT 1 FROM sync_snapshot_resources AS resource
          WHERE resource.snapshot_id=NEW.id
            AND (resource.page_count<>(SELECT COUNT(*) FROM sync_snapshot_pages AS page
                                      WHERE page.snapshot_id=resource.snapshot_id
                                        AND page.resource=resource.resource)
              OR resource.record_count<>COALESCE((
                    SELECT SUM(page.record_count) FROM sync_snapshot_pages AS page
                    WHERE page.snapshot_id=resource.snapshot_id
                      AND page.resource=resource.resource),0))
      )
)
BEGIN SELECT RAISE(ABORT,'ready sync snapshot must have a complete immutable manifest'); END;
CREATE TRIGGER sync_snapshot_requests_no_delete BEFORE DELETE ON sync_snapshot_requests
BEGIN SELECT RAISE(ABORT,'sync snapshot requests are retained'); END;

CREATE TRIGGER sync_snapshots_match_request
BEFORE INSERT ON sync_snapshots
WHEN NOT EXISTS(
    SELECT 1 FROM sync_snapshot_requests AS request
    WHERE request.id=NEW.id AND request.state='running'
      AND request.dataset_id=NEW.dataset_id AND request.dataset_epoch=NEW.dataset_epoch
      AND request.consumer_id=NEW.consumer_id AND request.key_id=NEW.key_id
      AND request.authz_version=NEW.authz_version
      AND request.projection_scope=NEW.projection_scope
      AND request.expires_at=NEW.expires_at
      AND NEW.resource_count=json_array_length(request.resources_json)
) OR NOT EXISTS(
    SELECT 1 FROM knowledge_checkpoints AS checkpoint
    WHERE checkpoint.id=NEW.knowledge_checkpoint_id
      AND checkpoint.dataset_id=NEW.dataset_id AND checkpoint.epoch=NEW.dataset_epoch
      AND checkpoint.high_water=NEW.high_water
) OR NEW.resource_count<>(
    SELECT COUNT(*) FROM sync_snapshot_resources WHERE snapshot_id=NEW.id
) OR NEW.record_count<>COALESCE((
    SELECT SUM(record_count) FROM sync_snapshot_resources WHERE snapshot_id=NEW.id
),0) OR EXISTS(
    SELECT 1 FROM sync_snapshot_resources AS resource
    WHERE resource.snapshot_id=NEW.id
      AND (resource.page_count<>(SELECT COUNT(*) FROM sync_snapshot_pages AS page
                                WHERE page.snapshot_id=resource.snapshot_id
                                  AND page.resource=resource.resource)
        OR resource.record_count<>COALESCE((
              SELECT SUM(page.record_count) FROM sync_snapshot_pages AS page
              WHERE page.snapshot_id=resource.snapshot_id
                AND page.resource=resource.resource),0))
)
BEGIN SELECT RAISE(ABORT,'sync snapshot must match its running request and checkpoint'); END;
CREATE TRIGGER sync_snapshots_no_update BEFORE UPDATE ON sync_snapshots
BEGIN SELECT RAISE(ABORT,'sync snapshots are immutable'); END;
CREATE TRIGGER sync_snapshots_no_delete BEFORE DELETE ON sync_snapshots
BEGIN SELECT RAISE(ABORT,'sync snapshots are immutable'); END;

CREATE TRIGGER sync_snapshot_resources_running_only
BEFORE INSERT ON sync_snapshot_resources
WHEN NOT EXISTS(
    SELECT 1 FROM sync_snapshot_requests AS request
    WHERE request.id=NEW.snapshot_id AND request.state='running'
      AND EXISTS(SELECT 1 FROM json_each(request.resources_json)
                 WHERE value=NEW.resource)
)
BEGIN SELECT RAISE(ABORT,'sync snapshot resources require a running request'); END;
CREATE TRIGGER sync_snapshot_resources_no_update BEFORE UPDATE ON sync_snapshot_resources
BEGIN SELECT RAISE(ABORT,'sync snapshot resources are immutable'); END;
CREATE TRIGGER sync_snapshot_resources_no_delete BEFORE DELETE ON sync_snapshot_resources
BEGIN SELECT RAISE(ABORT,'sync snapshot resources are immutable'); END;

CREATE TRIGGER sync_snapshot_pages_running_only
BEFORE INSERT ON sync_snapshot_pages
WHEN NOT EXISTS(SELECT 1 FROM sync_snapshot_requests
                WHERE id=NEW.snapshot_id AND state='running')
BEGIN SELECT RAISE(ABORT,'sync snapshot pages require a running request'); END;
CREATE TRIGGER sync_snapshot_pages_no_update BEFORE UPDATE ON sync_snapshot_pages
BEGIN SELECT RAISE(ABORT,'sync snapshot pages are immutable'); END;
CREATE TRIGGER sync_snapshot_pages_no_delete BEFORE DELETE ON sync_snapshot_pages
BEGIN SELECT RAISE(ABORT,'sync snapshot pages are immutable'); END;
"""


def _sync_snapshot_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, SYNC_SNAPSHOT_SCHEMA_SQL)


TONE_RELEASE_ADMISSION_SCHEMA_SQL = """
CREATE TABLE tone_release_admissions (
    id TEXT PRIMARY KEY,
    record_version TEXT NOT NULL CHECK(record_version='tone-release-decision-record-v1'),
    decision_id TEXT NOT NULL UNIQUE,
    bundle_id TEXT NOT NULL UNIQUE,
    bundle_sha256 TEXT NOT NULL CHECK(length(bundle_sha256)=64),
    registry_version TEXT NOT NULL,
    registry_sha256 TEXT NOT NULL CHECK(length(registry_sha256)=64),
    dataset_version TEXT NOT NULL,
    baseline_test_run_id TEXT NOT NULL,
    candidate_dev_run_id TEXT NOT NULL,
    candidate_test_run_id TEXT NOT NULL,
    candidate_security_run_id TEXT NOT NULL,
    calibration_version TEXT NOT NULL,
    operational_run_id TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    decided_at TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    approval_candidate INTEGER NOT NULL CHECK(approval_candidate IN (0,1)),
    record_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL UNIQUE CHECK(length(record_sha256)=64),
    bundle_json TEXT NOT NULL,
    imported_by TEXT NOT NULL CHECK(length(trim(imported_by))>0),
    imported_at TEXT NOT NULL,
    CHECK((decision='approve' AND approval_candidate=1)
       OR (decision='reject' AND approval_candidate=0))
);
CREATE INDEX idx_tone_release_admissions_dataset
    ON tone_release_admissions(dataset_version, imported_at);
CREATE TRIGGER tone_release_admissions_valid_insert
BEFORE INSERT ON tone_release_admissions
WHEN NOT json_valid(NEW.record_json) OR NOT json_valid(NEW.bundle_json)
  OR json_extract(NEW.record_json,'$.record_id')<>NEW.id
  OR json_extract(NEW.record_json,'$.decision_id')<>NEW.decision_id
  OR json_extract(NEW.record_json,'$.bundle_id')<>NEW.bundle_id
  OR json_extract(NEW.record_json,'$.decision')<>NEW.decision
  OR json_extract(NEW.bundle_json,'$.bundle_id')<>NEW.bundle_id
  OR json_extract(NEW.bundle_json,'$.dataset_version')<>NEW.dataset_version
BEGIN SELECT RAISE(ABORT,'tone release admission snapshot mismatch'); END;
CREATE TRIGGER tone_release_admissions_no_update
BEFORE UPDATE ON tone_release_admissions
BEGIN SELECT RAISE(ABORT,'tone release admissions are immutable'); END;
CREATE TRIGGER tone_release_admissions_no_delete
BEFORE DELETE ON tone_release_admissions
BEGIN SELECT RAISE(ABORT,'tone release admissions are immutable'); END;
"""


def _tone_release_admission_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TONE_RELEASE_ADMISSION_SCHEMA_SQL)


TONE_SHADOW_ROLLOUT_SCHEMA_SQL = """
CREATE TABLE tone_shadow_rollouts (
    id TEXT PRIMARY KEY,
    admission_id TEXT NOT NULL UNIQUE REFERENCES tone_release_admissions(id),
    rollout_schema_version TEXT NOT NULL CHECK(
        rollout_schema_version='tone-shadow-rollout-v1'),
    dataset_version TEXT NOT NULL,
    candidate_test_run_id TEXT NOT NULL,
    calibration_version TEXT NOT NULL,
    sample_bps INTEGER NOT NULL CHECK(sample_bps BETWEEN 1 AND 10000),
    config_json TEXT NOT NULL,
    config_sha256 TEXT NOT NULL UNIQUE CHECK(length(config_sha256)=64),
    created_by TEXT NOT NULL CHECK(length(trim(created_by))>0),
    created_at TEXT NOT NULL
);
CREATE TABLE tone_shadow_rollout_transitions (
    id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL REFERENCES tone_shadow_rollouts(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_transition_id TEXT UNIQUE REFERENCES tone_shadow_rollout_transitions(id),
    from_state TEXT CHECK(from_state IS NULL OR from_state IN (
        'planned','running','paused','completed','aborted')),
    to_state TEXT NOT NULL CHECK(to_state IN (
        'planned','running','paused','completed','aborted')),
    actor TEXT NOT NULL CHECK(length(trim(actor))>0),
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    occurred_at TEXT NOT NULL,
    UNIQUE(rollout_id,version)
);
CREATE INDEX idx_tone_shadow_rollout_transitions_latest
    ON tone_shadow_rollout_transitions(rollout_id,version DESC);
CREATE TRIGGER tone_shadow_rollouts_valid_insert
BEFORE INSERT ON tone_shadow_rollouts
WHEN NOT json_valid(NEW.config_json)
  OR NOT EXISTS(
      SELECT 1 FROM tone_release_admissions AS admission
      WHERE admission.id=NEW.admission_id AND admission.decision='approve'
        AND admission.approval_candidate=1
        AND admission.dataset_version=NEW.dataset_version
        AND admission.candidate_test_run_id=NEW.candidate_test_run_id
        AND admission.calibration_version=NEW.calibration_version)
BEGIN SELECT RAISE(ABORT,'tone shadow rollout requires matching approved admission'); END;
CREATE TRIGGER tone_shadow_rollouts_no_update BEFORE UPDATE ON tone_shadow_rollouts
BEGIN SELECT RAISE(ABORT,'tone shadow rollouts are immutable'); END;
CREATE TRIGGER tone_shadow_rollouts_no_delete BEFORE DELETE ON tone_shadow_rollouts
BEGIN SELECT RAISE(ABORT,'tone shadow rollouts are immutable'); END;
CREATE TRIGGER tone_shadow_rollout_transitions_valid_append
BEFORE INSERT ON tone_shadow_rollout_transitions
WHEN NOT (
    (NEW.version=1 AND NEW.previous_transition_id IS NULL
       AND NEW.from_state IS NULL AND NEW.to_state='planned'
       AND NOT EXISTS(SELECT 1 FROM tone_shadow_rollout_transitions
                      WHERE rollout_id=NEW.rollout_id))
    OR
    (NEW.version>1 AND NEW.previous_transition_id IS NOT NULL
       AND EXISTS(
           SELECT 1 FROM tone_shadow_rollout_transitions AS previous
           WHERE previous.id=NEW.previous_transition_id
             AND previous.rollout_id=NEW.rollout_id
             AND previous.version=NEW.version-1
             AND previous.to_state=NEW.from_state
             AND NOT EXISTS(
                 SELECT 1 FROM tone_shadow_rollout_transitions AS later
                 WHERE later.rollout_id=previous.rollout_id
                   AND later.version>previous.version))
       AND ((NEW.from_state='planned' AND NEW.to_state IN ('running','aborted'))
         OR (NEW.from_state='running' AND NEW.to_state IN ('paused','completed','aborted'))
         OR (NEW.from_state='paused' AND NEW.to_state IN ('running','aborted'))))
)
BEGIN SELECT RAISE(ABORT,'invalid tone shadow rollout transition'); END;
CREATE TRIGGER tone_shadow_rollout_transitions_no_update
BEFORE UPDATE ON tone_shadow_rollout_transitions
BEGIN SELECT RAISE(ABORT,'tone shadow rollout transitions are immutable'); END;
CREATE TRIGGER tone_shadow_rollout_transitions_no_delete
BEFORE DELETE ON tone_shadow_rollout_transitions
BEGIN SELECT RAISE(ABORT,'tone shadow rollout transitions are immutable'); END;
"""


def _tone_shadow_rollout_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TONE_SHADOW_ROLLOUT_SCHEMA_SQL)


TONE_SHADOW_OBSERVATION_SCHEMA_SQL = """
ALTER TABLE tone_shadow_rollout_transitions
    ADD COLUMN shadow_evaluation_id TEXT REFERENCES tone_shadow_evaluations(id);

CREATE TABLE tone_shadow_batches (
    id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL UNIQUE REFERENCES tone_shadow_rollouts(id),
    population_manifest_sha256 TEXT NOT NULL CHECK(length(population_manifest_sha256)=64),
    population_count INTEGER NOT NULL CHECK(population_count>0),
    selected_count INTEGER NOT NULL CHECK(selected_count>0 AND selected_count<=population_count),
    created_by TEXT NOT NULL CHECK(length(trim(created_by))>0),
    created_at TEXT NOT NULL
);
CREATE TABLE tone_shadow_batch_members (
    batch_id TEXT NOT NULL REFERENCES tone_shadow_batches(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    subject_version_id TEXT NOT NULL REFERENCES document_versions(id),
    selection_hash TEXT NOT NULL CHECK(length(selection_hash)=64),
    PRIMARY KEY(batch_id,subject_version_id),
    UNIQUE(batch_id,ordinal)
);
CREATE TABLE tone_shadow_population_members (
    batch_id TEXT NOT NULL REFERENCES tone_shadow_batches(id),
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    subject_version_id TEXT NOT NULL REFERENCES document_versions(id),
    selection_hash TEXT NOT NULL CHECK(length(selection_hash)=64),
    selected INTEGER NOT NULL CHECK(selected IN (0,1)),
    PRIMARY KEY(batch_id,subject_version_id),
    UNIQUE(batch_id,ordinal)
);
CREATE TABLE tone_shadow_observations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES tone_shadow_batches(id),
    subject_version_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('matched','disagreed','error')),
    candidate_result_sha256 TEXT CHECK(
        candidate_result_sha256 IS NULL OR length(candidate_result_sha256)=64),
    reference_result_sha256 TEXT CHECK(
        reference_result_sha256 IS NULL OR length(reference_result_sha256)=64),
    error_code TEXT,
    observed_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL CHECK(length(trim(recorded_by))>0),
    UNIQUE(batch_id,subject_version_id),
    FOREIGN KEY(batch_id,subject_version_id)
        REFERENCES tone_shadow_batch_members(batch_id,subject_version_id),
    CHECK((outcome IN ('matched','disagreed')
             AND candidate_result_sha256 IS NOT NULL
             AND reference_result_sha256 IS NOT NULL
             AND error_code IS NULL)
       OR (outcome='error' AND error_code IS NOT NULL))
);
CREATE TABLE tone_shadow_evaluations (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE REFERENCES tone_shadow_batches(id),
    decision TEXT NOT NULL CHECK(decision IN ('passed','failed')),
    metrics_json TEXT NOT NULL,
    metrics_sha256 TEXT NOT NULL CHECK(length(metrics_sha256)=64),
    evaluated_by TEXT NOT NULL CHECK(length(trim(evaluated_by))>0),
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    evaluated_at TEXT NOT NULL
);

CREATE TRIGGER tone_shadow_batches_running_only
BEFORE INSERT ON tone_shadow_batches
WHEN NOT EXISTS(
    SELECT 1 FROM tone_shadow_rollouts AS rollout
    JOIN tone_shadow_rollout_transitions AS transition
      ON transition.rollout_id=rollout.id
    WHERE rollout.id=NEW.rollout_id AND transition.to_state='running'
      AND NOT EXISTS(
          SELECT 1 FROM tone_shadow_rollout_transitions AS later
          WHERE later.rollout_id=transition.rollout_id
            AND later.version>transition.version))
BEGIN SELECT RAISE(ABORT,'tone shadow batch requires running rollout'); END;
CREATE TRIGGER tone_shadow_batches_no_update BEFORE UPDATE ON tone_shadow_batches
BEGIN SELECT RAISE(ABORT,'tone shadow batches are immutable'); END;
CREATE TRIGGER tone_shadow_batches_no_delete BEFORE DELETE ON tone_shadow_batches
BEGIN SELECT RAISE(ABORT,'tone shadow batches are immutable'); END;
CREATE TRIGGER tone_shadow_batch_members_no_update BEFORE UPDATE ON tone_shadow_batch_members
BEGIN SELECT RAISE(ABORT,'tone shadow batch members are immutable'); END;
CREATE TRIGGER tone_shadow_batch_members_no_delete BEFORE DELETE ON tone_shadow_batch_members
BEGIN SELECT RAISE(ABORT,'tone shadow batch members are immutable'); END;
CREATE TRIGGER tone_shadow_batch_members_no_late_insert
BEFORE INSERT ON tone_shadow_batch_members
WHEN (SELECT COUNT(*) FROM tone_shadow_batch_members WHERE batch_id=NEW.batch_id)
     >= (SELECT selected_count FROM tone_shadow_batches WHERE id=NEW.batch_id)
BEGIN SELECT RAISE(ABORT,'tone shadow batch manifest is complete'); END;
CREATE TRIGGER tone_shadow_batch_members_selected_only
BEFORE INSERT ON tone_shadow_batch_members
WHEN NOT EXISTS(
    SELECT 1 FROM tone_shadow_population_members AS population
    WHERE population.batch_id=NEW.batch_id
      AND population.subject_version_id=NEW.subject_version_id
      AND population.selection_hash=NEW.selection_hash
      AND population.selected=1)
BEGIN SELECT RAISE(ABORT,'tone shadow batch member is outside selected population'); END;
CREATE TRIGGER tone_shadow_population_members_no_update
BEFORE UPDATE ON tone_shadow_population_members
BEGIN SELECT RAISE(ABORT,'tone shadow population members are immutable'); END;
CREATE TRIGGER tone_shadow_population_members_no_delete
BEFORE DELETE ON tone_shadow_population_members
BEGIN SELECT RAISE(ABORT,'tone shadow population members are immutable'); END;
CREATE TRIGGER tone_shadow_population_members_no_late_insert
BEFORE INSERT ON tone_shadow_population_members
WHEN (SELECT COUNT(*) FROM tone_shadow_population_members WHERE batch_id=NEW.batch_id)
     >= (SELECT population_count FROM tone_shadow_batches WHERE id=NEW.batch_id)
BEGIN SELECT RAISE(ABORT,'tone shadow population manifest is complete'); END;
CREATE TRIGGER tone_shadow_observations_no_update BEFORE UPDATE ON tone_shadow_observations
BEGIN SELECT RAISE(ABORT,'tone shadow observations are immutable'); END;
CREATE TRIGGER tone_shadow_observations_no_delete BEFORE DELETE ON tone_shadow_observations
BEGIN SELECT RAISE(ABORT,'tone shadow observations are immutable'); END;
CREATE TRIGGER tone_shadow_observations_running_only
BEFORE INSERT ON tone_shadow_observations
WHEN NOT EXISTS(
    SELECT 1 FROM tone_shadow_batches AS batch
    JOIN tone_shadow_rollout_transitions AS transition
      ON transition.rollout_id=batch.rollout_id
    WHERE batch.id=NEW.batch_id AND transition.to_state='running'
      AND NOT EXISTS(
          SELECT 1 FROM tone_shadow_rollout_transitions AS later
          WHERE later.rollout_id=transition.rollout_id
            AND later.version>transition.version))
BEGIN SELECT RAISE(ABORT,'tone shadow observation requires running rollout'); END;
CREATE TRIGGER tone_shadow_evaluations_complete_insert
BEFORE INSERT ON tone_shadow_evaluations
WHEN NOT json_valid(NEW.metrics_json)
  OR (SELECT COUNT(*) FROM tone_shadow_batch_members WHERE batch_id=NEW.batch_id)
     <> (SELECT selected_count FROM tone_shadow_batches WHERE id=NEW.batch_id)
  OR (SELECT COUNT(*) FROM tone_shadow_population_members WHERE batch_id=NEW.batch_id)
     <> (SELECT population_count FROM tone_shadow_batches WHERE id=NEW.batch_id)
  OR (SELECT COUNT(*) FROM tone_shadow_observations WHERE batch_id=NEW.batch_id)
     <> (SELECT selected_count FROM tone_shadow_batches WHERE id=NEW.batch_id)
BEGIN SELECT RAISE(ABORT,'tone shadow evaluation requires complete batch observations'); END;
CREATE TRIGGER tone_shadow_evaluations_no_update BEFORE UPDATE ON tone_shadow_evaluations
BEGIN SELECT RAISE(ABORT,'tone shadow evaluations are immutable'); END;
CREATE TRIGGER tone_shadow_evaluations_no_delete BEFORE DELETE ON tone_shadow_evaluations
BEGIN SELECT RAISE(ABORT,'tone shadow evaluations are immutable'); END;

DROP TRIGGER tone_shadow_rollout_transitions_valid_append;
CREATE TRIGGER tone_shadow_rollout_transitions_valid_append
BEFORE INSERT ON tone_shadow_rollout_transitions
WHEN NOT (
    (NEW.version=1 AND NEW.previous_transition_id IS NULL
       AND NEW.from_state IS NULL AND NEW.to_state='planned'
       AND NEW.shadow_evaluation_id IS NULL
       AND NOT EXISTS(SELECT 1 FROM tone_shadow_rollout_transitions
                      WHERE rollout_id=NEW.rollout_id))
    OR
    (NEW.version>1 AND NEW.previous_transition_id IS NOT NULL
       AND EXISTS(
           SELECT 1 FROM tone_shadow_rollout_transitions AS previous
           WHERE previous.id=NEW.previous_transition_id
             AND previous.rollout_id=NEW.rollout_id
             AND previous.version=NEW.version-1
             AND previous.to_state=NEW.from_state
             AND NOT EXISTS(
                 SELECT 1 FROM tone_shadow_rollout_transitions AS later
                 WHERE later.rollout_id=previous.rollout_id
                   AND later.version>previous.version))
       AND ((NEW.from_state='planned' AND NEW.to_state IN ('running','aborted'))
         OR (NEW.from_state='running' AND NEW.to_state IN ('paused','aborted'))
         OR (NEW.from_state='running' AND NEW.to_state='completed'
             AND NEW.shadow_evaluation_id IS NOT NULL
             AND EXISTS(
                 SELECT 1 FROM tone_shadow_evaluations AS evaluation
                 JOIN tone_shadow_batches AS batch ON batch.id=evaluation.batch_id
                 WHERE evaluation.id=NEW.shadow_evaluation_id
                   AND evaluation.decision='passed'
                   AND batch.rollout_id=NEW.rollout_id))
         OR (NEW.from_state='paused' AND NEW.to_state IN ('running','aborted')))
       AND (NEW.to_state='completed' OR NEW.shadow_evaluation_id IS NULL))
)
BEGIN SELECT RAISE(ABORT,'invalid tone shadow rollout transition'); END;
"""


def _tone_shadow_observation_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TONE_SHADOW_OBSERVATION_SCHEMA_SQL)


TONE_RELEASE_ACTIVATION_SCHEMA_SQL = """
CREATE TABLE tone_release_activations (
    id TEXT PRIMARY KEY,
    rollout_id TEXT NOT NULL UNIQUE REFERENCES tone_shadow_rollouts(id),
    admission_id TEXT NOT NULL UNIQUE REFERENCES tone_release_admissions(id),
    shadow_evaluation_id TEXT NOT NULL UNIQUE REFERENCES tone_shadow_evaluations(id),
    profile_id TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
    profile_content_sha256 TEXT NOT NULL CHECK(length(profile_content_sha256)=64),
    candidate_run_json TEXT NOT NULL,
    candidate_run_sha256 TEXT NOT NULL CHECK(length(candidate_run_sha256)=64),
    candidate_run_content_sha256 TEXT NOT NULL CHECK(length(candidate_run_content_sha256)=64),
    runtime_config_json TEXT NOT NULL,
    runtime_config_sha256 TEXT NOT NULL CHECK(length(runtime_config_sha256)=64),
    activation_request_json TEXT NOT NULL,
    activation_request_sha256 TEXT NOT NULL UNIQUE CHECK(length(activation_request_sha256)=64),
    created_at TEXT NOT NULL
);
CREATE TABLE tone_release_activation_transitions (
    id TEXT PRIMARY KEY,
    activation_id TEXT NOT NULL REFERENCES tone_release_activations(id),
    version INTEGER NOT NULL CHECK(version>0),
    previous_transition_id TEXT UNIQUE REFERENCES tone_release_activation_transitions(id),
    from_state TEXT CHECK(from_state IN ('active','rolled_back')),
    to_state TEXT NOT NULL CHECK(to_state IN ('active','rolled_back')),
    actor TEXT NOT NULL CHECK(length(trim(actor))>0),
    reason TEXT NOT NULL CHECK(length(trim(reason))>0),
    occurred_at TEXT NOT NULL,
    registry_version TEXT NOT NULL CHECK(length(trim(registry_version))>0),
    registry_sha256 TEXT NOT NULL CHECK(length(registry_sha256)=64),
    authorization_json TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL UNIQUE CHECK(length(request_sha256)=64),
    UNIQUE(activation_id,version)
);
CREATE INDEX idx_tone_release_activation_state
    ON tone_release_activation_transitions(activation_id,version);

CREATE TRIGGER tone_release_activations_valid_insert
BEFORE INSERT ON tone_release_activations
WHEN NOT (
    json_valid(NEW.profile_json)
    AND json_valid(NEW.candidate_run_json)
    AND json_valid(NEW.runtime_config_json)
    AND json_valid(NEW.activation_request_json)
    AND EXISTS(
        SELECT 1 FROM tone_shadow_rollouts AS rollout
        JOIN tone_shadow_rollout_transitions AS transition
          ON transition.rollout_id=rollout.id
        JOIN tone_shadow_evaluations AS evaluation
          ON evaluation.id=transition.shadow_evaluation_id
        JOIN tone_shadow_batches AS batch ON batch.id=evaluation.batch_id
        WHERE rollout.id=NEW.rollout_id
          AND rollout.admission_id=NEW.admission_id
          AND transition.to_state='completed'
          AND transition.shadow_evaluation_id=NEW.shadow_evaluation_id
          AND evaluation.decision='passed'
          AND batch.rollout_id=rollout.id
          AND NOT EXISTS(
              SELECT 1 FROM tone_shadow_rollout_transitions AS later
              WHERE later.rollout_id=transition.rollout_id
                AND later.version>transition.version))
)
BEGIN SELECT RAISE(ABORT,'tone activation requires completed passed rollout'); END;
CREATE TRIGGER tone_release_activations_no_update
BEFORE UPDATE ON tone_release_activations
BEGIN SELECT RAISE(ABORT,'tone release activations are immutable'); END;
CREATE TRIGGER tone_release_activations_no_delete
BEFORE DELETE ON tone_release_activations
BEGIN SELECT RAISE(ABORT,'tone release activations are immutable'); END;
CREATE TRIGGER tone_release_activation_transitions_valid_append
BEFORE INSERT ON tone_release_activation_transitions
WHEN NOT (
    (NEW.version=1 AND NEW.previous_transition_id IS NULL
       AND NEW.from_state IS NULL AND NEW.to_state='active'
       AND NOT EXISTS(
           SELECT 1 FROM tone_release_activation_transitions
           WHERE activation_id=NEW.activation_id))
    OR
    (NEW.version=2 AND NEW.previous_transition_id IS NOT NULL
       AND NEW.from_state='active' AND NEW.to_state='rolled_back'
       AND EXISTS(
           SELECT 1 FROM tone_release_activation_transitions AS previous
           WHERE previous.id=NEW.previous_transition_id
             AND previous.activation_id=NEW.activation_id
             AND previous.version=1 AND previous.to_state='active'
             AND NOT EXISTS(
                 SELECT 1 FROM tone_release_activation_transitions AS later
                 WHERE later.activation_id=previous.activation_id
                   AND later.version>previous.version)))
)
BEGIN SELECT RAISE(ABORT,'invalid tone release activation transition'); END;
CREATE TRIGGER tone_release_activation_single_active
BEFORE INSERT ON tone_release_activation_transitions
WHEN NEW.to_state='active' AND EXISTS(
    SELECT 1 FROM tone_release_activation_transitions AS active
    WHERE active.to_state='active' AND active.activation_id<>NEW.activation_id
      AND NOT EXISTS(
          SELECT 1 FROM tone_release_activation_transitions AS later
          WHERE later.activation_id=active.activation_id
            AND later.version>active.version))
BEGIN SELECT RAISE(ABORT,'another tone release activation is active'); END;
CREATE TRIGGER tone_release_activation_transitions_no_update
BEFORE UPDATE ON tone_release_activation_transitions
BEGIN SELECT RAISE(ABORT,'tone release activation transitions are immutable'); END;
CREATE TRIGGER tone_release_activation_transitions_no_delete
BEFORE DELETE ON tone_release_activation_transitions
BEGIN SELECT RAISE(ABORT,'tone release activation transitions are immutable'); END;
"""


def _tone_release_activation_foundation(db: sqlite3.Connection) -> None:
    _execute_script(db, TONE_RELEASE_ACTIVATION_SCHEMA_SQL)


TONE_PUBLICATION_ACTIVATION_GATE_SQL = """
ALTER TABLE analysis_results
    ADD COLUMN tone_activation_id TEXT REFERENCES tone_release_activations(id);
CREATE INDEX idx_analysis_results_tone_activation
    ON analysis_results(tone_activation_id);
CREATE TRIGGER analysis_results_tone_activation_gate
BEFORE INSERT ON analysis_results
WHEN NOT (
    (NEW.tone_activation_id IS NULL AND NOT EXISTS(
        SELECT 1 FROM analysis_runs AS run
        WHERE run.id=NEW.run_id
          AND run.task_type='tone' AND NEW.result_status='valid'))
    OR
    (NEW.tone_activation_id IS NOT NULL AND EXISTS(
        SELECT 1
        FROM analysis_runs AS run
        JOIN tone_release_activations AS activation
          ON activation.id=NEW.tone_activation_id
        JOIN tone_release_activation_transitions AS transition
          ON transition.activation_id=activation.id
        WHERE run.id=NEW.run_id
          AND run.task_type='tone' AND NEW.result_status='valid'
          AND transition.to_state='active'
          AND NOT EXISTS(
              SELECT 1 FROM tone_release_activation_transitions AS later
              WHERE later.activation_id=transition.activation_id
                AND later.version>transition.version)
          AND json_valid(activation.runtime_config_json)
          AND json_extract(activation.runtime_config_json,'$.provider')=run.provider
          AND json_extract(activation.runtime_config_json,'$.requested_model')
              =run.requested_model
          AND json_extract(activation.runtime_config_json,'$.prompt_template_id')
              =run.prompt_template_id
          AND json_extract(activation.runtime_config_json,'$.prompt_sha256')
              =run.prompt_sha256
          AND json_extract(activation.runtime_config_json,'$.pipeline_version')
              =run.pipeline_version
          AND json(json_extract(activation.runtime_config_json,'$.parameters'))
              =json(run.parameters_json)
          AND json_extract(activation.runtime_config_json,'$.output_schema_version')
              =run.output_schema_version
          AND run.prepared_at>=activation.created_at
          AND NEW.created_at>=activation.created_at
          AND NEW.available_at>=activation.created_at
          AND json_valid(NEW.validated_output_json)
          AND json_extract(NEW.validated_output_json,'$.status')='valid'
          AND json_array_length(
              json_extract(NEW.validated_output_json,'$.data.assessments'))>0
          AND NOT EXISTS(
              SELECT 1 FROM json_each(
                  json_extract(NEW.validated_output_json,'$.data.assessments')) AS assessment
              WHERE json_type(
                        assessment.value,'$.confidence.calibrated_confidence') IS NULL
                 OR json_type(
                        assessment.value,'$.confidence.calibrated_confidence')
                        NOT IN ('integer','real')
                 OR json_extract(
                        assessment.value,'$.confidence.calibration_version')
                        IS NOT json_extract(
                            activation.runtime_config_json,'$.calibration_version'))))
)
BEGIN SELECT RAISE(ABORT,'valid tone publication requires active release'); END;
"""


def _tone_publication_activation_gate(db: sqlite3.Connection) -> None:
    _execute_script(db, TONE_PUBLICATION_ACTIVATION_GATE_SQL)


# claim_job orders claimable rows by priority first; idx_jobs_epoch_claim leads with state and
# next_attempt_at, so every claim sorted the whole backlog (93 ms per claim at 348k pending
# jobs in the cutover rehearsal). This partial index matches the WHERE and the full ORDER BY,
# so the first claimable row is read in index order. Claim order is unchanged.
JOB_CLAIM_ORDER_INDEX_SQL = """
CREATE INDEX idx_jobs_claim_order
    ON jobs(dataset_epoch, priority DESC, next_attempt_at, scheduled_for, created_at, id)
    WHERE state IN ('pending','retry_wait');
"""


def _job_claim_order_index(db: sqlite3.Connection) -> None:
    _execute_script(db, JOB_CLAIM_ORDER_INDEX_SQL)


# Two lookups ran on every publication and every model-call authorization but had no usable
# index, so their cost grew with all history: the idempotent change check by version_id scanned
# change_log, and the daily budget reservation sum scanned every authorization ever made. In the
# cutover rehearsal they cost 15 ms and 5 ms per job at 48k rows. Queries are unchanged.
PUBLICATION_LOOKUP_INDEX_SQL = """
CREATE INDEX idx_change_log_version ON change_log(version_id);
CREATE INDEX idx_analysis_authorizations_budget
    ON analysis_attempt_authorizations(provider, budget_day, decision, reserved_cost_microusd);
"""


def _publication_lookup_indexes(db: sqlite3.Connection) -> None:
    _execute_script(db, PUBLICATION_LOOKUP_INDEX_SQL)


# SQLite replaces a trigger statement's own OR REPLACE / OR IGNORE with the conflict policy of
# the statement that fired the trigger whenever that statement has one, and an UPSERT's
# DO UPDATE has one (ABORT). Superseding an analysis publication therefore failed with
# "UNIQUE constraint failed: curation_search_dirty.item_id" while the item was still queued,
# and an UPDATE OR IGNORE silently kept a stale queue reason. A trigger's own UPSERT clause is
# not overridden, so every queue trigger is rewritten with one; the queued rows are unchanged.
DIRTY_QUEUE_UPSERT_SQL = """
DROP TRIGGER items_derived_insert;
CREATE TRIGGER items_derived_insert AFTER INSERT ON items BEGIN
    INSERT INTO derived_dirty(item_id) VALUES(new.id) ON CONFLICT(item_id) DO NOTHING;
END;
DROP TRIGGER items_derived_update;
CREATE TRIGGER items_derived_update
AFTER UPDATE OF title,title_zh,summary,raw_summary,companies,score,tmt,event_type,
                ai_cat,official,extra,published_at,channel ON items BEGIN
    INSERT INTO derived_dirty(item_id) VALUES(new.id) ON CONFLICT(item_id) DO NOTHING;
END;
DROP TRIGGER curation_search_item_ai;
CREATE TRIGGER curation_search_item_ai AFTER INSERT ON items BEGIN
    INSERT INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.id,'item_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ON CONFLICT(item_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER curation_search_item_au;
CREATE TRIGGER curation_search_item_au AFTER UPDATE OF title,title_zh,summary,raw_summary,tmt ON items BEGIN
    INSERT INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.id,'item_update',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ON CONFLICT(item_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER curation_search_document_ai;
CREATE TRIGGER curation_search_document_ai AFTER INSERT ON documents BEGIN
    INSERT INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.legacy_item_id,'document_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ON CONFLICT(item_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER curation_search_document_au;
CREATE TRIGGER curation_search_document_au AFTER UPDATE OF current_version_id,status ON documents BEGIN
    INSERT INTO curation_search_dirty(item_id,reason,queued_at)
    VALUES(new.legacy_item_id,'document_update',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    ON CONFLICT(item_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER curation_search_publication_ai;
CREATE TRIGGER curation_search_publication_ai AFTER INSERT ON analysis_publications
WHEN new.subject_type='document' AND new.task_type IN ('translation','summarization','relevance') BEGIN
    INSERT INTO curation_search_dirty(item_id,reason,queued_at)
    SELECT d.legacy_item_id,'publication_insert',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM documents d WHERE d.current_version_id=new.subject_version_id
    ON CONFLICT(item_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER curation_search_publication_au;
CREATE TRIGGER curation_search_publication_au AFTER UPDATE OF current_publication_id ON analysis_publications
WHEN new.subject_type='document' AND new.task_type IN ('translation','summarization','relevance') BEGIN
    INSERT INTO curation_search_dirty(item_id,reason,queued_at)
    SELECT d.legacy_item_id,'publication_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
    FROM documents d WHERE d.current_version_id=new.subject_version_id
    ON CONFLICT(item_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER topic_statistics_topic_insert;
CREATE TRIGGER topic_statistics_topic_insert AFTER INSERT ON topic_catalog BEGIN
  INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at)
  VALUES(NEW.id,'topic_insert',NEW.created_at)
  ON CONFLICT(topic_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER topic_statistics_topic_update;
CREATE TRIGGER topic_statistics_topic_update AFTER UPDATE OF current_version_id,status ON topic_catalog BEGIN
  INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at)
  VALUES(NEW.id,'topic_update',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  ON CONFLICT(topic_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER topic_statistics_assignment_insert;
CREATE TRIGGER topic_statistics_assignment_insert AFTER INSERT ON document_topic_assignments BEGIN
  INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at)
  SELECT topic_id,'assignment_insert',NEW.available_at FROM topic_versions WHERE id=NEW.topic_version_id
  ON CONFLICT(topic_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER topic_statistics_review_insert;
CREATE TRIGGER topic_statistics_review_insert AFTER INSERT ON topic_assignment_reviews BEGIN
  INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at)
  SELECT version.topic_id,'review_insert',NEW.reviewed_at
  FROM document_topic_assignments AS assignment
  JOIN topic_versions AS version ON version.id=assignment.topic_version_id
  WHERE assignment.id=NEW.assignment_id
  ON CONFLICT(topic_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
DROP TRIGGER topic_statistics_event_update;
CREATE TRIGGER topic_statistics_event_update AFTER UPDATE OF current_version_id,status ON events BEGIN
  INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at)
  SELECT DISTINCT version.topic_id,'event_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM topic_versions AS version
  WHERE version.id IN (
    SELECT value FROM event_versions AS event_version,json_each(event_version.topics_json)
    WHERE event_version.id IN (OLD.current_version_id,NEW.current_version_id)
  )
  ON CONFLICT(topic_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
"""


def _dirty_queue_upsert(db: sqlite3.Connection) -> None:
    _execute_script(db, DIRTY_QUEUE_UPSERT_SQL)


# Every home page shows when the newest item was fetched; without an index SELECT
# MAX(fetched_at) scanned all items (about 22 ms of a 32 ms page on the rehearsal-size data).
ITEMS_FETCHED_INDEX_SQL = """
CREATE INDEX idx_items_fetched ON items(fetched_at);
"""


def _items_fetched_index(db: sqlite3.Connection) -> None:
    _execute_script(db, ITEMS_FETCHED_INDEX_SQL)


# "AFTER UPDATE OF col" fires whenever col appears in the SET list, changed or not. Every
# derived refresh rewrites title/channel/url/first_at/last_at of each story active in the
# last 14 days to refresh its decaying heat, so the hot-metrics queue received ~22,000
# stories per run on the rehearsal-size data and never drained, leaving the curated hot
# list permanently unusable. Story metrics only depend on values that actually change.
STORY_UPDATE_REAL_CHANGES_SQL = """
DROP TRIGGER curation_story_update;
CREATE TRIGGER curation_story_update
AFTER UPDATE OF anchor_item_id,title,url,channel,first_at,last_at,redirect_to ON stories
WHEN old.anchor_item_id IS NOT new.anchor_item_id OR old.title IS NOT new.title
  OR old.url IS NOT new.url OR old.channel IS NOT new.channel
  OR old.first_at IS NOT new.first_at OR old.last_at IS NOT new.last_at
  OR old.redirect_to IS NOT new.redirect_to BEGIN
    INSERT INTO curation_story_metrics_dirty(story_id,reason,queued_at)
    VALUES(new.id,'story_update',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(story_id) DO UPDATE SET reason=excluded.reason,queued_at=excluded.queued_at;
END;
"""


def _story_update_real_changes(db: sqlite3.Connection) -> None:
    _execute_script(db, STORY_UPDATE_REAL_CHANGES_SQL)


# With foreign keys on, deleting a job (and, by cascade, its attempts) makes SQLite look for
# rows referring to them. These two child keys had no index, so every deleted job meant a scan
# of every analysis run and every change.
JOB_CHILD_KEY_INDEX_SQL = """
CREATE INDEX idx_analysis_runs_job ON analysis_runs(job_id);
CREATE INDEX idx_change_log_lease ON change_log(lease_token);
"""


def _job_child_key_indexes(db: sqlite3.Connection) -> None:
    _execute_script(db, JOB_CHILD_KEY_INDEX_SQL)


# GET /api/v1/analyses/{id} and the tone checks in verification find a result's publication
# versions by result_id, which had no index: each lookup scanned every publication version
# (about 2.5 s per API request with 358k imported analyses).
PUBLICATION_RESULT_INDEX_SQL = """
CREATE INDEX idx_analysis_publication_versions_result ON analysis_publication_versions(result_id);
"""


def _publication_result_index(db: sqlite3.Connection) -> None:
    _execute_script(db, PUBLICATION_RESULT_INDEX_SQL)


# The health page asks each source when it last listed something new and how its recent new
# items were spaced (app/source_activity.py); without this index each question scanned every
# discovery.
SOURCE_DISCOVERY_INDEX_SQL = """
CREATE INDEX idx_item_discoveries_source_seen ON item_discoveries(source_id, first_seen_at);
"""


def _source_discovery_index(db: sqlite3.Connection) -> None:
    _execute_script(db, SOURCE_DISCOVERY_INDEX_SQL)


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
    Migration(26, "frozen resumable legacy topic assignment import",
              LEGACY_TOPIC_BACKFILL_SCHEMA_SQL, _legacy_topic_backfill_foundation),
    Migration(27, "append-only topic assignment review decisions",
              TOPIC_ASSIGNMENT_REVIEW_SCHEMA_SQL, _topic_assignment_review_foundation),
    Migration(28, "auditable topic statistics projection foundation",
              TOPIC_STATISTICS_SCHEMA_SQL, _topic_statistics_foundation),
    Migration(29, "append-only topic statistics publication admission",
              TOPIC_STATISTICS_ADMISSION_SCHEMA_SQL,
              _topic_statistics_admission_foundation),
    Migration(30, "stable topic assignment review queue",
              TOPIC_REVIEW_QUEUE_SCHEMA_SQL, _topic_review_queue_foundation),
    Migration(31, "immutable reproducible topic review sampling",
              TOPIC_REVIEW_SAMPLING_SCHEMA_SQL, _topic_review_sampling_foundation),
    Migration(32, "append-only topic review sample evaluation gate",
              TOPIC_REVIEW_SAMPLE_GATE_SCHEMA_SQL,
              _topic_review_sample_gate_foundation),
    Migration(33, "bind topic statistics admission to sampled quality",
              TOPIC_STATISTICS_QUALITY_GATE_SCHEMA_SQL,
              _topic_statistics_quality_gate_foundation),
    Migration(34, "append-only event publication admission",
              EVENT_ADMISSION_SCHEMA_SQL, _event_admission_foundation),
    Migration(35, "stable event match review queue and decisions",
              EVENT_MATCH_REVIEW_SCHEMA_SQL, _event_match_review_foundation),
    Migration(36, "immutable reproducible event match review sampling",
              EVENT_REVIEW_SAMPLING_SCHEMA_SQL, _event_review_sampling_foundation),
    Migration(37, "append-only event review sample evaluation gate",
              EVENT_REVIEW_SAMPLE_GATE_SCHEMA_SQL,
              _event_review_sample_gate_foundation),
    Migration(38, "append-only event dataset release gate",
              EVENT_DATASET_RELEASE_SCHEMA_SQL,
              _event_dataset_release_foundation),
    Migration(39, "immutable reliable-sync snapshot foundation",
              SYNC_SNAPSHOT_SCHEMA_SQL, _sync_snapshot_foundation),
    Migration(40, "append-only tone release admission ledger",
              TONE_RELEASE_ADMISSION_SCHEMA_SQL,
              _tone_release_admission_foundation),
    Migration(41, "controlled tone shadow rollout plan",
              TONE_SHADOW_ROLLOUT_SCHEMA_SQL,
              _tone_shadow_rollout_foundation),
    Migration(42, "complete tone shadow observation gate",
              TONE_SHADOW_OBSERVATION_SCHEMA_SQL,
              _tone_shadow_observation_foundation),
    Migration(43, "controlled tone release activation ledger",
              TONE_RELEASE_ACTIVATION_SCHEMA_SQL,
              _tone_release_activation_foundation),
    Migration(44, "bind valid tone publication to active release",
              TONE_PUBLICATION_ACTIVATION_GATE_SQL,
              _tone_publication_activation_gate),
    Migration(45, "ordered partial index for durable job claims",
              JOB_CLAIM_ORDER_INDEX_SQL, _job_claim_order_index),
    Migration(46, "indexes for publication idempotency and budget lookups",
              PUBLICATION_LOOKUP_INDEX_SQL, _publication_lookup_indexes),
    Migration(47, "queue triggers keep their own conflict handling",
              DIRTY_QUEUE_UPSERT_SQL, _dirty_queue_upsert),
    Migration(48, "index items by fetch time for the portal's last-update stamp",
              ITEMS_FETCHED_INDEX_SQL, _items_fetched_index),
    Migration(49, "queue story metrics only for real story changes",
              STORY_UPDATE_REAL_CHANGES_SQL, _story_update_real_changes),
    Migration(50, "index the job references so routine jobs can be pruned",
              JOB_CHILD_KEY_INDEX_SQL, _job_child_key_indexes),
    Migration(51, "index publication versions by result",
              PUBLICATION_RESULT_INDEX_SQL, _publication_result_index),
    Migration(52, "index item discoveries by source and first sighting",
              SOURCE_DISCOVERY_INDEX_SQL, _source_discovery_index),
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
    "legacy_topic_backfill_state", "legacy_topic_assignment_snapshot",
    "legacy_topic_assignment_mappings",
    "topic_assignment_reviews",
    "topic_statistics_builds", "topic_statistics_versions",
    "topic_statistics_members", "topic_statistics_publications",
    "topic_statistics_state", "topic_statistics_dirty",
    "topic_statistics_admission_reviews",
    "topic_assignment_review_queue",
    "topic_assignment_review_order", "topic_review_sampling_batches",
    "topic_review_sampling_members",
    "topic_review_sample_evaluations",
    "event_admission_reviews",
    "event_match_review_queue", "event_match_reviews",
    "event_match_review_order", "event_review_sampling_batches",
    "event_review_sampling_members",
    "event_review_sample_evaluations",
    "event_dataset_release_reviews",
    "sync_snapshot_requests", "sync_snapshots", "sync_snapshot_resources",
    "sync_snapshot_pages",
    "tone_release_admissions",
    "tone_shadow_rollouts", "tone_shadow_rollout_transitions",
    "tone_shadow_batches", "tone_shadow_batch_members",
    "tone_shadow_population_members",
    "tone_shadow_observations", "tone_shadow_evaluations",
    "tone_release_activations", "tone_release_activation_transitions",
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
    "legacy_topic_backfill_state": {
        "singleton", "dataset_id", "cutoff_item_id", "source_count",
        "source_sha256", "manifest_sha256", "status", "last_item_id",
        "last_topic_slug", "processed_count", "started_at", "updated_at",
        "finished_at", "error_detail",
    },
    "legacy_topic_assignment_snapshot": {
        "item_id", "topic_slug", "evidence_text", "evidence_sha256",
        "document_version_id", "topic_version_id", "captured_at",
    },
    "legacy_topic_assignment_mappings": {
        "item_id", "topic_slug", "assignment_id", "evidence_sha256", "available_at",
    },
    "topic_assignment_reviews": {
        "id", "assignment_id", "version", "previous_review_id", "decision",
        "reviewer_id", "reason", "evidence_ids_json", "reviewed_at",
    },
    "topic_statistics_builds": {
        "id", "dataset_id", "status", "assignment_policy_version",
        "event_policy_version", "last_topic_id", "topic_count", "started_at",
        "updated_at", "finished_at", "error_detail",
    },
    "topic_statistics_versions": {
        "id", "build_id", "topic_version_id", "document_count", "event_count",
        "input_manifest_sha256", "counted_at",
    },
    "topic_statistics_members": {
        "statistics_id", "ordinal", "member_type", "resource_id", "version_id",
        "provenance_json", "member_sha256",
    },
    "topic_statistics_publications": {
        "id", "version", "build_id", "previous_publication_id", "published_at",
    },
    "topic_statistics_state": {
        "singleton", "status", "current_build_id", "current_publication_id", "updated_at",
    },
    "topic_statistics_dirty": {"topic_id", "reason", "queued_at"},
    "topic_statistics_admission_reviews": {
        "id", "publication_id", "version", "previous_review_id", "decision",
        "minimum_decided_assignment_bps", "allow_zero_members", "metrics_json",
        "metrics_sha256", "reviewer_id", "reason", "reviewed_at",
        "policy_version", "sample_evaluation_id", "sample_metrics_sha256",
    },
    "topic_assignment_review_queue": {"sequence", "assignment_id"},
    "topic_assignment_review_order": {"sequence", "review_id"},
    "topic_review_sampling_batches": {
        "id", "dataset_id", "seed", "per_topic_limit",
        "assignment_cutoff_sequence", "review_cutoff_sequence",
        "candidate_count", "topic_count", "member_count", "manifest_sha256",
        "created_by", "created_at",
    },
    "topic_review_sampling_members": {
        "batch_id", "ordinal", "assignment_id", "topic_id", "queue_sequence",
        "selection_sha256",
    },
    "topic_review_sample_evaluations": {
        "id", "batch_id", "version", "previous_evaluation_id", "decision",
        "review_cutoff_sequence",
        "minimum_decided_bps", "minimum_topic_decided_bps",
        "minimum_acceptance_bps", "minimum_topic_acceptance_bps",
        "metrics_json", "metrics_sha256", "evaluator_id", "reason", "evaluated_at",
    },
    "event_admission_reviews": {
        "id", "event_version_id", "version", "previous_review_id", "decision",
        "metrics_json", "metrics_sha256", "reviewer_id", "reason", "reviewed_at",
        "policy_version",
    },
    "event_match_review_queue": {"sequence", "decision_id"},
    "event_match_reviews": {
        "id", "decision_id", "version", "previous_review_id", "decision",
        "evidence_ids_json", "reviewer_id", "reason", "reviewed_at",
    },
    "event_match_review_order": {"sequence", "review_id"},
    "event_review_sampling_batches": {
        "id", "dataset_id", "seed", "per_stratum_limit",
        "decision_cutoff_sequence", "review_cutoff_sequence", "candidate_count",
        "stratum_count", "member_count", "manifest_sha256", "created_by", "created_at",
    },
    "event_review_sampling_members": {
        "batch_id", "ordinal", "decision_id", "stratum_key", "queue_sequence",
        "selection_sha256",
    },
    "event_review_sample_evaluations": {
        "id", "batch_id", "version", "previous_evaluation_id", "decision",
        "review_cutoff_sequence", "minimum_decided_bps",
        "minimum_stratum_decided_bps", "minimum_acceptance_bps",
        "minimum_stratum_acceptance_bps", "metrics_json", "metrics_sha256",
        "evaluator_id", "reason", "evaluated_at",
    },
    "event_dataset_release_reviews": {
        "id", "dataset_id", "dataset_epoch", "version", "previous_review_id",
        "decision", "sample_evaluation_id", "sample_metrics_sha256",
        "event_manifest_json", "event_manifest_sha256", "metrics_json",
        "metrics_sha256", "reviewer_id", "reason", "reviewed_at", "policy_version",
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
        "result_status", "created_at", "available_at", "tone_activation_id",
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
    "sync_snapshot_requests": {
        "id", "dataset_id", "dataset_epoch", "consumer_id", "key_id",
        "authz_version", "idempotency_key", "request_json", "request_sha256",
        "resources_json", "scopes_json", "projection_scope", "state", "job_id",
        "error_code", "created_at", "started_at", "finished_at", "expires_at",
    },
    "sync_snapshots": {
        "id", "dataset_id", "dataset_epoch", "consumer_id", "key_id",
        "authz_version", "projection_scope", "high_water", "knowledge_checkpoint_id",
        "backup_sha256", "source_schema_version", "manifest_json", "manifest_sha256",
        "resource_count", "record_count", "snapshot_schema_version", "created_at",
        "ready_at", "expires_at",
    },
    "sync_snapshot_resources": {
        "snapshot_id", "resource", "record_count", "page_count", "content_sha256",
    },
    "sync_snapshot_pages": {
        "snapshot_id", "resource", "page_number", "first_resource_id",
        "last_resource_id", "record_count", "payload_ref", "payload_sha256",
        "size_bytes",
    },
    "tone_release_admissions": {
        "id", "record_version", "decision_id", "bundle_id", "bundle_sha256",
        "registry_version", "registry_sha256", "dataset_version",
        "baseline_test_run_id", "candidate_dev_run_id", "candidate_test_run_id",
        "candidate_security_run_id", "calibration_version", "operational_run_id",
        "approver_id", "decision", "reason", "decided_at", "policy_id",
        "approval_candidate", "record_json", "record_sha256", "bundle_json",
        "imported_by", "imported_at",
    },
    "tone_shadow_rollouts": {
        "id", "admission_id", "rollout_schema_version", "dataset_version",
        "candidate_test_run_id", "calibration_version", "sample_bps",
        "config_json", "config_sha256", "created_by", "created_at",
    },
    "tone_shadow_rollout_transitions": {
        "id", "rollout_id", "version", "previous_transition_id", "from_state",
        "to_state", "actor", "reason", "occurred_at", "shadow_evaluation_id",
    },
    "tone_shadow_batches": {
        "id", "rollout_id", "population_manifest_sha256", "population_count",
        "selected_count", "created_by", "created_at",
    },
    "tone_shadow_batch_members": {
        "batch_id", "ordinal", "subject_version_id", "selection_hash",
    },
    "tone_shadow_population_members": {
        "batch_id", "ordinal", "subject_version_id", "selection_hash", "selected",
    },
    "tone_shadow_observations": {
        "id", "batch_id", "subject_version_id", "outcome",
        "candidate_result_sha256", "reference_result_sha256", "error_code",
        "observed_at", "recorded_by",
    },
    "tone_shadow_evaluations": {
        "id", "batch_id", "decision", "metrics_json", "metrics_sha256",
        "evaluated_by", "reason", "evaluated_at",
    },
    "tone_release_activations": {
        "id", "rollout_id", "admission_id", "shadow_evaluation_id",
        "profile_id", "profile_json", "profile_sha256", "profile_content_sha256",
        "candidate_run_json", "candidate_run_sha256", "candidate_run_content_sha256",
        "runtime_config_json", "runtime_config_sha256",
        "activation_request_json", "activation_request_sha256", "created_at",
    },
    "tone_release_activation_transitions": {
        "id", "activation_id", "version", "previous_transition_id", "from_state",
        "to_state", "actor", "reason", "occurred_at", "registry_version",
        "registry_sha256", "authorization_json", "request_json", "request_sha256",
    },
}
EXPECTED_INGEST_TRIGGERS = {
    "sync_snapshot_requests_valid_manifest",
    "sync_snapshot_requests_valid_transition", "sync_snapshot_requests_ready_complete",
    "sync_snapshot_requests_no_delete", "sync_snapshots_match_request",
    "sync_snapshots_no_update", "sync_snapshots_no_delete",
    "sync_snapshot_resources_running_only", "sync_snapshot_resources_no_update",
    "sync_snapshot_resources_no_delete", "sync_snapshot_pages_running_only",
    "sync_snapshot_pages_no_update", "sync_snapshot_pages_no_delete",
    "tone_release_admissions_valid_insert", "tone_release_admissions_no_update",
    "tone_release_admissions_no_delete",
    "tone_shadow_rollouts_valid_insert", "tone_shadow_rollouts_no_update",
    "tone_shadow_rollouts_no_delete", "tone_shadow_rollout_transitions_valid_append",
    "tone_shadow_rollout_transitions_no_update",
    "tone_shadow_rollout_transitions_no_delete",
    "tone_shadow_batches_running_only", "tone_shadow_batches_no_update",
    "tone_shadow_batches_no_delete", "tone_shadow_batch_members_no_update",
    "tone_shadow_batch_members_no_delete", "tone_shadow_batch_members_no_late_insert",
    "tone_shadow_batch_members_selected_only",
    "tone_shadow_population_members_no_update",
    "tone_shadow_population_members_no_delete",
    "tone_shadow_population_members_no_late_insert",
    "tone_shadow_observations_no_update", "tone_shadow_observations_no_delete",
    "tone_shadow_observations_running_only",
    "tone_shadow_evaluations_complete_insert", "tone_shadow_evaluations_no_update",
    "tone_shadow_evaluations_no_delete",
    "tone_release_activations_valid_insert", "tone_release_activations_no_update",
    "tone_release_activations_no_delete",
    "tone_release_activation_transitions_valid_append",
    "tone_release_activation_single_active",
    "tone_release_activation_transitions_no_update",
    "tone_release_activation_transitions_no_delete",
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
    "legacy_topic_backfill_state_identity_immutable",
    "legacy_topic_backfill_state_no_delete",
    "legacy_topic_assignment_snapshot_no_update",
    "legacy_topic_assignment_snapshot_no_delete",
    "legacy_topic_assignment_snapshot_no_late_insert",
    "legacy_topic_assignment_mappings_no_update",
    "legacy_topic_assignment_mappings_no_delete",
    "topic_assignment_reviews_valid_append", "topic_assignment_reviews_no_update",
    "topic_assignment_reviews_no_delete",
    "topic_statistics_builds_identity_immutable",
    "topic_statistics_builds_valid_transition", "topic_statistics_builds_no_delete",
    "topic_statistics_versions_building_only", "topic_statistics_versions_no_update",
    "topic_statistics_versions_no_delete", "topic_statistics_members_building_only",
    "topic_statistics_members_no_update", "topic_statistics_members_no_delete",
    "topic_statistics_publications_valid_append",
    "topic_statistics_publications_no_update", "topic_statistics_publications_no_delete",
    "topic_statistics_state_ready_build", "topic_statistics_topic_insert",
    "topic_statistics_topic_update", "topic_statistics_assignment_insert",
    "topic_statistics_review_insert", "topic_statistics_event_update",
    "topic_statistics_admission_valid_append",
    "topic_statistics_admission_no_update", "topic_statistics_admission_no_delete",
    "topic_assignment_review_queue_assignment_insert",
    "topic_assignment_review_queue_no_update", "topic_assignment_review_queue_no_delete",
    "topic_assignment_review_order_review_insert",
    "topic_assignment_review_order_no_update", "topic_assignment_review_order_no_delete",
    "topic_review_sampling_batches_no_update", "topic_review_sampling_batches_no_delete",
    "topic_review_sampling_members_no_update", "topic_review_sampling_members_no_delete",
    "topic_review_sample_evaluations_valid_append",
    "topic_review_sample_evaluations_no_update",
    "topic_review_sample_evaluations_no_delete",
    "event_admission_reviews_valid_append", "event_admission_reviews_no_update",
    "event_admission_reviews_no_delete",
    "event_match_review_queue_decision_insert", "event_match_review_queue_no_update",
    "event_match_review_queue_no_delete", "event_match_reviews_valid_append",
    "event_match_reviews_no_update", "event_match_reviews_no_delete",
    "event_match_review_order_review_insert", "event_match_review_order_no_update",
    "event_match_review_order_no_delete", "event_review_sampling_batches_no_update",
    "event_review_sampling_batches_no_delete", "event_review_sampling_members_no_update",
    "event_review_sampling_members_no_delete",
    "event_review_sample_evaluations_valid_append",
    "event_review_sample_evaluations_no_update",
    "event_review_sample_evaluations_no_delete",
    "event_dataset_release_reviews_valid_append",
    "event_dataset_release_reviews_no_update",
    "event_dataset_release_reviews_no_delete",
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
    "analysis_results_tone_activation_gate",
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
            # Same larger page cache as verify_database, for this connection's check only.
            previous_cache = db.execute("PRAGMA cache_size").fetchone()[0]
            db.execute(f"PRAGMA cache_size=-{VERIFY_CACHE_KIB}")
            try:
                _assert_current_schema(db)
            finally:
                db.execute(f"PRAGMA cache_size={previous_cache}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return tuple(migration.version for migration in pending)


def _columns_as(db: sqlite3.Connection, table: str, alias: str) -> str:
    """Select list for ``alias.*`` with each column named "<alias>.<column>"."""
    return ",".join(
        '{0}."{1}" AS "{0}.{1}"'.format(alias, row["name"].replace('"', '""'))
        for row in db.execute(f'PRAGMA table_info("{table}")')
    )


def _split_joined(cursor: sqlite3.Cursor, keys: dict[str, str]):
    """Yield each row with one dict per LEFT JOINed alias selected via _columns_as().

    ``keys`` names, per alias, the column its ON clause compares; it is NULL exactly when
    the join matched no row, which then reads as None like an empty ``fetchone()``.
    """
    names = [column[0] for column in cursor.description]
    layout = {
        alias: [(index, name[len(alias) + 1:]) for index, name in enumerate(names)
                if name.startswith(alias + ".")]
        for alias in keys
    }
    match = {alias: names.index(f"{alias}.{key}") for alias, key in keys.items()}
    for row in cursor:
        yield row, {
            alias: None if row[match[alias]] is None
            else {column: row[index] for index, column in layout[alias]}
            for alias in keys
        }


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
    invalid_tone_admissions = 0
    for row in db.execute("SELECT * FROM tone_release_admissions ORDER BY imported_at,id"):
        try:
            record = json.loads(row["record_json"])
            bundle = json.loads(row["bundle_json"])
            canonical_record = json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
            valid = (
                hashlib.sha256(canonical_record.encode("utf-8")).hexdigest()
                == row["record_sha256"]
                and record["record_id"] == row["id"]
                and record["record_version"] == row["record_version"]
                and record["decision_id"] == row["decision_id"]
                and record["bundle_id"] == row["bundle_id"]
                and record["bundle_sha256"] == row["bundle_sha256"]
                and record["registry_version"] == row["registry_version"]
                and record["registry_sha256"] == row["registry_sha256"]
                and record["approver_id"] == row["approver_id"]
                and record["decision"] == row["decision"]
                and record["reason"] == row["reason"]
                and record["recorded_at"] == row["decided_at"]
                and record["policy_id"] == row["policy_id"]
                and int(record["approval_candidate_for_controlled_import"])
                == row["approval_candidate"]
                and bundle["bundle_id"] == row["bundle_id"]
                and bundle["dataset_version"] == row["dataset_version"]
                and bundle["baseline_test_run_id"] == row["baseline_test_run_id"]
                and bundle["candidate_dev_run_id"] == row["candidate_dev_run_id"]
                and bundle["candidate_test_run_id"] == row["candidate_test_run_id"]
                and bundle["candidate_security_run_id"]
                == row["candidate_security_run_id"]
                and bundle["calibration_version"] == row["calibration_version"]
                and bundle["operational_run_id"] == row["operational_run_id"]
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            valid = False
        if not valid:
            invalid_tone_admissions += 1
    if invalid_tone_admissions:
        raise DatabaseVerificationError(
            f"tone release admission ledger has {invalid_tone_admissions} invalid row(s)"
        )
    invalid_tone_rollouts = 0
    legal_rollout_transitions = {
        "planned": {"running", "aborted"},
        "running": {"paused", "completed", "aborted"},
        "paused": {"running", "aborted"},
    }
    for rollout in db.execute("SELECT * FROM tone_shadow_rollouts ORDER BY id"):
        try:
            config = json.loads(rollout["config_json"])
            identity = {
                "rollout_schema_version": rollout["rollout_schema_version"],
                "admission_id": rollout["admission_id"],
                "sample_bps": rollout["sample_bps"],
                "config": config,
            }
            expected_hash = hashlib.sha256(json.dumps(
                identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode()).hexdigest()
            admission = db.execute(
                """SELECT * FROM tone_release_admissions
                   WHERE id=? AND decision='approve' AND approval_candidate=1""",
                (rollout["admission_id"],),
            ).fetchone()
            transitions = db.execute(
                """SELECT * FROM tone_shadow_rollout_transitions
                   WHERE rollout_id=? ORDER BY version""",
                (rollout["id"],),
            ).fetchall()
            valid = (
                expected_hash == rollout["config_sha256"]
                and config.get("mode") == "shadow_only"
                and admission is not None
                and admission["dataset_version"] == rollout["dataset_version"]
                and admission["candidate_test_run_id"] == rollout["candidate_test_run_id"]
                and admission["calibration_version"] == rollout["calibration_version"]
                and bool(transitions)
            )
            previous = None
            for version, transition in enumerate(transitions, 1):
                valid = valid and transition["version"] == version
                if version == 1:
                    valid = valid and (
                        transition["previous_transition_id"] is None
                        and transition["from_state"] is None
                        and transition["to_state"] == "planned"
                    )
                else:
                    valid = valid and (
                        transition["previous_transition_id"] == previous["id"]
                        and transition["from_state"] == previous["to_state"]
                        and transition["to_state"]
                        in legal_rollout_transitions.get(previous["to_state"], set())
                    )
                if transition["to_state"] == "completed":
                    evaluation = db.execute(
                        """SELECT evaluation.decision,batch.rollout_id
                           FROM tone_shadow_evaluations AS evaluation
                           JOIN tone_shadow_batches AS batch ON batch.id=evaluation.batch_id
                           WHERE evaluation.id=?""",
                        (transition["shadow_evaluation_id"],),
                    ).fetchone()
                    valid = valid and (
                        evaluation is not None and evaluation["decision"] == "passed"
                        and evaluation["rollout_id"] == rollout["id"]
                    )
                else:
                    valid = valid and transition["shadow_evaluation_id"] is None
                previous = transition
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            valid = False
        if not valid:
            invalid_tone_rollouts += 1
    if invalid_tone_rollouts:
        raise DatabaseVerificationError(
            f"tone shadow rollout ledger has {invalid_tone_rollouts} invalid row(s)"
        )
    invalid_tone_shadow_batches = 0
    for batch in db.execute("SELECT * FROM tone_shadow_batches ORDER BY id"):
        try:
            rollout = db.execute(
                "SELECT * FROM tone_shadow_rollouts WHERE id=?", (batch["rollout_id"],)
            ).fetchone()
            population = db.execute(
                """SELECT * FROM tone_shadow_population_members
                   WHERE batch_id=? ORDER BY ordinal""", (batch["id"],)
            ).fetchall()
            selected = db.execute(
                """SELECT * FROM tone_shadow_batch_members
                   WHERE batch_id=? ORDER BY ordinal""", (batch["id"],)
            ).fetchall()
            observations = db.execute(
                "SELECT * FROM tone_shadow_observations WHERE batch_id=?",
                (batch["id"],),
            ).fetchall()
            population_ids = [row["subject_version_id"] for row in population]
            expected_manifest = hashlib.sha256(json.dumps(
                population_ids, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode()).hexdigest()
            valid = (
                rollout is not None
                and len(population) == batch["population_count"]
                and len(selected) == batch["selected_count"]
                and population_ids == sorted(population_ids)
                and [row["ordinal"] for row in population] == list(range(len(population)))
                and [row["ordinal"] for row in selected] == list(range(len(selected)))
                and expected_manifest == batch["population_manifest_sha256"]
            )
            expected_selected = []
            for row in population:
                expected_hash = hashlib.sha256(
                    f"{batch['rollout_id']}\0{row['subject_version_id']}".encode()
                ).hexdigest()
                expected_flag = int(expected_hash[:16], 16) % 10_000 < rollout["sample_bps"]
                valid = valid and (
                    row["selection_hash"] == expected_hash
                    and row["selected"] == int(expected_flag)
                )
                if expected_flag:
                    expected_selected.append((row["subject_version_id"], expected_hash))
            valid = valid and [
                (row["subject_version_id"], row["selection_hash"]) for row in selected
            ] == expected_selected
            valid = valid and all(
                any(member["subject_version_id"] == observation["subject_version_id"]
                    for member in selected)
                for observation in observations
            )
            observation_deadline = parse_utc(batch["created_at"]) + timedelta(
                hours=json.loads(rollout["config_json"])["observation_window_hours"]
            )
            valid = valid and all(
                parse_utc(batch["created_at"])
                <= parse_utc(observation["observed_at"])
                <= observation_deadline
                for observation in observations
            )
            for observation in observations:
                candidate_hash = observation["candidate_result_sha256"]
                reference_hash = observation["reference_result_sha256"]
                hashes_are_valid = all(
                    value is None
                    or (
                        len(value) == 64
                        and value == value.lower()
                        and all(character in "0123456789abcdef" for character in value)
                    )
                    for value in (candidate_hash, reference_hash)
                )
                if observation["outcome"] in {"matched", "disagreed"}:
                    evidence_is_valid = (
                        candidate_hash is not None
                        and reference_hash is not None
                        and observation["error_code"] is None
                    )
                else:
                    evidence_is_valid = (
                        candidate_hash is None
                        and isinstance(observation["error_code"], str)
                        and bool(observation["error_code"].strip())
                    )
                valid = valid and hashes_are_valid and evidence_is_valid
            evaluation = db.execute(
                "SELECT * FROM tone_shadow_evaluations WHERE batch_id=?", (batch["id"],)
            ).fetchone()
            if evaluation is not None:
                counts = {row["outcome"]: row["count"] for row in db.execute(
                    """SELECT outcome,COUNT(*) AS count FROM tone_shadow_observations
                       WHERE batch_id=? GROUP BY outcome""", (batch["id"],)
                )}
                total = batch["selected_count"]
                config = json.loads(rollout["config_json"])
                metrics = {
                    "selected_count": total,
                    "observed_count": sum(counts.values()),
                    "matched_count": counts.get("matched", 0),
                    "disagreement_count": counts.get("disagreed", 0),
                    "error_count": counts.get("error", 0),
                    "error_bps": counts.get("error", 0) * 10_000 // total,
                    "disagreement_bps": counts.get("disagreed", 0) * 10_000 // total,
                    "maximum_error_bps": config["maximum_error_bps"],
                    "maximum_disagreement_bps": config["maximum_disagreement_bps"],
                }
                metrics_json = json.dumps(
                    metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False,
                )
                decision = "passed" if (
                    metrics["observed_count"] >= config["minimum_observations"]
                    and metrics["error_bps"] <= config["maximum_error_bps"]
                    and metrics["disagreement_bps"] <= config["maximum_disagreement_bps"]
                ) else "failed"
                valid = valid and (
                    len(observations) == total
                    and evaluation["metrics_json"] == metrics_json
                    and evaluation["metrics_sha256"]
                    == hashlib.sha256(metrics_json.encode()).hexdigest()
                    and evaluation["decision"] == decision
                )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ZeroDivisionError):
            valid = False
        if not valid:
            invalid_tone_shadow_batches += 1
    if invalid_tone_shadow_batches:
        raise DatabaseVerificationError(
            f"tone shadow evaluation ledger has {invalid_tone_shadow_batches} invalid batch(es)"
        )
    invalid_tone_activations = 0
    for activation in db.execute("SELECT * FROM tone_release_activations ORDER BY id"):
        try:
            from .tone_release_activation import (
                ACTIVATE_SCOPE, ACTIVATION_REQUEST_FIELDS, ACTIVATION_SCHEMA,
                AUTHORIZATION_FIELDS, ROLLBACK_REQUEST_FIELDS, ROLLBACK_SCHEMA,
                ROLLBACK_SCOPE, ToneReleaseActivationError, _validate_runtime_profile,
            )

            canonical = lambda value: json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
            profile = json.loads(activation["profile_json"])
            candidate = json.loads(activation["candidate_run_json"])
            runtime = json.loads(activation["runtime_config_json"])
            activation_request = json.loads(activation["activation_request_json"])
            rollout = db.execute(
                "SELECT * FROM tone_shadow_rollouts WHERE id=?", (activation["rollout_id"],)
            ).fetchone()
            admission = db.execute(
                "SELECT * FROM tone_release_admissions WHERE id=?",
                (activation["admission_id"],),
            ).fetchone()
            evaluation = db.execute(
                "SELECT * FROM tone_shadow_evaluations WHERE id=?",
                (activation["shadow_evaluation_id"],),
            ).fetchone()
            completion = db.execute(
                """SELECT * FROM tone_shadow_rollout_transitions
                   WHERE rollout_id=? AND to_state='completed'
                     AND shadow_evaluation_id=?""",
                (activation["rollout_id"], activation["shadow_evaluation_id"]),
            ).fetchone()
            bundle = json.loads(admission["bundle_json"]) if admission else None
            checked_runtime, runtime_hash = _validate_runtime_profile(
                profile, candidate, admission, rollout
            )
            request_json = canonical(activation_request)
            request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
            expected_activation_id = "tone-activation-" + hashlib.sha256(canonical({
                "request_sha256": request_hash,
            }).encode("utf-8")).hexdigest()[:24]
            valid = (
                rollout is not None and admission is not None and evaluation is not None
                and completion is not None and evaluation["decision"] == "passed"
                and rollout["admission_id"] == admission["id"]
                and rollout["id"] == activation["rollout_id"]
                and evaluation["id"] == activation["shadow_evaluation_id"]
                and profile["profile_id"] == activation["profile_id"]
                and hashlib.sha256(
                    activation["profile_json"].encode("utf-8")
                ).hexdigest() == activation["profile_sha256"]
                and hashlib.sha256(canonical(profile).encode("utf-8")).hexdigest()
                    == activation["profile_content_sha256"]
                and hashlib.sha256(
                    activation["candidate_run_json"].encode("utf-8")
                ).hexdigest() == activation["candidate_run_sha256"]
                and hashlib.sha256(canonical(candidate).encode("utf-8")).hexdigest()
                    == activation["candidate_run_content_sha256"]
                and canonical(checked_runtime) == activation["runtime_config_json"]
                and runtime == checked_runtime
                and runtime_hash == activation["runtime_config_sha256"]
                and request_json == activation["activation_request_json"]
                and request_hash == activation["activation_request_sha256"]
                and expected_activation_id == activation["id"]
                and bundle["artifact_sha256"]["candidate_test_run"]
                    == activation["candidate_run_sha256"]
                and activation_request["rollout_id"] == activation["rollout_id"]
                and activation_request["shadow_evaluation_id"]
                    == activation["shadow_evaluation_id"]
                and activation_request["profile_id"] == activation["profile_id"]
                and activation_request["profile_sha256"] == activation["profile_sha256"]
                and activation_request["candidate_test_run_id"]
                    == admission["candidate_test_run_id"]
                and activation_request["candidate_run_sha256"]
                    == activation["candidate_run_sha256"]
                and parse_utc(profile["created_at"])
                    <= parse_utc(activation["created_at"])
                and parse_utc(candidate["generated_at"])
                    <= parse_utc(activation["created_at"])
                and parse_utc(evaluation["evaluated_at"])
                    <= parse_utc(activation["created_at"])
                and parse_utc(completion["occurred_at"])
                    <= parse_utc(activation["created_at"])
            )
            transitions = db.execute(
                """SELECT * FROM tone_release_activation_transitions
                   WHERE activation_id=? ORDER BY version""", (activation["id"],)
            ).fetchall()
            valid = valid and len(transitions) in {1, 2}
            previous = None
            for version, transition in enumerate(transitions, 1):
                authorization = json.loads(transition["authorization_json"])
                request = json.loads(transition["request_json"])
                serialized_request = canonical(request)
                request_sha = hashlib.sha256(serialized_request.encode("utf-8")).hexdigest()
                valid_from = parse_utc(authorization["valid_from"])
                valid_until = (
                    parse_utc(authorization["valid_until"])
                    if authorization["valid_until"] is not None else None
                )
                occurred = parse_utc(transition["occurred_at"])
                required_scope = ACTIVATE_SCOPE if version == 1 else ROLLBACK_SCOPE
                actor_field = "activator_id" if version == 1 else "operator_id"
                schema = ACTIVATION_SCHEMA if version == 1 else ROLLBACK_SCHEMA
                request_fields = (
                    ACTIVATION_REQUEST_FIELDS if version == 1
                    else ROLLBACK_REQUEST_FIELDS
                )
                valid = valid and (
                    transition["version"] == version
                    and canonical(authorization) == transition["authorization_json"]
                    and serialized_request == transition["request_json"]
                    and request_sha == transition["request_sha256"]
                    and set(request) == request_fields
                    and request["schema_version"] == schema
                    and request[actor_field] == transition["actor"]
                    and request["reason"].strip() == transition["reason"]
                    and format_utc(parse_utc(request["recorded_at"]))
                        == transition["occurred_at"]
                    and request["source"] == "human"
                    and request["model_assistance"] is False
                    and request["registry_version"] == transition["registry_version"]
                    and request["registry_sha256"] == transition["registry_sha256"]
                    and authorization["approver_id"] == transition["actor"]
                    and set(authorization) == AUTHORIZATION_FIELDS
                    and authorization["status"] == "active"
                    and authorization["scopes"]
                        == sorted(set(authorization["scopes"]))
                    and required_scope in authorization["scopes"]
                    and occurred >= valid_from
                    and (valid_until is None or occurred < valid_until)
                )
                if version == 1:
                    valid = valid and (
                        transition["previous_transition_id"] is None
                        and transition["from_state"] is None
                        and transition["to_state"] == "active"
                        and transition["request_sha256"]
                            == activation["activation_request_sha256"]
                        and transition["occurred_at"] == activation["created_at"]
                        and request["rollback_plan_acknowledged"] is True
                        and transition["actor"] not in {
                            admission["approver_id"], evaluation["evaluated_by"],
                            bundle["reviewer_a"], bundle["reviewer_b"],
                        }
                    )
                else:
                    valid = valid and (
                        previous is not None
                        and transition["previous_transition_id"] == previous["id"]
                        and transition["from_state"] == "active"
                        and transition["to_state"] == "rolled_back"
                        and request["activation_id"] == activation["id"]
                        and request["expected_previous_transition_id"] == previous["id"]
                        and occurred >= parse_utc(previous["occurred_at"])
                    )
                expected_transition_id = "tone-activation-transition-" + hashlib.sha256(
                    canonical(
                        {"activation_id": activation["id"], "version": version}
                        if version == 1 else {
                            "activation_id": activation["id"], "version": version,
                            "request_sha256": request_sha,
                        }
                    ).encode("utf-8")
                ).hexdigest()[:24]
                valid = valid and transition["id"] == expected_transition_id
                previous = transition
        except (
            KeyError, TypeError, ValueError, json.JSONDecodeError,
            ToneReleaseActivationError,
        ):
            valid = False
        if not valid:
            invalid_tone_activations += 1
    active_tone_activations = db.execute(
        """SELECT COUNT(*) FROM tone_release_activation_transitions AS transition
           WHERE transition.to_state='active' AND NOT EXISTS(
               SELECT 1 FROM tone_release_activation_transitions AS later
               WHERE later.activation_id=transition.activation_id
                 AND later.version>transition.version)"""
    ).fetchone()[0]
    if active_tone_activations > 1:
        invalid_tone_activations += active_tone_activations
    if invalid_tone_activations:
        raise DatabaseVerificationError(
            f"tone release activation ledger has {invalid_tone_activations} invalid row(s)"
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
    topic_statistics_state = db.execute(
        """SELECT state.singleton,state.status,state.current_build_id,build.status,
                  state.current_publication_id,publication.build_id AS publication_build_id
           FROM topic_statistics_state AS state
           LEFT JOIN topic_statistics_builds AS build ON build.id=state.current_build_id
           LEFT JOIN topic_statistics_publications AS publication
             ON publication.id=state.current_publication_id"""
    ).fetchall()
    if len(topic_statistics_state) != 1 or topic_statistics_state[0]["singleton"] != 1:
        raise DatabaseVerificationError("topic statistics must have one state row")
    if topic_statistics_state[0]["status"] == "ready" and (
        topic_statistics_state[0][3] != "ready"
        or topic_statistics_state[0]["publication_build_id"]
        != topic_statistics_state[0]["current_build_id"]
    ):
        raise DatabaseVerificationError("topic statistics state points to an invalid publication")
    if topic_statistics_state[0]["status"] == "ready":
        build_id = topic_statistics_state[0]["current_build_id"]
        invalid_topic_statistics = db.execute(
            """SELECT
               (SELECT topic_count FROM topic_statistics_builds WHERE id=?) <>
                 (SELECT COUNT(*) FROM topic_statistics_versions WHERE build_id=?)
               OR (SELECT COUNT(*) FROM topic_statistics_versions WHERE build_id=?) <>
                  (SELECT COUNT(*) FROM topic_catalog)
               OR EXISTS(
                 SELECT 1 FROM topic_catalog AS topic
                 LEFT JOIN topic_statistics_versions AS statistic
                   ON statistic.build_id=? AND statistic.topic_version_id=topic.current_version_id
                 WHERE statistic.id IS NULL
               )
               OR EXISTS(
                 SELECT 1 FROM topic_statistics_versions AS statistic
                 WHERE statistic.build_id=? AND (
                   statistic.document_count<>(SELECT COUNT(*) FROM topic_statistics_members
                     WHERE statistics_id=statistic.id AND member_type='document')
                   OR statistic.event_count<>(SELECT COUNT(*) FROM topic_statistics_members
                     WHERE statistics_id=statistic.id AND member_type='event')
                 )
               )""",
            (build_id, build_id, build_id, build_id, build_id),
        ).fetchone()[0]
        if invalid_topic_statistics:
            raise DatabaseVerificationError("ready topic statistics build is incomplete")
    invalid_topic_admissions = 0
    for review in db.execute("SELECT * FROM topic_statistics_admission_reviews"):
        try:
            metrics = json.loads(review["metrics_json"])
            digest = hashlib.sha256(json.dumps(
                metrics, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics, digest = None, ""
        if (
            not isinstance(metrics, dict)
            or digest != review["metrics_sha256"]
            or metrics.get("publication_id") != review["publication_id"]
            or (review["decision"] == "approved" and (
                metrics.get("dirty_topics") != 0
                or not isinstance(metrics.get("decided_assignment_bps"), int)
                or metrics["decided_assignment_bps"]
                   < review["minimum_decided_assignment_bps"]
                or (
                    metrics.get("published_document_members", 0)
                    + metrics.get("published_event_members", 0) == 0
                    and not review["allow_zero_members"]
                )
            ))
            or (review["policy_version"] == "sample-gated-v2" and review["decision"] == "approved" and (
                review["sample_evaluation_id"] is None
                or review["sample_metrics_sha256"] is None
                or not db.execute(
                    """SELECT 1 FROM topic_review_sample_evaluations AS evaluation
                       JOIN topic_review_sampling_batches AS batch
                         ON batch.id=evaluation.batch_id
                       JOIN topic_statistics_publications AS publication
                         ON publication.id=?
                       JOIN topic_statistics_builds AS build
                         ON build.id=publication.build_id
                       WHERE evaluation.id=? AND evaluation.decision='approved'
                         AND evaluation.metrics_sha256=?
                         AND batch.dataset_id=build.dataset_id""",
                    (
                        review["publication_id"], review["sample_evaluation_id"],
                        review["sample_metrics_sha256"],
                    ),
                ).fetchone()
            ))
        ):
            invalid_topic_admissions += 1
    if invalid_topic_admissions:
        raise DatabaseVerificationError(
            f"{invalid_topic_admissions} topic statistics admission review(s) are invalid"
        )
    missing_topic_queue_entries = db.execute(
        """SELECT COUNT(*) FROM document_topic_assignments AS assignment
           LEFT JOIN topic_assignment_review_queue AS queue
             ON queue.assignment_id=assignment.id
           WHERE queue.assignment_id IS NULL"""
    ).fetchone()[0]
    extra_topic_queue_entries = db.execute(
        """SELECT COUNT(*) FROM topic_assignment_review_queue AS queue
           LEFT JOIN document_topic_assignments AS assignment
             ON assignment.id=queue.assignment_id
           WHERE assignment.id IS NULL"""
    ).fetchone()[0]
    if missing_topic_queue_entries or extra_topic_queue_entries:
        raise DatabaseVerificationError(
            "topic assignment review queue does not cover every assignment"
        )
    try:
        from .topic_review_sampling import verify_sampling_batches
        verify_sampling_batches(db)
        from .topic_review_sample_gate import verify_sample_evaluations
        verify_sample_evaluations(db)
        from .event_match_reviews import verify_match_reviews
        verify_match_reviews(db)
        from .event_review_sampling import verify_sampling_batches as verify_event_samples
        verify_event_samples(db)
        from .event_review_sample_gate import verify_sample_evaluations as verify_event_gate
        verify_event_gate(db)
        from .event_dataset_release import verify_release_reviews
        verify_release_reviews(db)
        from .event_admission import verify_event_admission_reviews
        verify_event_admission_reviews(db)
    except (ValueError, RuntimeError) as exc:
        raise DatabaseVerificationError(str(exc)) from exc
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
    invalid_topic_imports = db.execute(
        """SELECT COUNT(*)
           FROM legacy_topic_assignment_mappings AS mapping
           JOIN legacy_topic_assignment_snapshot AS snapshot
             ON snapshot.item_id=mapping.item_id
            AND snapshot.topic_slug=mapping.topic_slug
           LEFT JOIN document_topic_assignments AS assignment
             ON assignment.id=mapping.assignment_id
           WHERE assignment.id IS NULL
              OR assignment.document_version_id<>snapshot.document_version_id
              OR assignment.topic_version_id<>snapshot.topic_version_id
              OR assignment.method<>'legacy_projection'
              OR assignment.method_version<>'legacy-item-topics-v1'
              OR assignment.status<>'candidate'
              OR mapping.evidence_sha256<>snapshot.evidence_sha256"""
    ).fetchone()[0]
    completed_topic_imports = db.execute(
        """SELECT COUNT(*) FROM legacy_topic_backfill_state AS state
           WHERE state.status='completed'
             AND (state.source_count<>state.processed_count
               OR state.source_count<>(SELECT COUNT(*) FROM legacy_topic_assignment_snapshot)
               OR state.source_count<>(SELECT COUNT(*) FROM legacy_topic_assignment_mappings))"""
    ).fetchone()[0]
    if invalid_topic_imports or completed_topic_imports:
        raise DatabaseVerificationError(
            "legacy topic assignment import is inconsistent: "
            f"mappings={invalid_topic_imports}, completed_state={completed_topic_imports}"
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
    # The analysis ledgers grow by one row set per published analysis, so each ledger is
    # read in one streamed join rather than with lookups per row; the checks are unchanged.
    # Grouped by rowid: a tampered TEXT primary key can be NULL and must still count per run.
    run_inputs = db.execute(
        """SELECT run.rowid AS "run.rowid",run.*,input.ordinal,input.document_version_id,
                  input.event_version_id,input.evidence_id,
                  EXISTS(SELECT 1 FROM document_version_inputs AS link
                         WHERE link.version_id=input.document_version_id
                           AND link.raw_record_id=input.evidence_id) AS evidence_linked
           FROM analysis_runs AS run
           LEFT JOIN analysis_inputs AS input ON input.run_id=run.id
           ORDER BY run.rowid,input.ordinal"""
    )
    for _, run_rows in groupby(run_inputs, key=lambda row: row["run.rowid"]):
        run_rows = list(run_rows)
        run = run_rows[0]
        valid_run = True
        try:
            manifest = json.loads(run["input_manifest_json"])
            parameters = json.loads(run["parameters_json"])
        except (TypeError, json.JSONDecodeError):
            valid_run = False
            manifest = parameters = None
        # A run without inputs joins to a single row whose input columns are NULL.
        inputs = [row for row in run_rows if row["ordinal"] is not None]
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
                    valid_inputs = valid_inputs and bool(item["evidence_linked"])
            else:
                valid_inputs = (
                    valid_inputs and item["event_version_id"] in event_version_ids
                    and item["evidence_id"] is None
                )
        manifest_hash = hashlib.sha256(
            run["input_manifest_json"].encode("utf-8")
        ).hexdigest()
        if (
            not valid_run or not isinstance(manifest, dict) or not isinstance(parameters, dict)
            or manifest_hash != run["input_manifest_sha256"]
            or manifest.get("parameters") != parameters
            or not subject_exists or not includes_subject or not valid_inputs
        ):
            invalid_analysis_runs += 1
    invalid_analysis_attempts = 0
    authorizations = db.execute(
        f"""SELECT authorization.*,run.id AS "run.id",run.provider AS "run.provider",
                   {_columns_as(db, "analysis_budget_policies", "policy")},
                   {_columns_as(db, "analysis_attempts", "attempt")}
            FROM analysis_attempt_authorizations AS authorization
            LEFT JOIN analysis_runs AS run ON run.id=authorization.run_id
            LEFT JOIN analysis_budget_policies AS policy
              ON policy.id=authorization.budget_policy_id
            LEFT JOIN analysis_attempts AS attempt
              ON attempt.authorization_id=authorization.id"""
    )
    for authorization, joined in _split_joined(
        authorizations, {"run": "id", "policy": "id", "attempt": "authorization_id"}
    ):
        run, policy, attempt = joined["run"], joined["policy"], joined["attempt"]
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
    from .analysis_runs import AnalysisRunError
    from .analysis_results import (
        TONE_RELEASE_VALIDATOR_VERSION, _tone_release_binding,
    )
    from .impact_contracts import (
        IMPACT_SCHEMA_VERSION, referenced_impact_entities, validate_impact_data,
        validate_impact_envelope, verify_impact_evidence,
    )
    results = db.execute(
        f"""SELECT result.*,{_columns_as(db, "analysis_runs", "run")},
                   {_columns_as(db, "analysis_attempts", "attempt")}
            FROM analysis_results AS result
            LEFT JOIN analysis_runs AS run ON run.id=result.run_id
            LEFT JOIN analysis_attempts AS attempt
              ON attempt.id=result.attempt_id AND attempt.run_id=result.run_id"""
    )
    for result, joined in _split_joined(results, {"run": "id", "attempt": "id"}):
        run, attempt = joined["run"], joined["attempt"]
        try:
            output = json.loads(result["validated_output_json"])
            report = json.loads(result["validation_report_json"])
        except (TypeError, json.JSONDecodeError):
            output = report = None
        valid_result = not (
            not run or not attempt or attempt["status"] not in {"succeeded", "refused"}
            or not isinstance(output, dict) or not isinstance(report, dict)
            or result["schema_version"] != run["output_schema_version"]
            or output.get("schema_version") != run["output_schema_version"]
            or output.get("subject") != {
                "type": run["subject_type"], "version_id": run["subject_version_id"]
            }
            or output.get("status") != result["result_status"]
            or report.get("status") != "passed"
            or result["raw_output_ref"] != attempt["raw_response_ref"]
            or result["raw_output_sha256"] != attempt["raw_response_sha256"]
            or result["created_at"] != attempt["finished_at"]
        )
        if valid_result:
            tone_release_report = report.get("tone_release")
            if run["task_type"] == "tone" and result["result_status"] == "valid":
                try:
                    binding = _tone_release_binding(
                        db, run, activation_id=result["tone_activation_id"],
                        require_active=False,
                    )
                    assessments = output["data"]["assessments"]
                    active_transition = db.execute(
                        """SELECT occurred_at FROM tone_release_activation_transitions
                           WHERE activation_id=? AND version=1 AND to_state='active'""",
                        (binding["activation_id"],),
                    ).fetchone()
                    rollback_transition = db.execute(
                        """SELECT occurred_at FROM tone_release_activation_transitions
                           WHERE activation_id=? AND version=2 AND to_state='rolled_back'""",
                        (binding["activation_id"],),
                    ).fetchone()
                    publications = db.execute(
                        "SELECT * FROM analysis_publication_versions WHERE result_id=?",
                        (result["id"],),
                    ).fetchall()
                    expected_report = {
                        "validator_version": TONE_RELEASE_VALIDATOR_VERSION,
                        **binding,
                    }
                    valid_result = (
                        result["tone_activation_id"] == binding["activation_id"]
                        and tone_release_report == expected_report
                        and isinstance(assessments, list) and bool(assessments)
                        and all(isinstance(item, dict) for item in assessments)
                        and all(
                            item.get("confidence", {}).get("calibrated_confidence")
                                is not None
                            and item.get("confidence", {}).get("calibration_version")
                                == binding["calibration_version"]
                            for item in assessments
                        )
                        and active_transition is not None
                        and parse_utc(active_transition["occurred_at"])
                            <= parse_utc(result["available_at"])
                        and (
                            rollback_transition is None
                            or parse_utc(result["available_at"])
                                <= parse_utc(rollback_transition["occurred_at"])
                        )
                        and bool(publications)
                        and all(item["evidence_status"] == "supported" for item in publications)
                    )
                    for publication in publications:
                        change = db.execute(
                            "SELECT payload_json FROM change_log WHERE seq=?",
                            (publication["publication_seq"],),
                        ).fetchone()
                        payload = json.loads(change["payload_json"]) if change else None
                        valid_result = valid_result and (
                            isinstance(payload, dict)
                            and payload.get("result_id") == result["id"]
                            and payload.get("tone_activation_id") == binding["activation_id"]
                        )
                except (
                    AnalysisRunError, KeyError, TypeError, ValueError, json.JSONDecodeError,
                ):
                    valid_result = False
            elif result["tone_activation_id"] is not None or tone_release_report is not None:
                valid_result = False
            if valid_result and run["task_type"] == "impact":
                try:
                    allowed_evidence = {
                        item[0] for item in db.execute(
                            """SELECT evidence_id FROM analysis_inputs
                               WHERE run_id=? AND evidence_id IS NOT NULL""",
                            (run["id"],),
                        )
                    }
                    clean_impact = validate_impact_data(
                        schema_version=run["output_schema_version"],
                        status=result["result_status"], data=output["data"],
                        allowed_evidence=allowed_evidence,
                    )
                    clean_envelope = dict(output)
                    clean_envelope["data"] = clean_impact
                    validate_impact_envelope(clean_envelope)
                    task_validation = verify_impact_evidence(
                        db, event_version_id=run["subject_version_id"],
                        data=clean_impact,
                    )
                    entity_references = referenced_impact_entities(clean_impact)
                    entities_valid = all(
                        bool(db.execute(
                            "SELECT 1 FROM entities WHERE id=? AND type=?",
                            (entity_id, expected_type),
                        ).fetchone())
                        for entity_id, expected_type, _ in entity_references
                    )
                    expected_task_report = (
                        task_validation if task_validation["status"] == "passed" else None
                    )
                    valid_result = (
                        run["subject_type"] == "event"
                        and run["output_schema_version"] == IMPACT_SCHEMA_VERSION
                        and result["result_status"] != "valid"
                        and clean_envelope == output
                        and clean_impact == output["data"]
                        and report.get("task_validation") == expected_task_report
                        and entities_valid
                    )
                except (AnalysisRunError, KeyError, TypeError, ValueError):
                    valid_result = False
        if not valid_result:
            invalid_analysis_results += 1
    pointers = db.execute(
        f"""SELECT pointer.*,{_columns_as(db, "analysis_publication_versions", "publication")},
                   {_columns_as(db, "analysis_results", "result")},
                   {_columns_as(db, "analysis_runs", "run")},
                   {_columns_as(db, "change_log", "change")}
            FROM analysis_publications AS pointer
            LEFT JOIN analysis_publication_versions AS publication
              ON publication.id=pointer.current_publication_id
            LEFT JOIN analysis_results AS result ON result.id=publication.result_id
            LEFT JOIN analysis_runs AS run ON run.id=result.run_id
            LEFT JOIN change_log AS change ON change.seq=publication.publication_seq"""
    )
    for pointer, joined in _split_joined(
        pointers, {"publication": "id", "result": "id", "run": "id", "change": "seq"}
    ):
        publication, result, run, change = (
            joined["publication"], joined["result"], joined["run"], joined["change"]
        )
        try:
            payload = json.loads(change["payload_json"]) if change else None
        except (TypeError, json.JSONDecodeError):
            payload = None
        if (
            not publication or not result or not run or not change
            or publication["subject_type"] != pointer["subject_type"]
            or publication["subject_version_id"] != pointer["subject_version_id"]
            or publication["task_type"] != pointer["task_type"]
            or publication["result_id"] != result["id"]
            or publication["subject_type"] != run["subject_type"]
            or publication["subject_version_id"] != run["subject_version_id"]
            or publication["task_type"] != run["task_type"]
            or publication["available_at"] != result["available_at"]
            or change["resource_type"] != "analysis"
            or change["version_id"] != publication["id"]
            or not isinstance(payload, dict)
            or payload.get("result_id") != result["id"]
            or payload.get("tone_activation_id") != result["tone_activation_id"]
        ):
            invalid_analysis_results += 1
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
    invalid_sync_snapshots = db.execute(
        """SELECT COUNT(*)
           FROM sync_snapshots AS snapshot
           LEFT JOIN sync_snapshot_requests AS request ON request.id=snapshot.id
           LEFT JOIN knowledge_checkpoints AS checkpoint
             ON checkpoint.id=snapshot.knowledge_checkpoint_id
           WHERE request.id IS NULL OR request.state NOT IN ('running','ready')
              OR request.dataset_id<>snapshot.dataset_id
              OR request.dataset_epoch<>snapshot.dataset_epoch
              OR request.consumer_id<>snapshot.consumer_id
              OR request.key_id<>snapshot.key_id
              OR request.authz_version<>snapshot.authz_version
              OR request.projection_scope<>snapshot.projection_scope
              OR request.expires_at<>snapshot.expires_at
              OR checkpoint.id IS NULL
              OR checkpoint.dataset_id<>snapshot.dataset_id
              OR checkpoint.epoch<>snapshot.dataset_epoch
              OR checkpoint.high_water<>snapshot.high_water
              OR snapshot.resource_count<>(
                  SELECT COUNT(*) FROM sync_snapshot_resources
                  WHERE snapshot_id=snapshot.id)
              OR snapshot.record_count<>COALESCE((
                  SELECT SUM(record_count) FROM sync_snapshot_resources
                  WHERE snapshot_id=snapshot.id),0)
              OR EXISTS(
                  SELECT 1 FROM sync_snapshot_resources AS resource
                  WHERE resource.snapshot_id=snapshot.id
                    AND (resource.page_count<>(
                          SELECT COUNT(*) FROM sync_snapshot_pages AS page
                          WHERE page.snapshot_id=resource.snapshot_id
                            AND page.resource=resource.resource)
                      OR resource.record_count<>COALESCE((
                          SELECT SUM(page.record_count) FROM sync_snapshot_pages AS page
                          WHERE page.snapshot_id=resource.snapshot_id
                            AND page.resource=resource.resource),0)))"""
    ).fetchone()[0]
    ready_without_snapshot = db.execute(
        """SELECT COUNT(*) FROM sync_snapshot_requests AS request
           WHERE request.state='ready'
             AND NOT EXISTS(SELECT 1 FROM sync_snapshots WHERE id=request.id)"""
    ).fetchone()[0]
    if invalid_sync_snapshots or ready_without_snapshot:
        raise DatabaseVerificationError(
            "reliable-sync snapshot ledger is inconsistent: "
            f"snapshots={invalid_sync_snapshots}, ready_requests={ready_without_snapshot}"
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


# PRAGMA integrity_check and foreign_key_check revisit index pages many times; SQLite's default
# 2 MB page cache made them reread those pages. 64 MB cut both by about a quarter on a 4.9 GB
# database. It is held only while this read-only connection is open.
VERIFY_CACHE_KIB = 64 * 1024


def require_safe_sqlite() -> None:
    """Refuse production on a SQLite with the WAL-reset corruption bug; warn elsewhere."""
    if database.sqlite_wal_reset_safe():
        return
    message = (
        f"SQLite {sqlite3.sqlite_version} can corrupt WAL databases under concurrent writers "
        "(https://sqlite.org/wal.html#walresetbug); production needs 3.51.3 or later "
        "(or 3.50.7 / 3.44.6)"
    )
    if config.ENVIRONMENT == "production":
        raise DatabaseSafetyError(message)
    global _UNSAFE_SQLITE_WARNED
    if not _UNSAFE_SQLITE_WARNED:
        _UNSAFE_SQLITE_WARNED = True
        _LOG.warning("%s", message)


_UNSAFE_SQLITE_WARNED = False


def verify_database(path: Path | str, require_current: bool = False) -> VerificationReport:
    # Every role verifies the database before it starts, so this is the one gate for all of them.
    require_safe_sqlite()
    target = Path(path).expanduser().resolve(strict=True)
    dataset_id = None
    dataset_epoch = None
    change_high_water = None
    with _connect_readonly(target) as db:
        db.execute(f"PRAGMA cache_size=-{VERIFY_CACHE_KIB}")
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


def same_bytes_as_verified(path: Path | str, verified: VerificationReport) -> VerificationReport:
    """Report for a file byte-identical to one that already passed verify_database.

    Full verification depends only on the file's bytes, so a renamed or copied file whose
    SHA-256 matches the verified one needs no second run of every check.
    """
    target = Path(path).expanduser().resolve(strict=True)
    if _sha256(target) != verified.file_sha256:
        raise DatabaseVerificationError(f"{target} differs from the verified database")
    return replace(verified, path=str(target), size_bytes=target.stat().st_size)


def backup_database(
    source_path: Path | str | None = None, destination: Path | str | None = None,
    *, require_current: bool = False,
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
        verified = verify_database(temporary, require_current=require_current)
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
    return same_bytes_as_verified(target, verified)


PRE_MIGRATION_DIRECTORY = "pre-migration"


def _pre_migration_target(source: Path) -> Path | None:
    """Pre-migration copies of the live database get a folder of their own, so the nightly
    backup can keep only the newest ones without touching manual backups."""
    if source != config.DB_PATH.expanduser().resolve():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return (config.BACKUP_PATH / PRE_MIGRATION_DIRECTORY
            / f"{source.stem}.{config.ENVIRONMENT_ID}.{stamp}.db")


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
        backup_path = backup_database(target, _pre_migration_target(target)).path

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
