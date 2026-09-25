from __future__ import annotations

"""Bounded, resumable projection of the portal's searchable item text.

The index is derived data. Every batch holds the SQLite writer lock so its
publication pointers, projected text, dirty acknowledgements and cursor commit
together. Web requests never build it.
"""

import sqlite3
from dataclasses import asdict, dataclass

from .curation_projection import display_curation, published_curation
from .database import get_db
from .timeutil import utc_now

INDEX_SCHEMA_VERSION = "curation-search-v1"


@dataclass(frozen=True)
class SearchBuildReport:
    status: str
    generation: int
    last_item_id: int
    indexed_count: int
    scanned: int
    refreshed: int
    dirty_remaining: int

    def to_dict(self) -> dict:
        return asdict(self)


def _refresh_items(db: sqlite3.Connection, item_ids: list[int]) -> None:
    if not item_ids:
        return
    marks = ",".join("?" for _ in item_ids)
    items = db.execute(f"SELECT * FROM items WHERE id IN ({marks})", item_ids).fetchall()
    projection = published_curation(db, item_ids)
    pointers = {
        row["item_id"]: row
        for row in db.execute(
            f"""SELECT i.id AS item_id, d.current_version_id,
                       t.current_publication_id AS translation_id,
                       s.current_publication_id AS summary_id
                FROM items i
                LEFT JOIN documents d ON d.legacy_item_id=i.id AND d.status='active'
                LEFT JOIN analysis_publications t ON t.subject_type='document'
                    AND t.subject_version_id=d.current_version_id AND t.task_type='translation'
                LEFT JOIN analysis_publications s ON s.subject_type='document'
                    AND s.subject_version_id=d.current_version_id AND s.task_type='summarization'
                WHERE i.id IN ({marks})""",
            item_ids,
        )
    }
    now = utc_now()
    for item in items:
        item_id = item["id"]
        view = display_curation(dict(item), projection.get(item_id))
        pointer = pointers[item_id]
        title_display = (view.get("title_zh") or "").strip() or item["title"]
        summary_display = view.get("summary") or ""
        db.execute(
            """INSERT INTO curation_search_documents(
                   item_id,document_version_id,translation_publication_id,
                   summary_publication_id,index_schema_version,title_original,
                   title_display,summary_display,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                   document_version_id=excluded.document_version_id,
                   translation_publication_id=excluded.translation_publication_id,
                   summary_publication_id=excluded.summary_publication_id,
                   title_original=excluded.title_original,
                   title_display=excluded.title_display,
                   summary_display=excluded.summary_display,
                   indexed_at=excluded.indexed_at
               WHERE curation_search_documents.document_version_id IS NOT excluded.document_version_id
                  OR curation_search_documents.translation_publication_id IS NOT excluded.translation_publication_id
                  OR curation_search_documents.summary_publication_id IS NOT excluded.summary_publication_id
                  OR curation_search_documents.title_original IS NOT excluded.title_original
                  OR curation_search_documents.title_display IS NOT excluded.title_display
                  OR curation_search_documents.summary_display IS NOT excluded.summary_display""",
            (item_id, pointer["current_version_id"], pointer["translation_id"],
             pointer["summary_id"], INDEX_SCHEMA_VERSION, item["title"],
             title_display, summary_display, now),
        )
    # A missing/deleted item cannot leave searchable text behind.
    missing = set(item_ids) - {item["id"] for item in items}
    if missing:
        db.executemany("DELETE FROM curation_search_documents WHERE item_id=?", [(i,) for i in missing])
    db.executemany("DELETE FROM curation_search_dirty WHERE item_id=?", [(i,) for i in item_ids])


def advance_search_index(limit: int = 200) -> SearchBuildReport:
    """Commit one bounded scan or dirty-queue batch; repeat until ready/clean."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute("SELECT * FROM curation_search_state WHERE singleton=1").fetchone()
        if state is None:
            raise RuntimeError("search schema has not been migrated")
        status, generation, cursor = state["status"], state["generation"], state["last_item_id"]
        if status == "empty":
            if cursor != 0 or db.execute("SELECT 1 FROM curation_search_documents LIMIT 1").fetchone():
                raise RuntimeError("empty search state contains indexed rows or a cursor")
            generation += 1
            status = "building"
        scanned = refreshed = 0
        if status == "building":
            ids = [row[0] for row in db.execute(
                "SELECT id FROM items WHERE id>? ORDER BY id LIMIT ?", (cursor, limit)
            )]
            if ids:
                _refresh_items(db, ids)
                scanned = len(ids)
                cursor = ids[-1]
        if not scanned:
            dirty = [row[0] for row in db.execute(
                "SELECT item_id FROM curation_search_dirty ORDER BY item_id LIMIT ?", (limit,)
            )]
            if dirty:
                _refresh_items(db, dirty)
                refreshed = len(dirty)
        dirty_remaining = db.execute("SELECT COUNT(*) FROM curation_search_dirty").fetchone()[0]
        if status == "building" and not scanned and not dirty_remaining:
            # Writers are excluded until commit, so ready covers all current items.
            if db.execute("SELECT 1 FROM items WHERE id>? LIMIT 1", (cursor,)).fetchone() is None:
                status = "ready"
        count = db.execute("SELECT COUNT(*) FROM curation_search_documents").fetchone()[0]
        db.execute(
            """UPDATE curation_search_state SET generation=?,status=?,last_item_id=?,
                      indexed_count=?,updated_at=? WHERE singleton=1""",
            (generation, status, cursor, count, utc_now()),
        )
        return SearchBuildReport(status, generation, cursor, count, scanned, refreshed, dirty_remaining)
