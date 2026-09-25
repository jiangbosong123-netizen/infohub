from __future__ import annotations

"""Resumable, evidence-honest migration of legacy portal rows."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Callable
from uuid import uuid4

from .crawler.runner import _normalize_url
from .database import get_db
from .documents import project_candidate
from .ingest import (
    IngestRun,
    RawObservation,
    begin_ingest_run,
    finish_ingest_run,
    observe_candidate,
    verify_payload,
)
from .timeutil import format_utc, parse_utc, utc_now


class LegacyBackfillError(RuntimeError):
    """Historical rows cannot be mapped without an unexplained loss."""


DISCOVERY_MAPPING_COUNT_SQL = """SELECT COUNT(*) FROM document_locators AS locator
    CROSS JOIN legacy_object_mappings AS mapping
      ON mapping.target_id=CAST(locator.source_id AS TEXT)||':'||locator.external_id
    WHERE mapping.dataset_id=? AND mapping.resource_type='item_discovery'
      AND mapping.target_type='document_locator'"""


@dataclass(frozen=True)
class LegacyBackfillReport:
    status: str
    cutoff_item_id: int
    cutoff_report_id: int
    items_total: int
    items_mapped: int
    discoveries_total: int
    discoveries_mapped: int
    reports_total: int
    reports_mapped: int
    unexplained_items: int
    unexplained_discoveries: int
    unexplained_reports: int
    batch_processed: int

    def to_dict(self) -> dict:
        return asdict(self)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _canonical_legacy_time(value: object, fallback: str) -> str:
    try:
        return format_utc(parse_utc(str(value or "")))
    except (TypeError, ValueError):
        return fallback


def _initialize() -> sqlite3.Row:
    now = utc_now()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT * FROM legacy_backfill_state WHERE singleton=1").fetchone()
        dataset = db.execute(
            "SELECT dataset_id FROM dataset_state WHERE singleton=1"
        ).fetchone()
        if not dataset:
            raise LegacyBackfillError("dataset identity is missing")
        if state:
            if state["dataset_id"] != dataset["dataset_id"]:
                raise LegacyBackfillError("backfill belongs to a different dataset")
            if state["status"] == "failed":
                db.execute(
                    """UPDATE legacy_backfill_state
                       SET status='running',updated_at=?,error_detail=NULL WHERE singleton=1""",
                    (now,),
                )
            return db.execute(
                "SELECT * FROM legacy_backfill_state WHERE singleton=1"
            ).fetchone()

        cutoff_item = db.execute("SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0]
        cutoff_report = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM daily_reports"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO legacy_backfill_state(
                   singleton,dataset_id,cutoff_item_id,cutoff_report_id,status,
                   started_at,updated_at
               ) VALUES(1,?,?,?,'running',?,?)""",
            (dataset["dataset_id"], cutoff_item, cutoff_report, now, now),
        )
        db.execute(
            """INSERT INTO legacy_backfill_sources(
                   source_id,dataset_id,last_item_id,processed_count,status,updated_at
               )
               SELECT DISTINCT source_id,?,0,0,'pending',? FROM items WHERE id<=?""",
            (dataset["dataset_id"], now, cutoff_item),
        )
        return db.execute(
            "SELECT * FROM legacy_backfill_state WHERE singleton=1"
        ).fetchone()


def _source_mapping(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _ensure_run(db: sqlite3.Connection, state: sqlite3.Row, source: sqlite3.Row) -> IngestRun:
    partition = db.execute(
        "SELECT * FROM legacy_backfill_sources WHERE source_id=?", (source["id"],)
    ).fetchone()
    trace_id = f"legacy-backfill-{state['dataset_id']}-{source['id']}"
    run_row = None
    if partition["active_run_id"]:
        run_row = db.execute(
            """SELECT id,source_id,config_version_id,trace_id,status
               FROM ingest_runs WHERE id=?""",
            (partition["active_run_id"],),
        ).fetchone()
    if run_row is None:
        run_row = db.execute(
            """SELECT id,source_id,config_version_id,trace_id,status
               FROM ingest_runs WHERE trace_id=?""",
            (trace_id,),
        ).fetchone()
    if run_row:
        if run_row["source_id"] != source["id"]:
            raise LegacyBackfillError("recovered backfill run belongs to another source")
        if run_row["status"] not in {"running", "succeeded"}:
            raise LegacyBackfillError("incomplete source points to a finished backfill run")
        db.execute(
            """UPDATE legacy_backfill_sources
               SET active_run_id=?,status='running',updated_at=?,error_detail=NULL
               WHERE source_id=?""",
            (run_row["id"], utc_now(), source["id"]),
        )
        return IngestRun(
            run_row["id"], run_row["source_id"],
            run_row["config_version_id"], run_row["trace_id"],
        )

    # begin_ingest_run owns its own connection, so release this read transaction
    # before creating the durable run. The deterministic trace recovers the tiny
    # crash window before the partition records the new run ID.
    db.commit()
    run = begin_ingest_run(_source_mapping(source), trace_id=trace_id)
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        """UPDATE legacy_backfill_sources
           SET active_run_id=?,status='running',updated_at=?,error_detail=NULL
           WHERE source_id=?""",
        (run.id, utc_now(), source["id"]),
    )
    db.commit()
    return run


