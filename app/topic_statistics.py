from __future__ import annotations

"""Resumable, evidence-preserving topic statistics builds."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from uuid import uuid4

from .database import get_db
from .timeutil import utc_now


ASSIGNMENT_POLICY_VERSION = "effective-review-v1"
EVENT_POLICY_VERSION = "current-stable-event-v1"


class TopicStatisticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class TopicStatisticsBuildReport:
    status: str
    build_id: str
    last_topic_id: str
    topic_count: int
    processed: int
    dirty_remaining: int
    publication_id: str | None
    publication_version: int | None
    error_detail: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _active_build(db: sqlite3.Connection) -> sqlite3.Row | None:
    rows = db.execute(
        "SELECT * FROM topic_statistics_builds WHERE status='building' ORDER BY started_at,id"
    ).fetchall()
    if len(rows) > 1:
        raise TopicStatisticsError("multiple topic statistics builds are active")
    return rows[0] if rows else None


def _initialize_build(db: sqlite3.Connection, now: str) -> sqlite3.Row | None:
    active = _active_build(db)
    if active:
        return active
    dirty = db.execute("SELECT COUNT(*) FROM topic_statistics_dirty").fetchone()[0]
    state = db.execute(
        "SELECT status,current_build_id FROM topic_statistics_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise TopicStatisticsError("topic statistics schema has not been migrated")
    if state["status"] == "ready" and not dirty:
        return None
    dataset = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if dataset is None:
        raise TopicStatisticsError("dataset identity is missing")
    build_id = f"topic_statistics_build_{uuid4().hex}"
    db.execute(
        """INSERT INTO topic_statistics_builds(
               id,dataset_id,status,assignment_policy_version,event_policy_version,
               started_at,updated_at)
           VALUES(?,?,'building',?,?,?,?)""",
        (
            build_id, dataset["dataset_id"], ASSIGNMENT_POLICY_VERSION,
            EVENT_POLICY_VERSION, now, now,
        ),
    )
    return db.execute(
        "SELECT * FROM topic_statistics_builds WHERE id=?", (build_id,)
    ).fetchone()


def _accepted_documents(
    db: sqlite3.Connection, topic_id: str, dataset_id: str
) -> list[dict]:
    rows = db.execute(
        """WITH latest_review AS (
               SELECT review.* FROM topic_assignment_reviews AS review
               WHERE NOT EXISTS(
                   SELECT 1 FROM topic_assignment_reviews AS later
                   WHERE later.assignment_id=review.assignment_id
                     AND later.version>review.version
               )
           )
           SELECT document.id AS resource_id,document_version.id AS version_id,
                  document_version.version AS document_version,
                  assignment.id AS assignment_id,assignment.topic_version_id,
                  assignment.method,assignment.method_version,assignment.available_at,
                  review.id AS review_id,review.version AS review_version,
                  COALESCE(review.decision,assignment.status) AS effective_status
           FROM document_topic_assignments AS assignment
           JOIN topic_versions AS assigned_topic
             ON assigned_topic.id=assignment.topic_version_id
           JOIN document_versions AS document_version
             ON document_version.id=assignment.document_version_id
           JOIN documents AS document ON document.id=document_version.document_id
           LEFT JOIN latest_review AS review ON review.assignment_id=assignment.id
           WHERE assigned_topic.topic_id=? AND document.dataset_id=?
             AND COALESCE(review.decision,assignment.status)='accepted'
           ORDER BY document.id,document_version.version,assignment.available_at,assignment.id""",
        (topic_id, dataset_id),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["resource_id"]].append(row)
    members: list[dict] = []
    for resource_id in sorted(grouped):
        assignments = grouped[resource_id]
        selected = assignments[-1]
        provenance = {
            "policy": ASSIGNMENT_POLICY_VERSION,
            "selected_assignment_id": selected["assignment_id"],
            "accepted_assignments": [
                {
                    "assignment_id": row["assignment_id"],
                    "topic_version_id": row["topic_version_id"],
                    "document_version_id": row["version_id"],
                    "method": row["method"],
                    "method_version": row["method_version"],
                    "review_id": row["review_id"],
                    "review_version": row["review_version"],
                }
                for row in assignments
            ],
        }
        members.append({
            "member_type": "document",
            "resource_id": resource_id,
            "version_id": selected["version_id"],
            "provenance": provenance,
        })
    return members


def _stable_events(
    db: sqlite3.Connection, topic_id: str, dataset_id: str
) -> list[dict]:
    rows = db.execute(
        """SELECT event.id AS resource_id,event.current_version_id AS version_id,
                  event.status,event_version.topics_json,event_version.knowledge_status
           FROM events AS event
           JOIN event_versions AS event_version ON event_version.id=event.current_version_id
           WHERE event.dataset_id=? AND event.status IN ('active','resolved')
             AND EXISTS(
                 SELECT 1 FROM json_each(event_version.topics_json) AS reference
                 JOIN topic_versions AS referenced_topic ON referenced_topic.id=reference.value
                 WHERE referenced_topic.topic_id=?
             )
           ORDER BY event.id""",
        (dataset_id, topic_id),
    ).fetchall()
    known_versions = {
        row[0] for row in db.execute(
            "SELECT id FROM topic_versions WHERE topic_id=?", (topic_id,)
        )
    }
    return [
        {
            "member_type": "event",
            "resource_id": row["resource_id"],
            "version_id": row["version_id"],
            "provenance": {
                "policy": EVENT_POLICY_VERSION,
                "event_status": row["status"],
                "knowledge_status": row["knowledge_status"],
                "matched_topic_version_ids": sorted(
                    value for value in json.loads(row["topics_json"])
                    if value in known_versions
                ),
            },
        }
        for row in rows
    ]


def _build_topic(
    db: sqlite3.Connection, build: sqlite3.Row, topic: sqlite3.Row, now: str
) -> None:
    members = (
        _accepted_documents(db, topic["id"], build["dataset_id"])
        + _stable_events(db, topic["id"], build["dataset_id"])
    )
    member_rows = []
    for ordinal, member in enumerate(members):
        payload = {
            "ordinal": ordinal,
            **member,
        }
        member_rows.append((payload, _sha(payload)))
    manifest = {
        "topic_id": topic["id"],
        "topic_version_id": topic["current_version_id"],
        "assignment_policy_version": build["assignment_policy_version"],
        "event_policy_version": build["event_policy_version"],
        "members": [digest for _, digest in member_rows],
    }
    statistics_id = f"topic_statistics_{uuid4().hex}"
    db.execute(
        """INSERT INTO topic_statistics_versions(
               id,build_id,topic_version_id,document_count,event_count,
               input_manifest_sha256,counted_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            statistics_id, build["id"], topic["current_version_id"],
            sum(row[0]["member_type"] == "document" for row in member_rows),
            sum(row[0]["member_type"] == "event" for row in member_rows),
            _sha(manifest), now,
        ),
    )
    db.executemany(
        """INSERT INTO topic_statistics_members(
               statistics_id,ordinal,member_type,resource_id,version_id,
               provenance_json,member_sha256)
           VALUES(?,?,?,?,?,?,?)""",
        [
            (
                statistics_id, payload["ordinal"], payload["member_type"],
                payload["resource_id"], payload["version_id"],
                _canonical(payload["provenance"]).decode("utf-8"), digest,
            )
            for payload, digest in member_rows
        ],
    )
    db.execute("DELETE FROM topic_statistics_dirty WHERE topic_id=?", (topic["id"],))


