from __future__ import annotations

"""Authorize one completed tone rollout for production, then fail closed on rollback."""

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .evaluation import EvaluationDatasetError
from .timeutil import format_utc, parse_utc
from .tone_contracts import TONE_SCHEMA_VERSION
from .tone_release_decision import _load_registry


PROFILE_SCHEMA = "tone-production-profile-v1"
ACTIVATION_SCHEMA = "tone-production-activation-v1"
ROLLBACK_SCHEMA = "tone-production-rollback-v1"
ACTIVATE_SCOPE = "tone:release:activate"
ROLLBACK_SCOPE = "tone:release:rollback"
RUNTIME_FIELDS = {
    "provider", "requested_model", "prompt_template_id", "prompt_sha256",
    "pipeline_version", "parameters", "output_schema_version", "calibration_version",
}
CANDIDATE_RUN_FIELDS = {
    "schema_version", "prediction_run_id", "dataset_version", "task",
    "task_contract", "output_schema_version", "vocabulary_version", "split",
    "method_id", "method_version", "method_config_sha256", "generated_at",
    "predictions_file", "dataset_manifest_sha256", "dataset_cases_sha256",
    "predictions_sha256", "abstain_label",
}
ACTIVATION_REQUEST_FIELDS = {
    "schema_version", "request_id", "rollout_id", "shadow_evaluation_id",
    "profile_id", "profile_sha256", "candidate_test_run_id",
    "candidate_run_sha256", "registry_version", "registry_sha256",
    "activator_id", "reason", "recorded_at", "source", "model_assistance",
    "rollback_plan_acknowledged",
}
ROLLBACK_REQUEST_FIELDS = {
    "schema_version", "request_id", "activation_id",
    "expected_previous_transition_id", "registry_version", "registry_sha256",
    "operator_id", "reason", "recorded_at", "source", "model_assistance",
}
AUTHORIZATION_FIELDS = {
    "approver_id", "status", "scopes", "valid_from", "valid_until",
}


class ToneReleaseActivationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToneReleaseActivation:
    activation_id: str
    rollout_id: str
    admission_id: str
    shadow_evaluation_id: str
    profile_id: str
    runtime_config_sha256: str
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
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ToneReleaseActivationError("cannot read tone activation input") from exc


def _file_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToneReleaseActivationError("cannot read UTF-8 tone activation input") from exc


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToneReleaseActivationError(f"cannot read tone {label}") from exc
    if not isinstance(value, dict):
        raise ToneReleaseActivationError(f"tone {label} must be an object")
    return value


