from __future__ import annotations

"""Typed v1 reads for stable documents and their current immutable version."""

import hashlib
import json
import re
import sqlite3
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import CursorError, decode_cursor, encode_cursor
from .timeutil import format_utc, parse_utc, utc_now


DOCUMENT_KINDS = frozenset({
    "article", "flash", "filing", "policy_release", "research",
    "commentary", "transcript", "other",
})
_SECRET_QUERY_NAMES = frozenset({
    "accesstoken", "apikey", "auth", "authorization", "clientsecret", "key",
    "password", "secret", "sign", "signature", "token",
})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemContentQuality(_StrictModel):
    origin: Literal[
        "publisher_text", "feed_excerpt", "generated_metadata", "legacy_unknown"
    ]
    extent: Literal["full", "excerpt", "title_only", "none"]
    truncated: bool
    extraction_status: Literal["complete", "partial", "not_attempted", "failed"]


class ItemTimeQuality(_StrictModel):
    published_at: str | None
    source_time_value_id: str | None = Field(default=None, max_length=128)
    precision: Literal["second", "minute", "date", "month", "unknown"]
    status: Literal[
        "parsed", "missing", "invalid", "missing_timezone", "ambiguous_local_time",
        "nonexistent_local_time", "future_suspect", "legacy_unverified",
    ]
    rule_version: str
    tzdb_version: str
    point_in_time_eligible: bool


class ItemVersionSummary(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    normalizer_version: str = Field(min_length=1, max_length=128)
    title: str = Field(max_length=1000)
    language: str = Field(min_length=1, max_length=32)
    text_length: int = Field(ge=0)
    content_sha256: str = Field(min_length=64, max_length=64)
    version_sha256: str = Field(min_length=64, max_length=64)
    canonical_url: str | None
    source_id: str = Field(min_length=1, max_length=128)
    publisher_id: str | None = Field(default=None, max_length=128)
    time: ItemTimeQuality
    content_quality: ItemContentQuality
    correction_kind: Literal["initial", "content_change", "metadata_change"]
    normalized_at: str
    available_at: str
    availability_basis: Literal["transaction_recorded", "legacy_unknown"]


class ItemVersionView(ItemVersionSummary):
    text: str


class ItemListView(_StrictModel):
    id: str = Field(min_length=1, max_length=96)
    kind: Literal[
        "article", "flash", "filing", "policy_release", "research",
        "commentary", "transcript", "other",
    ]
    status: Literal["active", "withdrawn"]
    first_seen_at: str
    current_version: ItemVersionSummary


class ItemView(_StrictModel):
    id: str = Field(min_length=1, max_length=96)
    kind: Literal[
        "article", "flash", "filing", "policy_release", "research",
        "commentary", "transcript", "other",
    ]
    status: Literal["active", "withdrawn"]
    first_seen_at: str
    current_version: ItemVersionView


class ItemPagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["live"] = "live"
    order: Literal["first_seen_at_desc_id_asc"] = "first_seen_at_desc_id_asc"


class ItemListResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: list[ItemListView]
    pagination: ItemPagination


class ItemResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: ItemView


class ItemVersionHistoryEntry(_StrictModel):
    previous_version_id: str | None = Field(default=None, max_length=128)
    is_current: bool
    version: ItemVersionSummary


class ItemVersionHistoryData(_StrictModel):
    id: str = Field(min_length=1, max_length=96)
    kind: Literal[
        "article", "flash", "filing", "policy_release", "research",
        "commentary", "transcript", "other",
    ]
    status: Literal["active", "withdrawn"]
    first_seen_at: str
    current_version_id: str = Field(min_length=1, max_length=128)
    versions: list[ItemVersionHistoryEntry]


class ItemVersionPagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["live"] = "live"
    order: Literal["version_desc"] = "version_desc"


class ItemVersionHistoryResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: ItemVersionHistoryData
    pagination: ItemVersionPagination


class ItemCatalogUnavailable(RuntimeError):
    pass


class ItemNotFound(LookupError):
    pass


class RestrictedItem(PermissionError):
    pass


class DuplicateItemAlias(RuntimeError):
    pass


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _identity(db: sqlite3.Connection) -> sqlite3.Row:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None:
        raise ItemCatalogUnavailable("dataset identity is missing")
    return identity


def _public_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ItemCatalogUnavailable("document URL is invalid") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        if parts.scheme == "urn" and not parts.netloc:
            return None
        raise ItemCatalogUnavailable("document URL is not a public HTTP URL")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        if parts.port:
            host = f"{host}:{parts.port}"
    except ValueError as exc:
        raise ItemCatalogUnavailable("document URL port is invalid") from exc
    query = [
        (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if re.sub(r"[^a-z0-9]", "", key.casefold()) not in _SECRET_QUERY_NAMES
    ]
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(query), ""))


