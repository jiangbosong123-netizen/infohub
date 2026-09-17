from __future__ import annotations

"""Conservative, auditable matching for structured event proposals.

The score produced here is a retrieval/ranking aid, not a probability that an
event is true.  Every accepted link remains a pending candidate until the gold
dataset and review gates from later phases exist.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .timeutil import utc_now


MATCHER_VERSION = "structured-event-candidate-v1"
MATCH_THRESHOLD = 0.78
MAX_REPORT_GAP_DAYS = 14


class EventMatchingError(RuntimeError):
    """A proposed match cannot be evaluated without losing provenance."""


@dataclass(frozen=True)
class EventProposal:
    document_version_id: str
    title: str
    event_type: str
    primary_entity_ids: tuple[str, ...]
    object_entity_ids: tuple[str, ...] = ()
    event_time_start: str | None = None
    event_time_end: str | None = None
    facts: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class MatchAssessment:
    decision: str
    score: float
    reason: str
    features: Mapping[str, Any]


@dataclass(frozen=True)
class MatchRecordingReport:
    candidates_evaluated: int
    decisions_created: int
    candidate_links_created: int
    evidence_links_created: int
    new_candidate_decision_created: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
    text = str(value).strip()
    return (text,) if text else ()


def _normal_title(value: str) -> str:
    text = value.casefold()
    replacements = {
        "人工智能": "ai", "发布": "announce", "宣布": "announce",
        "推出": "launch", "上线": "launch", "否认": "deny",
        "季度业绩": "quarter results", "财报": "earnings",
    }
    for before, after in replacements.items():
        text = text.replace(before, after)
    text = re.sub(
        r"\b(?:announces?|announced|releases?|released|launches?|launched|introduces?|introduced)\b",
        " announce ", text,
    )
    text = re.sub(r"\b(?:denies|denied|rejects|rejected)\b", " deny ", text)
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def _title_score(left: str, right: str) -> float:
    a, b = _normal_title(left), _normal_title(right)
    if min(len(a), len(b)) < 8:
        return 0.0
    return round(SequenceMatcher(None, a, b, autojunk=False).ratio(), 6)


def _quarter(value: str) -> str | None:
    text = value.casefold()
    patterns = (
        r"\b(?:q|quarter\s*)([1-4])\D{0,12}(20\d{2})\b",
        r"\b(20\d{2})\D{0,12}(?:q|quarter\s*)([1-4])\b",
    )
    match = re.search(patterns[0], text)
    if match:
        return f"{match.group(2)}-Q{match.group(1)}"
    match = re.search(patterns[1], text)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    match = re.search(r"(20\d{2})\s*年?\s*第?([一二三四1234])季度", text)
    if match:
        number = {"一": "1", "二": "2", "三": "3", "四": "4"}.get(
            match.group(2), match.group(2)
        )
        return f"{match.group(1)}-Q{number}"
    return None


def _period_keys(title: str, facts: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    values: set[str] = set()
    title_quarter = _quarter(title)
    if title_quarter:
        values.add(title_quarter)
    for fact in facts:
        period = fact.get("period")
        if period not in (None, "", [], {}):
            values.add(_json(period) if isinstance(period, (dict, list)) else str(period).strip())
        for key in ("time_interval", "reporting_period"):
            raw = fact.get(key)
            if raw:
                quarter = _quarter(_json(raw) if not isinstance(raw, str) else raw)
                values.add(quarter or (_json(raw) if isinstance(raw, (dict, list)) else str(raw)))
    return tuple(sorted(values))


def _product_versions(title: str, facts: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    values: set[str] = set()
    for product, version in re.findall(
        r"\b([a-z][a-z0-9-]{1,24})\s*[- ]?\s*(\d+(?:\.\d+){1,3})\b",
        title.casefold(),
    ):
        if product not in {"q", "fy", "year", "quarter"}:
            values.add(f"{product}:{version}")
    for fact in facts:
        for key in ("product_version", "model_version", "version"):
            raw = fact.get(key)
            if raw:
                values.update(_values(raw))
        obj = fact.get("object")
        if isinstance(obj, Mapping):
            for key in ("product_version", "model_version", "version"):
                raw = obj.get(key)
                if raw:
                    values.update(_values(raw))
    return tuple(sorted(values))


def _modalities(title: str, facts: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    values = {str(fact.get("modality", "")).strip().casefold() for fact in facts}
    values.discard("")
    normalized = _normal_title(title)
    if "deny" in normalized:
        values.add("denied")
    if "announce" in normalized or "launch" in normalized:
        values.add("announced")
    return tuple(sorted(values))


def _fact_signatures(facts: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    signatures: set[str] = set()
    ignored = {"evidence_ids", "fact_id", "conflict_group", "confidence"}
    for fact in facts:
        semantic = {key: value for key, value in fact.items() if key not in ignored}
        if semantic:
            signatures.add(_sha(semantic))
    return tuple(sorted(signatures))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hard_negative(
    proposal: EventProposal,
    candidate: Mapping[str, Any],
    features: dict[str, Any],
) -> str | None:
    left_entities = set(proposal.primary_entity_ids)
    right_entities = set(candidate["primary_entity_ids"])
    if left_entities and right_entities and not left_entities.intersection(right_entities):
        return "primary entities are disjoint"

    left_type, right_type = proposal.event_type, candidate["event_type"]
    if left_type != "other" and right_type != "other" and left_type != right_type:
        return "event types conflict"

    left_periods, right_periods = set(features["proposal_periods"]), set(features["candidate_periods"])
    if left_periods and right_periods and left_periods.isdisjoint(right_periods):
        return "reporting periods conflict"

    left_versions = set(features["proposal_product_versions"])
    right_versions = set(features["candidate_product_versions"])
    if left_versions and right_versions and left_versions.isdisjoint(right_versions):
        return "product or model versions conflict"

    left_modalities = set(features["proposal_modalities"])
    right_modalities = set(features["candidate_modalities"])
    denied = {"denied"}
    positive = {"announced", "asserted", "planned"}
    if (left_modalities & denied and right_modalities & positive) or (
        right_modalities & denied and left_modalities & positive
    ):
        return "denial and announcement are separate event facts"
    return None


def assess_event_match(
    proposal: EventProposal, candidate: Mapping[str, Any]
) -> MatchAssessment:
    """Compare a structured proposal with one fixed event version."""
    candidate_facts = tuple(candidate.get("facts", ()))
    features: dict[str, Any] = {
        "score_kind": "ranking_not_probability",
        "title_similarity": _title_score(proposal.title, str(candidate["title"])),
        "entity_overlap": sorted(
            set(proposal.primary_entity_ids).intersection(candidate["primary_entity_ids"])
        ),
        "proposal_periods": list(_period_keys(proposal.title, proposal.facts)),
        "candidate_periods": list(_period_keys(str(candidate["title"]), candidate_facts)),
        "proposal_product_versions": list(_product_versions(proposal.title, proposal.facts)),
        "candidate_product_versions": list(
            _product_versions(str(candidate["title"]), candidate_facts)
        ),
        "proposal_modalities": list(_modalities(proposal.title, proposal.facts)),
        "candidate_modalities": list(_modalities(str(candidate["title"]), candidate_facts)),
    }
    proposal_facts = set(_fact_signatures(proposal.facts))
    candidate_fact_set = set(_fact_signatures(candidate_facts))
    features["shared_fact_signatures"] = sorted(proposal_facts.intersection(candidate_fact_set))

    negative = _hard_negative(proposal, candidate, features)
    if negative:
        features["hard_negative"] = negative
        return MatchAssessment("no_match", 0.0, negative, features)

    left_time = _parse_time(proposal.event_time_start)
    right_time = _parse_time(candidate.get("event_time_start"))
    gap_days = None
    if left_time and right_time:
        gap_days = abs((left_time - right_time).total_seconds()) / 86400
    features["event_time_gap_days"] = round(gap_days, 6) if gap_days is not None else None

    title_score = float(features["title_similarity"])
    same_entities = bool(features["entity_overlap"])
    same_fact = bool(features["shared_fact_signatures"])
    same_period = bool(set(features["proposal_periods"]).intersection(features["candidate_periods"]))
    if same_fact and (same_entities or not proposal.primary_entity_ids):
        score = max(0.95, title_score)
        return MatchAssessment(
            "candidate_link", round(min(score, 1.0), 6),
            "same structured fact is reported by another document", features,
        )
    if gap_days is not None and gap_days > MAX_REPORT_GAP_DAYS:
        features["hard_negative"] = "event times exceed the candidate window"
        return MatchAssessment(
            "no_match", 0.0, "event times exceed the candidate window", features
        )
    if same_entities and title_score >= MATCH_THRESHOLD:
        return MatchAssessment(
            "candidate_link", title_score,
            "entity-compatible titles pass the conservative candidate threshold", features,
        )
    if same_entities and same_period and proposal.event_type == candidate["event_type"]:
        score = round(max(title_score, MATCH_THRESHOLD), 6)
        return MatchAssessment(
            "candidate_link", score,
            "same entity, event type, and reporting period", features,
        )
    score = round(title_score * (1.0 if same_entities else 0.5), 6)
    return MatchAssessment(
        "needs_review", score,
        "no hard conflict, but evidence is insufficient for an automatic candidate link",
        features,
    )


def _load_candidate(db: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    row = db.execute(
        """SELECT version.*,event.latest_report_at
           FROM event_versions AS version
           JOIN events AS event ON event.id=version.event_id
           WHERE version.id=?""",
        (version_id,),
    ).fetchone()
    if not row:
        raise EventMatchingError(f"event version {version_id} does not exist")
    try:
        return {
            "event_id": row["event_id"], "event_version_id": row["id"],
            "title": row["title"], "event_type": row["event_type"],
            "event_time_start": row["event_time_start"],
            "event_time_end": row["event_time_end"],
            "primary_entity_ids": tuple(json.loads(row["primary_entities_json"])),
            "object_entity_ids": tuple(json.loads(row["object_entities_json"])),
            "facts": tuple(json.loads(row["facts_json"])),
        }
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventMatchingError(f"event version {version_id} has invalid JSON") from exc


def _proposal_payload(proposal: EventProposal) -> dict[str, Any]:
    return {
        "document_version_id": proposal.document_version_id,
        "title": proposal.title,
        "event_type": proposal.event_type,
        "primary_entity_ids": sorted(proposal.primary_entity_ids),
        "object_entity_ids": sorted(proposal.object_entity_ids),
        "event_time_start": proposal.event_time_start,
        "event_time_end": proposal.event_time_end,
        "facts": list(proposal.facts),
    }


def record_candidate_matches(
    db: sqlite3.Connection,
    proposal: EventProposal,
    candidate_event_version_ids: Iterable[str],
) -> MatchRecordingReport:
    """Persist decisions and pending candidate links for one event proposal.

    Callers own the transaction.  Calling the function again with identical
    inputs is idempotent.  It never changes an event to active or confirmed.
    """
    document = db.execute(
        "SELECT available_at FROM document_versions WHERE id=?",
        (proposal.document_version_id,),
    ).fetchone()
    if not document:
        raise EventMatchingError(
            f"document version {proposal.document_version_id} does not exist"
        )
    dataset = db.execute(
        "SELECT dataset_id FROM dataset_state WHERE singleton=1"
    ).fetchone()
    if not dataset:
        raise EventMatchingError("dataset identity is missing")

    candidates = [_load_candidate(db, item) for item in dict.fromkeys(candidate_event_version_ids)]
    now = utc_now()
    payload = _proposal_payload(proposal)
    proposal_sha = _sha(payload)
    decisions_created = links_created = evidence_created = 0
    assessments: list[MatchAssessment] = []

    for candidate in candidates:
        assessment = assess_event_match(proposal, candidate)
        assessments.append(assessment)
        decision_key = (
            f"{MATCHER_VERSION}:proposal:{proposal_sha}:candidate:"
            f"{candidate['event_version_id']}"
        )
        existing = db.execute(
            "SELECT id FROM match_decisions WHERE decision_key=?", (decision_key,)
        ).fetchone()
        if existing:
            decision_id = existing["id"]
        else:
            decision_id = f"match_decision_{uuid4().hex}"
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,
                       score,decision,reason,review_status,available_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending',?)""",
                (
                    decision_id, dataset["dataset_id"], decision_key,
                    _json([proposal.document_version_id]),
                    _json([candidate["event_version_id"]]), MATCHER_VERSION,
                    _json({**assessment.features, "proposal": payload}),
                    assessment.score, assessment.decision, assessment.reason, now,
                ),
            )
            decisions_created += 1

        if assessment.decision != "candidate_link":
            continue
        exists = db.execute(
            """SELECT 1 FROM document_event_links
               WHERE document_version_id=? AND event_id=? AND event_version_id=?
                 AND role='candidate'""",
            (
                proposal.document_version_id, candidate["event_id"],
                candidate["event_version_id"],
            ),
        ).fetchone()
        if not exists:
            db.execute(
                """INSERT INTO document_event_links(
                       id,document_version_id,event_id,event_version_id,role,
                       decision_id,available_at,supersedes_link_id
                   ) VALUES(?,?,?,?, 'candidate',?,?,NULL)""",
                (
                    f"document_event_link_{uuid4().hex}", proposal.document_version_id,
                    candidate["event_id"], candidate["event_version_id"],
                    decision_id, now,
                ),
            )
            links_created += 1
        raw_inputs = db.execute(
            """SELECT raw_record_id FROM document_version_inputs
               WHERE version_id=? ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END,
                        raw_record_id""",
            (proposal.document_version_id,),
        ).fetchall()
        if not raw_inputs:
            raise EventMatchingError("a candidate link requires version-pinned raw evidence")
        for raw in raw_inputs:
            exists = db.execute(
                """SELECT 1 FROM event_evidence
                   WHERE event_version_id=? AND document_version_id=? AND evidence_id=?
                     AND fact_id IS NULL AND role='context'""",
                (
                    candidate["event_version_id"], proposal.document_version_id,
                    raw["raw_record_id"],
                ),
            ).fetchone()
            if not exists:
                db.execute(
                    """INSERT INTO event_evidence(
                           id,event_version_id,document_version_id,evidence_id,
                           fact_id,role,available_at
                       ) VALUES(?,?,?,?,NULL,'context',?)""",
                    (
                        f"event_evidence_{uuid4().hex}", candidate["event_version_id"],
                        proposal.document_version_id, raw["raw_record_id"], now,
                    ),
                )
                evidence_created += 1
        db.execute(
            """UPDATE events SET latest_report_at=MAX(latest_report_at,?) WHERE id=?""",
            (document["available_at"], candidate["event_id"]),
        )

    new_created = False
    if not candidates or all(item.decision == "no_match" for item in assessments):
        candidate_ids = sorted(item["event_version_id"] for item in candidates)
        decision_key = f"{MATCHER_VERSION}:proposal:{proposal_sha}:new-candidate"
        if not db.execute(
            "SELECT 1 FROM match_decisions WHERE decision_key=?", (decision_key,)
        ).fetchone():
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,
                       score,decision,reason,review_status,available_at
                   ) VALUES(?,?,?,?,?,?,?,NULL,'new_candidate',?,'pending',?)""",
                (
                    f"match_decision_{uuid4().hex}", dataset["dataset_id"], decision_key,
                    _json([proposal.document_version_id]), _json(candidate_ids),
                    MATCHER_VERSION, _json({"proposal": payload}),
                    (
                        "no candidate event was retrieved"
                        if not candidates
                        else "all retrieved candidates were rejected by hard constraints"
                    ),
                    now,
                ),
            )
            decisions_created += 1
            new_created = True

    return MatchRecordingReport(
        candidates_evaluated=len(candidates), decisions_created=decisions_created,
        candidate_links_created=links_created, evidence_links_created=evidence_created,
        new_candidate_decision_created=new_created,
    )
