from __future__ import annotations

"""Minimal downstream store for InfoHub's snapshot-plus-changes protocol.

The example owns a separate SQLite database. It never reads InfoHub's production database and it
never persists the bearer token. Callers remain responsible for HTTP, retry/backoff and secret
storage; this module validates responses and commits data together with the opaque resume cursor.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import rfc8785


RESOURCE_TYPES = {
    "items": "item", "events": "event", "entities": "entity", "topics": "topic",
    "sources": "source", "analyses": "analysis", "signals": "signal",
    "reports": "report", "evidence": "evidence",
}
OPERATIONS = {"create", "update", "withdraw", "merge", "split", "delete"}


class ConsumerProtocolError(RuntimeError):
    """The response cannot be committed without violating the sync contract."""


class SnapshotRequired(ConsumerProtocolError):
    """The local epoch no longer matches and the caller must create a fresh snapshot."""


@dataclass(frozen=True)
class ConsumerState:
    dataset_id: str
    dataset_epoch: str
    snapshot_id: str
    resources: tuple[str, ...]
    high_water: int
    resume_cursor: str
    consumer_received_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _jcs(value: object) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ConsumerProtocolError("response is not canonical-JSON compatible") from exc


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConsumerProtocolError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConsumerProtocolError(f"{name} must be a non-negative integer")
    return value


def _received_at(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("consumer receipt time must include a timezone")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ReliableSyncConsumer:
    """Validate and atomically materialize one InfoHub research subscription."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    dataset_id TEXT NOT NULL,
                    dataset_epoch TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    high_water INTEGER NOT NULL CHECK(high_water>=0),
                    resume_cursor TEXT NOT NULL,
                    consumer_received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_objects (
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    seq INTEGER NOT NULL CHECK(seq>=0),
                    snapshot_id TEXT NOT NULL,
                    consumer_received_at TEXT NOT NULL,
                    PRIMARY KEY(resource_type,resource_id)
                );
                """
            )

    def state(self) -> ConsumerState | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sync_state WHERE singleton=1").fetchone()
        if row is None:
            return None
        return ConsumerState(
            dataset_id=row["dataset_id"], dataset_epoch=row["dataset_epoch"],
            snapshot_id=row["snapshot_id"],
            resources=tuple(json.loads(row["resources_json"])),
            high_water=row["high_water"], resume_cursor=row["resume_cursor"],
            consumer_received_at=row["consumer_received_at"],
        )

    def objects(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT resource_type,resource_id,version_id,operation,payload_json,
                          payload_sha256,seq,snapshot_id,consumer_received_at
                   FROM sync_objects ORDER BY resource_type,resource_id"""
            ).fetchall()
        return [
            {
                **{key: row[key] for key in row.keys() if key != "payload_json"},
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def import_snapshot(
        self, status: Mapping[str, object],
        pages: Mapping[str, Sequence[Mapping[str, object]]],
        *, received_at: datetime | None = None,
    ) -> ConsumerState:
        """Replace the visible local projection only after the full snapshot verifies."""
        dataset_id = _text(status.get("dataset_id"), "dataset_id")
        dataset_epoch = _text(status.get("dataset_epoch"), "dataset_epoch")
        if status.get("api_version") != "v1" or status.get("schema_version") != "1.0.0":
            raise ConsumerProtocolError("snapshot response version is unsupported")
        view = status.get("data")
        if not isinstance(view, Mapping) or view.get("status") != "ready":
            raise ConsumerProtocolError("snapshot is not ready")
        if view.get("scope") != "research":
            raise ConsumerProtocolError("reference consumer only accepts research snapshots")
        snapshot_id = _text(view.get("id"), "snapshot_id")
        if (
            view.get("dataset_epoch") != dataset_epoch
            or view.get("checksum_algorithm") != "jcs-sha256-v1"
        ):
            raise ConsumerProtocolError("snapshot identity or checksum algorithm is invalid")
        high_water = _integer(view.get("high_water"), "snapshot high_water")
        resume_cursor = _text(view.get("resume_cursor"), "snapshot resume_cursor")
        resources_value = view.get("resources")
        if (
            not isinstance(resources_value, list) or not resources_value
            or any(
                not isinstance(item, str) or item not in RESOURCE_TYPES
                for item in resources_value
            )
            or len(resources_value) != len(set(resources_value))
        ):
            raise ConsumerProtocolError("snapshot resources are invalid")
        resources = tuple(resources_value)
        manifest_value = view.get("manifest")
        if not isinstance(manifest_value, list):
            raise ConsumerProtocolError("snapshot manifest is invalid")
        manifest: dict[str, Mapping[str, object]] = {}
        for entry in manifest_value:
            if not isinstance(entry, Mapping):
                raise ConsumerProtocolError("snapshot manifest is invalid")
            resource = entry.get("resource")
            if not isinstance(resource, str):
                raise ConsumerProtocolError("snapshot manifest resource is invalid")
            if resource in manifest:
                raise ConsumerProtocolError("snapshot manifest has duplicate resources")
            if resource not in resources:
                raise ConsumerProtocolError("snapshot manifest resource is unexpected")
            manifest[str(resource)] = entry
        if set(manifest) != set(resources) or set(pages) != set(resources):
            raise ConsumerProtocolError("snapshot manifest and downloaded resources differ")

        records: list[dict] = []
        for resource in resources:
            resource_records: list[dict] = []
            responses = list(pages[resource])
            for index, response in enumerate(responses):
                if (
                    response.get("api_version") != "v1"
                    or response.get("schema_version") != "1.0.0"
                    or response.get("dataset_id") != dataset_id
                    or response.get("dataset_epoch") != dataset_epoch
                    or response.get("snapshot_id") != snapshot_id
                    or response.get("resource") != resource
                    or response.get("high_water") != high_water
                ):
                    raise ConsumerProtocolError("snapshot page identity does not match")
                data = response.get("data")
                if not isinstance(data, list):
                    raise ConsumerProtocolError("snapshot page data is invalid")
                if response.get("page_sha256") != _sha(_jcs(data)):
                    raise ConsumerProtocolError("snapshot page hash does not match")
                pagination = response.get("pagination")
                if (
                    not isinstance(pagination, Mapping)
                    or pagination.get("consistency") != "snapshot"
                ):
                    raise ConsumerProtocolError("snapshot page pagination is invalid")
                if index < len(responses) - 1 and not pagination.get("next_cursor"):
                    raise ConsumerProtocolError("snapshot page chain ended early")
                if index == len(responses) - 1 and pagination.get("next_cursor") is not None:
                    raise ConsumerProtocolError("snapshot page chain is incomplete")
                resource_records.extend(data)
            previous: bytes | None = None
            stream = hashlib.sha256()
            for record in resource_records:
                if not isinstance(record, dict):
                    raise ConsumerProtocolError("snapshot record is invalid")
                resource_id = _text(record.get("resource_id"), "resource_id")
                encoded_id = resource_id.encode("utf-8")
                if previous is not None and encoded_id <= previous:
                    raise ConsumerProtocolError("snapshot resource IDs are not strictly ordered")
                previous = encoded_id
                if record.get("resource_type") != RESOURCE_TYPES[resource]:
                    raise ConsumerProtocolError("snapshot record resource type does not match")
                _text(record.get("version_id"), "version_id")
                if not isinstance(record.get("payload"), dict):
                    raise ConsumerProtocolError("snapshot record payload is invalid")
                stream.update(_jcs(record))
                stream.update(b"\n")
                records.append(record)
            entry = manifest[resource]
            if (
                _integer(entry.get("count"), "manifest count") != len(resource_records)
                or entry.get("sha256") != stream.hexdigest()
            ):
                raise ConsumerProtocolError("snapshot resource manifest does not match")

        received = _received_at(received_at)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM sync_objects")
            for record in records:
                payload_bytes = _jcs(record["payload"])
                db.execute(
                    """INSERT INTO sync_objects(
                           resource_type,resource_id,version_id,operation,payload_json,
                           payload_sha256,seq,snapshot_id,consumer_received_at)
                       VALUES(?,?,?,'snapshot',?,?,?,?,?)""",
                    (record["resource_type"], record["resource_id"], record["version_id"],
                     payload_bytes.decode("utf-8"), _sha(payload_bytes), high_water,
                     snapshot_id, received),
                )
            db.execute(
                """INSERT OR REPLACE INTO sync_state(
                       singleton,dataset_id,dataset_epoch,snapshot_id,resources_json,
                       high_water,resume_cursor,consumer_received_at)
                   VALUES(1,?,?,?,?,?,?,?)""",
                (dataset_id, dataset_epoch, snapshot_id,
                 json.dumps(resources, separators=(",", ":")), high_water,
                 resume_cursor, received),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        committed = self.state()
        if committed is None:  # pragma: no cover - protected by the transaction above
            raise ConsumerProtocolError("snapshot commit did not persist state")
        return committed

    def apply_changes(
        self, response: Mapping[str, object], *, received_at: datetime | None = None,
    ) -> ConsumerState:
        """Commit one verified change batch and its next cursor in the same transaction."""
        received = _received_at(received_at)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT * FROM sync_state WHERE singleton=1").fetchone()
            if state is None:
                raise SnapshotRequired("no local snapshot has been committed")
            if response.get("api_version") != "v1" or response.get("schema_version") != "1.0.0":
                raise ConsumerProtocolError("change response version is unsupported")
            if (
                response.get("dataset_id") != state["dataset_id"]
                or response.get("dataset_epoch") != state["dataset_epoch"]
            ):
                raise SnapshotRequired("dataset epoch changed; create a new snapshot")
            next_cursor = _text(response.get("next_cursor"), "next_cursor")
            high_water = _integer(response.get("high_water"), "change high_water")
            if high_water < state["high_water"]:
                raise SnapshotRequired("change high-water regressed; create a new snapshot")
            data = response.get("data")
            has_more = response.get("has_more")
            if not isinstance(data, list) or not isinstance(has_more, bool):
                raise ConsumerProtocolError("change batch is invalid")
            if has_more and not data:
                raise ConsumerProtocolError("non-final change batch cannot be empty")
            subscribed = {
                RESOURCE_TYPES[item] for item in json.loads(state["resources_json"])
            }
            previous = state["high_water"]
            verified: list[tuple[dict, bytes]] = []
            for item in data:
                if not isinstance(item, dict):
                    raise ConsumerProtocolError("change record is invalid")
                seq = _integer(item.get("seq"), "change seq")
                if seq <= previous or seq > high_water:
                    raise ConsumerProtocolError("change sequence is not strictly increasing")
                previous = seq
                if item.get("resource_type") not in subscribed:
                    raise ConsumerProtocolError("change resource is outside the subscription")
                _text(item.get("resource_id"), "change resource_id")
                _text(item.get("version_id"), "change version_id")
                if item.get("operation") not in OPERATIONS:
                    raise ConsumerProtocolError("change operation is invalid")
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    raise ConsumerProtocolError("change payload is invalid")
                payload_bytes = _jcs(payload)
                if item.get("payload_sha256") != _sha(payload_bytes):
                    raise ConsumerProtocolError("change payload hash does not match")
                verified.append((item, payload_bytes))
            position = previous if has_more else high_water
            for item, payload_bytes in verified:
                db.execute(
                    """INSERT INTO sync_objects(
                           resource_type,resource_id,version_id,operation,payload_json,
                           payload_sha256,seq,snapshot_id,consumer_received_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(resource_type,resource_id) DO UPDATE SET
                           version_id=excluded.version_id,
                           operation=excluded.operation,
                           payload_json=excluded.payload_json,
                           payload_sha256=excluded.payload_sha256,
                           seq=excluded.seq,
                           snapshot_id=excluded.snapshot_id,
                           consumer_received_at=excluded.consumer_received_at""",
                    (item["resource_type"], item["resource_id"], item["version_id"],
                     item["operation"], payload_bytes.decode("utf-8"),
                     item["payload_sha256"], item["seq"], state["snapshot_id"], received),
                )
            db.execute(
                """UPDATE sync_state SET high_water=?,resume_cursor=?,consumer_received_at=?
                   WHERE singleton=1""",
                (position, next_cursor, received),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        committed = self.state()
        if committed is None:  # pragma: no cover - protected by the transaction above
            raise ConsumerProtocolError("change commit lost consumer state")
        return committed
