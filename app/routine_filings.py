from __future__ import annotations

"""Mark routine official filings stored before the connectors flagged them (legacy import).

The SEC and HKEX connectors set ``extra.routine`` on insider forms (3/4/5/144) and next-day
disclosure returns, which keeps them out of the selected feed; items collected earlier lack it.
"""

from .crawler.hkex_source import _ROUTINE_GROUPS
from .crawler.sec_source import _routine
from .database import get_db

_UNMARKED = "(CASE WHEN json_valid(i.extra) THEN json_extract(i.extra,'$.routine') END) IS NULL"


def mark_routine_filings(*, apply: bool) -> dict:
    with get_db() as db:
        sec = [row["id"] for row in db.execute(
            f"""SELECT i.id, CASE WHEN json_valid(i.extra) THEN json_extract(i.extra,'$.form') END AS form
                FROM items i JOIN sources s ON s.id=i.source_id
                WHERE s.type='sec' AND {_UNMARKED}""") if row["form"] and _routine(str(row["form"]))]
        hkex = [row["id"] for row in db.execute(
            f"""SELECT i.id, i.title FROM items i JOIN sources s ON s.id=i.source_id
                WHERE s.type='hkex' AND {_UNMARKED}""") if any(group in row["title"] for group in _ROUTINE_GROUPS)]
        if apply:
            for item_id in sec + hkex:
                db.execute("""UPDATE items SET extra=json_set(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END,
                                                       '$.routine', json('true'))
                              WHERE id=?""", (item_id,))
    return {"applied": apply, "sec_insider_forms": len(sec), "hkex_next_day_returns": len(hkex)}
