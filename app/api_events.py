from __future__ import annotations

"""Typed v1 reads over the exact current approved event release manifest."""

import hashlib
import json
import sqlite3
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .event_dataset_release import EventDatasetReleaseError, approved_event_release
from .event_revisions import EVENT_TYPES
from .timeutil import utc_now


PUBLIC_STATES = frozenset({"reported", "corroborated", "confirmed"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventReleaseView(_StrictModel):
    review_id: str
    version: int = Field(ge=1)
    sample_evaluation_id: str
    manifest_sha256: str = Field(min_length=64, max_length=64)
    reviewed_at: str
    policy_version: Literal["event-release-v1"]


class EventAdmissionView(_StrictModel):
    review_id: str
    review_version: int = Field(ge=1)
    public_state: Literal["reported", "corroborated", "confirmed"]
    metrics_sha256: str = Field(min_length=64, max_length=64)
    reviewed_at: str
    policy_version: Literal["event-admission-v1"]


class EventFactView(_StrictModel):
    fact_id: str
    attributes: dict[str, JsonValue]


class EventTopicView(_StrictModel):
    id: str
    version_id: str


class EventVersionView(_StrictModel):
    id: str
    version: int = Field(ge=1)
    schema_version: str
    title: str
    event_type: Literal[
        "model_release", "product_update", "research_result", "earnings",
        "financing", "ma", "personnel", "buyback", "regulation",
        "litigation", "macro_release", "monetary_policy", "other",
    ]
    event_time_start: str | None
    event_time_end: str | None
    time_precision: Literal["unknown", "year", "month", "day", "minute", "second", "range"]
    primary_entity_ids: list[str]
    object_entity_ids: list[str]
    topics: list[EventTopicView]
    facts: list[EventFactView]
    knowledge_status: Literal["reported", "corroborated", "confirmed_by_primary"]
    available_at: str
    version_sha256: str = Field(min_length=64, max_length=64)


class EventView(_StrictModel):
    id: str
    first_seen_at: str
    latest_report_at: str
    version: EventVersionView
    admission: EventAdmissionView


class EventPagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["release"] = "release"
    order: Literal["event_id_asc"] = "event_id_asc"


class EventListResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    release: EventReleaseView
    data: list[EventView]
    pagination: EventPagination


class EventResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    release: EventReleaseView
    data: EventView


class EventCatalogUnavailable(RuntimeError):
    pass


class EventNotFound(LookupError):
    pass


def _approved_release(db: sqlite3.Connection) -> dict:
    try:
        return approved_event_release(db)
    except EventDatasetReleaseError as exc:
        raise EventCatalogUnavailable("event release is unavailable") from exc


def _decode_list(value: str, field: str) -> list:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventCatalogUnavailable(f"event {field} is invalid") from exc
    if not isinstance(decoded, list):
        raise EventCatalogUnavailable(f"event {field} is invalid")
    return decoded


def _release_view(release: dict) -> EventReleaseView:
    return EventReleaseView(
        review_id=release["release_review_id"], version=release["version"],
        sample_evaluation_id=release["sample_evaluation_id"],
        manifest_sha256=release["event_manifest_sha256"],
        reviewed_at=release["reviewed_at"], policy_version=release["policy_version"],
    )


def _event_view(db: sqlite3.Connection, manifest: dict) -> EventView:
    row = db.execute(
        """SELECT event.first_seen_at,event.latest_report_at,version.*,
                  review.reviewed_at,review.policy_version
           FROM events AS event
           JOIN event_versions AS version ON version.id=? AND version.event_id=event.id
           JOIN event_admission_reviews AS review ON review.id=?
           WHERE event.id=?""",
        (
            manifest["event_version_id"], manifest["admission_review_id"],
            manifest["event_id"],
        ),
    ).fetchone()
    if row is None:
        raise EventCatalogUnavailable("released event is unavailable")
    primary = _decode_list(row["primary_entities_json"], "primary entities")
    objects = _decode_list(row["object_entities_json"], "object entities")
    topic_versions = _decode_list(row["topics_json"], "topics")
    facts = _decode_list(row["facts_json"], "facts")
    if (
        any(not isinstance(value, str) or not value for value in primary + objects + topic_versions)
        or any(not isinstance(fact, dict) for fact in facts)
    ):
        raise EventCatalogUnavailable("released event semantic fields are invalid")
    topics = []
    for version_id in topic_versions:
        topic = db.execute(
            "SELECT topic_id FROM topic_versions WHERE id=?", (version_id,)
        ).fetchone()
        if topic is None:
            raise EventCatalogUnavailable("released event topic is unavailable")
        topics.append(EventTopicView(id=topic["topic_id"], version_id=version_id))
    fact_views = []
    for fact in facts:
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id:
            raise EventCatalogUnavailable("released event fact is invalid")
        fact_views.append(EventFactView(
            fact_id=fact_id,
            attributes={key: value for key, value in fact.items() if key != "fact_id"},
        ))
    return EventView(
        id=manifest["event_id"], first_seen_at=row["first_seen_at"],
        latest_report_at=row["latest_report_at"],
        version=EventVersionView(
            id=row["id"], version=row["version"], schema_version=row["schema_version"],
            title=row["title"], event_type=row["event_type"],
            event_time_start=row["event_time_start"], event_time_end=row["event_time_end"],
            time_precision=row["time_precision"], primary_entity_ids=primary,
            object_entity_ids=objects, topics=topics, facts=fact_views,
            knowledge_status=row["knowledge_status"], available_at=row["available_at"],
            version_sha256=row["version_sha256"],
        ),
        admission=EventAdmissionView(
            review_id=manifest["admission_review_id"],
            review_version=manifest["admission_review_version"],
            public_state=manifest["public_state"],
            metrics_sha256=manifest["metrics_sha256"],
            reviewed_at=row["reviewed_at"], policy_version=row["policy_version"],
        ),
    )


def _matches(
    event: EventView, *, query: str | None, event_type: str | None,
    public_state: str | None, entity_id: str | None, topic_id: str | None,
) -> bool:
    return (
        (query is None or query.casefold() in event.version.title.casefold())
        and (event_type is None or event.version.event_type == event_type)
        and (public_state is None or event.admission.public_state == public_state)
        and (
            entity_id is None
            or entity_id in event.version.primary_entity_ids
            or entity_id in event.version.object_entity_ids
        )
        and (topic_id is None or any(topic.id == topic_id for topic in event.version.topics))
    )


def list_events(
    db: sqlite3.Connection, principal: ApiPrincipal, *, request_id: str,
    limit: int, cursor: str | None, query: str | None, event_type: str | None,
    public_state: str | None, entity_id: str | None, topic_id: str | None,
) -> EventListResponse:
    release = _approved_release(db)
    filters = {
        "release_review_id": release["release_review_id"], "q": query,
        "type": event_type, "state": public_state, "entity_id": entity_id,
        "topic_id": topic_id,
    }
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="events", filters=filters,
            dataset_epoch=release["dataset_epoch"],
        ).last_id
    events = sorted(
        (
            _event_view(db, manifest) for manifest in release["event_manifest"]
            if manifest["event_id"] > last_id
        ),
        key=lambda item: item.id,
    )
    eligible = [
        event for event in events if _matches(
            event, query=query, event_type=event_type, public_state=public_state,
            entity_id=entity_id, topic_id=topic_id,
        )
    ]
    page = eligible[:limit]
    next_cursor = None
    if len(eligible) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="events", filters=filters,
            last_id=page[-1].id, dataset_epoch=release["dataset_epoch"],
        )
    return EventListResponse(
        dataset_id=release["dataset_id"], dataset_epoch=release["dataset_epoch"],
        request_id=request_id, generated_at=utc_now(), release=_release_view(release),
        data=page, pagination=EventPagination(limit=limit, next_cursor=next_cursor),
    )


def get_event(db: sqlite3.Connection, *, request_id: str, event_id: str) -> EventResponse:
    release = _approved_release(db)
    matches = [
        manifest for manifest in release["event_manifest"]
        if manifest["event_id"] == event_id
    ]
    if not matches:
        raise EventNotFound("event is not in the current release")
    if len(matches) != 1:
        raise EventCatalogUnavailable("event release manifest is ambiguous")
    return EventResponse(
        dataset_id=release["dataset_id"], dataset_epoch=release["dataset_epoch"],
        request_id=request_id, generated_at=utc_now(), release=_release_view(release),
        data=_event_view(db, matches[0]),
    )


def event_etag(response: EventResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "release": response.release.model_dump(mode="json"),
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'


__all__ = [
    "EVENT_TYPES", "PUBLIC_STATES", "EventCatalogUnavailable", "EventListResponse",
    "EventNotFound", "EventResponse", "event_etag", "get_event", "list_events",
]
