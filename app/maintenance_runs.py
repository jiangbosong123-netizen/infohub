from __future__ import annotations

"""One-command, resumable post-upgrade maintenance runs (used by the cutover plan).

Each run only calls the existing idempotent building blocks, so it can be interrupted and
started again; every call reports what is still left instead of assuming completion.
"""

from collections import Counter
from collections.abc import Callable

from .curation_hot_metrics import advance_hot_metrics
from .curation_search import advance_search_index
from .database import get_db, reused_connections
from .legacy_backfill import LegacyBackfillReport, backfill_legacy_batch
from .legacy_curation_import import (
    JOB_KIND,
    enqueue_legacy_curation_batch,
    process_one_legacy_curation_import,
)
from .legacy_topic_backfill import LegacyTopicBackfillReport, backfill_legacy_topics_batch
from .topic_statistics import advance_topic_statistics

Progress = Callable[[dict], None]
ENQUEUE_PAGE = 500
PROGRESS_EVERY = 5000
# The builders' own page limits.
BUILDERS = (
    ("search", advance_search_index, 500),
    ("hot", advance_hot_metrics, 100),
    ("topic_statistics", advance_topic_statistics, 250),
)


def run_legacy_backfill(batch_size: int) -> LegacyBackfillReport:
    """Backfill every legacy item, discovery and report (about five connections per item)."""
    with reused_connections():
        report = backfill_legacy_batch(batch_size)
        while report.status != "completed":
            report = backfill_legacy_batch(batch_size)
    return report


def run_legacy_topic_backfill(batch_size: int) -> LegacyTopicBackfillReport:
    """Backfill every frozen legacy topic assignment."""
    with reused_connections():
        report = backfill_legacy_topics_batch(batch_size)
        while report.status != "completed":
            report = backfill_legacy_topics_batch(batch_size)
    return report


def run_legacy_curation_import(
    *, worker_id: str = "maintenance-legacy-import", progress: Progress | None = None
) -> dict:
    with reused_connections():
        return _run_legacy_curation_import(worker_id=worker_id, progress=progress)


def _run_legacy_curation_import(*, worker_id: str, progress: Progress | None) -> dict:
    after = items = ensured = 0
    while True:
        page = enqueue_legacy_curation_batch(after_item_id=after, limit=ENQUEUE_PAGE)
        if not page["items_seen"]:
            break
        items += page["items_seen"]
        ensured += page["jobs_ensured"]
        after = page["next_after_item_id"]
    if progress:
        progress({"phase": "enqueue", "items_seen": items, "jobs_ensured": ensured})
    processed, outcomes = 0, Counter()
    while (result := process_one_legacy_curation_import(worker_id=worker_id)) is not None:
        processed += 1
        outcomes[result.status] += 1
        if progress and processed % PROGRESS_EVERY == 0:
            progress({"phase": "process", "processed": processed})
    with get_db() as db:
        remaining = dict(db.execute(
            "SELECT state,COUNT(*) FROM jobs WHERE kind=? AND state!='succeeded' GROUP BY state",
            (JOB_KIND,),
        ).fetchall())
    return {
        "items_seen": items,
        "jobs_ensured": ensured,
        "processed": processed,
        "outcomes": dict(sorted(outcomes.items())),
        # retry_wait jobs become claimable later; run again to finish them.
        "remaining": remaining,
        "complete": not remaining,
    }


def run_projection_builders(*, progress: Progress | None = None, max_calls: int = 100_000) -> dict:
    with reused_connections():
        return _run_projection_builders(progress=progress, max_calls=max_calls)


def _run_projection_builders(*, progress: Progress | None, max_calls: int) -> dict:
    results = {}
    for name, advance, limit in BUILDERS:
        calls, last = 0, {}
        while calls < max_calls:
            last = advance(limit).to_dict()
            calls += 1
            if last.get("status") != "building" and not last.get("dirty_remaining"):
                break
        results[name] = {"calls": calls, "status": last.get("status"),
                         "dirty_remaining": last.get("dirty_remaining", 0)}
        if progress:
            progress({"builder": name, **results[name]})
    return {"builders": results,
            "complete": all(r["status"] != "building" and not r["dirty_remaining"]
                            for r in results.values())}
