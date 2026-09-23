from __future__ import annotations

"""Portal-compatible view of one admitted topic-statistics publication."""

import sqlite3

from .topic_statistics_admission import approved_admission
from .topic_statistics_query import (
    TopicStatisticsNotFound,
    TopicStatisticsUnavailable,
    published_topic_statistics,
)


def published_portal_topics(db: sqlite3.Connection) -> list[dict]:
    publication = published_topic_statistics(db)
    approved_admission(db, publication.publication_id)
    if any(topic.status in {"restricted", "merged"} for topic in publication.topics):
        raise TopicStatisticsUnavailable(
            "portal topic projection contains an unsupported identity"
        )
    versions = {
        row["id"]: row
        for row in db.execute(
            "SELECT id,description FROM topic_versions WHERE id IN ("
            + ",".join("?" for _ in publication.topics) + ")",
            tuple(topic.topic_version_id for topic in publication.topics),
        )
    } if publication.topics else {}
    mapped = {
        row["topic_version_id"]: row
        for row in db.execute(
            """SELECT statistic.topic_version_id,COUNT(member.ordinal) AS member_count,
                      COUNT(version.id) AS version_count,COUNT(item.id) AS item_count,
                      COUNT(DISTINCT item.id) AS unique_item_count,
                      MAX(item.published_at) AS last_at
               FROM topic_statistics_versions AS statistic
               LEFT JOIN topic_statistics_members AS member
                 ON member.statistics_id=statistic.id AND member.member_type='document'
               LEFT JOIN documents AS document ON document.id=member.resource_id
               LEFT JOIN document_versions AS version
                 ON version.id=member.version_id AND version.document_id=document.id
               LEFT JOIN items AS item ON item.id=document.legacy_item_id
               WHERE statistic.build_id=?
               GROUP BY statistic.topic_version_id""",
            (publication.build_id,),
        )
    }
    result: list[dict] = []
    for topic in publication.topics:
        version = versions.get(topic.topic_version_id)
        mapping = mapped.get(topic.topic_version_id)
        if (
            version is None or mapping is None
            or mapping["member_count"] != topic.document_count
            or mapping["version_count"] != topic.document_count
            or mapping["item_count"] != topic.document_count
            or mapping["unique_item_count"] != topic.document_count
        ):
            raise TopicStatisticsUnavailable(
                "admitted topic documents do not have a complete portal projection"
            )
        if topic.status != "active":
            continue
        result.append({
            "id": topic.topic_id, "version_id": topic.topic_version_id,
            "slug": topic.slug, "name": topic.name, "group_key": topic.group_key,
            "description": version["description"], "status": topic.status,
            "total": topic.document_count, "selected": topic.document_count,
            "event_count": topic.event_count, "last_at": mapping["last_at"],
            "audited_statistics": True,
            "publication_id": publication.publication_id,
        })
    return result


def published_portal_topic(db: sqlite3.Connection, slug: str) -> dict:
    topics = published_portal_topics(db)
    matches = [topic for topic in topics if topic["slug"] == slug]
    if not matches:
        raise TopicStatisticsNotFound("portal topic does not exist")
    if len(matches) != 1:
        raise TopicStatisticsUnavailable("portal topic slug is ambiguous")
    return matches[0]