def _publication(db: sqlite3.Connection, build_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT id,version FROM topic_statistics_publications WHERE build_id=?",
        (build_id,),
    ).fetchone()


def _report(
    db: sqlite3.Connection,
    build: sqlite3.Row,
    *,
    processed: int,
) -> TopicStatisticsBuildReport:
    current = db.execute(
        "SELECT * FROM topic_statistics_builds WHERE id=?", (build["id"],)
    ).fetchone()
    publication = _publication(db, build["id"])
    return TopicStatisticsBuildReport(
        status=current["status"], build_id=current["id"],
        last_topic_id=current["last_topic_id"], topic_count=current["topic_count"],
        processed=processed,
        dirty_remaining=db.execute(
            "SELECT COUNT(*) FROM topic_statistics_dirty"
        ).fetchone()[0],
        publication_id=publication["id"] if publication else None,
        publication_version=publication["version"] if publication else None,
        error_detail=current["error_detail"],
    )


def _published_report(db: sqlite3.Connection) -> TopicStatisticsBuildReport:
    state = db.execute(
        "SELECT current_build_id FROM topic_statistics_state WHERE singleton=1"
    ).fetchone()
    build = db.execute(
        "SELECT * FROM topic_statistics_builds WHERE id=?", (state[0],)
    ).fetchone()
    return _report(db, build, processed=0)


