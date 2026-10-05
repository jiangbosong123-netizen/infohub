from __future__ import annotations

"""Build immutable reliable-sync snapshots from a completed SQLite backup."""

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

import rfc8785

from . import config
from .database import get_db
from .timeutil import parse_utc, utc_now


SNAPSHOT_SCHEMA_VERSION = "sync-snapshot-v1"
HASH_ALGORITHM = "jcs-sha256-v1"
DEFAULT_PAGE_SIZE = 100
RESOURCE_TYPES = {
    "items": "item",
    "events": "event",
    "entities": "entity",
    "topics": "topic",
    "sources": "source",
    "analyses": "analysis",
    "signals": "signal",
    "reports": "report",
    "evidence": "evidence",
}
RESOURCE_SCOPES = {
    "items": "read:items",
    "events": "read:events",
    "entities": "read:catalog",
    "topics": "read:catalog",
    "sources": "read:catalog",
    "analyses": "read:analyses",
    "signals": "read:signals",
    "reports": "read:reports",
    "evidence": "read:evidence",
}


class SyncSnapshotError(RuntimeError):
    """The requested export cannot be completed without violating its contract."""


@dataclass(frozen=True)
class SnapshotBuild:
    snapshot_id: str
    dataset_id: str
    dataset_epoch: str
    high_water: int
    knowledge_checkpoint_id: str
    resource_count: int
    record_count: int
    manifest_sha256: str
    expires_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _jcs(value: object) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise SyncSnapshotError("snapshot content cannot be represented as RFC 8785 JSON") from exc


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_root(snapshot_id: str) -> Path:
    digest = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()
    return Path(config.RUNTIME_PATH) / "sync-snapshots" / f"snapshot-{digest}"


def _relative(path: Path) -> str:
    return path.relative_to(Path(config.RUNTIME_PATH)).as_posix()


def _load_array(value: str, name: str) -> list[str]:
    try:
        result = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SyncSnapshotError(f"snapshot {name} manifest is invalid") from exc
    if (
        not isinstance(result, list)
        or not result
        or any(not isinstance(item, str) for item in result)
        or len(result) != len(set(result))
    ):
        raise SyncSnapshotError(f"snapshot {name} manifest is invalid")
    return result


