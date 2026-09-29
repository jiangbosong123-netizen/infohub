from __future__ import annotations

"""Freeze tone shadow samples, record every outcome, and evaluate rollout gates."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Iterable

from .timeutil import format_utc, parse_utc, utc_now


OUTCOMES = {"matched", "disagreed", "error"}


class ToneShadowEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToneShadowBatch:
    batch_id: str
    rollout_id: str
    population_count: int
    selected_count: int
    population_manifest_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ToneShadowEvaluation:
    evaluation_id: str
    batch_id: str
    decision: str
    metrics: dict
    metrics_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _time(value: str | None) -> str:
    try:
        return format_utc(parse_utc(value or utc_now()))
    except (TypeError, ValueError) as exc:
        raise ToneShadowEvaluationError("tone shadow timestamp requires a timezone") from exc


def _text(value: str, field: str, maximum: int = 2_000) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned or len(cleaned) > maximum:
        raise ToneShadowEvaluationError(f"tone shadow {field} is required and bounded")
    return cleaned


def _optional_sha256(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ToneShadowEvaluationError(f"tone shadow {field} must be a lowercase SHA-256")
    return value


def _rollout(db: sqlite3.Connection, rollout_id: str) -> sqlite3.Row:
    row = db.execute(
        """SELECT rollout.*,transition.to_state AS state,transition.id AS transition_id
           FROM tone_shadow_rollouts AS rollout
           JOIN tone_shadow_rollout_transitions AS transition
             ON transition.rollout_id=rollout.id
           WHERE rollout.id=? AND NOT EXISTS(
               SELECT 1 FROM tone_shadow_rollout_transitions AS later
               WHERE later.rollout_id=transition.rollout_id
                 AND later.version>transition.version)""",
        (rollout_id,),
    ).fetchone()
    if row is None:
        raise ToneShadowEvaluationError("tone shadow rollout does not exist")
    return row


def selected_for_shadow(rollout_id: str, subject_version_id: str, sample_bps: int) -> tuple[bool, str]:
    selection_hash = hashlib.sha256(
        f"{rollout_id}\0{subject_version_id}".encode()
    ).hexdigest()
    return int(selection_hash[:16], 16) % 10_000 < sample_bps, selection_hash


def create_tone_shadow_batch(
    db: sqlite3.Connection,
    *,
    rollout_id: str,
    population_subject_version_ids: Iterable[str],
    created_by: str,
    now: str | None = None,
) -> ToneShadowBatch:
    actor = _text(created_by, "created_by", 200)
    population = list(population_subject_version_ids)
    if (
        not population
        or any(not isinstance(item, str) or not item.strip() for item in population)
        or len(population) != len(set(population))
    ):
        raise ToneShadowEvaluationError("tone shadow population must be nonempty and unique")
    population = sorted(population)
    rollout = _rollout(db, rollout_id)
    if rollout["state"] != "running":
        raise ToneShadowEvaluationError("tone shadow batch requires a running rollout")
    existing_documents = {
        row[0] for row in db.execute(
            f"SELECT id FROM document_versions WHERE id IN ({','.join('?' for _ in population)})",
            population,
        )
    }
    missing = set(population) - existing_documents
    if missing:
        raise ToneShadowEvaluationError("tone shadow population contains unknown document versions")
    population_selections = []
    selected = []
    for subject_id in population:
        included, selection_hash = selected_for_shadow(
            rollout_id, subject_id, rollout["sample_bps"]
        )
        population_selections.append((subject_id, selection_hash, included))
        if included:
            selected.append((subject_id, selection_hash))
    config = json.loads(rollout["config_json"])
    if len(selected) < config["minimum_observations"]:
        raise ToneShadowEvaluationError(
            "tone shadow selected sample is below minimum_observations"
        )
    manifest_hash = _digest(population)
    batch_id = f"tone-shadow-batch-{_digest({'rollout_id': rollout_id, 'population': population})[:24]}"
    created_at = _time(now)
    db.execute("SAVEPOINT tone_shadow_batch_create")
    try:
        existing = db.execute(
            "SELECT * FROM tone_shadow_batches WHERE rollout_id=?", (rollout_id,)
        ).fetchone()
        if existing is not None:
            if existing["id"] == batch_id and existing["population_manifest_sha256"] == manifest_hash:
                db.execute("RELEASE SAVEPOINT tone_shadow_batch_create")
                return ToneShadowBatch(
                    existing["id"], existing["rollout_id"], existing["population_count"],
                    existing["selected_count"], existing["population_manifest_sha256"],
                )
            raise ToneShadowEvaluationError("tone shadow rollout already has a different batch")
        db.execute(
            """INSERT INTO tone_shadow_batches(
                   id,rollout_id,population_manifest_sha256,population_count,
                   selected_count,created_by,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (batch_id, rollout_id, manifest_hash, len(population), len(selected), actor, created_at),
        )
        db.executemany(
            """INSERT INTO tone_shadow_population_members(
                   batch_id,ordinal,subject_version_id,selection_hash,selected)
               VALUES(?,?,?,?,?)""",
            [(batch_id, ordinal, subject_id, selection_hash, int(included))
             for ordinal, (subject_id, selection_hash, included)
             in enumerate(population_selections)],
        )
        db.executemany(
            """INSERT INTO tone_shadow_batch_members(
                   batch_id,ordinal,subject_version_id,selection_hash)
               VALUES(?,?,?,?)""",
            [(batch_id, ordinal, subject_id, selection_hash)
             for ordinal, (subject_id, selection_hash) in enumerate(selected)],
        )
        db.execute("RELEASE SAVEPOINT tone_shadow_batch_create")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT tone_shadow_batch_create")
        db.execute("RELEASE SAVEPOINT tone_shadow_batch_create")
        raise
    return ToneShadowBatch(batch_id, rollout_id, len(population), len(selected), manifest_hash)


