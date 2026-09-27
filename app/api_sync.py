from __future__ import annotations

"""Principal-bound creation and verified reads for immutable sync snapshots."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from . import config
from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .jobs import _enqueue
from .timeutil import format_utc, parse_utc

RESOURCES = (
    "items", "events", "entities", "topics", "sources", "analyses",
    "signals", "reports", "evidence",
)
RESOURCE_SCOPES = {
    "items": "read:items", "events": "read:events",
    "entities": "read:catalog", "topics": "read:catalog",
    "sources": "read:catalog", "analyses": "read:analyses",
    "signals": "read:signals", "reports": "read:reports",
    "evidence": "read:evidence",
}
_IDEMPOTENCY = re.compile(r"^[\x21-\x7e]{8,128}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotCreate(_StrictModel):
    resources: list[Literal[
        "items", "events", "entities", "topics", "sources", "analyses",
        "signals", "reports", "evidence",
    ]] = Field(min_length=1)
    scope: Literal["research", "selected"]

    @field_validator("resources")
    @classmethod
    def unique_resources(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("resources must be unique")
        return value


class SnapshotManifestEntry(_StrictModel):
    resource: Literal[
        "items", "events", "entities", "topics", "sources", "analyses",
        "signals", "reports", "evidence",
    ]
    count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SnapshotView(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    status: Literal["pending", "running", "ready", "failed", "expired"]
    resources: list[str]
    scope: Literal["research", "selected"]
    dataset_epoch: str
    high_water: int | None
    knowledge_cutoff: dict[str, JsonValue] | None
    expires_at: str | None
    resume_cursor: str | None
    manifest: list[SnapshotManifestEntry]
    checksum_algorithm: Literal["jcs-sha256-v1"] = "jcs-sha256-v1"
    error_code: str | None
    created_at: str


class SnapshotResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: dict[str, JsonValue] | None
    data: SnapshotView


class SnapshotPagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["snapshot"] = "snapshot"


class SnapshotPageResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: dict[str, JsonValue]
    data: list[dict[str, JsonValue]]
    snapshot_id: str
    high_water: int = Field(ge=0)
    resource: str
    page_sha256: str
    pagination: SnapshotPagination


class SnapshotError(RuntimeError):
    pass


class SnapshotNotFound(SnapshotError):
    pass


class SnapshotConflict(SnapshotError):
    pass


class SnapshotNotReady(SnapshotError):
    pass


class SnapshotExpired(SnapshotError):
    pass


class SnapshotLimitReached(SnapshotError):
    pass


class SnapshotUnavailable(SnapshotError):
    pass


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("snapshot time must include a timezone")
    return current.astimezone(timezone.utc)


def _jcs(value: object) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotUnavailable("snapshot value is not canonical JSON") from exc


def required_scopes(resources: list[str]) -> frozenset[str]:
    return frozenset({"read:sync", *(RESOURCE_SCOPES[item] for item in resources)})


def _principal_row(db: sqlite3.Connection, principal: ApiPrincipal, current: datetime):
    row = db.execute(
        """SELECT key.expires_at,key.revoked_at,key.scopes_json,
                  consumer.status,consumer.authz_version
           FROM api_keys AS key JOIN api_consumers AS consumer
             ON consumer.id=key.consumer_id
           WHERE key.key_id=? AND key.consumer_id=?""",
        (principal.key_id, principal.consumer_id),
    ).fetchone()
    if row is None or row["status"] != "active" or row["revoked_at"] is not None:
        raise SnapshotConflict("snapshot authorization is no longer active")
    if row["authz_version"] != principal.authz_version:
        raise SnapshotConflict("snapshot authorization changed")
    try:
        scopes = frozenset(json.loads(row["scopes_json"]))
        expiry = parse_utc(row["expires_at"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotUnavailable("snapshot authorization is invalid") from exc
    if scopes != principal.scopes or expiry <= current:
        raise SnapshotConflict("snapshot authorization changed")
    return expiry


def create_snapshot(
    db: sqlite3.Connection, *, principal: ApiPrincipal, request_id: str,
    idempotency_key: str, request: SnapshotCreate, now: datetime | None = None,
) -> SnapshotResponse:
    current = _now(now)
    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(idempotency_key):
        raise ValueError("invalid idempotency key")
    if request.scope == "selected":
        raise SnapshotNotReady("selected projection has no approved policy")
    needed = required_scopes(request.resources)
    if not needed <= principal.scopes:
        raise PermissionError("snapshot resource scopes are incomplete")
    request_object = request.model_dump(mode="json")
    request_bytes = _jcs(request_object)
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    created_at = format_utc(current)

    db.execute("BEGIN IMMEDIATE")
    key_expiry = _principal_row(db, principal, current)
    existing = db.execute(
        """SELECT id,request_sha256 FROM sync_snapshot_requests
           WHERE key_id=? AND authz_version=? AND idempotency_key=?""",
        (principal.key_id, principal.authz_version, idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["request_sha256"] != request_sha:
            raise SnapshotConflict("idempotency key belongs to another request")
        return get_snapshot(
            db, principal=principal, request_id=request_id,
            snapshot_id=existing["id"], now=current, begin=False,
        )
    day_ago = format_utc(current - timedelta(hours=24))
    active = db.execute(
        """SELECT COUNT(*) FROM sync_snapshot_requests
           WHERE key_id=? AND authz_version=? AND state IN ('pending','running')
             AND expires_at>?""",
        (principal.key_id, principal.authz_version, created_at),
    ).fetchone()[0]
    daily = db.execute(
        """SELECT COUNT(*) FROM sync_snapshot_requests
           WHERE key_id=? AND authz_version=? AND created_at>=?""",
        (principal.key_id, principal.authz_version, day_ago),
    ).fetchone()[0]
    if active >= 1 or daily >= 5:
        raise SnapshotLimitReached("snapshot creation limit reached")
    state = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise SnapshotUnavailable("dataset identity is unavailable")
    snapshot_id = f"snapshot_{uuid4().hex}"
    expires_at = format_utc(min(current + timedelta(hours=24), key_expiry))
    job = _enqueue(
        db, kind="sync-snapshot",
        idempotency_key=(
            f"sync-snapshot:{principal.key_id}:{principal.authz_version}:{idempotency_key}"
        ),
        subject_id=snapshot_id, input_version=state["current_epoch"],
        payload={"snapshot_id": snapshot_id}, priority=0,
        scheduled_for=created_at, max_attempts=3, created_at=created_at,
    )
    db.execute(
        """INSERT INTO sync_snapshot_requests(
               id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
               idempotency_key,request_json,request_sha256,resources_json,
               scopes_json,projection_scope,state,job_id,created_at,expires_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
        (snapshot_id, state["dataset_id"], state["current_epoch"],
         principal.consumer_id, principal.key_id, principal.authz_version,
         idempotency_key, request_bytes.decode("utf-8"), request_sha,
         json.dumps(request.resources, separators=(",", ":")),
         json.dumps(sorted(principal.scopes), separators=(",", ":")),
         request.scope, job.id, created_at, expires_at),
    )
    return get_snapshot(
        db, principal=principal, request_id=request_id,
        snapshot_id=snapshot_id, now=current, begin=False,
    )