def _assert_request_authorized(db: sqlite3.Connection, request: sqlite3.Row, now: str) -> tuple[list[str], list[str]]:
    resources = _load_array(request["resources_json"], "resource")
    scopes = _load_array(request["scopes_json"], "scope")
    raw_request = request["request_json"].encode("utf-8")
    if _sha(raw_request) != request["request_sha256"]:
        raise SyncSnapshotError("snapshot request hash does not match")
    try:
        request_value = json.loads(request["request_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise SyncSnapshotError("snapshot request is invalid") from exc
    if (
        not isinstance(request_value, dict)
        or request_value.get("resources") != json.loads(request["resources_json"])
        or request_value.get("scope") != request["projection_scope"]
    ):
        raise SyncSnapshotError("snapshot request and frozen manifest disagree")
    if request["projection_scope"] != "research":
        raise SyncSnapshotError("selected snapshot projection has no approved selection policy")
    if set(resources) - RESOURCE_TYPES.keys():
        raise SyncSnapshotError("snapshot contains an unsupported resource")
    required = {"read:sync", *(RESOURCE_SCOPES[resource] for resource in resources)}
    if not required <= set(scopes):
        raise SyncSnapshotError("snapshot request does not contain every required scope")
    row = db.execute(
        """SELECT key.scopes_json,key.expires_at,key.revoked_at,
                  consumer.status,consumer.authz_version
           FROM api_keys AS key
           JOIN api_consumers AS consumer ON consumer.id=key.consumer_id
           WHERE key.key_id=? AND key.consumer_id=?""",
        (request["key_id"], request["consumer_id"]),
    ).fetchone()
    if row is None or row["revoked_at"] is not None or row["status"] != "active":
        raise SyncSnapshotError("snapshot authorization is no longer active")
    current_scopes = _load_array(row["scopes_json"], "current scope")
    if row["authz_version"] != request["authz_version"] or set(current_scopes) != set(scopes):
        raise SyncSnapshotError("snapshot authorization changed; create a new snapshot")
    if parse_utc(row["expires_at"]) <= parse_utc(now):
        raise SyncSnapshotError("snapshot API key has expired")
    if parse_utc(request["expires_at"]) <= parse_utc(now):
        raise SyncSnapshotError("snapshot request has expired")
    if parse_utc(request["expires_at"]) > parse_utc(row["expires_at"]):
        raise SyncSnapshotError("snapshot lifetime exceeds API key lifetime")
    return sorted(resources), sorted(scopes)


def _assert_job_lease(
    db: sqlite3.Connection, request: sqlite3.Row, job_id: str, lease_token: str, now: str,
) -> None:
    row = db.execute(
        "SELECT state,lease_token,lease_expires_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    if (
        request["job_id"] != job_id
        or row is None
        or row["state"] != "running"
        or row["lease_token"] != lease_token
        or row["lease_expires_at"] is None
        or parse_utc(row["lease_expires_at"]) <= parse_utc(now)
    ):
        raise SyncSnapshotError("snapshot worker no longer owns the durable job lease")


def _backup_database(target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as source:
        backup = sqlite3.connect(target)
        try:
            source.backup(backup)
            backup.execute("PRAGMA journal_mode=DELETE")
            backup.commit()
        finally:
            backup.close()
    # Streamed: the backup is a full copy of the live database (4.9 GB at rehearsal size), and
    # reading it whole held all of it in the worker's memory just to hash it.
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_durable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _backup_identity(db: sqlite3.Connection) -> tuple[str, str, int, int]:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise SyncSnapshotError("snapshot backup has no dataset identity")
    high_water = db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM change_log WHERE dataset_id=? AND epoch=?",
        (identity["dataset_id"], identity["current_epoch"]),
    ).fetchone()[0]
    schema_version = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
    if schema_version <= 0:
        raise SyncSnapshotError("snapshot backup has no supported schema version")
    return identity["dataset_id"], identity["current_epoch"], high_water, schema_version


def _resource_records(
    db: sqlite3.Connection, *, dataset_id: str, epoch: str, high_water: int,
    resource: str,
) -> Iterator[tuple[str, bytes]]:
    """Yield each resource's latest change as (resource_id, canonical record bytes).

    Records stream in byte order of their UTF-8 resource ids, which is SQLite's BINARY order
    for this UTF-8 database; holding a whole resource at once took 1.7 GB for the 358k analyses
    of a rehearsal-size dataset.
    """
    resource_type = RESOURCE_TYPES[resource]
    rows = db.execute(
        """SELECT change.* FROM change_log AS change
           JOIN (
               SELECT resource_id,MAX(seq) AS seq
               FROM change_log
               WHERE dataset_id=? AND epoch=? AND resource_type=? AND seq<=?
               GROUP BY resource_id
           ) AS latest ON latest.seq=change.seq
           ORDER BY change.resource_id COLLATE BINARY""",
        (dataset_id, epoch, resource_type, high_water),
    )
    previous: bytes | None = None
    for row in rows:
        if row["hash_algorithm"] != HASH_ALGORITHM:
            raise SyncSnapshotError("snapshot change uses an unsupported hash algorithm")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SyncSnapshotError("snapshot change payload is invalid") from exc
        if not isinstance(payload, dict) or _sha(_jcs(payload)) != row["payload_sha256"]:
            raise SyncSnapshotError("snapshot change payload hash does not match")
        key = row["resource_id"].encode("utf-8")
        if previous is not None and key <= previous:
            raise SyncSnapshotError("snapshot changes are not in resource id byte order")
        previous = key
        yield row["resource_id"], _jcs({
            "resource_type": resource_type,
            "resource_id": row["resource_id"],
            "version_id": row["version_id"],
            "payload": payload,
        })


def _write_page(
    root: Path, reference_root: Path, resource: str, page_number: int,
    records: list[tuple[str, bytes]],
) -> dict:
    # RFC 8785 writes an array as its canonical elements joined by commas, so this is exactly
    # the canonical form of the page's records without serializing them a second time.
    payload = b"[" + b",".join(record for _, record in records) + b"]"
    path = root / "pages" / resource / f"{page_number:08d}.json"
    _write_durable(path, payload)
    return {
        "page_number": page_number,
        "first_resource_id": records[0][0],
        "last_resource_id": records[-1][0],
        "record_count": len(records),
        "payload_ref": _relative(reference_root / path.relative_to(root)),
        "payload_sha256": _sha(payload),
        "size_bytes": len(payload),
    }


def _write_resources(
    backup: sqlite3.Connection, root: Path, reference_root: Path, *, snapshot_id: str,
    dataset_id: str, epoch: str, high_water: int, resources: list[str], page_size: int,
) -> tuple[list[dict], int]:
    manifests: list[dict] = []
    total = 0
    for resource in resources:
        stream = hashlib.sha256()
        pages: list[dict] = []
        page: list[tuple[str, bytes]] = []
        count = 0
        for resource_id, record in _resource_records(
            backup, dataset_id=dataset_id, epoch=epoch, high_water=high_water,
            resource=resource,
        ):
            stream.update(record)
            stream.update(b"\n")
            count += 1
            page.append((resource_id, record))
            if len(page) == page_size:
                pages.append(_write_page(root, reference_root, resource, len(pages) + 1, page))
                page = []
        if page:
            pages.append(_write_page(root, reference_root, resource, len(pages) + 1, page))
        manifests.append({
            "resource": resource,
            "record_count": count,
            "page_count": len(pages),
            "content_sha256": stream.hexdigest(),
            "pages": pages,
        })
        total += count
    return manifests, total


def _verify_files(root: Path, manifest_bytes: bytes, resources: list[dict]) -> None:
    if (root / "manifest.json").read_bytes() != manifest_bytes:
        raise SyncSnapshotError("snapshot manifest file failed verification")
    for resource in resources:
        for page in resource["pages"]:
            path = Path(config.RUNTIME_PATH) / page["payload_ref"]
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise SyncSnapshotError("snapshot page file is missing") from exc
            if len(payload) != page["size_bytes"] or _sha(payload) != page["payload_sha256"]:
                raise SyncSnapshotError("snapshot page file failed verification")


def _existing_build(db: sqlite3.Connection, snapshot_id: str) -> SnapshotBuild | None:
    row = db.execute(
        """SELECT snapshot.*,request.state
           FROM sync_snapshots AS snapshot
           JOIN sync_snapshot_requests AS request ON request.id=snapshot.id
           WHERE snapshot.id=?""",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        return None
    if row["state"] != "ready":
        raise SyncSnapshotError("snapshot result exists without a ready request")
    return SnapshotBuild(
        snapshot_id=row["id"], dataset_id=row["dataset_id"],
        dataset_epoch=row["dataset_epoch"], high_water=row["high_water"],
        knowledge_checkpoint_id=row["knowledge_checkpoint_id"],
        resource_count=row["resource_count"], record_count=row["record_count"],
        manifest_sha256=row["manifest_sha256"], expires_at=row["expires_at"],
    )


def build_sync_snapshot(
    snapshot_id: str, *, job_id: str, lease_token: str, page_size: int = DEFAULT_PAGE_SIZE,
    now: str | None = None, after_backup: Callable[[], None] | None = None,
) -> SnapshotBuild:
    """Build one snapshot; all public rows become visible in one final transaction."""
    if not 1 <= page_size <= 1000:
        raise ValueError("page_size must be between 1 and 1000")
    current = now or utc_now()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = _existing_build(db, snapshot_id)
        if existing:
            return existing
        request = db.execute(
            "SELECT * FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
        ).fetchone()
        if request is None or request["state"] not in {"pending", "running"}:
            raise SyncSnapshotError("snapshot request is not buildable")
        _assert_job_lease(db, request, job_id, lease_token, current)
        resources, scopes = _assert_request_authorized(db, request, current)
        state = db.execute(
            "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
        ).fetchone()
        if (
            state is None
            or state["dataset_id"] != request["dataset_id"]
            or state["current_epoch"] != request["dataset_epoch"]
        ):
            raise SyncSnapshotError("snapshot dataset epoch changed; create a new snapshot")
        if request["state"] == "pending":
            db.execute(
                "UPDATE sync_snapshot_requests SET state='running',started_at=? WHERE id=?",
                (current, snapshot_id),
            )
        partial = sum(db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()[0] for table in ("sync_snapshot_resources", "sync_snapshot_pages"))
        if partial:
            raise SyncSnapshotError("snapshot contains an incomplete immutable manifest")

    final_root = _safe_root(snapshot_id)
    staging = final_root.with_name(final_root.name + f".staging-{uuid4().hex}")
    if final_root.exists():
        shutil.rmtree(final_root)
    staging.mkdir(parents=True)
    backup_path = staging / "source.db"
    checkpoint_id = f"checkpoint_{uuid4().hex}"
    try:
        backup_sha256 = _backup_database(backup_path)
        backup = sqlite3.connect(backup_path)
        backup.row_factory = sqlite3.Row
        try:
            dataset_id, epoch, high_water, source_schema_version = _backup_identity(backup)
            if dataset_id != request["dataset_id"] or epoch != request["dataset_epoch"]:
                raise SyncSnapshotError("completed backup does not match the snapshot request")
            observed_at = now or utc_now()
            if after_backup:
                after_backup()
            resource_manifests, record_count = _write_resources(
                backup, staging, final_root, snapshot_id=snapshot_id, dataset_id=dataset_id,
                epoch=epoch, high_water=high_water, resources=resources,
                page_size=page_size,
            )
        finally:
            backup.close()
        manifest = {
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "hash_algorithm": HASH_ALGORITHM,
            "snapshot_id": snapshot_id,
            "dataset_id": dataset_id,
            "dataset_epoch": epoch,
            "high_water": high_water,
            "knowledge_cutoff": {
                "basis": "observed_checkpoint",
                "as_of": observed_at,
                "checkpoint_id": checkpoint_id,
                "dataset_epoch": epoch,
                "high_water": high_water,
                "observed_at": observed_at,
                "clock_status": "unknown",
            },
            "authz_version": request["authz_version"],
            "projection_scope": request["projection_scope"],
            "scopes": scopes,
            "expires_at": request["expires_at"],
            "source_schema_version": source_schema_version,
            "backup_sha256": backup_sha256,
            "resource_count": len(resources),
            "record_count": record_count,
            "resources": [{key: value for key, value in item.items() if key != "pages"}
                          for item in resource_manifests],
        }
        manifest_bytes = _jcs(manifest)
        _write_durable(staging / "manifest.json", manifest_bytes)
        os.replace(staging, final_root)
        _verify_files(final_root, manifest_bytes, resource_manifests)

        ready_at = now or utc_now()
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            request = db.execute(
                "SELECT * FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
            ).fetchone()
            if request is None or request["state"] != "running":
                raise SyncSnapshotError("snapshot request changed while its backup was building")
            _assert_job_lease(db, request, job_id, lease_token, ready_at)
            _assert_request_authorized(db, request, ready_at)
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            if state["dataset_id"] != dataset_id or state["current_epoch"] != epoch:
                raise SyncSnapshotError("dataset epoch changed while the snapshot was building")
            db.execute(
                """INSERT INTO knowledge_checkpoints(
                       id,dataset_id,epoch,high_water,observed_at,clock_status,clock_check_id
                   ) VALUES(?,?,?,?,?,'unknown',NULL)""",
                (checkpoint_id, dataset_id, epoch, high_water, observed_at),
            )
            for resource in resource_manifests:
                db.execute(
                    """INSERT INTO sync_snapshot_resources(
                           snapshot_id,resource,record_count,page_count,content_sha256
                       ) VALUES(?,?,?,?,?)""",
                    (snapshot_id, resource["resource"], resource["record_count"],
                     resource["page_count"], resource["content_sha256"]),
                )
                for page in resource["pages"]:
                    db.execute(
                        """INSERT INTO sync_snapshot_pages(
                               snapshot_id,resource,page_number,first_resource_id,
                               last_resource_id,record_count,payload_ref,payload_sha256,size_bytes
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (snapshot_id, resource["resource"], page["page_number"],
                         page["first_resource_id"], page["last_resource_id"],
                         page["record_count"], page["payload_ref"],
                         page["payload_sha256"], page["size_bytes"]),
                    )
            db.execute(
                """INSERT INTO sync_snapshots(
                       id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                       projection_scope,high_water,knowledge_checkpoint_id,backup_sha256,
                       source_schema_version,manifest_json,manifest_sha256,resource_count,
                       record_count,snapshot_schema_version,created_at,ready_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, dataset_id, epoch, request["consumer_id"], request["key_id"],
                 request["authz_version"], request["projection_scope"], high_water,
                 checkpoint_id, backup_sha256, source_schema_version,
                 manifest_bytes.decode("utf-8"), _sha(manifest_bytes), len(resources),
                 record_count, SNAPSHOT_SCHEMA_VERSION, request["created_at"], ready_at,
                 request["expires_at"]),
            )
            db.execute(
                """UPDATE sync_snapshot_requests
                   SET state='ready',finished_at=? WHERE id=?""",
                (ready_at, snapshot_id),
            )
        return SnapshotBuild(
            snapshot_id=snapshot_id, dataset_id=dataset_id, dataset_epoch=epoch,
            high_water=high_water, knowledge_checkpoint_id=checkpoint_id,
            resource_count=len(resources), record_count=record_count,
            manifest_sha256=_sha(manifest_bytes), expires_at=request["expires_at"],
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if final_root.exists():
            with get_db() as db:
                published = db.execute(
                    "SELECT 1 FROM sync_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
            if not published:
                shutil.rmtree(final_root, ignore_errors=True)
        raise


def fail_sync_snapshot(snapshot_id: str, error_code: str, *, now: str | None = None) -> None:
    code = error_code.strip()[:100] or "snapshot_failed"
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT state,started_at FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
        ).fetchone()
        if row and row["state"] in {"pending", "running"}:
            db.execute(
                """UPDATE sync_snapshot_requests
                   SET state='failed',error_code=?,finished_at=? WHERE id=?""",
                (code, now or utc_now(), snapshot_id),
            )
