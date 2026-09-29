from __future__ import annotations

"""Plan and control tone shadow evaluation without changing served publications."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Mapping

from .timeutil import format_utc, parse_utc, utc_now


ROLLOUT_SCHEMA_VERSION = "tone-shadow-rollout-v1"
STATES = {"planned", "running", "paused", "completed", "aborted"}
TRANSITIONS = {
    "planned": {"running", "aborted"},
    "running": {"paused", "completed", "aborted"},
    "paused": {"running", "aborted"},
}
CONFIG_FIELDS = {
    "mode", "minimum_observations", "observation_window_hours",
    "maximum_error_bps", "maximum_disagreement_bps",
}


class ToneShadowRolloutError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToneShadowRollout:
    rollout_id: str
    admission_id: str
    dataset_version: str
    candidate_test_run_id: str
    calibration_version: str
    sample_bps: int
    config_sha256: str
    state: str
    transition_id: str
    transition_version: int

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
        raise ToneShadowRolloutError("tone shadow rollout time requires a timezone") from exc


def _actor(value: str, field: str) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned or len(cleaned) > 200:
        raise ToneShadowRolloutError(f"tone shadow rollout requires bounded {field}")
    return cleaned


def _config(value: Mapping[str, object]) -> dict:
    if not isinstance(value, Mapping) or set(value) != CONFIG_FIELDS:
        raise ToneShadowRolloutError("tone shadow rollout config fields are invalid")
    if value.get("mode") != "shadow_only":
        raise ToneShadowRolloutError("tone rollout mode must remain shadow_only")
    bounded = {
        "minimum_observations": (1, 1_000_000),
        "observation_window_hours": (1, 24 * 90),
        "maximum_error_bps": (0, 10_000),
        "maximum_disagreement_bps": (0, 10_000),
    }
    clean = {"mode": "shadow_only"}
    for field, (lower, upper) in bounded.items():
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or not lower <= item <= upper:
            raise ToneShadowRolloutError(f"tone shadow rollout {field} is invalid")
        clean[field] = item
    return clean


def _row(db: sqlite3.Connection, rollout_id: str) -> ToneShadowRollout:
    row = db.execute(
        """SELECT rollout.*,transition.id AS transition_id,
                  transition.version AS transition_version,
                  transition.to_state AS state
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
        raise ToneShadowRolloutError("tone shadow rollout does not exist")
    return ToneShadowRollout(
        row["id"], row["admission_id"], row["dataset_version"],
        row["candidate_test_run_id"], row["calibration_version"], row["sample_bps"],
        row["config_sha256"], row["state"], row["transition_id"],
        row["transition_version"],
    )


def create_tone_shadow_rollout(
    db: sqlite3.Connection,
    *,
    admission_id: str,
    sample_bps: int,
    config: Mapping[str, object],
    created_by: str,
    reason: str,
    now: str | None = None,
) -> ToneShadowRollout:
    if isinstance(sample_bps, bool) or not isinstance(sample_bps, int) or not 1 <= sample_bps <= 10_000:
        raise ToneShadowRolloutError("tone shadow rollout sample_bps must be 1..10000")
    actor = _actor(created_by, "created_by")
    explanation = _actor(reason, "reason")
    clean_config = _config(config)
    admission = db.execute(
        """SELECT * FROM tone_release_admissions
           WHERE id=? AND decision='approve' AND approval_candidate=1""",
        (admission_id,),
    ).fetchone()
    if admission is None:
        raise ToneShadowRolloutError("tone shadow rollout requires an approved admission")
    identity = {
        "rollout_schema_version": ROLLOUT_SCHEMA_VERSION,
        "admission_id": admission_id,
        "sample_bps": sample_bps,
        "config": clean_config,
    }
    config_json = _canonical(clean_config)
    config_sha256 = _digest(identity)
    rollout_id = f"tone-shadow-{config_sha256[:24]}"
    occurred_at = _time(now)
    transition_id = f"tone-shadow-transition-{_digest({'rollout_id': rollout_id, 'version': 1, 'state': 'planned'})[:24]}"
    db.execute("SAVEPOINT tone_shadow_rollout_create")
    try:
        existing = db.execute(
            "SELECT id,config_sha256 FROM tone_shadow_rollouts WHERE admission_id=?",
            (admission_id,),
        ).fetchone()
        if existing is not None:
            if existing["id"] == rollout_id and existing["config_sha256"] == config_sha256:
                db.execute("RELEASE SAVEPOINT tone_shadow_rollout_create")
                return _row(db, rollout_id)
            raise ToneShadowRolloutError("tone release admission already has a different rollout")
        db.execute(
            """INSERT INTO tone_shadow_rollouts(
                   id,admission_id,rollout_schema_version,dataset_version,
                   candidate_test_run_id,calibration_version,sample_bps,
                   config_json,config_sha256,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rollout_id, admission_id, ROLLOUT_SCHEMA_VERSION,
                admission["dataset_version"], admission["candidate_test_run_id"],
                admission["calibration_version"], sample_bps, config_json,
                config_sha256, actor, occurred_at,
            ),
        )
        db.execute(
            """INSERT INTO tone_shadow_rollout_transitions(
                   id,rollout_id,version,previous_transition_id,from_state,to_state,
                   actor,reason,occurred_at)
               VALUES(?,?,1,NULL,NULL,'planned',?,?,?)""",
            (transition_id, rollout_id, actor, explanation, occurred_at),
        )
        db.execute("RELEASE SAVEPOINT tone_shadow_rollout_create")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT tone_shadow_rollout_create")
        db.execute("RELEASE SAVEPOINT tone_shadow_rollout_create")
        raise
    return _row(db, rollout_id)


def transition_tone_shadow_rollout(
    db: sqlite3.Connection,
    *,
    rollout_id: str,
    to_state: str,
    expected_previous_transition_id: str,
    actor: str,
    reason: str,
    now: str | None = None,
) -> ToneShadowRollout:
    if to_state not in STATES:
        raise ToneShadowRolloutError("unsupported tone shadow rollout state")
    operator = _actor(actor, "actor")
    explanation = _actor(reason, "reason")
    current = _row(db, rollout_id)
    if current.transition_id != expected_previous_transition_id:
        raise ToneShadowRolloutError("tone shadow rollout changed; refresh before transitioning")
    if to_state not in TRANSITIONS.get(current.state, set()):
        raise ToneShadowRolloutError("invalid tone shadow rollout transition")
    version = current.transition_version + 1
    transition_id = f"tone-shadow-transition-{_digest({'rollout_id': rollout_id, 'version': version, 'from': current.state, 'to': to_state})[:24]}"
    occurred_at = _time(now)
    try:
        db.execute(
            """INSERT INTO tone_shadow_rollout_transitions(
                   id,rollout_id,version,previous_transition_id,from_state,to_state,
                   actor,reason,occurred_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                transition_id, rollout_id, version, current.transition_id,
                current.state, to_state, operator, explanation, occurred_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ToneShadowRolloutError(
            "tone shadow rollout changed; refresh before transitioning"
        ) from exc
    return _row(db, rollout_id)
