from __future__ import annotations

"""Read-only admission check for the portal search and hot projections."""

from contextlib import closing
from pathlib import Path

from .curation_hot_metrics import METRIC_VERSION
from .curation_hot_query import hot_metrics_usable
from .curation_search import INDEX_SCHEMA_VERSION
from .curation_search_query import search_index_usable
from .db_admin import _connect_readonly, database_state


def audit_curation_projections(path: str | Path) -> dict:
    target = Path(path).expanduser().resolve(strict=True)
    with closing(_connect_readonly(target)) as db:
        db.execute("BEGIN")
        state, version = database_state(db)
        if state != "current":
            return {"status": "unavailable", "schema_state": state,
                    "schema_version": version, "reason": "current_schema_required"}

        def count(sql: str, params: tuple = ()) -> int:
            return db.execute(sql, params).fetchone()[0]

        search_state = db.execute(
            "SELECT status,generation,indexed_count FROM curation_search_state WHERE singleton=1"
        ).fetchone()
        hot_state = db.execute(
            "SELECT status,generation,indexed_count FROM curation_story_metrics_state WHERE singleton=1"
        ).fetchone()
        search = {
            "state": dict(search_state) if search_state else None,
            "items": count("SELECT COUNT(*) FROM items"),
            "documents": count("SELECT COUNT(*) FROM curation_search_documents"),
            "dirty": count("SELECT COUNT(*) FROM curation_search_dirty"),
            "wrong_schema_version": count(
                "SELECT COUNT(*) FROM curation_search_documents WHERE index_schema_version!=?",
                (INDEX_SCHEMA_VERSION,),
            ),
            "stale_publication_pointers": count("""
                SELECT COUNT(*) FROM curation_search_documents sd
                JOIN items i ON i.id=sd.item_id
                LEFT JOIN documents d ON d.legacy_item_id=i.id AND d.status='active'
                LEFT JOIN analysis_publications t ON t.subject_type='document'
                    AND t.subject_version_id=d.current_version_id AND t.task_type='translation'
                LEFT JOIN analysis_publications s ON s.subject_type='document'
                    AND s.subject_version_id=d.current_version_id AND s.task_type='summarization'
                WHERE sd.document_version_id IS NOT d.current_version_id
                   OR sd.translation_publication_id IS NOT t.current_publication_id
                   OR sd.summary_publication_id IS NOT s.current_publication_id
            """),
            "usable": search_index_usable(db),
        }
        hot = {
            "state": dict(hot_state) if hot_state else None,
            "stories": count("SELECT COUNT(*) FROM stories"),
            "metrics": count("SELECT COUNT(*) FROM curation_story_metrics"),
            "dirty": count("SELECT COUNT(*) FROM curation_story_metrics_dirty"),
            "wrong_schema_version": count(
                "SELECT COUNT(*) FROM curation_story_metrics WHERE metric_schema_version!=?",
                (METRIC_VERSION,),
            ),
            "invalid_counts": count("""SELECT COUNT(*) FROM curation_story_metrics
                WHERE publisher_count>visible_item_count
                   OR (visible_item_count=0 AND representative_item_id IS NOT NULL)
                   OR (visible_item_count>0 AND representative_item_id IS NULL)"""),
            "usable": hot_metrics_usable(db),
        }
        reasons = []
        if not search["usable"] or search["wrong_schema_version"] or search["stale_publication_pointers"]:
            reasons.append("search_projection_not_current")
        if not hot["usable"] or hot["wrong_schema_version"] or hot["invalid_counts"]:
            reasons.append("hot_projection_not_current")
        return {"status": "ok" if not reasons else "failed",
                "schema_version": version, "search": search, "hot": hot, "reasons": reasons}