def _finish_build(
    db: sqlite3.Connection, build: sqlite3.Row, now: str
) -> TopicStatisticsBuildReport:
    dirty = db.execute("SELECT COUNT(*) FROM topic_statistics_dirty").fetchone()[0]
    current_topics = db.execute("SELECT COUNT(*) FROM topic_catalog").fetchone()[0]
    valid_rows = db.execute(
        """SELECT COUNT(*) FROM topic_statistics_versions AS statistic
           JOIN topic_catalog AS topic ON topic.current_version_id=statistic.topic_version_id
           WHERE statistic.build_id=?""",
        (build["id"],),
    ).fetchone()[0]
    built_rows = db.execute(
        "SELECT COUNT(*) FROM topic_statistics_versions WHERE build_id=?", (build["id"],)
    ).fetchone()[0]
    if dirty or built_rows != current_topics or valid_rows != current_topics:
        detail = (
            f"inputs changed during build: dirty={dirty}, built={built_rows}, "
            f"current={current_topics}, current_versions={valid_rows}"
        )
        db.execute(
            """UPDATE topic_statistics_builds SET status='failed',updated_at=?,
                      finished_at=?,error_detail=? WHERE id=?""",
            (now, now, detail, build["id"]),
        )
        return _report(db, build, processed=0)
    db.execute(
        """UPDATE topic_statistics_builds SET status='ready',topic_count=?,updated_at=?,
                  finished_at=?,error_detail=NULL WHERE id=?""",
        (built_rows, now, now, build["id"]),
    )
    previous = db.execute(
        "SELECT id,version FROM topic_statistics_publications ORDER BY version DESC LIMIT 1"
    ).fetchone()
    publication_id = f"topic_statistics_publication_{uuid4().hex}"
    db.execute(
        """INSERT INTO topic_statistics_publications(
               id,version,build_id,previous_publication_id,published_at)
           VALUES(?,?,?,?,?)""",
        (
            publication_id, (previous["version"] + 1) if previous else 1,
            build["id"], previous["id"] if previous else None, now,
        ),
    )
    db.execute(
        """UPDATE topic_statistics_state SET status='ready',current_build_id=?,
                  current_publication_id=?,updated_at=? WHERE singleton=1""",
        (build["id"], publication_id, now),
    )
    return _report(db, build, processed=0)


def advance_topic_statistics(limit: int = 25) -> TopicStatisticsBuildReport:
    """Commit one bounded topic batch, publishing only a complete stable build."""
    if limit < 1 or limit > 250:
        raise ValueError("limit must be between 1 and 250")
    active_id: str | None = None
    try:
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = utc_now()
            build = _initialize_build(db, now)
            if build is None:
                return _published_report(db)
            active_id = build["id"]
            topics = db.execute(
                """SELECT id,current_version_id FROM topic_catalog
                   WHERE id>? ORDER BY id LIMIT ?""",
                (build["last_topic_id"], limit),
            ).fetchall()
            for topic in topics:
                if topic["current_version_id"] is None:
                    raise TopicStatisticsError(
                        f"topic {topic['id']} has no current version"
                    )
                _build_topic(db, build, topic, now)
            if topics:
                db.execute(
                    """UPDATE topic_statistics_builds
                       SET last_topic_id=?,topic_count=topic_count+?,updated_at=?
                       WHERE id=?""",
                    (topics[-1]["id"], len(topics), now, build["id"]),
                )
                return _report(db, build, processed=len(topics))
            return _finish_build(db, build, now)
    except Exception as exc:
        if active_id:
            with get_db() as db:
                row = db.execute(
                    "SELECT status FROM topic_statistics_builds WHERE id=?", (active_id,)
                ).fetchone()
                if row and row["status"] == "building":
                    now = utc_now()
                    db.execute(
                        """UPDATE topic_statistics_builds SET status='failed',updated_at=?,
                                  finished_at=?,error_detail=? WHERE id=?""",
                        (now, now, f"{type(exc).__name__}: {str(exc)[:300]}", active_id),
                    )
        raise
