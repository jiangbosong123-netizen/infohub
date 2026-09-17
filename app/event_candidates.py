from __future__ import annotations

"""Shadow stable-event projection for legacy stories.

This migration is deliberately conservative: title clustering becomes a
candidate decision, never corroboration or confirmation.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from uuid import uuid4

from .timeutil import utc_now


EVENT_SCHEMA_VERSION = "event-candidate-v1"
LEGACY_MATCHER_VERSION = "legacy-story-import-v1"


class EventProjectionError(RuntimeError):
    """Legacy story state cannot be projected without losing provenance."""


@dataclass(frozen=True)
class EventProjectionReport:
    stories_seen: int
    events_created: int
    event_versions_created: int
    mappings_created: int
    decisions_created: int
    links_created: int
    evidence_created: int

    def to_dict(self) -> dict:
        return asdict(self)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _dataset_id(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT dataset_id FROM dataset_state WHERE singleton=1").fetchone()
    if not row:
        raise EventProjectionError("dataset identity is missing")
    return row[0]


def _canonical_story(stories: dict[str, sqlite3.Row], story_id: str) -> str:
    seen: set[str] = set()
    current = story_id
    while True:
        if current in seen:
            raise EventProjectionError(f"legacy story redirect cycle contains {current}")
        seen.add(current)
        story = stories.get(current)
        if story is None:
            raise EventProjectionError(f"legacy story redirect target {current} is missing")
        target = story["redirect_to"]
        if not target:
            return current
        current = target


def _event_type(value: object) -> str:
    return {
        "product": "product_update",
        "earnings": "earnings",
        "offering": "financing",
        "ma": "ma",
        "personnel": "personnel",
        "insider": "personnel",
        "buyback": "buyback",
        "regulation": "regulation",
        "rating": "other",
    }.get(str(value or ""), "other")


def _story_documents(db: sqlite3.Connection, story_id: str) -> list[sqlite3.Row]:
    rows = db.execute(
        """SELECT item.id AS item_id,item.event_type,item.companies,
                  membership.match_reason,membership.match_score,
                  document.id AS document_id,document.current_version_id AS document_version_id,
                  document.first_seen_at,version.available_at,
                  input.raw_record_id
           FROM story_items AS membership
           JOIN items AS item ON item.id=membership.item_id
           LEFT JOIN documents AS document ON document.legacy_item_id=item.id
           LEFT JOIN document_versions AS version ON version.id=document.current_version_id
           LEFT JOIN document_version_inputs AS input
             ON input.version_id=version.id AND input.role='primary'
           WHERE membership.story_id=?
           ORDER BY item.id,input.raw_record_id""",
        (story_id,),
    ).fetchall()
    missing = [row["item_id"] for row in rows if not row["document_version_id"] or not row["raw_record_id"]]
    if missing:
        raise EventProjectionError(
            f"legacy story {story_id} has item(s) without document evidence: {missing[:5]}"
        )
    return rows


def _entity_ids(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[str]:
    slugs: set[str] = set()
    for row in rows:
        try:
            values = json.loads(row["companies"] or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        slugs.update(value for value in values if isinstance(value, str))
    if not slugs:
        return []
    placeholders = ",".join("?" for _ in slugs)
    mapped = db.execute(
        f"""SELECT company.slug,mapping.entity_id
            FROM companies AS company
            JOIN legacy_company_entities AS mapping ON mapping.company_id=company.id
            WHERE company.slug IN ({placeholders})""",
        sorted(slugs),
    ).fetchall()
    mapping = {row["slug"]: row["entity_id"] for row in mapped}
    return sorted(mapping[slug] for slug in slugs if slug in mapping)


def _topic_versions(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[str]:
    item_ids = sorted({row["item_id"] for row in rows})
    if not item_ids:
        return []
    placeholders = ",".join("?" for _ in item_ids)
    return [
        row[0] for row in db.execute(
            f"""SELECT DISTINCT catalog.current_version_id
                FROM item_topics AS assignment
                JOIN topic_slug_aliases AS alias ON alias.slug=assignment.topic_slug
                JOIN topic_catalog AS catalog ON catalog.id=alias.topic_id
                WHERE assignment.item_id IN ({placeholders})
                  AND catalog.current_version_id IS NOT NULL
                ORDER BY catalog.current_version_id""",
            item_ids,
        )
    ]


def _ensure_event(
    db: sqlite3.Connection,
    *,
    story: sqlite3.Row,
    rows: list[sqlite3.Row],
    now: str,
    dataset_id: str,
) -> tuple[str, str, bool, bool]:
    mapping = db.execute(
        "SELECT event_id FROM legacy_story_events WHERE story_id=?", (story["id"],)
    ).fetchone()
    created = False
    if mapping:
        event_id = mapping["event_id"]
    else:
        event_id = f"event_{uuid4().hex}"
        observed = [row["first_seen_at"] for row in rows if row["first_seen_at"]]
        latest = [row["available_at"] for row in rows if row["available_at"]]
        first_seen = min(observed) if observed else now
        latest_report = max([first_seen, *latest])
        db.execute(
            """INSERT INTO events(
                   id,dataset_id,first_seen_at,latest_report_at,last_fact_change_at,status
               ) VALUES(?,?,?,?,?,'candidate')""",
            (event_id, dataset_id, first_seen, latest_report, None),
        )
        created = True

    anchor = db.execute(
        "SELECT event_type FROM items WHERE id=?", (story["anchor_item_id"],)
    ).fetchone()
    payload = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "title": story["title"],
        "event_type": _event_type(anchor["event_type"] if anchor else None),
        "event_time_start": None,
        "event_time_end": None,
        "time_precision": "unknown",
        "primary_entities": _entity_ids(db, rows),
        "object_entities": [],
        "facts": [],
        "topics": _topic_versions(db, rows),
        "knowledge_status": "unknown",
        "created_by": "legacy_story_projection",
        "method_version": LEGACY_MATCHER_VERSION,
    }
    version_sha = _sha(payload)
    current = db.execute(
        """SELECT version.id,version.version,version.version_sha256
           FROM events AS event
           LEFT JOIN event_versions AS version ON version.id=event.current_version_id
           WHERE event.id=?""",
        (event_id,),
    ).fetchone()
    version_created = not current or current["version_sha256"] != version_sha
    if version_created:
        version = (current["version"] + 1) if current and current["version"] else 1
        version_id = f"event_version_{uuid4().hex}"
        db.execute(
            """INSERT INTO event_versions(
                   id,event_id,version,previous_version_id,schema_version,title,event_type,
                   event_time_start,event_time_end,time_precision,primary_entities_json,
                   object_entities_json,facts_json,topics_json,knowledge_status,
                   version_sha256,available_at,created_by,method_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id, event_id, version, current["id"] if current else None,
                payload["schema_version"], payload["title"], payload["event_type"], None,
                None, "unknown", _json(payload["primary_entities"]), "[]", "[]",
                _json(payload["topics"]), "unknown", version_sha, now,
                payload["created_by"], payload["method_version"],
            ),
        )
        db.execute(
            "UPDATE events SET current_version_id=? WHERE id=?",
            (version_id, event_id),
        )
    else:
        version_id = current["id"]

    latest = [row["available_at"] for row in rows if row["available_at"]]
    if latest:
        db.execute(
            """UPDATE events SET latest_report_at=MAX(latest_report_at,?) WHERE id=?""",
            (max(latest), event_id),
        )
    return event_id, version_id, created, version_created


