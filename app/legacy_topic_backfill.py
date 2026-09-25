from __future__ import annotations

"""Freeze and import legacy ``item_topics`` as unreviewed stable assertions."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass

from .database import get_db
from .timeutil import utc_now


class LegacyTopicBackfillError(RuntimeError):
    """The legacy topic projection cannot be imported without losing provenance."""


@dataclass(frozen=True)
class LegacyTopicBackfillReport:
    status: str
    cutoff_item_id: int
    source_count: int
    snapshot_count: int
    mapped_count: int
    unexplained_count: int
    batch_processed: int
    source_sha256: str
    manifest_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rows_sha(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical(row))
        digest.update(b"\n")
    return digest.hexdigest()


def _initialize(db: sqlite3.Connection) -> sqlite3.Row:
    db.execute("BEGIN IMMEDIATE")
    dataset = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if not dataset:
        raise LegacyTopicBackfillError("dataset identity is missing")
    state = db.execute(
        "SELECT * FROM legacy_topic_backfill_state WHERE singleton=1"
    ).fetchone()
    if state:
        if state["dataset_id"] != dataset["dataset_id"]:
            raise LegacyTopicBackfillError("topic backfill belongs to a different dataset")
        if state["status"] == "failed":
            db.execute(
                """UPDATE legacy_topic_backfill_state
                   SET status='running',updated_at=?,finished_at=NULL,error_detail=NULL
                   WHERE singleton=1""",
                (utc_now(),),
            )
        return db.execute(
            "SELECT * FROM legacy_topic_backfill_state WHERE singleton=1"
        ).fetchone()

    legacy = db.execute(
        "SELECT dataset_id,cutoff_item_id,status FROM legacy_backfill_state WHERE singleton=1"
    ).fetchone()
    if not legacy or legacy["status"] != "completed":
        raise LegacyTopicBackfillError(
            "stable document backfill must be completed before topic import"
        )
    if legacy["dataset_id"] != dataset["dataset_id"]:
        raise LegacyTopicBackfillError("document backfill belongs to a different dataset")

    rows = db.execute(
        """SELECT legacy.item_id,legacy.topic_slug,legacy.evidence,
                  document.current_version_id AS document_version_id,
                  catalog.current_version_id AS topic_version_id
           FROM item_topics AS legacy
           LEFT JOIN legacy_object_mappings AS mapping
             ON mapping.dataset_id=? AND mapping.resource_type='item'
            AND mapping.target_type='document'
            AND mapping.legacy_key=CAST(legacy.item_id AS TEXT)
           LEFT JOIN documents AS document ON document.id=mapping.target_id
           LEFT JOIN topic_slug_aliases AS alias ON alias.slug=legacy.topic_slug
           LEFT JOIN topic_catalog AS catalog ON catalog.id=alias.topic_id
           WHERE legacy.item_id<=?
           ORDER BY legacy.item_id,legacy.topic_slug""",
        (dataset["dataset_id"], legacy["cutoff_item_id"]),
    ).fetchall()
    unresolved = [
        f"{row['item_id']}:{row['topic_slug']}"
        for row in rows
        if not row["document_version_id"] or not row["topic_version_id"]
    ]
    if unresolved:
        raise LegacyTopicBackfillError(
            "legacy topic rows have no frozen document/topic version: "
            + ", ".join(unresolved[:5])
        )

    now = utc_now()
    source_rows: list[dict] = []
    manifest_rows: list[dict] = []
    for row in rows:
        evidence = row["evidence"]
        evidence_sha = _sha_bytes(evidence.encode("utf-8"))
        source = {
            "item_id": row["item_id"],
            "topic_slug": row["topic_slug"],
            "evidence_text": evidence,
        }
        manifest = {
            **source,
            "evidence_sha256": evidence_sha,
            "document_version_id": row["document_version_id"],
            "topic_version_id": row["topic_version_id"],
        }
        source_rows.append(source)
        manifest_rows.append(manifest)
        db.execute(
            """INSERT INTO legacy_topic_assignment_snapshot(
                   item_id,topic_slug,evidence_text,evidence_sha256,
                   document_version_id,topic_version_id,captured_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                row["item_id"], row["topic_slug"], evidence, evidence_sha,
                row["document_version_id"], row["topic_version_id"], now,
            ),
        )
    db.execute(
        """INSERT INTO legacy_topic_backfill_state(
               singleton,dataset_id,cutoff_item_id,source_count,source_sha256,
               manifest_sha256,status,started_at,updated_at
           ) VALUES(1,?,?,?,?,?,'running',?,?)""",
        (
            dataset["dataset_id"], legacy["cutoff_item_id"], len(rows),
            _rows_sha(source_rows), _rows_sha(manifest_rows), now, now,
        ),
    )
    return db.execute(
        "SELECT * FROM legacy_topic_backfill_state WHERE singleton=1"
    ).fetchone()


def _assignment_id(state: sqlite3.Row, row: sqlite3.Row) -> str:
    payload = {
        "dataset_id": state["dataset_id"],
        "item_id": row["item_id"],
        "topic_slug": row["topic_slug"],
        "evidence_sha256": row["evidence_sha256"],
        "document_version_id": row["document_version_id"],
        "topic_version_id": row["topic_version_id"],
        "method_version": "legacy-item-topics-v1",
    }
    return "topic_assignment_legacy_" + _sha_bytes(_canonical(payload))


