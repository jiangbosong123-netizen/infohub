from __future__ import annotations

"""Recompute and bind all tone release evidence without publishing it."""

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationDatasetError
from .tone_evaluation import ToneEvaluationReport, validate_tone_evaluation_dataset
from .tone_model_comparison import ToneModelComparison, compare_tone_runs
from .tone_operational_evaluation import ToneOperationalReport, evaluate_tone_operations
from .tone_review_agreement import ToneAgreementReport, measure_tone_agreement
from .tone_security_gate import ToneSecurityDecision, evaluate_tone_security
from .tone_temperature_calibration import (
    TemperatureCalibrationReport,
    fit_and_evaluate_temperature_calibration,
)

BUNDLE_VERSION = "tone-release-evidence-bundle-v1"
ARTIFACT_KEYS = {
    "dataset_manifest",
    "dataset_cases",
    "baseline_test_run",
    "candidate_dev_run",
    "candidate_dev_calibration_input",
    "candidate_test_run",
    "candidate_test_calibration_input",
    "candidate_test_operations",
    "candidate_security_run",
}


@dataclass(frozen=True)
class ToneReleaseEvidenceBundle:
    bundle_version: str
    bundle_id: str
    dataset_version: str
    baseline_test_run_id: str
    candidate_dev_run_id: str
    candidate_test_run_id: str
    candidate_security_run_id: str
    calibration_version: str
    operational_run_id: str
    reviewer_a: str
    reviewer_b: str
    artifact_sha256: dict[str, str]
    checks: dict[str, bool]
    measurements: dict[str, float | int | str | None]
    evidence_ready_for_human_review: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assess_tone_release_evidence(
    dataset: ToneEvaluationReport,
    agreement: ToneAgreementReport,
    comparison: ToneModelComparison,
    calibration: TemperatureCalibrationReport,
    operations: ToneOperationalReport,
    security: ToneSecurityDecision,
    *,
    artifact_sha256: dict[str, str],
) -> ToneReleaseEvidenceBundle:
    if set(artifact_sha256) != ARTIFACT_KEYS or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in artifact_sha256.values()
    ):
        raise EvaluationDatasetError("tone release evidence requires exact artifact hashes")
    versions = {
        dataset.dataset_version,
        agreement.dataset_version,
        comparison.dataset_version,
        calibration.dataset_version,
        operations.dataset_version,
        security.dataset_version,
    }
    if len(versions) != 1:
        raise EvaluationDatasetError("tone release evidence dataset versions differ")
    candidate_ids = {
        comparison.candidate_run_id,
        calibration.test_prediction_run_id,
        operations.prediction_run_id,
        security.test_prediction_run_id,
    }
    if len(candidate_ids) != 1:
        raise EvaluationDatasetError("tone release evidence candidate test runs differ")
    test_cases = comparison.measurements.get("cases")
    if (
        isinstance(test_cases, bool)
        or not isinstance(test_cases, int)
        or test_cases <= 0
        or agreement.dataset_cases != test_cases
        or agreement.paired_cases != test_cases
        or calibration.measurements.test_cases != test_cases
        or operations.cases != test_cases
    ):
        raise EvaluationDatasetError("tone release evidence test denominators differ")
    checks = {
        "publishable_private_gold": dataset.publishable_tone_gold,
        "test_reviewer_pair_complete": (
            agreement.scope_split == "test"
            and agreement.paired_cases == agreement.dataset_cases
            and agreement.paired_by_split.get("test") == agreement.dataset_cases
        ),
        "review_agreement_gate_eligible": agreement.quality_gate_eligible,
        "polarity_kappa_at_least_0_70": agreement.polarity_kappa_passed,
        "candidate_model_comparison_passed": comparison.candidate_gate_passed,
        "candidate_has_no_unreviewed_regression": not comparison.requires_regression_review,
        "temperature_calibration_admitted": calibration.admission_ready,
        "operational_readiness_admitted": operations.admission_ready,
        "security_gate_passed": security.passed,
    }
    ready = all(checks.values())
    measurements: dict[str, float | int | str | None] = {
        "test_cases": test_cases,
        "security_cases": security.security_cases,
        "polarity_cohen_kappa": agreement.polarity_cohen_kappa,
        "candidate_macro_f1": comparison.measurements.get("candidate_macro_f1"),
        "candidate_unknown_recall": comparison.measurements.get("candidate_unknown_recall"),
        "calibrated_test_ece": calibration.measurements.test_ece_after,
        "calibrated_test_brier": calibration.measurements.test_brier_after,
        "first_attempt_schema_rate": operations.first_attempt_schema_rate,
        "final_schema_rate": operations.final_schema_rate,
        "cost_microusd_per_100_cases": operations.cost_microusd_per_100_cases,
        "latency_ms_p95": operations.latency_ms_p95,
        "policy_id": operations.policy_id,
    }
    identity = {
        "bundle_version": BUNDLE_VERSION,
        "dataset_version": dataset.dataset_version,
        "baseline_test_run_id": comparison.baseline_run_id,
        "candidate_dev_run_id": calibration.fit_prediction_run_id,
        "candidate_test_run_id": comparison.candidate_run_id,
        "candidate_security_run_id": security.security_prediction_run_id,
        "calibration_version": calibration.calibration_version,
        "operational_run_id": operations.operational_run_id,
        "reviewer_a": agreement.reviewer_a,
        "reviewer_b": agreement.reviewer_b,
        "artifact_sha256": artifact_sha256,
        "checks": checks,
        "measurements": measurements,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    warnings = []
    if not ready:
        warnings.append("tone evidence bundle is incomplete; valid publication remains blocked")
    warnings.append("evidence readiness is not approval; a separate authorized human decision is required")
    warnings.append("this bundle does not write analysis results or move a publication pointer")
    return ToneReleaseEvidenceBundle(
        BUNDLE_VERSION,
        f"tone-release-evidence-{digest[:24]}",
        dataset.dataset_version,
        comparison.baseline_run_id,
        calibration.fit_prediction_run_id,
        comparison.candidate_run_id,
        security.security_prediction_run_id,
        calibration.calibration_version,
        operations.operational_run_id,
        agreement.reviewer_a,
        agreement.reviewer_b,
        dict(sorted(artifact_sha256.items())),
        checks,
        measurements,
        ready,
        tuple(warnings),
    )


def build_tone_release_evidence(
    dataset_path: Path | str,
    baseline_test_run_path: Path | str,
    candidate_dev_run_path: Path | str,
    candidate_dev_calibration_input_path: Path | str,
    candidate_test_run_path: Path | str,
    candidate_test_calibration_input_path: Path | str,
    candidate_test_operations_path: Path | str,
    candidate_security_run_path: Path | str,
    *,
    reviewer_a: str,
    reviewer_b: str,
) -> ToneReleaseEvidenceBundle:
    root = Path(dataset_path)
    paths = {
        "baseline_test_run": Path(baseline_test_run_path),
        "candidate_dev_run": Path(candidate_dev_run_path),
        "candidate_dev_calibration_input": Path(candidate_dev_calibration_input_path),
        "candidate_test_run": Path(candidate_test_run_path),
        "candidate_test_calibration_input": Path(candidate_test_calibration_input_path),
        "candidate_test_operations": Path(candidate_test_operations_path),
        "candidate_security_run": Path(candidate_security_run_path),
    }
    dataset = validate_tone_evaluation_dataset(root)
    agreement = measure_tone_agreement(
        root, reviewer_a=reviewer_a, reviewer_b=reviewer_b, split="test"
    )
    comparison = compare_tone_runs(
        root, paths["baseline_test_run"], paths["candidate_test_run"]
    )
    calibration = fit_and_evaluate_temperature_calibration(
        root,
        paths["candidate_dev_run"],
        paths["candidate_dev_calibration_input"],
        paths["candidate_test_run"],
        paths["candidate_test_calibration_input"],
    )
    operations = evaluate_tone_operations(
        root, paths["candidate_test_run"], paths["candidate_test_operations"]
    )
    security = evaluate_tone_security(
        root, paths["candidate_test_run"], paths["candidate_security_run"]
    )
    artifact_hashes = {
        "dataset_manifest": _sha(root / "manifest.json"),
        "dataset_cases": _sha(root / "cases.jsonl"),
        **{name: _sha(path) for name, path in paths.items()},
    }
    return assess_tone_release_evidence(
        dataset,
        agreement,
        comparison,
        calibration,
        operations,
        security,
        artifact_sha256=artifact_hashes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recompute a tone release evidence bundle")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--baseline-test-run", required=True, type=Path)
    parser.add_argument("--candidate-dev-run", required=True, type=Path)
    parser.add_argument("--candidate-dev-calibration-input", required=True, type=Path)
    parser.add_argument("--candidate-test-run", required=True, type=Path)
    parser.add_argument("--candidate-test-calibration-input", required=True, type=Path)
    parser.add_argument("--candidate-test-operations", required=True, type=Path)
    parser.add_argument("--candidate-security-run", required=True, type=Path)
    parser.add_argument("--reviewer-a", required=True)
    parser.add_argument("--reviewer-b", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    bundle = build_tone_release_evidence(
        args.dataset,
        args.baseline_test_run,
        args.candidate_dev_run,
        args.candidate_dev_calibration_input,
        args.candidate_test_run,
        args.candidate_test_calibration_input,
        args.candidate_test_operations,
        args.candidate_security_run,
        reviewer_a=args.reviewer_a,
        reviewer_b=args.reviewer_b,
    )
    payload = json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if bundle.evidence_ready_for_human_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
