from __future__ import annotations

"""Bounded reliable incremental reads after an immutable sync snapshot."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .api_auth import ApiPrincipal
from .api_change_cursor import decode_change_cursor, encode_change_cursor
from .api_cursor import CursorEpochChanged
from .api_sync import RESOURCES, RESOURCE_SCOPES
from .timeutil import format_utc, parse_utc

RESOURCE_TYPES = {
    "items": "item", "events": "event", "entities": "entity", "topics": "topic",
    "sources": "source", "analyses": "analysis", "signals": "signal",
    "reports": "report", "evidence": "evidence",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangeView(_StrictModel):
    seq: int = Field(ge=1)
    resource_type: Literal[
        "item", "event", "entity", "topic", "source", "analysis", "signal",
        "report", "evidence",
    ]
    resource_id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)
    operation: Literal["create", "update", "withdraw", "merge", "split", "delete"]
    available_at: str
    payload: dict[str, JsonValue]
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ChangesResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: list[ChangeView]
    next_cursor: str
    high_water: int = Field(ge=0)
    has_more: bool


class ChangeFeedUnavailable(RuntimeError):
    pass


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("change feed time must include a timezone")
    return current.astimezone(timezone.utc)


def list_changes(
    db: sqlite3.Connection, *, principal: ApiPrincipal, request_id: str,
    cursor: str, limit: int, now: datetime | None = None,
) -> ChangesResponse:
    current = _now(now)
    if not 1 <= limit <= 100:
        raise ValueError("change limit is invalid")
    db.execute("BEGIN")
    position = decode_change_cursor(
        db, principal, cursor, allowed_resources=frozenset(RESOURCES),
        now=current.timestamp(),
    )
    required = {"read:sync", *(RESOURCE_SCOPES[item] for item in position.resources)}
    if not required <= principal.scopes:
        raise PermissionError("change cursor resource scope is missing")
    resource_types = tuple(RESOURCE_TYPES[item] for item in position.resources)
    high_water = db.execute(
        """SELECT COALESCE(MAX(seq),0) FROM change_log
           WHERE dataset_id=? AND epoch=?""",
        (position.dataset_id, position.dataset_epoch),
    ).fetchone()[0]
    if high_water < position.seq:
        raise CursorEpochChanged("change high-water regressed; create a new snapshot")
    placeholders = ",".join("?" for _ in resource_types)
    rows = db.execute(
        f"""SELECT seq,resource_type,resource_id,version_id,operation,available_at,
                   payload_json,payload_sha256,hash_algorithm
            FROM change_log
            WHERE dataset_id=? AND epoch=? AND seq>? AND seq<=?
              AND resource_type IN ({placeholders})
            ORDER BY seq LIMIT ?""",
        (position.dataset_id, position.dataset_epoch, position.seq, high_water,
         *resource_types, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    selected = rows[:limit]
    data: list[ChangeView] = []
    for row in selected:
        if row["hash_algorithm"] != "jcs-sha256-v1":
            raise ChangeFeedUnavailable("change uses an unsupported hash algorithm")
        try:
            payload = json.loads(row["payload_json"])
            parse_utc(row["available_at"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ChangeFeedUnavailable("change payload is invalid") from exc
        if (not isinstance(payload, dict)
                or hashlib.sha256(rfc8785.dumps(payload)).hexdigest() != row["payload_sha256"]):
            raise ChangeFeedUnavailable("change payload hash does not match")
        data.append(ChangeView(
            seq=row["seq"], resource_type=row["resource_type"],
            resource_id=row["resource_id"], version_id=row["version_id"],
            operation=row["operation"], available_at=row["available_at"], payload=payload,
            payload_sha256=row["payload_sha256"],
        ))
    next_seq = selected[-1]["seq"] if has_more else high_water
    next_cursor = encode_change_cursor(
        db, principal, dataset_id=position.dataset_id,
        dataset_epoch=position.dataset_epoch, resources=position.resources,
        seq=next_seq, now=current.timestamp(), expires_at=position.expires_at,
    )
    return ChangesResponse(
        dataset_id=position.dataset_id, dataset_epoch=position.dataset_epoch,
        request_id=request_id, generated_at=format_utc(current), data=data,
        next_cursor=next_cursor, high_water=high_water, has_more=has_more,
    )
