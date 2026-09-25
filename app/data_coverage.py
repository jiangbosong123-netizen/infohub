from __future__ import annotations

"""Read-only inventory of legacy portal rows and their new-model projections.

Coverage means a linked row exists; it does not certify evidence quality, model
accuracy, or readiness to switch a portal route to a new read model.
"""

from contextlib import closing
from pathlib import Path

from .db_admin import _connect_readonly, database_state


def _count(db, query: str) -> int:
    return int(db.execute(query).fetchone()[0])


def _coverage(covered: int, total: int) -> dict:
    return {"covered": covered, "total": total,
            "percent": round(100 * covered / total, 2) if total else None}


def audit_data_coverage(path: str | Path) -> dict:
    """Inspect one SQLite read snapshot without creating, migrating or writing it."""
    target = Path(path).expanduser().resolve(strict=True)
    with closing(_connect_readonly(target)) as db:
        db.execute("BEGIN")
        state, schema_version = database_state(db)
        if state == "empty":
            return {"schema_state": state, "schema_version": schema_version,
                    "status": "no_data", "blocked_reasons": ["database_empty"]}

        items = _count(db, "SELECT COUNT(*) FROM items")
        by_channel = {row[0]: row[1] for row in db.execute(
            "SELECT channel,COUNT(*) FROM items GROUP BY channel ORDER BY channel")}
        stories = _count(db, "SELECT COUNT(*) FROM stories WHERE redirect_to IS NULL") \
            if _has_table(db, "stories") else 0
        legacy_reports = _count(db, "SELECT COUNT(*) FROM daily_reports")
        report = {
            "schema_state": state, "schema_version": schema_version,
            "legacy": {"items": items, "items_by_channel": by_channel,
                       "canonical_stories": stories, "daily_reports": legacy_reports},
        }
        if state != "current":
            report.update(status="blocked", blocked_reasons=["current_schema_missing"],
                          coverage_unavailable="new-model tables require the current schema")
            return report

        mapped = _count(db, """SELECT COUNT(*) FROM items i JOIN documents d
            ON d.legacy_item_id=i.id WHERE d.current_version_id IS NOT NULL""")
        discoveries = _count(db, "SELECT COUNT(*) FROM item_discoveries")
        mapped_discoveries = _count(db, """SELECT COUNT(*) FROM item_discoveries x
            JOIN legacy_object_mappings m ON m.resource_type='item_discovery'
             AND m.legacy_key=CAST(x.item_id AS TEXT)||':'||CAST(x.source_id AS TEXT)
             AND m.target_type='document_locator'""")
        mapped_reports = _count(db, """SELECT COUNT(*) FROM daily_reports r
            JOIN legacy_object_mappings m ON m.resource_type='daily_report'
             AND m.legacy_key=CAST(r.id AS TEXT) AND m.target_type='legacy_report'
            JOIN legacy_report_identities l ON l.id=m.target_id
             AND l.legacy_report_id=r.id""")
        active = _count(db, """SELECT COUNT(*) FROM items i JOIN documents d
            ON d.legacy_item_id=i.id WHERE d.status='active'
            AND d.current_version_id IS NOT NULL""")
        with_raw = _count(db, """SELECT COUNT(DISTINCT d.legacy_item_id)
            FROM documents d JOIN document_version_inputs vi
              ON vi.version_id=d.current_version_id
            JOIN raw_records r ON r.id=vi.raw_record_id""")
        legacy_excerpt = _count(db, """SELECT COUNT(DISTINCT d.legacy_item_id)
            FROM documents d JOIN document_version_inputs vi
              ON vi.version_id=d.current_version_id
            JOIN raw_records r ON r.id=vi.raw_record_id
            WHERE r.payload_kind='legacy_excerpt'""")
        eligible = _count(db, """SELECT COUNT(*) FROM documents d
            JOIN document_versions v ON v.id=d.current_version_id
            WHERE v.point_in_time_eligible=1""")
        event_mapped = _count(db, """SELECT COUNT(*) FROM stories s
            JOIN legacy_story_events l ON l.story_id=s.id
            JOIN events e ON e.id=l.event_id
            WHERE s.redirect_to IS NULL AND e.current_version_id IS NOT NULL""")
        published = _count(db, "SELECT COUNT(*) FROM report_publications")
        analysis_docs = _count(db, """SELECT COUNT(DISTINCT d.legacy_item_id)
            FROM documents d JOIN analysis_publications p
              ON p.subject_type='document'
             AND p.subject_version_id=d.current_version_id
            WHERE d.status='active'""")
        analysis_tasks = {row[0]: row[1] for row in db.execute("""SELECT task_type,COUNT(*)
            FROM analysis_publications GROUP BY task_type ORDER BY task_type""")}
        search = db.execute("SELECT status,indexed_count FROM curation_search_state WHERE singleton=1").fetchone()
        search_current = _count(db, """SELECT COUNT(*) FROM curation_search_documents c
            JOIN documents d ON d.legacy_item_id=c.item_id AND d.status='active'
            WHERE c.document_version_id=d.current_version_id""")
        search_dirty = _count(db, "SELECT COUNT(*) FROM curation_search_dirty")
        story_state = db.execute("SELECT status,indexed_count FROM curation_story_metrics_state WHERE singleton=1").fetchone()
        story_dirty = _count(db, "SELECT COUNT(*) FROM curation_story_metrics_dirty")
        backfill = db.execute("SELECT status,cutoff_item_id FROM legacy_backfill_state WHERE singleton=1").fetchone()

        report["new_model"] = {
            "backfill": {"status": backfill[0] if backfill else "not_started",
                         "cutoff_item_id": backfill[1] if backfill else None},
            "legacy_discoveries_mapped": _coverage(mapped_discoveries, discoveries),
            "legacy_report_identities_mapped": _coverage(mapped_reports, legacy_reports),
            "documents_with_current_version": _coverage(mapped, items),
            "active_documents_with_current_version": _coverage(active, items),
            "documents_with_raw_input": _coverage(with_raw, items),
            "documents_with_legacy_excerpt_input": legacy_excerpt,
            "point_in_time_eligible_documents": _coverage(eligible, items),
            "canonical_stories_with_candidate_event": _coverage(event_mapped, stories),
            "document_analysis_publications": _coverage(analysis_docs, active),
            "analysis_publications_by_task": analysis_tasks,
            "versioned_report_publications": published,
            "search": {"state": search[0] if search else "missing",
                       "indexed_count": search[1] if search else 0,
                       "current_active_document_entries": _coverage(search_current, active),
                       "dirty_count": search_dirty},
            "story_metrics": {"state": story_state[0] if story_state else "missing",
                              "indexed_count": story_state[1] if story_state else 0,
                              "dirty_count": story_dirty},
        }
        reasons = []
        if not items:
            reasons.append("no_items_to_assess")
        if items and (not backfill or backfill[0] != "completed" or mapped < items
                      or mapped_discoveries < discoveries or mapped_reports < legacy_reports):
            reasons.append("legacy_backfill_incomplete")
        if stories and event_mapped < stories:
            reasons.append("legacy_story_event_mapping_incomplete")
        if active and analysis_docs < active:
            reasons.append("document_analysis_not_fully_published")
        if active and (not search or search[0] != "ready" or search_current < active or search_dirty):
            reasons.append("curation_search_not_current")
        if stories and (not story_state or story_state[0] != "ready" or story_dirty):
            reasons.append("curation_story_metrics_not_current")
        if legacy_reports and not published:
            reasons.append("no_versioned_report_publications")
        report["status"] = "gaps_found" if reasons else "coverage_observed"
        report["blocked_reasons"] = reasons
        report["interpretation"] = (
            "Counts show row/link coverage only; they do not verify CAS bytes, "
            "publication review, NLP accuracy or portal cutover readiness."
        )
        return report


def _has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
