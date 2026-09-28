from __future__ import annotations

"""Validate a human tone release decision against an authorized approver registry."""

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import EvaluationDatasetError
from .tone_release_admission import validate_tone_release_bundle

REGISTRY_SCHEMA = "tone-release-approver-registry-v1"
DECISION_SCHEMA = "tone-release-decision-v1"
RECORD_VERSION = "tone-release-decision-record-v1"
REQUIRED_SCOPE = "tone:release:approve"


@dataclass(frozen=True)
class ToneReleaseDecisionRecord:
    record_version: str
    record_id: str
    decision_id: str
    bundle_id: str
    bundle_sha256: str
    registry_version: str
    registry_sha256: str
    approver_id: str
    decision: str
    reason: str
    recorded_at: str
    policy_id: str
    approval_candidate_for_controlled_import: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvaluationDatasetError(f"tone release decision requires {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError(f"tone release decision has invalid {field}") from exc
    if parsed.tzinfo is None:
        raise EvaluationDatasetError(f"tone release decision {field} requires timezone")
    return parsed


def _load_registry(path: Path) -> tuple[dict, dict[str, dict]]:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDatasetError("cannot read tone release approver registry") from exc
    fields = {"schema_version", "registry_version", "generated_at", "approvers"}
    if (
        not isinstance(registry, dict)
        or set(registry) != fields
        or registry.get("schema_version") != REGISTRY_SCHEMA
        or not isinstance(registry.get("registry_version"), str)
        or not registry["registry_version"].strip()
        or not isinstance(registry.get("approvers"), list)
    ):
        raise EvaluationDatasetError("invalid tone release approver registry")
    _time(registry.get("generated_at"), "registry generated_at")
    by_id = {}
    approver_fields = {"approver_id", "status", "scopes", "valid_from", "valid_until"}
    for approver in registry["approvers"]:
        if not isinstance(approver, dict) or set(approver) != approver_fields:
            raise EvaluationDatasetError("invalid tone release approver entry")
        approver_id = approver.get("approver_id")
        scopes = approver.get("scopes")
        if (
            not isinstance(approver_id, str)
            or not approver_id.strip()
            or approver_id in by_id
            or approver.get("status") not in {"active", "revoked"}
            or not isinstance(scopes, list)
            or scopes != sorted(set(scopes))
            or any(not isinstance(scope, str) or not scope for scope in scopes)
        ):
            raise EvaluationDatasetError("invalid tone release approver entry")
        valid_from = _time(approver.get("valid_from"), "approver valid_from")
        valid_until = None
        if approver.get("valid_until") is not None:
            valid_until = _time(approver["valid_until"], "approver valid_until")
            if valid_until <= valid_from:
                raise EvaluationDatasetError("tone release approver validity window is invalid")
        by_id[approver_id] = approver
    return registry, by_id


def record_tone_release_decision(
    bundle_path: Path | str,
    registry_path: Path | str,
    decision_path: Path | str,
) -> ToneReleaseDecisionRecord:
    bundle_path = Path(bundle_path)
    registry_path = Path(registry_path)
    decision_path = Path(decision_path)
    try:
        bundle = validate_tone_release_bundle(bundle_path)
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDatasetError("cannot read tone release decision inputs") from exc
    registry, approvers = _load_registry(registry_path)
    fields = {
        "schema_version", "decision_id", "bundle_id", "bundle_sha256",
        "registry_version", "registry_sha256", "approver_id", "decision", "reason",
        "recorded_at", "source", "model_assistance",
        "blind_holdout_exclusion_acknowledged", "budget_policy_id",
        "rollback_plan_acknowledged",
    }
    if (
        not isinstance(decision, dict)
        or set(decision) != fields
        or decision.get("schema_version") != DECISION_SCHEMA
        or decision.get("source") != "human"
        or decision.get("model_assistance") is not False
        or decision.get("decision") not in {"approve", "reject"}
    ):
        raise EvaluationDatasetError("invalid tone release decision")
    for field in ("decision_id", "approver_id", "reason"):
        if not isinstance(decision.get(field), str) or not decision[field].strip():
            raise EvaluationDatasetError(f"tone release decision requires {field}")
    if len(decision["reason"].strip()) > 2_000:
        raise EvaluationDatasetError("tone release decision reason is too long")
    if (
        decision.get("bundle_id") != bundle["bundle_id"]
        or decision.get("bundle_sha256") != _sha(bundle_path)
    ):
        raise EvaluationDatasetError("tone release decision bundle binding differs")
    if (
        decision.get("registry_version") != registry["registry_version"]
        or decision.get("registry_sha256") != _sha(registry_path)
    ):
        raise EvaluationDatasetError("tone release decision registry binding differs")
    recorded = _time(decision.get("recorded_at"), "recorded_at")
    if _time(registry["generated_at"], "registry generated_at") > recorded:
        raise EvaluationDatasetError("tone release approver registry postdates the decision")
    approver = approvers.get(decision["approver_id"])
    if approver is None:
        raise EvaluationDatasetError("tone release decision approver is not registered")
    valid_from = _time(approver["valid_from"], "approver valid_from")
    valid_until = (
        _time(approver["valid_until"], "approver valid_until")
        if approver["valid_until"] is not None
        else None
    )
    if (
        approver["status"] != "active"
        or REQUIRED_SCOPE not in approver["scopes"]
        or recorded < valid_from
        or (valid_until is not None and recorded >= valid_until)
    ):
        raise EvaluationDatasetError("tone release decision approver is not authorized at recorded_at")
    if decision["approver_id"] in {bundle["reviewer_a"], bundle["reviewer_b"]}:
        raise EvaluationDatasetError("tone release approver must be independent from annotation reviewers")
    policy_id = bundle["measurements"].get("policy_id")
    if decision.get("budget_policy_id") != policy_id:
        raise EvaluationDatasetError("tone release decision budget policy differs")
    approving = decision["decision"] == "approve"
    if approving and not bundle["evidence_ready_for_human_review"]:
        raise EvaluationDatasetError("cannot approve incomplete tone release evidence")
    if approving and (
        decision.get("blind_holdout_exclusion_acknowledged") is not True
        or decision.get("rollback_plan_acknowledged") is not True
    ):
        raise EvaluationDatasetError("tone release approval requires holdout and rollback acknowledgements")
    identity = {
        "record_version": RECORD_VERSION,
        "decision_id": decision["decision_id"],
        "bundle_id": bundle["bundle_id"],
        "bundle_sha256": decision["bundle_sha256"],
        "registry_version": registry["registry_version"],
        "registry_sha256": decision["registry_sha256"],
        "approver_id": decision["approver_id"],
        "decision": decision["decision"],
        "reason": decision["reason"].strip(),
        "recorded_at": decision["recorded_at"],
        "policy_id": policy_id,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    warnings = (
        "controlled import must revalidate this record against the current append-only decision ledger",
        "this record alone does not write a publication or enable production",
    )
    return ToneReleaseDecisionRecord(
        RECORD_VERSION,
        f"tone-release-decision-{digest[:24]}",
        decision["decision_id"],
        bundle["bundle_id"],
        decision["bundle_sha256"],
        registry["registry_version"],
        decision["registry_sha256"],
        decision["approver_id"],
        decision["decision"],
        decision["reason"].strip(),
        decision["recorded_at"],
        policy_id,
        approving,
        warnings,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an authorized human tone release decision")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--decision", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    record = record_tone_release_decision(args.bundle, args.registry, args.decision)
    payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