def _time(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ToneReleaseActivationError(f"tone activation requires {field}")
    try:
        return format_utc(parse_utc(value))
    except (TypeError, ValueError) as exc:
        raise ToneReleaseActivationError(
            f"tone activation {field} requires a timezone"
        ) from exc


def _text(value: object, field: str, maximum: int = 2_000) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned or len(cleaned) > maximum:
        raise ToneReleaseActivationError(f"tone activation requires bounded {field}")
    return cleaned


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ToneReleaseActivationError(f"tone activation {field} must be a SHA-256")
    return value


def _authorized_operator(
    registry_path: Path,
    *,
    actor_id: str,
    required_scope: str,
    occurred_at: str,
) -> tuple[dict, dict, str]:
    try:
        registry, operators = _load_registry(registry_path)
    except EvaluationDatasetError as exc:
        raise ToneReleaseActivationError(str(exc)) from exc
    registry_sha = _file_sha256(registry_path)
    occurred = parse_utc(occurred_at)
    generated = parse_utc(registry["generated_at"])
    operator = operators.get(actor_id)
    if generated > occurred or operator is None:
        raise ToneReleaseActivationError("tone activation operator is not authorized")
    valid_from = parse_utc(operator["valid_from"])
    valid_until = (
        parse_utc(operator["valid_until"])
        if operator["valid_until"] is not None else None
    )
    if (
        operator["status"] != "active"
        or required_scope not in operator["scopes"]
        or occurred < valid_from
        or (valid_until is not None and occurred >= valid_until)
    ):
        raise ToneReleaseActivationError("tone activation operator is not authorized")
    return registry, operator, registry_sha


def _validate_runtime_profile(profile: dict, candidate: dict, admission, rollout) -> tuple[dict, str]:
    fields = {
        "schema_version", "profile_id", "candidate_test_run_id", "method_id",
        "method_version", "runtime_config", "runtime_config_sha256", "created_at",
    }
    if set(profile) != fields or profile.get("schema_version") != PROFILE_SCHEMA:
        raise ToneReleaseActivationError("invalid tone production profile")
    for field in ("profile_id", "candidate_test_run_id", "method_id", "method_version"):
        _text(profile.get(field), f"profile {field}", 500)
    _time(profile.get("created_at"), "profile created_at")
    runtime = profile.get("runtime_config")
    if not isinstance(runtime, Mapping) or set(runtime) != RUNTIME_FIELDS:
        raise ToneReleaseActivationError("invalid tone production runtime config")
    for field in (
        "provider", "requested_model", "prompt_template_id", "pipeline_version",
        "output_schema_version", "calibration_version",
    ):
        _text(runtime.get(field), f"runtime {field}", 500)
    _sha256(runtime.get("prompt_sha256"), "runtime prompt_sha256")
    if not isinstance(runtime.get("parameters"), Mapping):
        raise ToneReleaseActivationError("tone activation runtime parameters must be an object")
    try:
        runtime_clean = json.loads(_canonical(dict(runtime)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToneReleaseActivationError(
            "tone activation runtime parameters must be finite JSON"
        ) from exc
    runtime_hash = _digest(runtime_clean)
    if profile.get("runtime_config_sha256") != runtime_hash:
        raise ToneReleaseActivationError("tone production profile config hash differs")
    if runtime_clean["output_schema_version"] != TONE_SCHEMA_VERSION:
        raise ToneReleaseActivationError("tone production profile output schema differs")
    if runtime_clean["calibration_version"] != rollout["calibration_version"]:
        raise ToneReleaseActivationError("tone production profile calibration differs")
    if set(candidate) != CANDIDATE_RUN_FIELDS:
        raise ToneReleaseActivationError("invalid tone candidate run manifest")
    expected_candidate = {
        "schema_version": "tone-prediction-run-v1",
        "prediction_run_id": admission["candidate_test_run_id"],
        "dataset_version": admission["dataset_version"],
        "task": "tone.polarity",
        "task_contract": "infohub.tone-evaluation/1.0",
        "output_schema_version": TONE_SCHEMA_VERSION,
        "vocabulary_version": "tone-vocabulary-v1",
        "split": "test",
        "abstain_label": "__abstain__",
    }
    if any(candidate.get(key) != value for key, value in expected_candidate.items()):
        raise ToneReleaseActivationError("tone candidate run identity differs")
    for field in ("method_id", "method_version", "predictions_file"):
        _text(candidate.get(field), f"candidate {field}", 500)
    _time(candidate.get("generated_at"), "candidate generated_at")
    for field in (
        "method_config_sha256", "dataset_manifest_sha256", "dataset_cases_sha256",
        "predictions_sha256",
    ):
        _sha256(candidate.get(field), f"candidate {field}")
    if (
        profile["candidate_test_run_id"] != candidate["prediction_run_id"]
        or profile["method_id"] != candidate["method_id"]
        or profile["method_version"] != candidate["method_version"]
        or candidate["method_config_sha256"] != runtime_hash
    ):
        raise ToneReleaseActivationError("tone production profile differs from evaluated method")
    return runtime_clean, runtime_hash


def _row(db: sqlite3.Connection, activation_id: str) -> ToneReleaseActivation:
    row = db.execute(
        """SELECT activation.*,transition.id AS transition_id,
                  transition.version AS transition_version,
                  transition.to_state AS state
           FROM tone_release_activations AS activation
           JOIN tone_release_activation_transitions AS transition
             ON transition.activation_id=activation.id
           WHERE activation.id=? AND NOT EXISTS(
               SELECT 1 FROM tone_release_activation_transitions AS later
               WHERE later.activation_id=transition.activation_id
                 AND later.version>transition.version)""",
        (activation_id,),
    ).fetchone()
    if row is None:
        raise ToneReleaseActivationError("tone release activation does not exist")
    return ToneReleaseActivation(
        row["id"], row["rollout_id"], row["admission_id"],
        row["shadow_evaluation_id"], row["profile_id"],
        row["runtime_config_sha256"], row["state"], row["transition_id"],
        row["transition_version"],
    )


def activate_tone_release(
    db: sqlite3.Connection,
    *,
    rollout_id: str,
    candidate_run_path: Path | str,
    profile_path: Path | str,
    registry_path: Path | str,
    request_path: Path | str,
) -> ToneReleaseActivation:
    rollout = db.execute(
        """SELECT rollout.*,transition.to_state AS state,
                  transition.shadow_evaluation_id,transition.occurred_at AS completed_at
           FROM tone_shadow_rollouts AS rollout
           JOIN tone_shadow_rollout_transitions AS transition
             ON transition.rollout_id=rollout.id
           WHERE rollout.id=? AND NOT EXISTS(
               SELECT 1 FROM tone_shadow_rollout_transitions AS later
               WHERE later.rollout_id=transition.rollout_id
                 AND later.version>transition.version)""",
        (rollout_id,),
    ).fetchone()
    if rollout is None or rollout["state"] != "completed":
        raise ToneReleaseActivationError("tone activation requires a completed rollout")
    evaluation = db.execute(
        "SELECT * FROM tone_shadow_evaluations WHERE id=?",
        (rollout["shadow_evaluation_id"],),
    ).fetchone()
    admission = db.execute(
        "SELECT * FROM tone_release_admissions WHERE id=?",
        (rollout["admission_id"],),
    ).fetchone()
    if evaluation is None or evaluation["decision"] != "passed" or admission is None:
        raise ToneReleaseActivationError("tone activation requires passed shadow evidence")
    candidate_path = Path(candidate_run_path)
    profile_path = Path(profile_path)
    registry_path = Path(registry_path)
    request = _load_object(Path(request_path), "activation request")
    profile = _load_object(profile_path, "production profile")
    candidate = _load_object(candidate_path, "candidate run manifest")
    runtime, runtime_hash = _validate_runtime_profile(profile, candidate, admission, rollout)
    bundle = json.loads(admission["bundle_json"])
    candidate_sha = _file_sha256(candidate_path)
    profile_sha = _file_sha256(profile_path)
    candidate_source = _file_text(candidate_path)
    profile_source = _file_text(profile_path)
    profile_content_sha = _digest(profile)
    candidate_content_sha = _digest(candidate)
    expected_candidate_sha = bundle.get("artifact_sha256", {}).get("candidate_test_run")
    if candidate_sha != expected_candidate_sha:
        raise ToneReleaseActivationError("tone candidate run artifact differs from admission")
    if (
        set(request) != ACTIVATION_REQUEST_FIELDS
        or request.get("schema_version") != ACTIVATION_SCHEMA
        or request.get("source") != "human"
        or request.get("model_assistance") is not False
        or request.get("rollback_plan_acknowledged") is not True
    ):
        raise ToneReleaseActivationError("invalid tone activation request")
    for field in ("request_id", "activator_id", "reason"):
        _text(request.get(field), field)
    recorded_at = _time(request.get("recorded_at"), "recorded_at")
    recorded = parse_utc(recorded_at)
    if any(
        parse_utc(value) > recorded
        for value in (
            profile["created_at"], candidate["generated_at"],
            evaluation["evaluated_at"], rollout["completed_at"],
        )
    ):
        raise ToneReleaseActivationError("tone activation predates its release evidence")
    registry, authorization, registry_sha = _authorized_operator(
        registry_path, actor_id=request["activator_id"],
        required_scope=ACTIVATE_SCOPE, occurred_at=recorded_at,
    )
    expected = {
        "rollout_id": rollout_id,
        "shadow_evaluation_id": evaluation["id"],
        "profile_id": profile["profile_id"],
        "profile_sha256": profile_sha,
        "candidate_test_run_id": candidate["prediction_run_id"],
        "candidate_run_sha256": candidate_sha,
        "registry_version": registry["registry_version"],
        "registry_sha256": registry_sha,
    }
    if any(request.get(field) != value for field, value in expected.items()):
        raise ToneReleaseActivationError("tone activation request binding differs")
    if request["activator_id"] in {
        admission["approver_id"], evaluation["evaluated_by"],
        bundle.get("reviewer_a"), bundle.get("reviewer_b"),
    }:
        raise ToneReleaseActivationError("tone activator must be independent")
    request_json = _canonical(request)
    request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    activation_id = f"tone-activation-{_digest({'request_sha256': request_sha})[:24]}"
    transition_id = f"tone-activation-transition-{_digest({'activation_id': activation_id, 'version': 1})[:24]}"
    db.execute("SAVEPOINT tone_release_activate")
    try:
        existing = db.execute(
            "SELECT id,activation_request_sha256 FROM tone_release_activations WHERE rollout_id=?",
            (rollout_id,),
        ).fetchone()
        if existing is not None:
            if existing["id"] == activation_id and existing["activation_request_sha256"] == request_sha:
                db.execute("RELEASE SAVEPOINT tone_release_activate")
                return _row(db, activation_id)
            raise ToneReleaseActivationError("tone rollout already has a different activation")
        db.execute(
            """INSERT INTO tone_release_activations(
                   id,rollout_id,admission_id,shadow_evaluation_id,profile_id,
                   profile_json,profile_sha256,profile_content_sha256,
                   candidate_run_json,candidate_run_sha256,candidate_run_content_sha256,
                   runtime_config_json,runtime_config_sha256,activation_request_json,
                   activation_request_sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                activation_id, rollout_id, admission["id"], evaluation["id"],
                profile["profile_id"], profile_source, profile_sha,
                profile_content_sha, candidate_source, candidate_sha,
                candidate_content_sha, _canonical(runtime), runtime_hash,
                request_json, request_sha, recorded_at,
            ),
        )
        db.execute(
            """INSERT INTO tone_release_activation_transitions(
                   id,activation_id,version,previous_transition_id,from_state,to_state,
                   actor,reason,occurred_at,registry_version,registry_sha256,
                   authorization_json,request_json,request_sha256)
               VALUES(?,?,1,NULL,NULL,'active',?,?,?,?,?,?,?,?)""",
            (
                transition_id, activation_id, request["activator_id"],
                request["reason"].strip(), recorded_at, registry["registry_version"],
                registry_sha, _canonical(authorization), request_json, request_sha,
            ),
        )
        db.execute("RELEASE SAVEPOINT tone_release_activate")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT tone_release_activate")
        db.execute("RELEASE SAVEPOINT tone_release_activate")
        raise
    return _row(db, activation_id)


def rollback_tone_release(
    db: sqlite3.Connection,
    *,
    activation_id: str,
    expected_previous_transition_id: str,
    registry_path: Path | str,
    request_path: Path | str,
) -> ToneReleaseActivation:
    current = _row(db, activation_id)
    request = _load_object(Path(request_path), "rollback request")
    if (
        set(request) != ROLLBACK_REQUEST_FIELDS
        or request.get("schema_version") != ROLLBACK_SCHEMA
        or request.get("source") != "human"
        or request.get("model_assistance") is not False
    ):
        raise ToneReleaseActivationError("invalid tone rollback request")
    for field in ("request_id", "operator_id", "reason"):
        _text(request.get(field), field)
    recorded_at = _time(request.get("recorded_at"), "recorded_at")
    current_transition = db.execute(
        "SELECT occurred_at FROM tone_release_activation_transitions WHERE id=?",
        (current.transition_id,),
    ).fetchone()
    if (
        current_transition is None
        or parse_utc(recorded_at) < parse_utc(current_transition["occurred_at"])
    ):
        raise ToneReleaseActivationError("tone rollback predates the active transition")
    registry, authorization, registry_sha = _authorized_operator(
        Path(registry_path), actor_id=request["operator_id"],
        required_scope=ROLLBACK_SCOPE, occurred_at=recorded_at,
    )
    expected = {
        "activation_id": activation_id,
        "expected_previous_transition_id": expected_previous_transition_id,
        "registry_version": registry["registry_version"],
        "registry_sha256": registry_sha,
    }
    if any(request.get(field) != value for field, value in expected.items()):
        raise ToneReleaseActivationError("tone rollback request binding differs")
    request_json = _canonical(request)
    request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    transition_id = f"tone-activation-transition-{_digest({'activation_id': activation_id, 'version': 2, 'request_sha256': request_sha})[:24]}"
    if current.state == "rolled_back":
        existing = db.execute(
            """SELECT request_sha256 FROM tone_release_activation_transitions
               WHERE id=? AND activation_id=?""",
            (transition_id, activation_id),
        ).fetchone()
        if existing is not None and existing["request_sha256"] == request_sha:
            return current
        raise ToneReleaseActivationError("tone release activation is already rolled back")
    if current.state != "active" or current.transition_id != expected_previous_transition_id:
        raise ToneReleaseActivationError("tone release activation changed; refresh before rollback")
    try:
        db.execute(
            """INSERT INTO tone_release_activation_transitions(
                   id,activation_id,version,previous_transition_id,from_state,to_state,
                   actor,reason,occurred_at,registry_version,registry_sha256,
                   authorization_json,request_json,request_sha256)
               VALUES(?,?,2,?,'active','rolled_back',?,?,?,?,?,?,?,?)""",
            (
                transition_id, activation_id, current.transition_id,
                request["operator_id"], request["reason"].strip(), recorded_at,
                registry["registry_version"], registry_sha,
                _canonical(authorization), request_json, request_sha,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ToneReleaseActivationError(
            "tone release activation changed; refresh before rollback"
        ) from exc
    return _row(db, activation_id)


__all__ = [
    "ToneReleaseActivation", "ToneReleaseActivationError",
    "activate_tone_release", "rollback_tone_release",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Control an evaluated tone production activation"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--database", required=True, type=Path)
    activate_parser.add_argument("--rollout-id", required=True)
    activate_parser.add_argument("--candidate-run", required=True, type=Path)
    activate_parser.add_argument("--profile", required=True, type=Path)
    activate_parser.add_argument("--registry", required=True, type=Path)
    activate_parser.add_argument("--request", required=True, type=Path)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--database", required=True, type=Path)
    rollback_parser.add_argument("--activation-id", required=True)
    rollback_parser.add_argument("--expected-previous-transition-id", required=True)
    rollback_parser.add_argument("--registry", required=True, type=Path)
    rollback_parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    from . import db_admin
    from .database import get_db

    db_admin.verify_database(args.database, require_current=True)
    with get_db(args.database) as db:
        if args.command == "activate":
            result = activate_tone_release(
                db, rollout_id=args.rollout_id, candidate_run_path=args.candidate_run,
                profile_path=args.profile, registry_path=args.registry,
                request_path=args.request,
            )
        else:
            result = rollback_tone_release(
                db, activation_id=args.activation_id,
                expected_previous_transition_id=args.expected_previous_transition_id,
                registry_path=args.registry, request_path=args.request,
            )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