def _owned_request(db: sqlite3.Connection, principal: ApiPrincipal, snapshot_id: str):
    if not isinstance(snapshot_id, str) or len(snapshot_id) > 128:
        raise SnapshotNotFound("snapshot does not exist")
    row = db.execute(
        "SELECT * FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
    ).fetchone()
    if (row is None or row["key_id"] != principal.key_id
            or row["consumer_id"] != principal.consumer_id):
        raise SnapshotNotFound("snapshot does not exist")
    if row["authz_version"] != principal.authz_version:
        raise SnapshotConflict("snapshot authorization changed")
    return row


def _manifest(
    db: sqlite3.Connection, snapshot_id: str, raw: str,
) -> tuple[dict, list[SnapshotManifestEntry]]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotUnavailable("snapshot manifest is invalid") from exc
    if not isinstance(decoded, dict):
        raise SnapshotUnavailable("snapshot manifest is invalid")
    rows = db.execute(
        """SELECT resource,record_count,content_sha256
           FROM sync_snapshot_resources WHERE snapshot_id=? ORDER BY resource""",
        (snapshot_id,),
    ).fetchall()
    entries = [SnapshotManifestEntry(
        resource=row["resource"], count=row["record_count"], sha256=row["content_sha256"]
    ) for row in rows]
    expected = sorted(decoded.get("resources", []), key=lambda item: item.get("resource", ""))
    actual = [{"resource": item.resource, "record_count": item.count,
               "page_count": next((x.get("page_count") for x in expected
                                   if x.get("resource") == item.resource), None),
               "content_sha256": item.sha256} for item in entries]
    if expected != actual:
        raise SnapshotUnavailable("snapshot manifest does not match its ledger")
    return decoded, entries


