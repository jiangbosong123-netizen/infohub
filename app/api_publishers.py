from __future__ import annotations

"""Typed v1 catalog for stable publishers and verified public assertions."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .timeutil import utc_now


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublisherDomain(_StrictModel):
    domain: str = Field(min_length=1, max_length=253)
    valid_from: str | None
    valid_to: str | None
    evidence_id: str | None = Field(default=None, max_length=128)


class PublisherView(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "retired"]
    organization_entity_id: str | None = Field(default=None, max_length=128)
    aliases: list[str]
    domains: list[PublisherDomain]
    available_at: str


class PublisherPagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["live"] = "live"


class PublisherListResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: list[PublisherView]
    pagination: PublisherPagination


class PublisherResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: PublisherView


class PublisherCatalogUnavailable(RuntimeError):
    pass


class PublisherNotFound(LookupError):
    pass


class RestrictedPublisher(PermissionError):
    pass


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _identity(db: sqlite3.Connection) -> sqlite3.Row:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise PublisherCatalogUnavailable("dataset identity is missing")
    return identity


def _validate_rows(
    db: sqlite3.Connection, rows: list[sqlite3.Row], dataset_id: str
) -> None:
    for row in rows:
        if row["dataset_id"] != dataset_id:
            raise PublisherCatalogUnavailable("publisher belongs to another dataset")
        if row["version_id"] is None:
            raise PublisherCatalogUnavailable("publisher current version is missing")
        if row["version_publisher_id"] != row["id"]:
            raise PublisherCatalogUnavailable("publisher current version belongs to another identity")
        if row["publisher_status"] != row["version_status"]:
            raise PublisherCatalogUnavailable("publisher current version is inconsistent")
        payload = {"name": row["name"], "status": row["version_status"]}
        digest = hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        if digest != row["version_sha256"]:
            raise PublisherCatalogUnavailable("publisher version hash does not match")
        if not row["name"].strip() or len(row["name"]) > 200:
            raise PublisherCatalogUnavailable("publisher name is invalid")
        entity_id = row["organization_entity_id"]
        if entity_id:
            entity = db.execute(
                """SELECT entity.dataset_id,entity.type,entity.status,version.id AS version_id,
                          version.entity_id AS version_entity_id,
                          version.type AS version_type,version.status AS version_status
                   FROM entities entity
                   LEFT JOIN entity_versions version ON version.id=entity.current_version_id
                   WHERE entity.id=?""",
                (entity_id,),
            ).fetchone()
            if (entity is None or entity["dataset_id"] != dataset_id
                    or entity["type"] != "organization"
                    or entity["status"] not in {"active", "inactive"}
                    or entity["version_id"] is None
                    or entity["version_entity_id"] != entity_id
                    or entity["version_type"] != entity["type"]
                    or entity["version_status"] != entity["status"]):
                raise PublisherCatalogUnavailable("publisher organization link is invalid")


def _publisher_views(
    db: sqlite3.Connection, rows: list[sqlite3.Row]
) -> list[PublisherView]:
    ids = [row["id"] for row in rows]
    aliases: dict[str, set[str]] = defaultdict(set)
    domains: dict[str, list[PublisherDomain]] = defaultdict(list)
    if ids:
        marks = ",".join("?" for _ in ids)
        for row in db.execute(
            f"""SELECT publisher_id,name,language,status,assertion_sha256
                FROM publisher_names
                WHERE publisher_id IN ({marks}) AND status='active'
                ORDER BY publisher_id,name_key,name""",
            ids,
        ):
            assertion = {
                "name": row["name"], "language": row["language"],
                "status": row["status"],
            }
            if (not row["name"].strip() or len(row["name"]) > 200
                    or hashlib.sha256(json.dumps(
                        assertion, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")).hexdigest() != row["assertion_sha256"]):
                raise PublisherCatalogUnavailable("publisher name assertion is invalid")
            aliases[row["publisher_id"]].add(row["name"])
        for row in db.execute(
            f"""SELECT publisher_id,domain,valid_from,valid_to,evidence_id,
                       verification_status,assertion_sha256
                FROM publisher_domains
                WHERE publisher_id IN ({marks}) AND verification_status='verified'
                ORDER BY publisher_id,domain,valid_from,valid_to""",
            ids,
        ):
            assertion = {
                "domain": row["domain"], "valid_from": row["valid_from"],
                "valid_to": row["valid_to"], "evidence_id": row["evidence_id"],
                "verification_status": row["verification_status"],
            }
            domain = row["domain"]
            if (not domain or len(domain) > 253 or domain != domain.lower()
                    or "/" in domain or ":" in domain or " " in domain
                    or hashlib.sha256(json.dumps(
                        assertion, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")).hexdigest() != row["assertion_sha256"]):
                raise PublisherCatalogUnavailable("publisher domain assertion is invalid")
            domains[row["publisher_id"]].append(PublisherDomain(
                domain=row["domain"], valid_from=row["valid_from"],
                valid_to=row["valid_to"], evidence_id=row["evidence_id"],
            ))
    result = []
    for row in rows:
        names = aliases[row["id"]]
        names.discard(row["name"])
        result.append(PublisherView(
            id=row["id"], version_id=row["version_id"], name=row["name"],
            status="retired" if row["version_status"] == "inactive" else "active",
            organization_entity_id=row["organization_entity_id"],
            aliases=sorted(names, key=str.casefold), domains=domains[row["id"]],
            available_at=row["available_at"],
        ))
    return result


_CURRENT = """LEFT JOIN publisher_versions version
    ON version.id=publisher.current_version_id"""
_SELECT = """publisher.id,publisher.dataset_id,publisher.organization_entity_id,
    publisher.status AS publisher_status,version.id AS version_id,version.name,
    version.publisher_id AS version_publisher_id,version.status AS version_status,
    version.version_sha256,version.available_at"""


def list_publishers(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    limit: int,
    cursor: str | None,
    query: str | None,
) -> PublisherListResponse:
    identity = _identity(db)
    all_rows = db.execute(
        f"SELECT {_SELECT} FROM publishers publisher {_CURRENT} ORDER BY publisher.id"
    ).fetchall()
    _validate_rows(db, all_rows, identity["dataset_id"])
    if any(row["publisher_status"] in {"merged", "restricted"} for row in all_rows):
        raise PublisherCatalogUnavailable(
            "publisher catalog contains identities requiring a reviewed public projection"
        )
    # A broken assertion on a later page must not make early pages look complete.
    _publisher_views(db, all_rows)
    filters = {"q": query}
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="publishers", filters=filters,
            dataset_epoch=identity["current_epoch"],
        ).last_id
    clauses = ["publisher.dataset_id=?", "publisher.id>?"]
    values: list[object] = [identity["dataset_id"], last_id]
    if query:
        pattern = _like(query)
        clauses.append("""(version.name LIKE ? ESCAPE '\\' COLLATE NOCASE OR EXISTS(
            SELECT 1 FROM publisher_names publisher_name
            WHERE publisher_name.publisher_id=publisher.id
              AND publisher_name.status='active'
              AND publisher_name.name LIKE ? ESCAPE '\\' COLLATE NOCASE))""")
        values.extend((pattern, pattern))
    values.append(limit + 1)
    rows = db.execute(
        f"""SELECT {_SELECT} FROM publishers publisher {_CURRENT}
            WHERE {' AND '.join(clauses)} ORDER BY publisher.id LIMIT ?""",
        values,
    ).fetchall()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="publishers", filters=filters,
            last_id=page[-1]["id"], dataset_epoch=identity["current_epoch"],
        )
    return PublisherListResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        data=_publisher_views(db, page),
        pagination=PublisherPagination(limit=limit, next_cursor=next_cursor),
    )


def get_publisher(
    db: sqlite3.Connection, *, request_id: str, publisher_id: str
) -> PublisherResponse:
    identity = _identity(db)
    row = db.execute(
        f"""SELECT {_SELECT} FROM publishers publisher {_CURRENT}
            WHERE publisher.id=? AND publisher.dataset_id=?""",
        (publisher_id, identity["dataset_id"]),
    ).fetchone()
    if row is None:
        raise PublisherNotFound("publisher does not exist")
    _validate_rows(db, [row], identity["dataset_id"])
    if row["publisher_status"] == "restricted":
        raise RestrictedPublisher("publisher is restricted")
    if row["publisher_status"] == "merged":
        raise PublisherCatalogUnavailable("merged publisher projection is not ready")
    return PublisherResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        data=_publisher_views(db, [row])[0],
    )


def publisher_etag(response: PublisherResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'
