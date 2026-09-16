from __future__ import annotations

"""Immutable ingest runs, content-addressed payloads, and observations.

P06a records the exact candidate object emitted by today's fetchers as
``generated_metadata``. It does not claim that the candidate is publisher
body text: source-specific raw entry/response capture is introduced with the
source parsing rules in P06b. The immutable identity and CAS rules here are the
same for both forms.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from . import config
from .database import get_db
from .timeutil import format_utc


_FINAL_RUN_STATES = {"succeeded", "partial", "failed", "skipped"}
_SECRET_KEY_SUFFIXES = (
    "authorization", "cookie", "password", "secret", "token", "apikey",
    "signature", "privatekey", "clientsecret", "accesstoken", "refreshtoken",
)
_SECRET_QUERY_NAMES = {
    "access_token", "api_key", "apikey", "auth", "authorization", "key",
    "password", "secret", "sign", "signature", "token",
}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_CONFIG_FIELDS = (
    "key", "name", "channel", "tier", "type", "url", "company_slug",
    "interval_minutes",
)
_CANDIDATE_FIELDS = (
    "url", "title", "summary", "published_at", "event_type", "official",
    "companies", "extra",
)


class IngestEvidenceError(RuntimeError):
    """An ingest observation cannot be persisted without weakening evidence."""


class PayloadIntegrityError(IngestEvidenceError):
    """A CAS object is missing, corrupt, or does not match its reference."""


@dataclass(frozen=True)
class IngestRun:
    id: str
    source_id: int
    config_version_id: str
    trace_id: str


@dataclass(frozen=True)
class RawObservation:
    raw_record_id: str
    observation_id: str
    payload_sha256: str
    payload_ref: str
    size_bytes: int
    new_record: bool


@dataclass(frozen=True)
class PayloadAudit:
    records: int
    verified: int
    missing: int
    corrupt: int

    @property
    def healthy(self) -> bool:
        return self.missing == 0 and self.corrupt == 0

    def to_dict(self) -> dict:
        return {
            "status": "ok" if self.healthy else "failed",
            "records": self.records,
            "verified": self.verified,
            "missing": self.missing,
            "corrupt": self.corrupt,
        }


def _canonical_time(value: datetime | str | None = None) -> str:
    if value is None:
        return format_utc(datetime.now(timezone.utc))
    if isinstance(value, datetime):
        return format_utc(value)
    # format_utc/parse_utc validation lives in the time utility and rejects
    # naive strings. Avoid storing host-local timestamps in new tables.
    from .timeutil import parse_utc
    return format_utc(parse_utc(value))


def _safe_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    netloc = parts.netloc.rsplit("@", 1)[-1]
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        query.append((
            key,
            "[redacted]"
            if key.casefold() in _SECRET_QUERY_NAMES or _is_secret_key(key)
            else item,
        ))
    # URL fragments can carry OAuth tokens and are never sent to the origin.
    # Keeping them adds no request provenance, so omit them entirely.
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(normalized.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


def _safe_value(value: object, *, key: str = "", depth: int = 0) -> object:
    if _is_secret_key(key):
        return "[redacted]"
    if depth > 8:
        return "[depth-limited]"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_value(item, key=str(item_key), depth=depth + 1)
            for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in value]
    return str(value)


def _candidate_payload(candidate: Mapping) -> bytes:
    payload = {
        field: (
            _safe_url(candidate.get(field))
            if field == "url"
            else _safe_value(candidate.get(field), key=field)
        )
        for field in _CANDIDATE_FIELDS
        if field in candidate
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _source_config(source: Mapping) -> tuple[str, str]:
    value = {
        field: (
            _safe_url(source.get(field))
            if field == "url"
            else _safe_value(source.get(field), key=field)
        )
        for field in _SOURCE_CONFIG_FIELDS
        if field in source
    }
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def payload_path(payload_sha256: str, root: Path | None = None) -> Path:
    if not _HASH.fullmatch(payload_sha256):
        raise ValueError("payload hash must be 64 lower-case hexadecimal characters")
    base = Path(root or config.BLOB_PATH).resolve()
    return base / "sha256" / payload_sha256[:2] / payload_sha256


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Directory fsync is not available on every Windows filesystem.
        pass


def store_payload(payload: bytes, root: Path | None = None) -> tuple[str, str]:
    if len(payload) > config.RAW_PAYLOAD_MAX_BYTES:
        raise IngestEvidenceError(
            f"payload exceeds {config.RAW_PAYLOAD_MAX_BYTES} byte limit"
        )
    digest = hashlib.sha256(payload).hexdigest()
    target = payload_path(digest, root)
    relative = target.relative_to(Path(root or config.BLOB_PATH).resolve()).as_posix()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != len(payload) or _hash_file(target) != digest:
            raise PayloadIntegrityError("existing CAS object does not match its hash")
        return digest, relative

    temporary = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Same-hash concurrent writers carry identical bytes. Replacing within
        # one directory atomically publishes either complete copy.
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    if target.stat().st_size != len(payload) or _hash_file(target) != digest:
        raise PayloadIntegrityError("published CAS object failed verification")
    return digest, relative


def verify_payload(payload_ref: str, payload_sha256: str, root: Path | None = None) -> Path:
    base = Path(root or config.BLOB_PATH).resolve()
    target = (base / payload_ref).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise PayloadIntegrityError("payload reference escapes the blob root") from exc
    expected = payload_path(payload_sha256, base)
    if target != expected or not target.is_file() or _hash_file(target) != payload_sha256:
        raise PayloadIntegrityError("payload reference is missing or corrupt")
    return target


def audit_payloads(root: Path | None = None) -> PayloadAudit:
    """Fully verify every referenced payload; intended for backup/release audits."""
    with get_db() as db:
        rows = db.execute(
            "SELECT payload_ref,payload_sha256,size_bytes FROM raw_records ORDER BY id"
        ).fetchall()
    verified = missing = corrupt = 0
    for row in rows:
        try:
            target = verify_payload(row["payload_ref"], row["payload_sha256"], root)
            if target.stat().st_size != row["size_bytes"]:
                raise PayloadIntegrityError("payload size does not match its record")
            verified += 1
        except PayloadIntegrityError:
            target = Path(root or config.BLOB_PATH).resolve() / row["payload_ref"]
            if not target.exists():
                missing += 1
            else:
                corrupt += 1
    return PayloadAudit(len(rows), verified, missing, corrupt)


def begin_ingest_run(
    source: Mapping,
    *,
    scheduled_for: datetime | str | None = None,
    started_at: datetime | str | None = None,
    trace_id: str | None = None,
    parent_run_id: str | None = None,
) -> IngestRun:
    started = _canonical_time(started_at)
    scheduled = _canonical_time(scheduled_for or started)
    config_json, config_hash = _source_config(source)
    run_id = f"run_{uuid4().hex}"
    trace = trace_id or f"trace_{uuid4().hex}"
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        source_row = db.execute(
            "SELECT id FROM sources WHERE key=?", (str(source.get("key") or ""),)
        ).fetchone()
        if not source_row:
            raise IngestEvidenceError("source must be registered before an ingest run starts")
        config_row = db.execute(
            """SELECT id FROM source_config_versions
               WHERE source_id=? AND config_hash=?""",
            (source_row["id"], config_hash),
        ).fetchone()
        if config_row:
            config_id = config_row["id"]
        else:
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM source_config_versions WHERE source_id=?",
                (source_row["id"],),
            ).fetchone()[0]
            config_id = f"source_config_{uuid4().hex}"
            db.execute(
                """INSERT INTO source_config_versions(
                       id,source_id,version,config_json,config_hash,available_at
                   ) VALUES(?,?,?,?,?,?)""",
                (config_id, source_row["id"], version, config_json, config_hash, started),
            )
        dataset = db.execute(
            "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
        ).fetchone()
        db.execute(
            """INSERT INTO ingest_runs(
                   id,source_id,config_version_id,dataset_id,dataset_epoch,
                   parent_run_id,scheduled_for,
                   started_at,status,trace_id
               ) VALUES(?,?,?,?,?,?,?,?, 'running',?)""",
            (
                run_id, source_row["id"], config_id,
                dataset["dataset_id"], dataset["current_epoch"],
                parent_run_id, scheduled, started, trace,
            ),
        )
    return IngestRun(run_id, source_row["id"], config_id, trace)


def observe_candidate(
    run: IngestRun,
    candidate: Mapping,
    *,
    ordinal: int,
    observed_at: datetime | str | None = None,
    payload_kind: str = "generated_metadata",
    retention_class: str = "private-metadata",
) -> RawObservation:
    if ordinal < 0:
        raise ValueError("observation ordinal must be non-negative")
    observed = _canonical_time(observed_at)
    payload = _candidate_payload(candidate)
    digest, reference = store_payload(payload)
    external_id = _safe_url(candidate.get("url")) or str(candidate.get("external_id") or "")
    record_id = f"raw_{uuid4().hex}"
    observation_id = f"observation_{uuid4().hex}"
    final_url = _safe_url(candidate.get("url")) or None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT source_id,status FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()
        if not active or active["status"] != "running" or active["source_id"] != run.source_id:
            raise IngestEvidenceError("observation requires its active ingest run")
        inserted = db.execute(
            """INSERT OR IGNORE INTO raw_records(
                   id,first_ingest_run_id,source_id,external_id,observed_at,ingested_at,
                   request_url,final_url,http_status,selected_headers,media_type,encoding,
                   payload_sha256,payload_ref,payload_kind,truncated,size_bytes,retention_class
               ) VALUES(?,?,?,?,?,?,?,?,NULL,'{}','application/json','utf-8',?,?,?,?,?,?)""",
            (
                record_id, run.id, run.source_id, external_id, observed, observed,
                None, final_url, digest, reference, payload_kind, 0, len(payload), retention_class,
            ),
        ).rowcount
        if not inserted:
            row = db.execute(
                """SELECT id,payload_ref,size_bytes FROM raw_records
                   WHERE source_id=? AND external_id=? AND payload_sha256=?""",
                (run.source_id, external_id, digest),
            ).fetchone()
            if not row:
                raise IngestEvidenceError("raw record conflict could not be resolved")
            record_id = row["id"]
            if row["payload_ref"] != reference or row["size_bytes"] != len(payload):
                raise PayloadIntegrityError("raw record metadata disagrees with CAS content")
        existing = db.execute(
            """SELECT id,raw_record_id FROM raw_observations
               WHERE ingest_run_id=? AND ordinal=?""",
            (run.id, ordinal),
        ).fetchone()
        if existing:
            if existing["raw_record_id"] != record_id:
                raise IngestEvidenceError("observation ordinal was reused for different content")
            observation_id = existing["id"]
        else:
            db.execute(
                """INSERT INTO raw_observations(
                       id,raw_record_id,ingest_run_id,ordinal,observed_at
                   ) VALUES(?,?,?,?,?)""",
                (observation_id, record_id, run.id, ordinal, observed),
            )
    verify_payload(reference, digest)
    return RawObservation(
        record_id, observation_id, digest, reference, len(payload), bool(inserted)
    )


def finish_ingest_run(
    run: IngestRun,
    *,
    status: str,
    raw_count: int,
    accepted_count: int,
    duplicate_count: int,
    rejected_count: int,
    byte_count: int,
    request_count: int = 1,
    error_code: str | None = None,
    finished_at: datetime | str | None = None,
    watermark_after: str | None = None,
) -> None:
    if status not in _FINAL_RUN_STATES:
        raise ValueError(f"invalid final ingest status: {status}")
    counts = (request_count, raw_count, accepted_count, duplicate_count, rejected_count, byte_count)
    if any(value < 0 for value in counts):
        raise ValueError("ingest counters cannot be negative")
    if accepted_count + rejected_count != raw_count:
        raise ValueError("accepted and rejected counts must partition raw candidates")
    if duplicate_count > accepted_count:
        raise ValueError("duplicate count cannot exceed accepted count")
    finished = _canonical_time(finished_at)
    safe_error = re.sub(r"[^a-z0-9_.-]+", "_", (error_code or "").casefold())[:100] or None
    with get_db() as db:
        updated = db.execute(
            """UPDATE ingest_runs SET finished_at=?,status=?,request_count=?,raw_count=?,
                      accepted_count=?,duplicate_count=?,rejected_count=?,bytes=?,
                      watermark_after=?,error_code=?
               WHERE id=? AND status='running'""",
            (
                finished, status, request_count, raw_count, accepted_count,
                duplicate_count, rejected_count, byte_count, watermark_after,
                safe_error, run.id,
            ),
        )
        if updated.rowcount != 1:
            raise IngestEvidenceError("ingest run is missing or already finished")
