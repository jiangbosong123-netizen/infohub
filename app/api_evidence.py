from __future__ import annotations

"""Public, redacted provenance views over immutable raw records."""

import hashlib
import sqlite3
from typing import Literal, Mapping

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .timeutil import utc_now


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RawEvidenceView(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    observed_at: str
    ingested_at: str
    media_type: str = Field(min_length=1, max_length=255)
    payload_sha256: str = Field(min_length=64, max_length=64)
    payload_kind: Literal[
        "feed_entry", "api_record", "html", "pdf", "legacy_excerpt",
        "generated_metadata",
    ]
    truncated: bool
    size_bytes: int = Field(ge=0)


class EvidenceDocumentRef(_StrictModel):
    id: str = Field(min_length=1, max_length=96)
    version_id: str = Field(min_length=1, max_length=128)
    input_role: Literal["primary", "metadata", "additional"]


class EvidenceDetailView(RawEvidenceView):
    document_refs: list[EvidenceDocumentRef]


class EvidenceResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    data: EvidenceDetailView


class ItemEvidenceView(_StrictModel):
    input_role: Literal["primary", "metadata", "additional"]
    raw_record: RawEvidenceView


class ItemEvidenceSubject(_StrictModel):
    id: str = Field(min_length=1, max_length=96)
    version_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)


class EvidencePagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["immutable_version"] = "immutable_version"
    order: Literal["evidence_id_asc"] = "evidence_id_asc"


class ItemEvidenceResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    knowledge_cutoff: None = None
    item: ItemEvidenceSubject
    data: list[ItemEvidenceView]
    pagination: EvidencePagination


class EvidenceUnavailable(RuntimeError):
    pass


class EvidenceNotFound(LookupError):
    pass


class EvidenceRestricted(PermissionError):
    pass


def _identity(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise EvidenceUnavailable("dataset identity is unavailable")
    return row


def raw_evidence_view(row: Mapping[str, object], prefix: str = "") -> RawEvidenceView:
    return RawEvidenceView(
        id=row[f"{prefix}id"], source_id=row[f"{prefix}source_key"],
        observed_at=row[f"{prefix}observed_at"], ingested_at=row[f"{prefix}ingested_at"],
        media_type=row[f"{prefix}media_type"],
        payload_sha256=row[f"{prefix}payload_sha256"],
        payload_kind=row[f"{prefix}payload_kind"],
        truncated=bool(row[f"{prefix}truncated"]), size_bytes=row[f"{prefix}size_bytes"],
    )


_RAW_SELECT = """raw.id,source.key AS source_key,raw.observed_at,raw.ingested_at,
    raw.media_type,raw.payload_sha256,raw.payload_kind,raw.truncated,raw.size_bytes"""


def get_evidence(
    db: sqlite3.Connection, *, request_id: str, evidence_id: str,
) -> EvidenceResponse:
    identity = _identity(db)
    row = db.execute(
        f"""SELECT {_RAW_SELECT} FROM raw_records AS raw
            JOIN sources AS source ON source.id=raw.source_id
            WHERE raw.id=?""",
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise EvidenceNotFound("raw evidence does not exist")
    references = db.execute(
        """SELECT document.id,version.id AS version_id,input.role,
                  document.dataset_id,document.status
           FROM document_version_inputs AS input
           JOIN document_versions AS version ON version.id=input.version_id
           JOIN documents AS document ON document.id=version.document_id
           WHERE input.raw_record_id=? ORDER BY document.id,version.version,input.role""",
        (evidence_id,),
    ).fetchall()
    if not references:
        raise EvidenceNotFound("raw evidence is not attached to a document version")
    public_refs = []
    for reference in references:
        if reference["dataset_id"] != identity["dataset_id"]:
            raise EvidenceUnavailable("evidence crosses dataset boundaries")
        if reference["status"] == "restricted":
            continue
        if reference["status"] == "duplicate_alias":
            continue
        if reference["status"] not in {"active", "withdrawn"}:
            raise EvidenceUnavailable("evidence document status is unsupported")
        public_refs.append(EvidenceDocumentRef(
            id=reference["id"], version_id=reference["version_id"],
            input_role=reference["role"],
        ))
    if not public_refs:
        raise EvidenceRestricted("evidence is only attached to restricted documents")
    raw = raw_evidence_view(row)
    return EvidenceResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        data=EvidenceDetailView(**raw.model_dump(), document_refs=public_refs),
    )


def list_item_evidence(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    item_id: str,
    version_id: str,
    limit: int,
    cursor: str | None,
) -> ItemEvidenceResponse:
    identity = _identity(db)
    version = db.execute(
        """SELECT document.id,document.dataset_id,document.status,
                  version.id AS version_id,version.version
           FROM documents AS document
           JOIN document_versions AS version ON version.document_id=document.id
           WHERE document.id=? AND version.id=?""",
        (item_id, version_id),
    ).fetchone()
    if version is None or version["dataset_id"] != identity["dataset_id"]:
        raise EvidenceNotFound("document version does not exist")
    if version["status"] == "restricted":
        raise EvidenceRestricted("document is restricted")
    if version["status"] == "duplicate_alias":
        raise EvidenceUnavailable("duplicate document evidence is unavailable")
    if version["status"] not in {"active", "withdrawn"}:
        raise EvidenceUnavailable("document status is unsupported")
    filters = {"item_id": item_id, "version_id": version_id}
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="item_evidence", filters=filters,
            dataset_epoch=identity["current_epoch"],
        ).last_id
    rows = db.execute(
        f"""SELECT {_RAW_SELECT},input.role AS input_role
            FROM document_version_inputs AS input
            JOIN raw_records AS raw ON raw.id=input.raw_record_id
            JOIN sources AS source ON source.id=raw.source_id
            WHERE input.version_id=? AND raw.id>?
            ORDER BY raw.id ASC LIMIT ?""",
        (version_id, last_id, limit + 1),
    ).fetchall()
    page = rows[:limit]
    data = [
        ItemEvidenceView(input_role=row["input_role"], raw_record=raw_evidence_view(row))
        for row in page
    ]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="item_evidence", filters=filters,
            last_id=data[-1].raw_record.id, dataset_epoch=identity["current_epoch"],
        )
    return ItemEvidenceResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        item=ItemEvidenceSubject(
            id=version["id"], version_id=version["version_id"], version=version["version"],
        ),
        data=data, pagination=EvidencePagination(limit=limit, next_cursor=next_cursor),
    )


def evidence_etag(response: EvidenceResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'


__all__ = [
    "EvidenceNotFound", "EvidenceResponse", "EvidenceRestricted", "EvidenceUnavailable",
    "ItemEvidenceResponse", "evidence_etag", "get_evidence", "list_item_evidence",
]
