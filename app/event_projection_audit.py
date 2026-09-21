from __future__ import annotations

"""Read-only stage gate for the first legacy-story candidate projection."""

from contextlib import closing
from pathlib import Path

from .db_admin import _connect_readonly, database_state
from .event_candidates import LEGACY_MATCHER_VERSION


def _canonical(stories: dict[str, str | None], story_id: str,
               cache: dict[str, str | None]) -> str | None:
    trail = []
    seen = set()
    current = story_id
    while current not in cache:
        if current in seen or current not in stories:
            result = None
            break
        seen.add(current)
        trail.append(current)
        target = stories[current]
        if target is None:
            result = current
            break
        current = target
    else:
        result = cache[current]
    for member in trail:
        cache[member] = result
    return result


def audit_event_projection(path: str | Path) -> dict:
    """Verify first-stage candidate semantics, not later reviewed event revisions."""
    target = Path(path).expanduser().resolve(strict=True)
    with closing(_connect_readonly(target)) as db:
        db.execute("BEGIN")
        state, version = database_state(db)
        if state != "current":
            return {"status": "unavailable", "schema_state": state,
                    "schema_version": version, "reason": "current_schema_required"}

        stories = {row["id"]: row["redirect_to"] for row in db.execute(
            "SELECT id,redirect_to FROM stories")}
        mappings = {row["story_id"]: row for row in db.execute(
            "SELECT story_id,event_id,canonical_story_id FROM legacy_story_events")}
        cache: dict[str, str | None] = {}
        unresolved = wrong_canonical = wrong_event = 0
        for story_id in stories:
            canonical = _canonical(stories, story_id, cache)
            if canonical is None:
                unresolved += 1
                continue
            mapping = mappings.get(story_id)
            if mapping is None:
                continue
            if mapping["canonical_story_id"] != canonical:
                wrong_canonical += 1
            canonical_mapping = mappings.get(canonical)
            if canonical_mapping and mapping["event_id"] != canonical_mapping["event_id"]:
                wrong_event += 1

        event_rows = db.execute("""SELECT DISTINCT e.id,e.status,e.last_fact_change_at,
                   e.current_version_id,v.knowledge_status,v.facts_json,
                   v.time_precision,v.event_time_start,v.event_time_end,v.created_by
            FROM legacy_story_events m JOIN events e ON e.id=m.event_id
            LEFT JOIN event_versions v ON v.id=e.current_version_id""").fetchall()
        invalid_events = sum(
            row["status"] != "candidate" or row["last_fact_change_at"] is not None
            or row["current_version_id"] is None or row["knowledge_status"] != "unknown"
            or row["facts_json"] != "[]" or row["time_precision"] != "unknown"
            or row["event_time_start"] is not None or row["event_time_end"] is not None
            or row["created_by"] != "legacy_story_projection"
            for row in event_rows
        )
        legacy_decisions = db.execute("""SELECT COUNT(*) AS total,
                   COALESCE(SUM(decision!='candidate_link' OR review_status!='pending'),0) AS invalid
            FROM match_decisions WHERE matcher_version=?""",
            (LEGACY_MATCHER_VERSION,),
        ).fetchone()
        imported_links = db.execute("""SELECT COUNT(*) AS total,
                   COALESCE(SUM(link.role!='candidate'),0) AS invalid
            FROM document_event_links link JOIN match_decisions decision
              ON decision.id=link.decision_id
            WHERE decision.matcher_version=?""", (LEGACY_MATCHER_VERSION,)).fetchone()
        imported_evidence = db.execute("""SELECT COUNT(*) AS total,
                   COALESCE(SUM(evidence.role!='context' OR evidence.fact_id IS NOT NULL),0) AS invalid
            FROM event_evidence evidence JOIN event_versions version
              ON version.id=evidence.event_version_id
            WHERE version.created_by='legacy_story_projection'""").fetchone()
        canonical_count = sum(redirect is None for redirect in stories.values())
        mapped_stories = sum(story_id in mappings for story_id in stories)
        orphan_mappings = sum(story_id not in stories for story_id in mappings)
        mapped_canonical = sum(story_id in mappings for story_id, redirect in stories.items()
                               if redirect is None)
        reasons = []
        if not stories:
            reasons.append("no_legacy_stories")
        if mapped_stories != len(stories):
            reasons.append("story_mapping_incomplete")
        if mapped_canonical != canonical_count or len(event_rows) != canonical_count:
            reasons.append("canonical_event_coverage_incomplete")
        if legacy_decisions["total"] != imported_links["total"] or imported_links["total"] != imported_evidence["total"]:
            reasons.append("legacy_evidence_link_count_mismatch")
        violations = {
            "unresolved_redirects": unresolved,
            "orphan_mappings": orphan_mappings,
            "wrong_canonical_story": wrong_canonical,
            "redirect_event_mismatch": wrong_event,
            "invalid_candidate_events": invalid_events,
            "invalid_legacy_decisions": legacy_decisions["invalid"],
            "invalid_imported_links": imported_links["invalid"],
            "invalid_imported_evidence": imported_evidence["invalid"],
        }
        if any(violations.values()):
            status = "invalid"
        elif reasons:
            status = "incomplete"
        else:
            status = "ok"
        return {
            "status": status, "schema_version": version,
            "stories": len(stories), "canonical_stories": canonical_count,
            "mapped_stories": mapped_stories, "mapped_canonical_stories": mapped_canonical,
            "mapped_events": len(event_rows),
            "legacy_decisions": legacy_decisions["total"],
            "imported_candidate_links": imported_links["total"],
            "imported_context_evidence": imported_evidence["total"],
            "violations": violations, "blocked_reasons": reasons,
            "scope": "Initial shadow candidate projection only; later human-reviewed revisions require a different audit.",
        }
