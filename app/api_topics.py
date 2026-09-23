from __future__ import annotations

"""Typed v1 topic catalog backed only by a complete statistics publication."""

import hashlib
import sqlite3
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field

from .api_auth import ApiPrincipal
from .api_cursor import decode_cursor, encode_cursor
from .timeutil import utc_now
from .topic_statistics_query import (
    PublishedTopicStatistics,
    TopicStatistic,
    TopicStatisticsNotFound,
    TopicStatisticsUnavailable,
    published_topic_statistic,
    published_topic_statistics,
)


TOPIC_GROUPS = frozenset({"company_model", "technology", "format", "macro", "research"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopicCountPolicy(_StrictModel):
    assignment_policy_version: str
    event_policy_version: str
    unreviewed_assignments_excluded: Literal[True] = True


class TopicPublication(_StrictModel):
    id: str
    version: int = Field(ge=1)
    build_id: str
    published_at: str
    started_at: str
    finished_at: str
    count_policy: TopicCountPolicy


class TopicView(_StrictModel):
    id: str
    version_id: str
    slug: str
    name: str
    group: Literal["company_model", "technology", "format", "macro", "research"]
    status: Literal["active", "retired"]
    document_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    counted_at: str
    input_manifest_sha256: str = Field(min_length=64, max_length=64)


class TopicPagination(_StrictModel):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    consistency: Literal["publication"] = "publication"


class TopicListResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    publication: TopicPublication
    data: list[TopicView]
    pagination: TopicPagination


class TopicResponse(_StrictModel):
    api_version: Literal["v1"] = "v1"
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str
    dataset_epoch: str
    request_id: str
    generated_at: str
    publication: TopicPublication
    data: TopicView


class RestrictedTopic(PermissionError):
    pass


def _identity(db: sqlite3.Connection, publication: PublishedTopicStatistics) -> sqlite3.Row:
    identity = db.execute(
        "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if identity is None or identity["dataset_id"] != publication.dataset_id:
        raise TopicStatisticsUnavailable("topic publication belongs to another dataset")
    return identity


def _publication_view(publication: PublishedTopicStatistics) -> TopicPublication:
    return TopicPublication(
        id=publication.publication_id, version=publication.publication_version,
        build_id=publication.build_id, published_at=publication.published_at,
        started_at=publication.started_at, finished_at=publication.finished_at,
        count_policy=TopicCountPolicy(
            assignment_policy_version=publication.assignment_policy_version,
            event_policy_version=publication.event_policy_version,
        ),
    )


def _topic_view(topic: TopicStatistic) -> TopicView:
    if topic.status == "restricted":
        raise RestrictedTopic("topic is restricted")
    if topic.status == "merged":
        raise TopicStatisticsUnavailable("merged topic canonical projection is not ready")
    if topic.status not in {"active", "inactive"}:
        raise TopicStatisticsUnavailable("topic status is unsupported")
    return TopicView(
        id=topic.topic_id, version_id=topic.topic_version_id, slug=topic.slug,
        name=topic.name, group=topic.group_key,
        status="retired" if topic.status == "inactive" else "active",
        document_count=topic.document_count, event_count=topic.event_count,
        counted_at=topic.counted_at,
        input_manifest_sha256=topic.input_manifest_sha256,
    )


def list_topics(
    db: sqlite3.Connection,
    principal: ApiPrincipal,
    *,
    request_id: str,
    limit: int,
    cursor: str | None,
    group: str | None,
) -> TopicListResponse:
    publication = published_topic_statistics(db)
    identity = _identity(db, publication)
    if any(topic.status in {"restricted", "merged"} for topic in publication.topics):
        raise TopicStatisticsUnavailable(
            "topic catalog contains identities requiring a reviewed public projection"
        )
    filters = {"group": group, "publication_id": publication.publication_id}
    last_id = ""
    if cursor:
        last_id = decode_cursor(
            db, principal, token=cursor, resource="topics", filters=filters,
            dataset_epoch=identity["current_epoch"],
        ).last_id
    eligible = sorted(
        (
            topic for topic in publication.topics
            if topic.topic_id > last_id and (group is None or topic.group_key == group)
        ),
        key=lambda topic: topic.topic_id,
    )
    page = eligible[:limit]
    next_cursor = None
    if len(eligible) > limit:
        next_cursor = encode_cursor(
            db, principal, resource="topics", filters=filters,
            last_id=page[-1].topic_id, dataset_epoch=identity["current_epoch"],
        )
    return TopicListResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        publication=_publication_view(publication),
        data=[_topic_view(topic) for topic in page],
        pagination=TopicPagination(limit=limit, next_cursor=next_cursor),
    )


def get_topic(
    db: sqlite3.Connection, *, request_id: str, topic_id: str
) -> TopicResponse:
    publication, topic = published_topic_statistic(db, topic_id=topic_id)
    identity = _identity(db, publication)
    return TopicResponse(
        dataset_id=identity["dataset_id"], dataset_epoch=identity["current_epoch"],
        request_id=request_id, generated_at=utc_now(),
        publication=_publication_view(publication), data=_topic_view(topic),
    )


def topic_etag(response: TopicResponse, principal: ApiPrincipal) -> str:
    payload = {
        "consumer_id": principal.consumer_id,
        "authz_version": principal.authz_version,
        "scopes": sorted(principal.scopes),
        "dataset_id": response.dataset_id,
        "dataset_epoch": response.dataset_epoch,
        "publication": response.publication.model_dump(mode="json"),
        "data": response.data.model_dump(mode="json"),
    }
    return '"' + hashlib.sha256(rfc8785.dumps(payload)).hexdigest() + '"'


__all__ = [
    "RestrictedTopic", "TOPIC_GROUPS", "TopicListResponse", "TopicResponse",
    "TopicStatisticsNotFound", "TopicStatisticsUnavailable", "get_topic",
    "list_topics", "topic_etag",
]