def _legacy_candidate(item: sqlite3.Row, observed_at: str) -> dict:
    snapshot = {key: item[key] for key in item.keys()}
    try:
        snapshot["extra"] = json.loads(snapshot.get("extra") or "{}")
    except (TypeError, json.JSONDecodeError):
        snapshot["extra"] = {"legacy_unparsed": str(snapshot.get("extra") or "")}
    try:
        companies = json.loads(item["companies"] or "[]")
    except (TypeError, json.JSONDecodeError):
        companies = []
    return {
        "url": item["url"],
        "external_id": f"legacy-item:{item['id']}",
        "title": item["title"],
        "summary": item["raw_summary"] if item["raw_summary"] is not None else item["summary"],
        "published_at": None,
        "event_type": item["event_type"],
        "official": bool(item["official"]),
        "companies": companies,
        "extra": {"legacy_snapshot": snapshot},
        "observed_at": observed_at,
        "payload_kind": "legacy_excerpt",
    }


def _recover_observation(run: IngestRun, item_id: int) -> tuple[RawObservation, dict] | None:
    with get_db() as db:
        row = db.execute(
            """SELECT observation.id AS observation_id,record.*
               FROM raw_observations AS observation
               JOIN raw_records AS record ON record.id=observation.raw_record_id
               WHERE observation.ingest_run_id=? AND observation.ordinal=?""",
            (run.id, item_id),
        ).fetchone()
    if not row:
        return None
    path = verify_payload(row["payload_ref"], row["payload_sha256"])
    payload = json.loads(path.read_text("utf-8"))
    if not isinstance(payload, dict):
        raise LegacyBackfillError("legacy payload is not a JSON object")
    observation = RawObservation(
        row["id"], row["observation_id"], row["payload_sha256"],
        row["payload_ref"], row["size_bytes"], False,
    )
    return observation, payload


def _map_discoveries(
    db: sqlite3.Connection,
    *,
    state: sqlite3.Row,
    item: sqlite3.Row,
    document_id: str,
    canonical_url: str,
    fallback_time: str,
) -> None:
    discoveries = db.execute(
        """SELECT item_id,source_id,first_seen_at,last_seen_at
           FROM item_discoveries WHERE item_id=? ORDER BY source_id""",
        (item["id"],),
    ).fetchall()
    for discovery in discoveries:
        first_seen = _canonical_legacy_time(discovery["first_seen_at"], fallback_time)
        last_seen = _canonical_legacy_time(discovery["last_seen_at"], first_seen)
        if last_seen < first_seen:
            last_seen = first_seen
        locator = db.execute(
            """SELECT document_id,first_observed_at,last_observed_at
               FROM document_locators WHERE source_id=? AND external_id=?""",
            (discovery["source_id"], canonical_url),
        ).fetchone()
        if locator and locator["document_id"] != document_id:
            raise LegacyBackfillError("legacy discovery locator maps to another document")
        if locator:
            if last_seen > locator["last_observed_at"]:
                db.execute(
                    """UPDATE document_locators SET last_observed_at=?
                       WHERE source_id=? AND external_id=?""",
                    (last_seen, discovery["source_id"], canonical_url),
                )
        else:
            db.execute(
                """INSERT INTO document_locators(
                       document_id,source_id,external_id,canonical_url,relation,
                       first_observed_at,last_observed_at
                   ) VALUES(?,?,?,?, 'source_alias',?,?)""",
                (
                    document_id, discovery["source_id"], canonical_url, canonical_url,
                    first_seen, last_seen,
                ),
            )
        value = {
            "item_id": discovery["item_id"],
            "source_id": discovery["source_id"],
            "first_seen_at": discovery["first_seen_at"],
            "last_seen_at": discovery["last_seen_at"],
        }
        db.execute(
            """INSERT OR IGNORE INTO legacy_object_mappings(
                   dataset_id,resource_type,legacy_key,target_type,target_id,
                   legacy_sha256,mapping_status,detail_json,available_at
               ) VALUES(?,'item_discovery',?,'document_locator',?,?,
                        'mapped_unverified',?,?)""",
            (
                state["dataset_id"],
                f"{discovery['item_id']}:{discovery['source_id']}",
                f"{discovery['source_id']}:{canonical_url}",
                _sha(value), _json({"legacy_time_status": "legacy_unverified"}), utc_now(),
            ),
        )