def get_snapshot(
    db: sqlite3.Connection, *, principal: ApiPrincipal, request_id: str,
    snapshot_id: str, now: datetime | None = None, begin: bool = True,
) -> SnapshotResponse:
    current = _now(now)
    if begin:
        db.execute("BEGIN")
    _principal_row(db, principal, current)
    request = _owned_request(db, principal, snapshot_id)
    resources = json.loads(request["resources_json"])
    expired = parse_utc(request["expires_at"]) <= current
    state = "expired" if expired else request["state"]
    high_water = None
    cutoff = None
    entries: list[SnapshotManifestEntry] = []
    if request["state"] == "ready":
        snapshot = db.execute("SELECT * FROM sync_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if snapshot is None:
            raise SnapshotUnavailable("ready snapshot has no immutable result")
        raw = snapshot["manifest_json"]
        if hashlib.sha256(raw.encode("utf-8")).hexdigest() != snapshot["manifest_sha256"]:
            raise SnapshotUnavailable("snapshot manifest hash does not match")
        manifest, entries = _manifest(db, snapshot_id, raw)
        high_water = snapshot["high_water"]
        cutoff = manifest.get("knowledge_cutoff")
        if not isinstance(cutoff, dict):
            raise SnapshotUnavailable("snapshot knowledge cutoff is invalid")
    view = SnapshotView(
        id=request["id"], status=state, resources=resources,
        scope=request["projection_scope"], dataset_epoch=request["dataset_epoch"],
        high_water=high_water, knowledge_cutoff=cutoff, expires_at=request["expires_at"],
        resume_cursor=None, manifest=entries, error_code=request["error_code"],
        created_at=request["created_at"],
    )
    return SnapshotResponse(
        dataset_id=request["dataset_id"], dataset_epoch=request["dataset_epoch"],
        request_id=request_id, generated_at=format_utc(current),
        knowledge_cutoff=cutoff, data=view,
    )


def _verified_page(row: sqlite3.Row) -> list[dict]:
    root = Path(config.RUNTIME_PATH).resolve()
    path = (root / row["payload_ref"]).resolve()
    if path == root or root not in path.parents:
        raise SnapshotUnavailable("snapshot page path escapes runtime storage")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SnapshotUnavailable("snapshot page is missing") from exc
    if (len(payload) != row["size_bytes"]
            or hashlib.sha256(payload).hexdigest() != row["payload_sha256"]):
        raise SnapshotUnavailable("snapshot page hash does not match")
    try:
        records = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotUnavailable("snapshot page is invalid") from exc
    if (not isinstance(records, list) or len(records) != row["record_count"]
            or not records or records[0].get("resource_id") != row["first_resource_id"]
            or records[-1].get("resource_id") != row["last_resource_id"]):
        raise SnapshotUnavailable("snapshot page does not match its ledger")
    return records


def read_snapshot_page(
    db: sqlite3.Connection, *, principal: ApiPrincipal, request_id: str,
    snapshot_id: str, resource: str, limit: int, cursor: str | None,
    now: datetime | None = None,
) -> SnapshotPageResponse:
    current = _now(now)
    if resource not in RESOURCES or not 1 <= limit <= 100:
        raise ValueError("invalid page request")
    db.execute("BEGIN")
    _principal_row(db, principal, current)
    request = _owned_request(db, principal, snapshot_id)
    if parse_utc(request["expires_at"]) <= current:
        raise SnapshotExpired("snapshot expired")
    if request["state"] != "ready":
        raise SnapshotNotReady("snapshot is not ready")
    resources = json.loads(request["resources_json"])
    if resource not in resources:
        raise SnapshotNotFound("snapshot resource does not exist")
    needed = RESOURCE_SCOPES[resource]
    if needed not in principal.scopes:
        raise PermissionError("snapshot resource scope is missing")
    snapshot = db.execute("SELECT * FROM sync_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise SnapshotUnavailable("snapshot result is unavailable")
    raw_manifest = snapshot["manifest_json"]
    if hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest() != snapshot["manifest_sha256"]:
        raise SnapshotUnavailable("snapshot manifest hash does not match")
    manifest, _ = _manifest(db, snapshot_id, raw_manifest)
    cutoff = manifest.get("knowledge_cutoff")
    if not isinstance(cutoff, dict):
        raise SnapshotUnavailable("snapshot knowledge cutoff is invalid")
    page_number, offset = 1, 0
    cursor_resource = f"sync:{snapshot_id}:{resource}"
    if cursor:
        position = decode_cursor(
            db, principal, cursor, resource=cursor_resource, filters={},
            dataset_epoch=request["dataset_epoch"], now=current.timestamp(),
        )
        try:
            page_number, offset = (int(part) for part in position.last_id.split(":"))
        except (ValueError, TypeError) as exc:
            raise SnapshotUnavailable("snapshot cursor position is invalid") from exc
        if page_number < 1 or offset < 0:
            raise SnapshotUnavailable("snapshot cursor position is invalid")
    result: list[dict] = []
    while len(result) < limit:
        row = db.execute(
            """SELECT * FROM sync_snapshot_pages
               WHERE snapshot_id=? AND resource=? AND page_number=?""",
            (snapshot_id, resource, page_number),
        ).fetchone()
        if row is None:
            break
        records = _verified_page(row)
        if offset >= len(records):
            raise SnapshotUnavailable("snapshot cursor position is invalid")
        take = min(limit - len(result), len(records) - offset)
        result.extend(records[offset:offset + take])
        offset += take
        if offset == len(records):
            page_number += 1
            offset = 0
    more = db.execute(
        """SELECT 1 FROM sync_snapshot_pages
           WHERE snapshot_id=? AND resource=? AND page_number>=? LIMIT 1""",
        (snapshot_id, resource, page_number),
    ).fetchone() is not None
    next_cursor = None
    if more:
        next_cursor = encode_cursor(
            db, principal, resource=cursor_resource, filters={},
            last_id=f"{page_number}:{offset}", dataset_epoch=request["dataset_epoch"],
            now=current.timestamp(),
        )
    page_bytes = _jcs(result)
    return SnapshotPageResponse(
        dataset_id=request["dataset_id"], dataset_epoch=request["dataset_epoch"],
        request_id=request_id, generated_at=format_utc(current), knowledge_cutoff=cutoff,
        data=result, snapshot_id=snapshot_id, high_water=snapshot["high_water"],
        resource=resource, page_sha256=hashlib.sha256(page_bytes).hexdigest(),
        pagination=SnapshotPagination(limit=limit, next_cursor=next_cursor),
    )