def _version_digest(row: sqlite3.Row) -> str:
    value = {
        "normalizer_version": row["normalizer_version"],
        "title_original": row["title_original"],
        "language": row["language"],
        "text": row["text"],
        "canonical_url": row["canonical_url"],
        "source_id": row["source_numeric_id"],
        "publisher_id": row["publisher_id"],
        "published_at": row["published_at"],
        "published_precision": row["published_precision"],
        "time_status": row["time_status"],
        "time_rule_version": row["time_rule_version"],
        "tzdb_version": row["tzdb_version"],
        "content_origin": row["content_origin"],
        "content_extent": row["content_extent"],
        "truncated": row["truncated"],
        "extraction_status": row["extraction_status"],
    }
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _item_view(db: sqlite3.Connection, row: sqlite3.Row) -> ItemView:
    if row["dataset_id"] != row["current_dataset_id"]:
        raise ItemCatalogUnavailable("document belongs to another dataset")
    if row["version_id"] is None or row["version_document_id"] != row["id"]:
        raise ItemCatalogUnavailable("document current version is invalid")
    if row["kind"] not in DOCUMENT_KINDS or len(row["id"]) > 96:
        raise ItemCatalogUnavailable("document identity is unsupported")
    if row["status"] == "restricted":
        raise RestrictedItem("document is restricted")
    if row["status"] == "duplicate_alias":
        raise DuplicateItemAlias("duplicate document canonical projection is unavailable")
    if row["status"] not in {"active", "withdrawn"}:
        raise ItemCatalogUnavailable("document status is unsupported")
    if not row["source_key"]:
        raise ItemCatalogUnavailable("document acquisition source is missing")
    if hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["content_sha256"]:
        raise ItemCatalogUnavailable("document content hash does not match")
    if _version_digest(row) != row["version_sha256"]:
        raise ItemCatalogUnavailable("document version hash does not match")
    time_value_id = row["published_time_value_id"]
    if bool(row["published_at"]) != bool(time_value_id):
        raise ItemCatalogUnavailable("document published time evidence is incomplete")
    if row["published_at"]:
        time_value = db.execute(
            """SELECT source_time.utc,source_time.precision,source_time.status,
                      source_time.rule_version,source_time.tzdb_version
               FROM source_time_values source_time
               JOIN document_version_inputs input
                 ON input.raw_record_id=source_time.raw_record_id
               WHERE source_time.id=? AND input.version_id=?
                 AND source_time.role='published'""",
            (time_value_id, row["version_id"]),
        ).fetchone()
        if (time_value is None or time_value["utc"] != row["published_at"]
                or time_value["precision"] != row["published_precision"]
                or time_value["status"] != "valid" or row["time_status"] != "parsed"
                or time_value["rule_version"] != row["time_rule_version"]
                or time_value["tzdb_version"] != row["tzdb_version"]):
            raise ItemCatalogUnavailable("document published time evidence is invalid")
    elif row["time_status"] == "parsed":
        raise ItemCatalogUnavailable("parsed document time has no published value")
    if row["publisher_id"]:
        publisher = db.execute(
            """SELECT publisher.dataset_id,publisher.status,version.publisher_id,
                      version.status AS version_status
               FROM publishers publisher
               LEFT JOIN publisher_versions version ON version.id=publisher.current_version_id
               WHERE publisher.id=?""",
            (row["publisher_id"],),
        ).fetchone()
        if (publisher is None or publisher["dataset_id"] != row["current_dataset_id"]
                or publisher["publisher_id"] != row["publisher_id"]
                or publisher["status"] != publisher["version_status"]
                or publisher["status"] in {"merged", "restricted"}):
            raise ItemCatalogUnavailable("document publisher link is invalid")
    try:
        first_seen = format_utc(parse_utc(row["first_seen_at"]))
        normalized_at = format_utc(parse_utc(row["normalized_at"]))
        available_at = format_utc(parse_utc(row["available_at"]))
        published_at = (
            format_utc(parse_utc(row["published_at"])) if row["published_at"] else None
        )
    except (ValueError, TypeError, OverflowError) as exc:
        raise ItemCatalogUnavailable("document timestamp is invalid") from exc
    return ItemView(
        id=row["id"], kind=row["kind"], status=row["status"], first_seen_at=first_seen,
        current_version=ItemVersionView(
            id=row["version_id"], version=row["version"],
            normalizer_version=row["normalizer_version"], title=row["title_original"],
            language=row["language"], text_length=len(row["text"]), text=row["text"],
            content_sha256=row["content_sha256"], version_sha256=row["version_sha256"],
            canonical_url=_public_url(row["canonical_url"]), source_id=row["source_key"],
            publisher_id=row["publisher_id"],
            time=ItemTimeQuality(
                published_at=published_at, source_time_value_id=time_value_id,
                precision=row["published_precision"],
                status=row["time_status"], rule_version=row["time_rule_version"],
                tzdb_version=row["tzdb_version"],
                point_in_time_eligible=bool(row["point_in_time_eligible"]),
            ),
            content_quality=ItemContentQuality(
                origin=row["content_origin"], extent=row["content_extent"],
                truncated=bool(row["truncated"]),
                extraction_status=row["extraction_status"],
            ),
            correction_kind=row["correction_kind"], normalized_at=normalized_at,
            available_at=available_at, availability_basis=row["availability_basis"],
        ),
    )


