from __future__ import annotations

"""Coherent reads from one complete topic-statistics publication."""

import sqlite3
from dataclasses import asdict, dataclass


class TopicStatisticsUnavailable(RuntimeError):
    """No complete, current topic-statistics publication can be served."""


class TopicStatisticsNotFound(LookupError):
    pass


@dataclass(frozen=True)
class TopicStatistic:
    topic_id: str
    topic_version_id: str
    slug: str
    name: str
    group_key: str
    status: str
    document_count: int
    event_count: int
    counted_at: str
    input_manifest_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PublishedTopicStatistics:
    publication_id: str
    publication_version: int
    published_at: str
    build_id: str
    dataset_id: str
    assignment_policy_version: str
    event_policy_version: str
    started_at: str
    finished_at: str
    topics: tuple[TopicStatistic, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["topics"] = [topic.to_dict() for topic in self.topics]
        return value


def _publication(db: sqlite3.Connection) -> sqlite3.Row:
    state = db.execute(
        """SELECT state.status,state.current_build_id,state.current_publication_id,
                  publication.version AS publication_version,
                  publication.published_at,publication.build_id AS publication_build_id,
                  build.dataset_id,build.status AS build_status,
                  build.assignment_policy_version,build.event_policy_version,
                  build.topic_count,build.started_at,build.finished_at,
                  dataset.dataset_id AS current_dataset_id
           FROM topic_statistics_state AS state
           LEFT JOIN topic_statistics_publications AS publication
             ON publication.id=state.current_publication_id
           LEFT JOIN topic_statistics_builds AS build ON build.id=state.current_build_id
           LEFT JOIN dataset_state AS dataset ON dataset.singleton=1
           WHERE state.singleton=1"""
    ).fetchone()
    if (
        state is None or state["status"] != "ready"
        or state["build_status"] != "ready"
        or state["current_build_id"] != state["publication_build_id"]
        or state["current_publication_id"] is None
        or state["finished_at"] is None
        or state["dataset_id"] != state["current_dataset_id"]
    ):
        raise TopicStatisticsUnavailable("topic statistics have no current publication")
    if db.execute("SELECT 1 FROM topic_statistics_dirty LIMIT 1").fetchone():
        raise TopicStatisticsUnavailable("topic statistics have pending input changes")
    return state


def published_topic_statistics(db: sqlite3.Connection) -> PublishedTopicStatistics:
    """Return a complete publication or fail closed before returning any row."""
    publication = _publication(db)
    rows = db.execute(
        """SELECT topic.id AS topic_id,topic.current_version_id AS topic_version_id,
                  version.slug,version.name,version.group_key,topic.status,
                  statistic.document_count,statistic.event_count,statistic.counted_at,
                  statistic.input_manifest_sha256,
                  (SELECT COUNT(*) FROM topic_statistics_members AS member
                   WHERE member.statistics_id=statistic.id
                     AND member.member_type='document') AS document_members,
                  (SELECT COUNT(*) FROM topic_statistics_members AS member
                   WHERE member.statistics_id=statistic.id
                     AND member.member_type='event') AS event_members
           FROM topic_catalog AS topic
           JOIN topic_versions AS version ON version.id=topic.current_version_id
           LEFT JOIN topic_statistics_versions AS statistic
             ON statistic.build_id=? AND statistic.topic_version_id=topic.current_version_id
           ORDER BY version.group_key,version.name,topic.id""",
        (publication["current_build_id"],),
    ).fetchall()
    stored_rows = db.execute(
        "SELECT COUNT(*) FROM topic_statistics_versions WHERE build_id=?",
        (publication["current_build_id"],),
    ).fetchone()[0]
    if len(rows) != publication["topic_count"] or stored_rows != publication["topic_count"]:
        raise TopicStatisticsUnavailable("topic statistics do not cover the current catalog")
    topics: list[TopicStatistic] = []
    for row in rows:
        if (
            row["document_count"] is None
            or row["document_count"] != row["document_members"]
            or row["event_count"] != row["event_members"]
        ):
            raise TopicStatisticsUnavailable("topic statistic membership is incomplete")
        topics.append(TopicStatistic(
            topic_id=row["topic_id"], topic_version_id=row["topic_version_id"],
            slug=row["slug"], name=row["name"], group_key=row["group_key"],
            status=row["status"], document_count=row["document_count"],
            event_count=row["event_count"], counted_at=row["counted_at"],
            input_manifest_sha256=row["input_manifest_sha256"],
        ))
    return PublishedTopicStatistics(
        publication_id=publication["current_publication_id"],
        publication_version=publication["publication_version"],
        published_at=publication["published_at"],
        build_id=publication["current_build_id"], dataset_id=publication["dataset_id"],
        assignment_policy_version=publication["assignment_policy_version"],
        event_policy_version=publication["event_policy_version"],
        started_at=publication["started_at"], finished_at=publication["finished_at"],
        topics=tuple(topics),
    )


def published_topic_statistic(
    db: sqlite3.Connection, *, topic_id: str | None = None, slug: str | None = None
) -> tuple[PublishedTopicStatistics, TopicStatistic]:
    if (topic_id is None) == (slug is None):
        raise ValueError("provide exactly one of topic_id or slug")
    publication = published_topic_statistics(db)
    matches = [
        topic for topic in publication.topics
        if topic.topic_id == topic_id or topic.slug == slug
    ]
    if not matches:
        raise TopicStatisticsNotFound("topic statistic does not exist")
    if len(matches) != 1:
        raise TopicStatisticsUnavailable("topic slug is ambiguous")
    return publication, matches[0]
