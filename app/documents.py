from __future__ import annotations

"""Stable documents and immutable normalized versions.

This module writes a shadow normalized model while the portal continues to read
``items``. A crawler candidate may update that compatibility projection, but a
document version is accepted only when its immutable raw input is present and
verified in the content-addressed store.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Mapping
from uuid import uuid4

from .ingest import RawObservation, verify_payload
from .timeutil import utc_now


NORMALIZER_VERSION = "document-normalizer-v1"


class DocumentProjectionError(RuntimeError):
    """A raw record cannot be projected without breaking provenance."""


@dataclass(frozen=True)
class DocumentProjection:
    document_id: str
    version_id: str
    version: int
    created_version: bool
    published_at: str | None


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document_kind(source_type: str) -> str:
    if source_type == "sec":
        return "filing"
    if source_type == "hkex":
        return "filing"
    if source_type in {"cls", "wscn_live", "sina"}:
        return "flash"
    return "article"


def _quality(candidate: Mapping, payload_kind: str) -> tuple[str, str, int, str]:
    text = _clean_text(candidate.get("summary"))
    source_type = str(candidate.get("_source_type") or "")
    if payload_kind == "feed_entry":
        return (
            "feed_excerpt",
            "excerpt" if text else "title_only",
            0,
            "partial" if text else "not_attempted",
        )
    if source_type in {"cls", "wscn_live"}:
        source_record = candidate.get("source_record")
        has_full_field = False
        if isinstance(source_record, Mapping):
            full_field = "content" if source_type == "cls" else "content_text"
            has_full_field = bool(source_record.get(full_field))
        return (
            "publisher_text",
            "full" if text and has_full_field else ("excerpt" if text else "title_only"),
            0,
            "complete" if text and has_full_field else ("partial" if text else "not_attempted"),
        )
    if source_type == "sina":
        source_record = candidate.get("source_record")
        raw_text = ""
        if isinstance(source_record, Mapping):
            raw_text = str(source_record.get("rich_text") or "")
        truncated = int(bool(raw_text) and len(text) >= 400)
        return (
            "publisher_text",
            "excerpt" if truncated else ("full" if text else "title_only"),
            truncated,
            "partial" if truncated else ("complete" if text else "not_attempted"),
        )
    return (
        "generated_metadata",
        "excerpt" if text else "title_only",
        0,
        "partial" if text else "not_attempted",
    )


def _published_time(
    db: sqlite3.Connection, raw_record_id: str
) -> tuple[str | None, str | None, str, str, str, str]:
    row = db.execute(
        """SELECT id,utc,precision,status,rule_version,tzdb_version
           FROM source_time_values
           WHERE raw_record_id=? AND role='published'
           ORDER BY CASE WHEN status='valid' AND utc IS NOT NULL THEN 0 ELSE 1 END,
                    ordinal
           LIMIT 1""",
        (raw_record_id,),
    ).fetchone()
    if not row:
        return None, None, "unknown", "missing", "none", "unknown"
    published_at = row["utc"] if row["status"] == "valid" else None
    published_time_id = row["id"] if published_at is not None else None
    status = "parsed" if row["status"] == "valid" else row["status"]
    return (
        published_at, published_time_id, row["precision"], status,
        row["rule_version"], row["tzdb_version"],
    )


def project_candidate(
    db: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    legacy_item_id: int,
    candidate: Mapping,
    canonical_url: str,
    observation: RawObservation,
) -> DocumentProjection:
    """Project one verified raw record into a stable document/version chain.

    The caller owns the database transaction. Blob verification happens before
    any normalized row is written, so a missing object aborts both the new
    document version and the legacy ``items`` projection.
    """
    raw = db.execute(
        """SELECT id,source_id,external_id,observed_at,payload_sha256,payload_ref,
                  payload_kind,size_bytes
           FROM raw_records WHERE id=?""",
        (observation.raw_record_id,),
    ).fetchone()
    if not raw or raw["source_id"] != source["id"]:
        raise DocumentProjectionError("raw record does not belong to the source")
    if (
        raw["payload_sha256"] != observation.payload_sha256
        or raw["payload_ref"] != observation.payload_ref
        or raw["size_bytes"] != observation.size_bytes
    ):
        raise DocumentProjectionError("raw observation metadata does not match its record")
    payload = verify_payload(raw["payload_ref"], raw["payload_sha256"])
    if payload.stat().st_size != raw["size_bytes"]:
        raise DocumentProjectionError("raw payload size does not match its record")

    dataset = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if not dataset:
        raise DocumentProjectionError("dataset identity is missing")
    observed_bounds = db.execute(
        """SELECT MIN(observation.observed_at),MAX(observation.observed_at)
           FROM raw_observations AS observation
           JOIN raw_records AS record ON record.id=observation.raw_record_id
           WHERE record.source_id=? AND record.external_id=?""",
        (source["id"], raw["external_id"]),
    ).fetchone()
    first_observed_at, last_observed_at = observed_bounds

    locator = db.execute(
        """SELECT document_id FROM document_locators
           WHERE source_id=? AND external_id=?""",
        (source["id"], raw["external_id"]),
    ).fetchone()
    document = None
    if locator:
        document = db.execute(
            "SELECT * FROM documents WHERE id=?", (locator["document_id"],)
        ).fetchone()
    if document is None:
        document = db.execute(
            "SELECT * FROM documents WHERE legacy_item_id=?", (legacy_item_id,)
        ).fetchone()

    document_id = document["id"] if document else f"document_{uuid4().hex}"
    if document is None:
        db.execute(
            """INSERT INTO documents(
                   id,dataset_id,legacy_item_id,kind,first_seen_at,current_version_id,status
               ) VALUES(?,?,?,?,?,NULL,'active')""",
            (
                document_id,
                dataset["dataset_id"],
                legacy_item_id,
                _document_kind(source["type"]),
                first_observed_at,
            ),
        )
        document = db.execute(
            "SELECT * FROM documents WHERE id=?", (document_id,)
        ).fetchone()

    existing_locator = db.execute(
        """SELECT document_id,first_observed_at,last_observed_at
           FROM document_locators WHERE source_id=? AND external_id=?""",
        (source["id"], raw["external_id"]),
    ).fetchone()
    if existing_locator and existing_locator["document_id"] != document_id:
        raise DocumentProjectionError("one source locator resolved to multiple documents")
    if existing_locator:
        if last_observed_at > existing_locator["last_observed_at"]:
            db.execute(
                """UPDATE document_locators SET last_observed_at=?,canonical_url=?
                   WHERE source_id=? AND external_id=?""",
                (last_observed_at, canonical_url, source["id"], raw["external_id"]),
            )
    else:
        db.execute(
            """INSERT INTO document_locators(
                   document_id,source_id,external_id,canonical_url,relation,
                   first_observed_at,last_observed_at
               ) VALUES(?,?,?,?, 'canonical',?,?)""",
            (
                document_id, source["id"], raw["external_id"], canonical_url,
                first_observed_at, last_observed_at,
            ),
        )

    title = _clean_text(candidate.get("title"))
    text = _clean_text(candidate.get("summary"))
    if not title:
        raise DocumentProjectionError("a document version requires a title")
    language = _clean_text(candidate.get("language")) or "und"
    (
        published_at, published_time_id, published_precision, time_status,
        time_rule_version, tzdb_version,
    ) = _published_time(db, raw["id"])
    origin, extent, truncated, extraction = _quality(
        {**candidate, "_source_type": source["type"]}, raw["payload_kind"]
    )
    version_value = {
        "normalizer_version": NORMALIZER_VERSION,
        "title_original": title,
        "language": language,
        "text": text,
        "canonical_url": canonical_url,
        "source_id": source["id"],
        "publisher_id": None,
        "published_at": published_at,
        "published_precision": published_precision,
        "time_status": time_status,
        "time_rule_version": time_rule_version,
        "tzdb_version": tzdb_version,
        "content_origin": origin,
        "content_extent": extent,
        "truncated": truncated,
        "extraction_status": extraction,
    }
    version_sha = _sha256(json.dumps(
        version_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))
    current = None
    if document["current_version_id"]:
        current = db.execute(
            "SELECT * FROM document_versions WHERE id=?",
            (document["current_version_id"],),
        ).fetchone()
        if not current:
            raise DocumentProjectionError("document current version is missing")

    if current and current["version_sha256"] == version_sha:
        db.execute(
            """INSERT OR IGNORE INTO document_version_inputs(version_id,raw_record_id,role)
               VALUES(?,?, 'additional')""",
            (current["id"], raw["id"]),
        )
        return DocumentProjection(
            document_id, current["id"], current["version"], False, published_at
        )

    version = 1 if current is None else current["version"] + 1
    version_id = f"document_version_{uuid4().hex}"
    correction_kind = "initial"
    if current:
        correction_kind = (
            "content_change"
            if current["content_sha256"] != _sha256(text)
            or current["title_original"] != title
            else "metadata_change"
        )
    normalized_at = utc_now()
    db.execute(
        """INSERT INTO document_versions(
               id,document_id,version,previous_version_id,normalizer_version,normalized_at,
               title_original,language,text,content_sha256,version_sha256,
               canonical_url,source_id,publisher_id,published_at,published_time_value_id,
               published_precision,time_status,time_rule_version,tzdb_version,
               content_origin,content_extent,truncated,extraction_status,
               correction_kind,available_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version_id, document_id, version, current["id"] if current else None,
            NORMALIZER_VERSION, normalized_at, title, language, text, _sha256(text), version_sha,
            canonical_url, source["id"], None, published_at, published_time_id,
            published_precision, time_status, time_rule_version, tzdb_version,
            origin, extent, truncated, extraction, correction_kind, normalized_at,
        ),
    )
    db.execute(
        """INSERT INTO document_version_inputs(version_id,raw_record_id,role)
           VALUES(?,?, 'primary')""",
        (version_id, raw["id"]),
    )
    updated = db.execute(
        "UPDATE documents SET current_version_id=? WHERE id=? AND current_version_id IS ?",
        (version_id, document_id, current["id"] if current else None),
    )
    if updated.rowcount != 1:
        raise DocumentProjectionError("document current version changed concurrently")
    return DocumentProjection(document_id, version_id, version, True, published_at)