def _item_summary(item: ItemView) -> ItemListView:
    version = item.current_version.model_dump(mode="python", exclude={"text"})
    return ItemListView(
        id=item.id, kind=item.kind, status=item.status, first_seen_at=item.first_seen_at,
        current_version=ItemVersionSummary(**version),
    )


_SELECT = """document.id,document.dataset_id,document.kind,document.status,
    document.first_seen_at,state.dataset_id AS current_dataset_id,
    version.id AS version_id,version.document_id AS version_document_id,version.version,
    version.previous_version_id,
    version.normalizer_version,version.normalized_at,version.title_original,version.language,
    version.text,version.content_sha256,version.version_sha256,version.canonical_url,
    version.source_id AS source_numeric_id,source.key AS source_key,version.publisher_id,
    version.published_at,version.published_time_value_id,version.published_precision,
    version.time_status,
    version.time_rule_version,version.tzdb_version,version.content_origin,
    version.content_extent,version.truncated,version.extraction_status,
    version.correction_kind,version.available_at,version.availability_basis,
    version.point_in_time_eligible"""
_FROM = """FROM documents document
    JOIN dataset_state state ON state.singleton=1
    LEFT JOIN document_versions version ON version.id=document.current_version_id
    LEFT JOIN sources source ON source.id=version.source_id"""
_HISTORY_FROM = """FROM documents document
    JOIN dataset_state state ON state.singleton=1
    JOIN document_versions version ON version.document_id=document.id
    LEFT JOIN sources source ON source.id=version.source_id"""


def _position(row: sqlite3.Row) -> str:
    value = f"{row['first_seen_at']}|{row['id']}"
    if len(value) > 128:
        raise ItemCatalogUnavailable("document cursor position is too long")
    return value


def _decode_position(value: str) -> tuple[str, str]:
    first_seen_at, separator, document_id = value.partition("|")
    if not separator or not document_id or len(document_id) > 96:
        raise CursorError("item cursor position is invalid")
    try:
        normalized = format_utc(parse_utc(first_seen_at))
    except (ValueError, TypeError, OverflowError) as exc:
        raise CursorError("item cursor time is invalid") from exc
    if normalized != first_seen_at:
        raise CursorError("item cursor time is not canonical")
    return first_seen_at, document_id


def list_items(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    limit: int,
    cursor: str | None,
    query: str | None,
    kind: str | None,
    language: str | None,
    source_id: str | None,
    publisher_id: str | None,
) -> ItemListResponse:
    identity = _identity(db)
    invalid = db.execute(
        """SELECT COUNT(*) FROM documents document
           LEFT JOIN document_versions version ON version.id=document.current_version_id
           WHERE document.status='active' AND (
               document.dataset_id<>? OR version.id IS NULL OR version.document_id<>document.id
           )""",
        (identity["dataset_id"],),
    ).fetchone()[0]
    if invalid:
        raise ItemCatalogUnavailable("active document catalog is incomplete")
    filters = {
        "q": query, "kind": kind, "language": language,
        "source_id": source_id, "publisher_id": publisher_id,
    }
    cursor_time = cursor_id = None
    if cursor:
        position = decode_cursor(
            db, principal, token=cursor, resource="items", filters=filters,
            dataset_epoch=identity["current_epoch"],
        )
        cursor_time, cursor_id = _decode_position(position.last_id)
    clauses = ["document.dataset_id=?", "document.status='active'"]
    values: list[object] = [identity["dataset_id"]]
    if cursor_time:
        clauses.append(
            "(document.first_seen_at<? OR (document.first_seen_at=? AND document.id>?))"
        )
        values.extend((cursor_time, cursor_time, cursor_id))
    if query:
        clauses.append("version.title_original LIKE ? ESCAPE '\\' COLLATE NOCASE")
        values.append(_like(query))
    if kind:
        clauses.append("document.kind=?")
        values.append(kind)
    if language:
        clauses.append("version.language=?")
        values.append(language)
    if source_id:
        clauses.append("source.key=?")
        values.append(source_id)
    if publisher_id:
        clauses.append("version.publisher_id=?")
        values.append(publisher_id)
    values.append(limit + 1)
    rows = db.execute(
        f"""SELECT {_SELECT} {_FROM} WHERE {' AND '.join(clauses)}
            ORDER BY document.first_seen_at DESC,document.id ASC LIMIT ?""",
        values,
    ).fetchall()
    page = rows[:limit]
    data = [_item_summary(_item_view(db, row)) for row in page]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="items", filters=filters,
            last_id=_position(page[-1]), dataset_epoch=identity["current_epoch"],
        )
    return ItemListResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), data=data,
        pagination=ItemPagination(limit=limit, next_cursor=next_cursor),
    )


