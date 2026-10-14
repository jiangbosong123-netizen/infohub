from __future__ import annotations

"""Set stored SEC filings to the acceptance time their EDGAR header states (legacy import).

The old collector timed each filing by the submissions API's acceptanceDateTime, which is not a
reliable instant: of 60 rehearsal filings, the 11 caught within minutes of acceptance were stored
4 h early. Each filing's header is read once (about 3 KB, a few per second within SEC's limit);
items whose header was already read are skipped, so the command can be stopped and run again.
"""

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone

from .crawler.sec_source import HeaderFormatError, read_acceptance
from .database import get_db
from .source_time import parse_source_time

_FILING_URL = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/(\d+)/(\d{10})(\d{2})(\d{6})/")


def _accepted_utc(header: dict) -> datetime | None:
    value = parse_source_time(
        header["acceptance_datetime"], field_path="header.acceptance_datetime", role="accepted",
        interpretation="SEC EDGAR filing header ACCEPTANCE-DATETIME",
        timezone_name="America/New_York", pattern="%Y%m%d%H%M%S",
    )
    if value.status != "valid" or not value.utc:
        return None
    return datetime.fromisoformat(value.utc.replace("Z", "+00:00"))


def correct_acceptance_times(*, apply: bool, pause: float = 0.15) -> dict:
    with get_db() as db:
        rows = db.execute(
            """SELECT i.id, i.url, i.published_at FROM items i JOIN sources s ON s.id=i.source_id
               WHERE s.type='sec'
                 AND (CASE WHEN json_valid(i.extra) THEN json_extract(i.extra,'$.sec_acceptance') END) IS NULL
               ORDER BY i.id"""
        ).fetchall()
    shifts: Counter[str] = Counter()
    report = {"applied": apply, "checked": 0, "matches": 0, "differs": 0, "no_header": 0,
              "remaining": len(rows), "stopped": None}
    for row in rows:
        match = _FILING_URL.match(row["url"])
        header = None
        if match:
            cik, accession = match.group(1), "-".join(match.group(2, 3, 4))
            try:
                header = read_acceptance(cik, accession)
            except HeaderFormatError:
                header = None  # counted under no_header
            except Exception as exc:  # noqa: BLE001 - stop; a later run resumes here
                report["stopped"] = f"item {row['id']}: {exc}"[:300]
                break
            time.sleep(pause)
        report["checked"] += 1
        accepted = _accepted_utc(header) if header else None
        if accepted is None:
            report["no_header"] += 1
            continue
        stored = datetime.fromisoformat(str(row["published_at"]).replace("Z", "+00:00"))
        hours = round((stored - accepted).total_seconds() / 3600, 2)
        if hours:
            shifts[f"{hours:+g} h"] += 1
            report["differs"] += 1
        else:
            report["matches"] += 1
        if apply:
            with get_db() as db:
                db.execute(
                    """UPDATE items SET published_at=?,
                              extra=json_set(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END,
                                             '$.sec_acceptance', json(?), '$._legacy_time_basis', 'source_accepted')
                       WHERE id=?""",
                    (accepted.astimezone(timezone.utc).isoformat(), json.dumps(header), row["id"]),
                )
    report["remaining"] -= report["checked"]
    report["stored_minus_header"] = dict(shifts.most_common())
    return report
