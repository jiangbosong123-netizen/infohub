"""Read coherent current-publication hotspot metrics from a complete projection."""

import sqlite3
from datetime import datetime, timezone

from .curation_query import portal_curation_sql


def hot_metrics_usable(db: sqlite3.Connection) -> bool:
    state = db.execute(
        "SELECT status,indexed_count FROM curation_story_metrics_state WHERE singleton=1"
    ).fetchone()
    if not state or state["status"] != "ready":
        return False
    if db.execute("SELECT 1 FROM curation_story_metrics_dirty LIMIT 1").fetchone():
        return False
    story_count = db.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
    return (state["indexed_count"] == story_count
            and story_count == db.execute("SELECT COUNT(*) FROM curation_story_metrics").fetchone()[0])


def curated_top_clusters(
    db: sqlite3.Connection, *, limit: int, channel: str, topic: str,
    cutoff: str, now: datetime | None = None,
) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cte, join, visible, _, _ = portal_curation_sql(True)
    filters = []
    params: list = [cutoff]
    if channel != "all":
        filters.append(f"""st.id IN (SELECT si.story_id FROM story_items si
                          JOIN items i ON i.id=si.item_id {join}
                          WHERE i.channel=? AND {visible})""")
        params.append(channel)
    if topic:
        filters.append(f"""st.id IN (SELECT si.story_id FROM story_items si JOIN item_topics it ON it.item_id=si.item_id
                         JOIN items i ON i.id=si.item_id {join}
                         WHERE it.topic_slug=? AND {visible})""")
        params.append(topic)
    where = " AND " + " AND ".join(filters) if filters else ""
    rows = db.execute(
        (cte if filters else "") + f"""SELECT st.id,st.channel,
               m.title_display AS title,m.url_display AS url,
               m.visible_item_count AS item_count,m.publisher_count AS source_count,
               m.heat,m.computed_at,m.company_slugs,m.last_visible_at AS updated_at
           FROM curation_story_metrics m JOIN stories st ON st.id=m.story_id
           WHERE st.redirect_to IS NULL AND m.visible_item_count>0
             AND m.last_visible_at>=? {where}""",
        params,
    ).fetchall()
    ranked = []
    for row in rows:
        value = dict(row)
        computed = datetime.fromisoformat(value["computed_at"].replace("Z", "+00:00"))
        elapsed_hours = max(0.0, (current - computed).total_seconds() / 3600)
        value["heat"] *= 0.5 ** (elapsed_hours / 18.0)
        ranked.append(value)
    ranked.sort(key=lambda row: (-(row["source_count"] >= 2), -row["heat"], row["id"]))
    return ranked[:limit]