def get_item(db: sqlite3.Connection, *, request_id: str, item_id: str) -> ItemResponse:
    identity = _identity(db)
    row = db.execute(
        f"""SELECT {_SELECT} {_FROM}
            WHERE document.id=? AND document.dataset_id=?""",
        (item_id, identity["dataset_id"]),
    ).fetchone()
    if row is None:
        raise ItemNotFound("document does not exist")
    return ItemResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(), data=_item_view(db, row),
    )


def list_item_versions(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    item_id: str,
    limit: int,
    cursor: str | None,
) -> ItemVersionHistoryResponse:
    identity = _identity(db)
    document = db.execute(
        """SELECT id,kind,status,first_seen_at,current_version_id
           FROM documents WHERE id=? AND dataset_id=?""",
        (item_id, identity["dataset_id"]),
    ).fetchone()
    if document is None:
        raise ItemNotFound("document does not exist")
    if document["status"] == "restricted":
        raise RestrictedItem("document is restricted")
    if document["status"] == "duplicate_alias":
        raise DuplicateItemAlias("duplicate document canonical projection is unavailable")
    if document["status"] not in {"active", "withdrawn"}:
        raise ItemCatalogUnavailable("document status is unsupported")
    chain = db.execute(
        """SELECT COUNT(*) AS count,MIN(version) AS first_version,
                  MAX(version) AS last_version,
                  SUM(CASE
                      WHEN version=1 AND previous_version_id IS NULL THEN 0
                      WHEN version>1 AND EXISTS(
                          SELECT 1 FROM document_versions predecessor
                          WHERE predecessor.id=document_versions.previous_version_id
                            AND predecessor.document_id=document_versions.document_id
                            AND predecessor.version=document_versions.version-1
                      ) THEN 0 ELSE 1 END) AS invalid_links
           FROM document_versions WHERE document_id=?""",
        (item_id,),
    ).fetchone()
    if (chain is None or chain["count"] < 1 or chain["first_version"] != 1
            or chain["last_version"] != chain["count"] or chain["invalid_links"] != 0):
        raise ItemCatalogUnavailable("document version chain is invalid")
    current = db.execute(
        """SELECT version FROM document_versions
           WHERE id=? AND document_id=?""",
        (document["current_version_id"], item_id),
    ).fetchone()
    if current is None or current["version"] != chain["last_version"]:
        raise ItemCatalogUnavailable("document current version is not the latest version")
    last_version = None
    if cursor:
        position = decode_cursor(
            db, principal, token=cursor, resource="item_versions",
            filters={"item_id": item_id}, dataset_epoch=identity["current_epoch"],
        )
        try:
            last_version = int(position.last_id)
        except ValueError as exc:
            raise CursorError("item version cursor position is invalid") from exc
        if str(last_version) != position.last_id or last_version < 1:
            raise CursorError("item version cursor position is invalid")
    clauses = ["document.id=?", "document.dataset_id=?"]
    values: list[object] = [item_id, identity["dataset_id"]]
    if last_version is not None:
        clauses.append("version.version<?")
        values.append(last_version)
    values.append(limit + 1)
    rows = db.execute(
        f"""SELECT {_SELECT} {_HISTORY_FROM}
            WHERE {' AND '.join(clauses)}
            ORDER BY version.version DESC LIMIT ?""",
        values,
    ).fetchall()
    page = rows[:limit]
    entries = [
        ItemVersionHistoryEntry(
            previous_version_id=row["previous_version_id"],
            is_current=row["version_id"] == document["current_version_id"],
            version=_item_summary(_item_view(db, row)).current_version,
        )
        for row in page
    ]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="item_versions", filters={"item_id": item_id},
            last_id=str(page[-1]["version"]), dataset_epoch=identity["current_epoch"],
        )
    try:
        first_seen_at = format_utc(parse_utc(document["first_seen_at"]))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ItemCatalogUnavailable("document timestamp is invalid") from exc
    return ItemVersionHistoryResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        data=ItemVersionHistoryData(
            id=document["id"], kind=document["kind"], status=document["status"],
            first_seen_at=first_seen_at,
            current_version_id=document["current_version_id"], versions=entries,
        ),
        pagination=ItemVersionPagination(limit=limit, next_cursor=next_cursor),
    )


def item_etag(response: ItemResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'
