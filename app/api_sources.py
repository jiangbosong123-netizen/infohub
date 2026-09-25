from __future__ import annotations

"""Typed v1 catalog for active acquisition sources and frozen configurations."""

import hashlib
import json
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .timeutil import utc_now


SOURCE_CHANNELS = frozenset({"ai", "robot", "stock"})
SOURCE_TIERS = frozenset({"official", "media", "info", "reconcile"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceConfigView(_StrictModel):
    version_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    available_at: str


class SourceView(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    channel: Literal["ai", "robot", "stock"]
    tier: Literal["official", "media", "info", "reconcile"]
    collector_type: str = Field(min_length=1, max_length=64)
    origin_host: str | None = Field(default=None, max_length=253)
    collection_interval_seconds: int = Field(gt=0)
    collection_subject_entity_id: str | None = Field(default=None, max_length=128)
    status: Literal["active"] = "active"
    configuration: SourceConfigView


class SourcePagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["live"] = "live"


class SourceListResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: list[SourceView]
    pagination: SourcePagination


class SourceResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: SourceView


class SourceCatalogUnavailable(RuntimeError):
    pass


class SourceNotFound(LookupError):
    pass


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _identity(db: sqlite3.Connection) -> sqlite3.Row:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise SourceCatalogUnavailable("dataset identity is missing")
    missing = db.execute(
        """SELECT COUNT(*) FROM sources AS source
           WHERE source.enabled=1 AND NOT EXISTS(
               SELECT 1 FROM source_config_versions AS config
               WHERE config.source_id=source.id
           )"""
    ).fetchone()[0]
    if missing:
        raise SourceCatalogUnavailable("active source configuration snapshot is missing")
    return identity


def _source_view(db: sqlite3.Connection, row: sqlite3.Row) -> SourceView:
    try:
        config = json.loads(row["config_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise SourceCatalogUnavailable("source configuration is invalid") from exc
    if not isinstance(config, dict):
        raise SourceCatalogUnavailable("source configuration is invalid")
    encoded = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["config_hash"]:
        raise SourceCatalogUnavailable("source configuration hash does not match")
    required = {
        "key": str, "name": str, "channel": str, "tier": str, "type": str,
        "url": str, "company_slug": str, "interval_minutes": int,
    }
    if any(not isinstance(config.get(key), expected) for key, expected in required.items()):
        raise SourceCatalogUnavailable("source configuration fields are invalid")
    if config["key"] != row["key"]:
        raise SourceCatalogUnavailable("source key and configuration disagree")
    if config["channel"] not in SOURCE_CHANNELS or config["tier"] not in SOURCE_TIERS:
        raise SourceCatalogUnavailable("source classification is unsupported")
    if (not config["name"].strip() or len(config["name"]) > 200
            or not config["type"].strip() or len(config["type"]) > 64
            or config["interval_minutes"] <= 0):
        raise SourceCatalogUnavailable("source configuration is incomplete")
    try:
        origin_host = urlsplit(config["url"]).hostname
    except ValueError as exc:
        raise SourceCatalogUnavailable("source URL is invalid") from exc
    parsed = urlsplit(config["url"])
    if config["url"] and (parsed.scheme not in {"http", "https"} or not origin_host):
        raise SourceCatalogUnavailable("source URL has no origin host")
    subject_entity_id = None
    if config["company_slug"]:
        subject = db.execute(
            """SELECT mapping.entity_id FROM companies AS company
               JOIN legacy_company_entities AS mapping ON mapping.company_id=company.id
               WHERE company.slug=?""",
            (config["company_slug"],),
        ).fetchone()
        if subject is None:
            raise SourceCatalogUnavailable("source collection subject is unresolved")
        subject_entity_id = subject["entity_id"]
    return SourceView(
        id=config["key"], name=config["name"], channel=config["channel"],
        tier=config["tier"], collector_type=config["type"],
        origin_host=origin_host.lower() if origin_host else None,
        collection_interval_seconds=config["interval_minutes"] * 60,
        collection_subject_entity_id=subject_entity_id,
        configuration=SourceConfigView(
            version_id=row["config_id"], version=row["config_version"],
            sha256=row["config_hash"], available_at=row["config_available_at"],
        ),
    )


_LATEST_CONFIG = """JOIN source_config_versions AS config ON config.source_id=source.id
    AND NOT EXISTS(
        SELECT 1 FROM source_config_versions AS later
        WHERE later.source_id=config.source_id AND later.version>config.version
    )"""


def list_sources(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    limit: int,
    cursor: str | None,
    query: str | None,
    channel: str | None,
    tier: str | None,
) -> SourceListResponse:
    identity = _identity(db)
    filters = {"q": query, "channel": channel, "tier": tier}
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="sources", filters=filters,
            dataset_epoch=identity["current_epoch"],
        ).last_id
    validation_rows = db.execute(
        f"""SELECT source.key,config.id AS config_id,config.version AS config_version,
                   config.config_json,config.config_hash,
                   config.available_at AS config_available_at
            FROM sources AS source {_LATEST_CONFIG}
            WHERE source.enabled=1 ORDER BY source.key"""
    ).fetchall()
    for validation_row in validation_rows:
        _source_view(db, validation_row)
    clauses = ["source.enabled=1", "source.key>?"]
    values: list[object] = [last_id]
    if query:
        clauses.append("(source.key LIKE ? ESCAPE '\\' COLLATE NOCASE OR json_extract(config.config_json,'$.name') LIKE ? ESCAPE '\\' COLLATE NOCASE)")
        pattern = _like(query)
        values.extend((pattern, pattern))
    if channel:
        clauses.append("json_extract(config.config_json,'$.channel')=?")
        values.append(channel)
    if tier:
        clauses.append("json_extract(config.config_json,'$.tier')=?")
        values.append(tier)
    values.append(limit + 1)
    rows = db.execute(
        f"""SELECT source.key,config.id AS config_id,config.version AS config_version,
                   config.config_json,config.config_hash,
                   config.available_at AS config_available_at
            FROM sources AS source {_LATEST_CONFIG}
            WHERE {' AND '.join(clauses)} ORDER BY source.key LIMIT ?""",
        values,
    ).fetchall()
    page = rows[:limit]
    data = [_source_view(db, row) for row in page]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="sources", filters=filters,
            last_id=page[-1]["key"], dataset_epoch=identity["current_epoch"],
        )
    return SourceListResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), data=data,
        pagination=SourcePagination(limit=limit, next_cursor=next_cursor),
    )


def get_source(
    db: sqlite3.Connection, *, request_id: str, source_id: str
) -> SourceResponse:
    identity = _identity(db)
    row = db.execute(
        f"""SELECT source.key,config.id AS config_id,config.version AS config_version,
                   config.config_json,config.config_hash,
                   config.available_at AS config_available_at
            FROM sources AS source {_LATEST_CONFIG}
            WHERE source.enabled=1 AND source.key=?""",
        (source_id,),
    ).fetchone()
    if row is None:
        raise SourceNotFound("active source does not exist")
    return SourceResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), data=_source_view(db, row),
    )


def source_etag(response: SourceResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'