def _project_item(
    state: sqlite3.Row,
    source: sqlite3.Row,
    item: sqlite3.Row,
    run: IngestRun,
    *,
    after_observation: Callable[[int], None] | None = None,
) -> None:
    canonical_url = _normalize_url(item["url"] or "")
    if not canonical_url:
        canonical_url = f"urn:infohub:legacy-item:{item['id']}"
    fallback = state["started_at"]
    with get_db() as db:
        seen = db.execute(
            "SELECT MIN(first_seen_at) FROM item_discoveries WHERE item_id=?",
            (item["id"],),
        ).fetchone()[0]
    observed_at = _canonical_legacy_time(seen or item["fetched_at"], fallback)
    recovered = _recover_observation(run, item["id"])
    if recovered:
        observation, candidate = recovered
    else:
        candidate = _legacy_candidate(item, observed_at)
        observation = observe_candidate(
            run, candidate, ordinal=item["id"], observed_at=observed_at,
            payload_kind="legacy_excerpt", retention_class="legacy-unverified",
        )
    if after_observation:
        after_observation(item["id"])

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing_mapping = db.execute(
            """SELECT target_id,legacy_sha256 FROM legacy_object_mappings
               WHERE dataset_id=? AND resource_type='item' AND legacy_key=?""",
            (state["dataset_id"], str(item["id"])),
        ).fetchone()
        if existing_mapping:
            if existing_mapping["legacy_sha256"] != observation.payload_sha256:
                raise LegacyBackfillError("legacy item changed after its mapping was frozen")
            return
        document = db.execute(
            "SELECT id,current_version_id FROM documents WHERE legacy_item_id=?",
            (item["id"],),
        ).fetchone()
        if document:
            db.execute(
                """INSERT OR IGNORE INTO document_version_inputs(
                       version_id,raw_record_id,role
                   ) VALUES(?,?, 'additional')""",
                (document["current_version_id"], observation.raw_record_id),
            )
            document_id = document["id"]
        else:
            projection = project_candidate(
                db, source=source, legacy_item_id=item["id"], candidate=candidate,
                canonical_url=canonical_url, observation=observation,
            )
            document_id = projection.document_id
        _map_discoveries(
            db, state=state, item=item, document_id=document_id,
            canonical_url=canonical_url, fallback_time=observed_at,
        )
        db.execute(
            """INSERT INTO legacy_object_mappings(
                   dataset_id,resource_type,legacy_key,target_type,target_id,
                   legacy_sha256,mapping_status,detail_json,available_at
               ) VALUES(?,'item',?,'document',?,?,'mapped_unverified',?,?)""",
            (
                state["dataset_id"], str(item["id"]), document_id,
                observation.payload_sha256,
                _json({
                    "content_origin": "legacy_unknown",
                    "time_status": "legacy_unverified",
                    "point_in_time_eligible": False,
                }),
                utc_now(),
            ),
        )


def _complete_source(state: sqlite3.Row, source_id: int, run: IngestRun | None) -> None:
    if run:
        with get_db() as db:
            run_row = db.execute(
                "SELECT status FROM ingest_runs WHERE id=?", (run.id,)
            ).fetchone()
            counts = db.execute(
                """SELECT COUNT(*),COALESCE(SUM(record.size_bytes),0)
                   FROM raw_observations AS observation
                   JOIN raw_records AS record ON record.id=observation.raw_record_id
                   WHERE observation.ingest_run_id=?""",
                (run.id,),
            ).fetchone()
        if run_row and run_row["status"] == "running":
            finish_ingest_run(
                run, status="succeeded", request_count=0, raw_count=counts[0],
                accepted_count=counts[0], duplicate_count=0, rejected_count=0,
                byte_count=counts[1],
            )
    with get_db() as db:
        db.execute(
            """UPDATE legacy_backfill_sources
               SET active_run_id=NULL,status='completed',updated_at=?,error_detail=NULL
               WHERE source_id=? AND dataset_id=?""",
            (utc_now(), source_id, state["dataset_id"]),
        )


