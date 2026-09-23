from __future__ import annotations

"""Typed read models for the first v1 catalog surface."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .timeutil import format_utc, parse_utc, utc_now


ENTITY_TYPES = frozenset({
    "organization", "security", "person", "product", "model", "industry",
    "region", "macro_concept",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identifier(_StrictModel):
    namespace: str
    value: str
    exchange: str | None
    valid_from: str | None
    valid_to: str | None
    evidence_id: str | None


class ObjectRef(_StrictModel):
    id: str
    version_id: str


class EntityRelation(_StrictModel):
    relation: str
    target: ObjectRef
    valid_from: str | None
    valid_to: str | None
    evidence_ids: list[str]


class EntityView(_StrictModel):
    id: str
    version_id: str
    type: Literal[
        "organization", "security", "person", "product", "model", "industry",
        "region", "macro_concept",
    ]
    canonical_name: str
    status: Literal["active", "retired", "merged"]
    canonical_id: str | None
    aliases: list[str]
    identifiers: list[Identifier]
    relations: list[EntityRelation]
    available_at: str


class Pagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["live"] = "live"


class EntityListResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: list[EntityView]
    pagination: Pagination


class KnowledgeCutoff(_StrictModel):
    basis: Literal["logical_as_of"] = "logical_as_of"
    as_of: str
    checkpoint_id: None = None
    dataset_epoch: str
    high_water: None = None
    observed_at: None = None
    clock_status: Literal["unknown"] = "unknown"


class EntityResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: KnowledgeCutoff | None
    data: EntityView


class CatalogUnavailable(RuntimeError):
    """The shadow catalog cannot safely satisfy the public contract yet."""


class CatalogNotFound(LookupError):
    """The requested identity or version does not exist in the selected view."""


class RestrictedCatalog(PermissionError):
    """The selected identity version cannot be returned with catalog scope."""


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _entity_views(
    db: sqlite3.Connection,
    rows: list[sqlite3.Row],
    *,
    assertion_cutoff: str | None = None,
) -> list[EntityView]:
    ids = [row["id"] for row in rows]
    aliases: dict[str, set[str]] = defaultdict(set)
    identifiers: dict[str, list[Identifier]] = defaultdict(list)
    relations: dict[str, list[EntityRelation]] = defaultdict(list)
    if ids:
        marks = ",".join("?" for _ in ids)
        cutoff_sql = " AND available_at<=?" if assertion_cutoff else ""
        cutoff_values = [assertion_cutoff] if assertion_cutoff else []
        for row in db.execute(
            f"""SELECT entity_id,alias FROM entity_aliases
                WHERE entity_id IN ({marks}) AND status='active' AND ambiguity='unique'
                {cutoff_sql}
                ORDER BY entity_id,alias_key,alias""", [*ids, *cutoff_values],
        ):
            aliases[row["entity_id"]].add(row["alias"])
        for row in db.execute(
            f"""SELECT entity_id,namespace,value,qualifier_json,valid_from,valid_to,evidence_id
                FROM entity_identifiers
                WHERE entity_id IN ({marks}) AND verification_status='verified'
                {cutoff_sql}
                ORDER BY entity_id,namespace,value""", [*ids, *cutoff_values],
        ):
            try:
                qualifier = json.loads(row["qualifier_json"])
            except (TypeError, json.JSONDecodeError):
                qualifier = {}
            identifiers[row["entity_id"]].append(Identifier(
                namespace=row["namespace"], value=row["value"],
                exchange=qualifier.get("exchange") if isinstance(qualifier, dict) else None,
                valid_from=row["valid_from"], valid_to=row["valid_to"],
                evidence_id=row["evidence_id"],
            ))
        if assertion_cutoff:
            target_version = """(
                SELECT historical.id FROM entity_versions historical
                WHERE historical.entity_id=relation.to_entity_id
                  AND historical.available_at<=?
                ORDER BY historical.available_at DESC,historical.version DESC LIMIT 1
            )"""
            relation_values: list[object] = [assertion_cutoff, *ids, assertion_cutoff]
        else:
            target_version = "target.current_version_id"
            relation_values = [*ids]
        for row in db.execute(
            f"""SELECT relation.from_entity_id,relation.relation,relation.to_entity_id,
                       {target_version} AS target_version_id,
                       relation.valid_from,relation.valid_to,relation.evidence_id
                FROM entity_relations relation
                JOIN entities target ON target.id=relation.to_entity_id
                WHERE relation.from_entity_id IN ({marks})
                  AND relation.verification_status='verified'
                  {cutoff_sql.replace('available_at', 'relation.available_at')}
                ORDER BY relation.from_entity_id,relation.relation,relation.to_entity_id""",
            relation_values,
        ):
            if row["target_version_id"] is None:
                continue
            relations[row["from_entity_id"]].append(EntityRelation(
                relation=row["relation"],
                target=ObjectRef(id=row["to_entity_id"], version_id=row["target_version_id"]),
                valid_from=row["valid_from"], valid_to=row["valid_to"],
                evidence_ids=[row["evidence_id"]] if row["evidence_id"] else [],
            ))
    return [EntityView(
        id=row["id"], version_id=row["version_id"], type=row["type"],
        canonical_name=row["canonical_name"],
        status="retired" if row["status"] == "inactive" else row["status"],
        canonical_id=None, aliases=sorted(aliases[row["id"]], key=str.casefold),
        identifiers=identifiers[row["id"]], relations=relations[row["id"]],
        available_at=row["available_at"],
    ) for row in rows]


def list_entities(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    limit: int,
    cursor: str | None,
    query: str | None,
    entity_type: str | None,
    identifier_namespace: str | None,
    identifier_value: str | None,
    exchange: str | None,
) -> EntityListResponse:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise CatalogUnavailable("dataset identity is missing")
    unsupported = db.execute(
        """SELECT COUNT(*) FROM entities entity
           LEFT JOIN entity_versions version ON version.id=entity.current_version_id
           WHERE entity.dataset_id<>? OR version.id IS NULL
              OR entity.type<>version.type OR entity.status<>version.status
              OR entity.status IN ('merged','restricted')""",
        (identity["dataset_id"],),
    ).fetchone()[0]
    if unsupported:
        raise CatalogUnavailable("catalog contains identities requiring a reviewed public projection")
    filters = {
        "q": query,
        "type": entity_type,
        "identifier_namespace": identifier_namespace,
        "identifier_value": identifier_value,
        "exchange": exchange,
    }
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="entities", filters=filters,
            dataset_epoch=identity["current_epoch"],
        ).last_id
    clauses = ["entity.dataset_id=?", "entity.id>?", "entity.current_version_id IS NOT NULL"]
    values: list[object] = [identity["dataset_id"], last_id]
    if entity_type:
        clauses.append("version.type=?")
        values.append(entity_type)
    if query:
        pattern = _like(query)
        clauses.append("""(version.canonical_name LIKE ? ESCAPE '\\' COLLATE NOCASE OR EXISTS(
            SELECT 1 FROM entity_aliases alias
            WHERE alias.entity_id=entity.id AND alias.status='active'
              AND alias.ambiguity='unique'
              AND alias.alias LIKE ? ESCAPE '\\' COLLATE NOCASE))""")
        values.extend((pattern, pattern))
    if identifier_namespace:
        identifier_clause = """entity.id IN (
            SELECT identifier.entity_id FROM entity_identifiers identifier
            WHERE identifier.verification_status='verified'
              AND identifier.namespace=?
              AND identifier.value=?"""
        values.extend((identifier_namespace, identifier_value))
        if exchange:
            identifier_clause += """
              AND json_extract(identifier.qualifier_json,'$.exchange')=?"""
            values.append(exchange)
        clauses.append(identifier_clause + ")")
    values.append(limit + 1)
    rows = db.execute(
        f"""SELECT entity.id,version.id AS version_id,version.type,
                   version.canonical_name,version.status,version.available_at
            FROM entities entity
            JOIN entity_versions version ON version.id=entity.current_version_id
            WHERE {' AND '.join(clauses)}
            ORDER BY entity.id ASC LIMIT ?""",
        values,
    ).fetchall()
    page = rows[:limit]
    data = _entity_views(db, page)
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="entities", filters=filters,
            last_id=page[-1]["id"], dataset_epoch=identity["current_epoch"],
        )
    return EntityListResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), data=data,
        pagination=Pagination(limit=limit, next_cursor=next_cursor),
    )


def get_entity(
    db: sqlite3.Connection,
    *,
    request_id: str,
    entity_id: str,
    version_id: str | None,
    as_of: str | None,
) -> EntityResponse:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise CatalogUnavailable("dataset identity is missing")
    entity = db.execute(
        "SELECT * FROM entities WHERE id=? AND dataset_id=?",
        (entity_id, identity["dataset_id"]),
    ).fetchone()
    if entity is None:
        raise CatalogNotFound("entity does not exist")
    current = db.execute(
        "SELECT type,status FROM entity_versions WHERE id=? AND entity_id=?",
        (entity["current_version_id"], entity_id),
    ).fetchone()
    if current is None or current["type"] != entity["type"] or current["status"] != entity["status"]:
        raise CatalogUnavailable("entity current version is inconsistent")
    if entity["status"] == "restricted":
        raise RestrictedCatalog("entity is restricted")
    normalized_as_of = format_utc(parse_utc(as_of)) if as_of else None
    if version_id:
        version = db.execute(
            """SELECT entity.id,version.id AS version_id,version.type,
                      version.canonical_name,version.status,version.available_at
               FROM entities entity JOIN entity_versions version ON version.entity_id=entity.id
               WHERE entity.id=? AND version.id=?""",
            (entity_id, version_id),
        ).fetchone()
        assertion_cutoff = version["available_at"] if version else None
    elif normalized_as_of:
        version = db.execute(
            """SELECT entity.id,version.id AS version_id,version.type,
                      version.canonical_name,version.status,version.available_at
               FROM entities entity JOIN entity_versions version ON version.entity_id=entity.id
               WHERE entity.id=? AND version.available_at<=?
               ORDER BY version.available_at DESC,version.version DESC LIMIT 1""",
            (entity_id, normalized_as_of),
        ).fetchone()
        assertion_cutoff = normalized_as_of
    else:
        version = db.execute(
            """SELECT entity.id,version.id AS version_id,version.type,
                      version.canonical_name,version.status,version.available_at
               FROM entities entity JOIN entity_versions version
                 ON version.id=entity.current_version_id
               WHERE entity.id=?""",
            (entity_id,),
        ).fetchone()
        assertion_cutoff = None
    if version is None:
        raise CatalogNotFound("entity version does not exist in this view")
    if version["status"] == "restricted":
        raise RestrictedCatalog("entity version is restricted")
    if version["status"] == "merged":
        raise CatalogUnavailable("merged entity canonical projection is not ready")
    if version["status"] not in ("active", "inactive"):
        raise CatalogUnavailable("entity version status is unsupported")
    view = _entity_views(db, [version], assertion_cutoff=assertion_cutoff)[0]
    cutoff = KnowledgeCutoff(
        as_of=normalized_as_of, dataset_epoch=identity["current_epoch"],
    ) if normalized_as_of else None
    return EntityResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), knowledge_cutoff=cutoff, data=view,
    )


def entity_etag(response: EntityResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "knowledge_cutoff": (
            response.knowledge_cutoff.model_dump(mode="json")
            if response.knowledge_cutoff else None
        ),
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'