def _project_links(
    db: sqlite3.Connection,
    *,
    event_id: str,
    event_version_id: str,
    story_id: str,
    rows: list[sqlite3.Row],
    dataset_id: str,
    now: str,
) -> tuple[int, int, int]:
    decisions = links = evidence = 0
    seen_documents: set[tuple[str, str]] = set()
    for row in rows:
        document_version_id = row["document_version_id"]
        raw_record_id = row["raw_record_id"]
        pair = (document_version_id, raw_record_id)
        if pair in seen_documents:
            continue
        seen_documents.add(pair)
        decision_key = (
            f"legacy-story:{story_id}:item:{row['item_id']}:"
            f"document-version:{document_version_id}:event-version:{event_version_id}"
        )
        decision = db.execute(
            "SELECT id FROM match_decisions WHERE decision_key=?", (decision_key,)
        ).fetchone()
        if decision:
            decision_id = decision["id"]
        else:
            decision_id = f"match_decision_{uuid4().hex}"
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,
                       score,decision,reason,review_status,available_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, dataset_id, decision_key,
                    _json([document_version_id]), _json([event_version_id]),
                    LEGACY_MATCHER_VERSION,
                    _json({
                        "legacy_story_id": story_id,
                        "legacy_match_reason": row["match_reason"],
                        "legacy_match_score": row["match_score"],
                    }),
                    row["match_score"], "candidate_link",
                    "Imported from legacy title clustering; semantic equivalence is unverified.",
                    "pending", now,
                ),
            )
            decisions += 1
        current_link = db.execute(
            """SELECT id,event_version_id FROM document_event_links
               WHERE document_version_id=? AND event_id=? AND role='candidate'
               ORDER BY available_at DESC,id DESC LIMIT 1""",
            (document_version_id, event_id),
        ).fetchone()
        if not current_link or current_link["event_version_id"] != event_version_id:
            db.execute(
                """INSERT INTO document_event_links(
                       id,document_version_id,event_id,event_version_id,role,decision_id,
                       available_at,supersedes_link_id
                   ) VALUES(?,?,?,?, 'candidate',?,?,?)""",
                (
                    f"document_event_link_{uuid4().hex}", document_version_id,
                    event_id, event_version_id, decision_id, now,
                    current_link["id"] if current_link else None,
                ),
            )
            links += 1
        existing_evidence = db.execute(
            """SELECT 1 FROM event_evidence
               WHERE event_version_id=? AND document_version_id=? AND evidence_id=?
                 AND fact_id IS NULL AND role='context'""",
            (event_version_id, document_version_id, raw_record_id),
        ).fetchone()
        if not existing_evidence:
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,
                       fact_id,role,available_at
                   ) VALUES(?,?,?,?,NULL,'context',?)""",
                (
                    f"event_evidence_{uuid4().hex}", event_version_id,
                    document_version_id, raw_record_id, now,
                ),
            )
            evidence += 1
    return decisions, links, evidence


def project_legacy_stories(db: sqlite3.Connection) -> EventProjectionReport:
    """Map one stable snapshot of all legacy stories into candidate events.

    Run only after document and identity backfills. Existing mappings are never
    silently redirected if the mutable legacy graph later changes.
    """
    now = utc_now()
    dataset_id = _dataset_id(db)
    stories = {row["id"]: row for row in db.execute("SELECT * FROM stories ORDER BY id")}
    canonical = {story_id: _canonical_story(stories, story_id) for story_id in stories}
    created = versions = mappings = decisions = links = evidence = 0

    event_by_canonical: dict[str, tuple[str, str]] = {}
    for story_id in sorted({value for value in canonical.values()}):
        story = stories[story_id]
        rows = _story_documents(db, story_id)
        event_id, version_id, event_created, version_created = _ensure_event(
            db, story=story, rows=rows, now=now, dataset_id=dataset_id,
        )
        event_by_canonical[story_id] = (event_id, version_id)
        created += int(event_created)
        versions += int(version_created)

    for story_id in sorted(stories):
        canonical_id = canonical[story_id]
        event_id, version_id = event_by_canonical[canonical_id]
        existing = db.execute(
            "SELECT event_id,canonical_story_id FROM legacy_story_events WHERE story_id=?",
            (story_id,),
        ).fetchone()
        if existing and (
            existing["event_id"] != event_id
            or existing["canonical_story_id"] != canonical_id
        ):
            raise EventProjectionError(
                f"legacy story {story_id} redirect topology changed after projection"
            )
        if not existing:
            db.execute(
                """INSERT INTO legacy_story_events(
                       story_id,event_id,canonical_story_id,mapping_status,available_at
                   ) VALUES(?,?,?,'candidate',?)""",
                (story_id, event_id, canonical_id, now),
            )
            mappings += 1
        if story_id != canonical_id:
            continue
        rows = _story_documents(db, story_id)
        added = _project_links(
            db, event_id=event_id, event_version_id=version_id,
            story_id=story_id, rows=rows, dataset_id=dataset_id, now=now,
        )
        decisions += added[0]
        links += added[1]
        evidence += added[2]

    return EventProjectionReport(
        stories_seen=len(stories), events_created=created,
        event_versions_created=versions, mappings_created=mappings,
        decisions_created=decisions, links_created=links, evidence_created=evidence,
    )
