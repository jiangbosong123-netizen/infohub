from __future__ import annotations

"""Release-bound provenance reads for a public event version."""

import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .api_events import EventAdmissionView, EventNotFound, EventReleaseView, get_event
from .api_items import ItemCatalogUnavailable, public_document_url
from .timeutil import utc_now


EVIDENCE_ROLES = frozenset({"supports", "contradicts", "context"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceDocumentView(_StrictModel):
    id: str = Field(min_length=1, max_length=96)
    version_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    title: str = Field(max_length=1000)
    canonical_url: str | None
    source_id: str = Field(min_length=1, max_length=128)
    publisher_id: str | None = Field(default=None, max_length=128)
    published_at: str | None
    input_role: Literal["primary", "metadata", "additional"]


class EvidenceRawRecordView(_StrictModel):
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


class EventEvidenceView(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    role: Literal["supports", "contradicts", "context"]
    fact_id: str | None = Field(default=None, max_length=128)
    available_at: str
    document: EvidenceDocumentView
    raw_record: EvidenceRawRecordView


class EventEvidenceSubject(_StrictModel):
    id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)
    admission: EventAdmissionView


class EventEvidencePagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["release"] = "release"
    order: Literal["evidence_id_asc"] = "evidence_id_asc"


class EventEvidenceResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    release: EventReleaseView
    event: EventEvidenceSubject
    data: list[EventEvidenceView]
    pagination: EventEvidencePagination


class EventEvidenceUnavailable(RuntimeError):
    pass


def _evidence_document_url(value: str) -> str | None:
    try:
        return public_document_url(value)
    except ItemCatalogUnavailable as exc:
        raise EventEvidenceUnavailable("evidence document URL is invalid") from exc


def list_event_evidence(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    event_id: str,
    limit: int,
    cursor: str | None,
    role: str | None,
    fact_id: str | None,
) -> EventEvidenceResponse:
    event = get_event(db, request_id=request_id, event_id=event_id)
    filters = {
        "release_review_id": event.release.review_id,
        "event_id": event_id,
        "role": role,
        "fact_id": fact_id,
    }
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="event_evidence", filters=filters,
            dataset_epoch=event.dataset_epoch,
        ).last_id
    rows = db.execute(
        """SELECT evidence.id,evidence.role,evidence.fact_id,evidence.available_at,
                  document.id AS document_id,version.id AS document_version_id,
                  version.version,version.title_original,version.canonical_url,
                  document_source.key AS document_source_key,version.publisher_id,
                  version.published_at,input.role AS input_role,
                  raw.id AS raw_id,raw_source.key AS raw_source_key,raw.observed_at,
                  raw.ingested_at,raw.media_type,raw.payload_sha256,raw.payload_kind,
                  raw.truncated AS raw_truncated,raw.size_bytes
           FROM event_evidence AS evidence
           JOIN document_versions AS version ON version.id=evidence.document_version_id
           JOIN documents AS document ON document.id=version.document_id
           JOIN document_version_inputs AS input
             ON input.version_id=version.id AND input.raw_record_id=evidence.evidence_id
           JOIN raw_records AS raw ON raw.id=evidence.evidence_id
           JOIN sources AS document_source ON document_source.id=version.source_id
           JOIN sources AS raw_source ON raw_source.id=raw.source_id
           WHERE evidence.event_version_id=? AND evidence.id>?
             AND (? IS NULL OR evidence.role=?)
             AND (? IS NULL OR evidence.fact_id=?)
           ORDER BY evidence.id ASC LIMIT ?""",
        (
            event.data.version.id, last_id, role, role, fact_id, fact_id,
            limit + 1,
        ),
    ).fetchall()
    page_rows = rows[:limit]
    data = [
        EventEvidenceView(
            id=row["id"], role=row["role"], fact_id=row["fact_id"],
            available_at=row["available_at"],
            document=EvidenceDocumentView(
                id=row["document_id"], version_id=row["document_version_id"],
                version=row["version"], title=row["title_original"],
                canonical_url=_evidence_document_url(row["canonical_url"]),
                source_id=row["document_source_key"], publisher_id=row["publisher_id"],
                published_at=row["published_at"], input_role=row["input_role"],
            ),
            raw_record=EvidenceRawRecordView(
                id=row["raw_id"], source_id=row["raw_source_key"],
                observed_at=row["observed_at"], ingested_at=row["ingested_at"],
                media_type=row["media_type"], payload_sha256=row["payload_sha256"],
                payload_kind=row["payload_kind"], truncated=bool(row["raw_truncated"]),
                size_bytes=row["size_bytes"],
            ),
        )
        for row in page_rows
    ]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="event_evidence", filters=filters,
            last_id=data[-1].id, dataset_epoch=event.dataset_epoch,
        )
    return EventEvidenceResponse(
        dataset_id=event.dataset_id, dataset_epoch=event.dataset_epoch,
        request_id=request_id, generated_at=utc_now(), release=event.release,
        event=EventEvidenceSubject(
            id=event.data.id, version_id=event.data.version.id,
            admission=event.data.admission,
        ),
        data=data,
        pagination=EventEvidencePagination(limit=limit, next_cursor=next_cursor),
    )


__all__ = [
    "EVIDENCE_ROLES", "EventEvidenceResponse", "EventEvidenceUnavailable",
    "EventNotFound", "list_event_evidence",
]