def _map_reports(state: sqlite3.Row, batch_size: int) -> int:
    with get_db() as db:
        reports = db.execute(
            """SELECT report.* FROM daily_reports AS report
               LEFT JOIN legacy_object_mappings AS mapping
                 ON mapping.dataset_id=? AND mapping.resource_type='daily_report'
                AND mapping.legacy_key=CAST(report.id AS TEXT)
               WHERE report.id<=? AND mapping.legacy_key IS NULL
               ORDER BY report.id LIMIT ?""",
            (state["dataset_id"], state["cutoff_report_id"], batch_size),
        ).fetchall()
    for report in reports:
        now = utc_now()
        content_hash = hashlib.sha256(report["content"].encode("utf-8")).hexdigest()
        with get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            identity = db.execute(
                """SELECT id,content_sha256 FROM legacy_report_identities
                   WHERE dataset_id=? AND legacy_report_id=?""",
                (state["dataset_id"], report["id"]),
            ).fetchone()
            if identity and identity["content_sha256"] != content_hash:
                raise LegacyBackfillError("legacy report changed after identity was frozen")
            report_id = identity["id"] if identity else f"legacy_report_{uuid4().hex}"
            if not identity:
                db.execute(
                    """INSERT INTO legacy_report_identities(
                           id,dataset_id,legacy_report_id,report_date,content_sha256,
                           legacy_created_at,available_at,status
                       ) VALUES(?,?,?,?,?,?,?,'legacy_unverified')""",
                    (
                        report_id, state["dataset_id"], report["id"], report["date"],
                        content_hash, report["created_at"], now,
                    ),
                )
            db.execute(
                """INSERT OR IGNORE INTO legacy_object_mappings(
                       dataset_id,resource_type,legacy_key,target_type,target_id,
                       legacy_sha256,mapping_status,detail_json,available_at
                   ) VALUES(?,'daily_report',?,'legacy_report',?,?,
                            'pending_domain_upgrade',?,?)""",
                (
                    state["dataset_id"], str(report["id"]), report_id,
                    _sha({key: report[key] for key in report.keys()}),
                    _json({"point_in_time_eligible": False}), now,
                ),
            )
    return len(reports)


def _report(state: sqlite3.Row, batch_processed: int) -> LegacyBackfillReport:
    # The locator key is computed from two columns. Force locator-first order so
    # SQLite probes idx_legacy_mappings_target by that key; mapping-first would
    # scan every locator for every mapped discovery on a large historical DB.
    with get_db() as db:
        values = db.execute(
            f"""SELECT
               (SELECT COUNT(*) FROM items WHERE id<=?),
               (SELECT COUNT(*) FROM legacy_object_mappings AS mapping
                 JOIN documents AS document ON document.id=mapping.target_id
                 WHERE mapping.dataset_id=? AND mapping.resource_type='item'
                   AND mapping.target_type='document'),
               (SELECT COUNT(*) FROM item_discoveries AS discovery
                 JOIN items AS item ON item.id=discovery.item_id WHERE item.id<=?),
               ({DISCOVERY_MAPPING_COUNT_SQL}),
               (SELECT COUNT(*) FROM daily_reports WHERE id<=?),
               (SELECT COUNT(*) FROM legacy_object_mappings AS mapping
                 JOIN legacy_report_identities AS report ON report.id=mapping.target_id
                 WHERE mapping.dataset_id=? AND mapping.resource_type='daily_report'
                   AND mapping.target_type='legacy_report')""",
            (
                state["cutoff_item_id"], state["dataset_id"], state["cutoff_item_id"],
                state["dataset_id"], state["cutoff_report_id"], state["dataset_id"],
            ),
        ).fetchone()
        current = db.execute(
            "SELECT status FROM legacy_backfill_state WHERE singleton=1"
        ).fetchone()[0]
    items_total, items_mapped, discoveries_total, discoveries_mapped, reports_total, reports_mapped = values
    return LegacyBackfillReport(
        current, state["cutoff_item_id"], state["cutoff_report_id"],
        items_total, items_mapped, discoveries_total, discoveries_mapped,
        reports_total, reports_mapped,
        items_total - items_mapped,
        discoveries_total - discoveries_mapped,
        reports_total - reports_mapped,
        batch_processed,
    )