def _report(db: sqlite3.Connection, state: sqlite3.Row, batch: int) -> LegacyTopicBackfillReport:
    snapshot_count = db.execute(
        "SELECT COUNT(*) FROM legacy_topic_assignment_snapshot"
    ).fetchone()[0]
    mapped_count = db.execute(
        "SELECT COUNT(*) FROM legacy_topic_assignment_mappings"
    ).fetchone()[0]
    current = db.execute(
        "SELECT status FROM legacy_topic_backfill_state WHERE singleton=1"
    ).fetchone()[0]
    return LegacyTopicBackfillReport(
        status=current,
        cutoff_item_id=state["cutoff_item_id"],
        source_count=state["source_count"],
        snapshot_count=snapshot_count,
        mapped_count=mapped_count,
        unexplained_count=state["source_count"] - mapped_count,
        batch_processed=batch,
        source_sha256=state["source_sha256"],
        manifest_sha256=state["manifest_sha256"],
    )


def backfill_legacy_topics_batch(batch_size: int = 250) -> LegacyTopicBackfillReport:
    """Import one frozen batch; all writes and cursor movement are atomic."""
    if batch_size < 1 or batch_size > 5000:
        raise ValueError("batch_size must be between 1 and 5000")
    try:
        with get_db() as db:
            state = _initialize(db)
            if state["status"] == "completed":
                return _report(db, state, 0)
            rows = db.execute(
                """SELECT * FROM legacy_topic_assignment_snapshot
                   WHERE item_id>? OR (item_id=? AND topic_slug>?)
                   ORDER BY item_id,topic_slug LIMIT ?""",
                (
                    state["last_item_id"], state["last_item_id"],
                    state["last_topic_slug"], batch_size,
                ),
            ).fetchall()
            now = utc_now()
            for row in rows:
                assignment_id = _assignment_id(state, row)
                existing = db.execute(
                    """SELECT document_version_id,topic_version_id,method,method_version,
                              analysis_result_id,evidence_ids_json,status
                       FROM document_topic_assignments WHERE id=?""",
                    (assignment_id,),
                ).fetchone()
                expected = (
                    row["document_version_id"], row["topic_version_id"],
                    "legacy_projection", "legacy-item-topics-v1", None, "[]", "candidate",
                )
                if existing and tuple(existing) != expected:
                    raise LegacyTopicBackfillError(
                        f"assignment identity collision for {row['item_id']}:{row['topic_slug']}"
                    )
                if not existing:
                    db.execute(
                        """INSERT INTO document_topic_assignments(
                               id,document_version_id,topic_version_id,method,method_version,
                               analysis_result_id,evidence_ids_json,status,available_at
                           ) VALUES(?,?,?,'legacy_projection','legacy-item-topics-v1',
                                    NULL,'[]','candidate',?)""",
                        (
                            assignment_id, row["document_version_id"],
                            row["topic_version_id"], now,
                        ),
                    )
                db.execute(
                    """INSERT INTO legacy_topic_assignment_mappings(
                           item_id,topic_slug,assignment_id,evidence_sha256,available_at
                       ) VALUES(?,?,?,?,?)""",
                    (
                        row["item_id"], row["topic_slug"], assignment_id,
                        row["evidence_sha256"], now,
                    ),
                )
            if rows:
                last = rows[-1]
                db.execute(
                    """UPDATE legacy_topic_backfill_state
                       SET last_item_id=?,last_topic_slug=?,
                           processed_count=processed_count+?,updated_at=?,error_detail=NULL
                       WHERE singleton=1""",
                    (last["item_id"], last["topic_slug"], len(rows), now),
                )
            remaining = db.execute(
                """SELECT COUNT(*) FROM legacy_topic_assignment_snapshot AS snapshot
                   LEFT JOIN legacy_topic_assignment_mappings AS mapping
                     ON mapping.item_id=snapshot.item_id
                    AND mapping.topic_slug=snapshot.topic_slug
                   WHERE mapping.assignment_id IS NULL"""
            ).fetchone()[0]
            if remaining == 0:
                counts = db.execute(
                    """SELECT source_count,processed_count,
                              (SELECT COUNT(*) FROM legacy_topic_assignment_snapshot),
                              (SELECT COUNT(*) FROM legacy_topic_assignment_mappings)
                       FROM legacy_topic_backfill_state WHERE singleton=1"""
                ).fetchone()
                if len(set(counts)) != 1:
                    raise LegacyTopicBackfillError(
                        "legacy topic coverage is incomplete despite exhausted cursor"
                    )
                db.execute(
                    """UPDATE legacy_topic_backfill_state
                       SET status='completed',updated_at=?,finished_at=?,error_detail=NULL
                       WHERE singleton=1""",
                    (now, now),
                )
            state = db.execute(
                "SELECT * FROM legacy_topic_backfill_state WHERE singleton=1"
            ).fetchone()
            return _report(db, state, len(rows))
    except Exception as exc:
        with get_db() as db:
            if db.execute(
                "SELECT 1 FROM legacy_topic_backfill_state WHERE singleton=1"
            ).fetchone():
                db.execute(
                    """UPDATE legacy_topic_backfill_state
                       SET status='failed',updated_at=?,finished_at=NULL,error_detail=?
                       WHERE singleton=1""",
                    (utc_now(), f"{type(exc).__name__}: {str(exc)[:300]}"),
                )
        raise
