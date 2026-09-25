"""Read the versioned curation search projection only when fully current."""

import sqlite3

from .curation_query import CURATION_FILTER_CTE


def search_index_usable(db: sqlite3.Connection) -> bool:
    state = db.execute(
        "SELECT status,indexed_count FROM curation_search_state WHERE singleton=1"
    ).fetchone()
    if not state or state["status"] != "ready":
        return False
    if db.execute("SELECT 1 FROM curation_search_dirty LIMIT 1").fetchone():
        return False
    # Item deletion cascades into the index but does not enqueue a dirty row.
    # A maintenance refresh will reconcile the recorded count before cutover.
    item_count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if state["indexed_count"] != item_count:
        return False
    return item_count == db.execute("SELECT COUNT(*) FROM curation_search_documents").fetchone()[0]


def search_curated(
    db: sqlite3.Connection, query: str, *, limit: int, offset: int
) -> list[sqlite3.Row]:
    """Return current-visible items matching literal title/summary text."""
    if not query or not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise ValueError("invalid search bounds")
    base = """ SELECT i.*,s.name AS source_name FROM curation_search_documents sd
        JOIN items i ON i.id=sd.item_id JOIN sources s ON s.id=i.source_id
        JOIN curation_values cv ON cv.item_id=i.id"""
    if len(query) >= 3:
        phrase = '"' + query.replace('"', '""') + '"'
        try:
            rows = db.execute(
                CURATION_FILTER_CTE + base + " JOIN curation_search_fts ON curation_search_fts.rowid=sd.item_id"
                " WHERE curation_search_fts MATCH ? AND cv.visible=1"
                " ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?",
                (phrase, limit, offset),
            ).fetchall()
            if rows:
                return rows
            if offset and db.execute(
                CURATION_FILTER_CTE + base
                + " JOIN curation_search_fts ON curation_search_fts.rowid=sd.item_id"
                  " WHERE curation_search_fts MATCH ? AND cv.visible=1 LIMIT 1",
                (phrase,),
            ).fetchone():
                return []
        except sqlite3.OperationalError:
            # FTS query parser can reject punctuation; literal LIKE is safe.
            pass
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = "%" + escaped + "%"
    return db.execute(
        CURATION_FILTER_CTE + base + " WHERE cv.visible=1 AND ("
        "sd.title_original LIKE ? ESCAPE '\\' OR sd.title_display LIKE ? ESCAPE '\\'"
        " OR sd.summary_display LIKE ? ESCAPE '\\')"
        " ORDER BY i.published_at DESC,i.id DESC LIMIT ? OFFSET ?",
        (like, like, like, limit, offset),
    ).fetchall()