def backfill_legacy_batch(
    batch_size: int = 250,
    *,
    after_observation: Callable[[int], None] | None = None,
) -> LegacyBackfillReport:
    """Process at most one source batch, then one report batch."""
    if batch_size < 1 or batch_size > 5000:
        raise ValueError("batch_size must be between 1 and 5000")
    state = _initialize()
    if state["status"] == "completed":
        return _report(state, 0)
    processed = 0
    try:
        with get_db() as db:
            partition = db.execute(
                """SELECT partition.*,source.*
                   FROM legacy_backfill_sources AS partition
                   JOIN sources AS source ON source.id=partition.source_id
                   WHERE partition.dataset_id=? AND partition.status<>'completed'
                   ORDER BY partition.source_id LIMIT 1""",
                (state["dataset_id"],),
            ).fetchone()
        if partition:
            with get_db() as db:
                run = _ensure_run(db, state, partition)
            with get_db() as db:
                items = db.execute(
                    """SELECT * FROM items
                       WHERE source_id=? AND id>? AND id<=?
                       ORDER BY id LIMIT ?""",
                    (
                        partition["source_id"], partition["last_item_id"],
                        state["cutoff_item_id"], batch_size,
                    ),
                ).fetchall()
            for item in items:
                _project_item(
                    state, partition, item, run, after_observation=after_observation
                )
                processed += 1
                with get_db() as db:
                    db.execute(
                        """UPDATE legacy_backfill_sources
                           SET last_item_id=?,processed_count=processed_count+1,
                               status='running',updated_at=?,error_detail=NULL
                           WHERE source_id=?""",
                        (item["id"], utc_now(), partition["source_id"]),
                    )
            with get_db() as db:
                remains = db.execute(
                    """SELECT 1 FROM items WHERE source_id=? AND id>? AND id<=? LIMIT 1""",
                    (
                        partition["source_id"],
                        items[-1]["id"] if items else partition["last_item_id"],
                        state["cutoff_item_id"],
                    ),
                ).fetchone()
            if not remains:
                _complete_source(state, partition["source_id"], run)
        else:
            processed += _map_reports(state, batch_size)

        with get_db() as db:
            pending_sources = db.execute(
                """SELECT COUNT(*) FROM legacy_backfill_sources
                   WHERE dataset_id=? AND status<>'completed'""",
                (state["dataset_id"],),
            ).fetchone()[0]
            pending_reports = db.execute(
                """SELECT COUNT(*) FROM daily_reports AS report
                   LEFT JOIN legacy_object_mappings AS mapping
                     ON mapping.dataset_id=? AND mapping.resource_type='daily_report'
                    AND mapping.legacy_key=CAST(report.id AS TEXT)
                   WHERE report.id<=? AND mapping.legacy_key IS NULL""",
                (state["dataset_id"], state["cutoff_report_id"]),
            ).fetchone()[0]
            now = utc_now()
            if pending_sources == 0 and pending_reports == 0:
                coverage = _report(state, processed)
                if any((
                    coverage.unexplained_items,
                    coverage.unexplained_discoveries,
                    coverage.unexplained_reports,
                )):
                    raise LegacyBackfillError(
                        "legacy coverage is incomplete despite exhausted cursors"
                    )
                db.execute(
                    """UPDATE legacy_backfill_state
                       SET status='completed',updated_at=?,finished_at=?,error_detail=NULL
                       WHERE singleton=1""",
                    (now, now),
                )
            else:
                db.execute(
                    """UPDATE legacy_backfill_state
                       SET status='running',updated_at=?,finished_at=NULL,error_detail=NULL
                       WHERE singleton=1""",
                    (now,),
                )
        return _report(state, processed)
    except Exception as exc:
        with get_db() as db:
            db.execute(
                """UPDATE legacy_backfill_state
                   SET status='failed',updated_at=?,error_detail=? WHERE singleton=1""",
                (utc_now(), f"{type(exc).__name__}: {str(exc)[:300]}"),
            )
            if "partition" in locals() and partition:
                db.execute(
                    """UPDATE legacy_backfill_sources
                       SET status='failed',updated_at=?,error_detail=? WHERE source_id=?""",
                    (
                        utc_now(), f"{type(exc).__name__}: {str(exc)[:300]}",
                        partition["source_id"],
                    ),
                )
        raise