def record_tone_shadow_observation(
    db: sqlite3.Connection,
    *,
    batch_id: str,
    subject_version_id: str,
    outcome: str,
    candidate_result_sha256: str | None,
    reference_result_sha256: str | None,
    error_code: str | None,
    recorded_by: str,
    observed_at: str | None = None,
) -> str:
    if outcome not in OUTCOMES:
        raise ToneShadowEvaluationError("unsupported tone shadow outcome")
    actor = _text(recorded_by, "recorded_by", 200)
    when = _time(observed_at)
    candidate_hash = _optional_sha256(candidate_result_sha256, "candidate result hash")
    reference_hash = _optional_sha256(reference_result_sha256, "reference result hash")
    batch = db.execute(
        """SELECT batch.*,rollout.config_json,transition.to_state AS state
           FROM tone_shadow_batches AS batch
           JOIN tone_shadow_rollouts AS rollout ON rollout.id=batch.rollout_id
           JOIN tone_shadow_rollout_transitions AS transition
             ON transition.rollout_id=rollout.id
           WHERE batch.id=? AND NOT EXISTS(
               SELECT 1 FROM tone_shadow_rollout_transitions AS later
               WHERE later.rollout_id=transition.rollout_id
                 AND later.version>transition.version)""",
        (batch_id,),
    ).fetchone()
    if batch is None or batch["state"] != "running":
        raise ToneShadowEvaluationError("tone shadow observations require a running rollout")
    deadline = parse_utc(batch["created_at"]) + timedelta(
        hours=json.loads(batch["config_json"])["observation_window_hours"]
    )
    if parse_utc(when) < parse_utc(batch["created_at"]) or parse_utc(when) > deadline:
        raise ToneShadowEvaluationError("tone shadow observation is outside its window")
    if outcome in {"matched", "disagreed"}:
        valid = candidate_hash is not None and reference_hash is not None and error_code is None
        cleaned_error = None
    else:
        cleaned_error = _text(error_code, "error_code", 200)
        valid = candidate_hash is None
    if not valid:
        raise ToneShadowEvaluationError("tone shadow observation evidence is inconsistent")
    observation_id = f"tone-shadow-observation-{_digest({'batch_id': batch_id, 'subject_version_id': subject_version_id})[:24]}"
    try:
        db.execute(
            """INSERT INTO tone_shadow_observations(
                   id,batch_id,subject_version_id,outcome,candidate_result_sha256,
                   reference_result_sha256,error_code,observed_at,recorded_by)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                observation_id, batch_id, subject_version_id, outcome,
                candidate_hash, reference_hash, cleaned_error, when, actor,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ToneShadowEvaluationError(
            "tone shadow subject is not selected or was already observed"
        ) from exc
    return observation_id


def evaluate_tone_shadow_batch(
    db: sqlite3.Connection,
    *,
    batch_id: str,
    evaluated_by: str,
    reason: str,
    now: str | None = None,
) -> ToneShadowEvaluation:
    actor = _text(evaluated_by, "evaluated_by", 200)
    explanation = _text(reason, "reason")
    batch = db.execute(
        """SELECT batch.*,rollout.config_json
           FROM tone_shadow_batches AS batch
           JOIN tone_shadow_rollouts AS rollout ON rollout.id=batch.rollout_id
           WHERE batch.id=?""",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise ToneShadowEvaluationError("tone shadow batch does not exist")
    counts = {row["outcome"]: row["count"] for row in db.execute(
        """SELECT outcome,COUNT(*) AS count FROM tone_shadow_observations
           WHERE batch_id=? GROUP BY outcome""", (batch_id,)
    )}
    total = batch["selected_count"]
    observed = sum(counts.values())
    if observed != total:
        raise ToneShadowEvaluationError("tone shadow batch observations are incomplete")
    config = json.loads(batch["config_json"])
    errors = counts.get("error", 0)
    disagreements = counts.get("disagreed", 0)
    metrics = {
        "selected_count": total,
        "observed_count": observed,
        "matched_count": counts.get("matched", 0),
        "disagreement_count": disagreements,
        "error_count": errors,
        "error_bps": errors * 10_000 // total,
        "disagreement_bps": disagreements * 10_000 // total,
        "maximum_error_bps": config["maximum_error_bps"],
        "maximum_disagreement_bps": config["maximum_disagreement_bps"],
    }
    decision = "passed" if (
        observed >= config["minimum_observations"]
        and metrics["error_bps"] <= config["maximum_error_bps"]
        and metrics["disagreement_bps"] <= config["maximum_disagreement_bps"]
    ) else "failed"
    metrics_hash = _digest(metrics)
    evaluation_id = f"tone-shadow-evaluation-{_digest({'batch_id': batch_id, 'metrics_sha256': metrics_hash, 'decision': decision})[:24]}"
    try:
        db.execute(
            """INSERT INTO tone_shadow_evaluations(
                   id,batch_id,decision,metrics_json,metrics_sha256,
                   evaluated_by,reason,evaluated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                evaluation_id, batch_id, decision, _canonical(metrics), metrics_hash,
                actor, explanation, _time(now),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ToneShadowEvaluationError("tone shadow batch was already evaluated") from exc
    return ToneShadowEvaluation(evaluation_id, batch_id, decision, metrics, metrics_hash)
