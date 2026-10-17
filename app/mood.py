from __future__ import annotations

"""The portal's mood face: is the last day's news mostly good or bad?

Each scored item carries the model's rough call (item_moods, -2..2). The day's mood is their
average weighted by importance (the AI score), so one big piece of news counts for more than
ten routine ones. It is a playful summary for people, not a tone result (D09): unreviewed,
never published through the API, never used for decisions.
"""

from datetime import datetime, timedelta, timezone

WINDOW = timedelta(hours=24)
MIN_ITEMS = 20  # fewer judged items than this: the face is still asleep
# (lower bound of the weighted average, level), from the happiest down.
LEVELS = ((0.5, "great"), (0.15, "good"), (-0.15, "calm"), (-0.5, "worried"), (float("-inf"), "sad"))
LABELS = {"great": "很乐观", "good": "偏乐观", "calm": "平稳", "worried": "偏悲观", "sad": "很悲观",
          "sleepy": "还在攒数据"}


def summary(db, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    since = (current - WINDOW).isoformat()
    rows = db.execute(
        """SELECT i.id, COALESCE(NULLIF(i.title_zh,''), i.title) AS title, i.url, i.score, m.mood
           FROM items i JOIN item_moods m ON m.item_id=i.id
           WHERE i.fetched_at>=? AND i.score>=0 AND COALESCE(i.tmt,1)!=0""",
        (since,)).fetchall()
    counts = {"good": sum(row["mood"] > 0 for row in rows), "calm": sum(row["mood"] == 0 for row in rows),
              "bad": sum(row["mood"] < 0 for row in rows)}
    weight = sum(max(row["score"], 1) for row in rows)
    value = sum(row["mood"] * max(row["score"], 1) for row in rows) / weight if weight else 0.0
    if len(rows) < MIN_ITEMS:
        level = "sleepy"
    else:
        level = next(name for bound, name in LEVELS if value >= bound)

    def top(sign: int):
        picked = [row for row in rows if row["mood"] * sign > 0]
        if not picked:
            return None
        best = max(picked, key=lambda row: (abs(row["mood"]), row["score"], row["id"]))
        return {"id": best["id"], "title": best["title"], "url": best["url"], "score": best["score"],
                "mood": best["mood"]}

    return {"level": level, "label": LABELS[level], "value": round(value, 2), "judged": len(rows),
            "counts": counts, "best": top(1), "worst": top(-1)}
