from __future__ import annotations

"""Evaluate multiclass tone confidence calibration on a frozen held-out run."""

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_cases, _load_json
from .tone_contracts import POLARITIES
from .tone_evaluation_metrics import ABSTAIN, evaluate_tone_classification

INPUT_SCHEMA = "tone-calibration-input-v1"
REPORT_VERSION = "tone-calibration-report-v1"
MIN_HELDOUT = 200
MAX_ECE = 0.10


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_confidence: float | None
    accuracy: float | None
    calibration_gap: float | None


@dataclass(frozen=True)
class ToneCalibrationReport:
    report_version: str
    dataset_version: str
    prediction_run_id: str
    calibration_evaluation_id: str
    split: str
    total: int
    scored: int
    abstained_or_missing: int
    probability_coverage: float
    multiclass_brier_score: float | None
    expected_calibration_error: float | None
    reliability_bins: tuple[ReliabilityBin, ...]
    checks: dict[str, bool]
    calibration_claim_allowed: bool
    calibration_admission_ready: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _local_file(parent: Path, value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or value in {".", ".."}
        or "\\" in value
    ):
        raise EvaluationDatasetError("tone calibration probabilities_file must be local")
    return parent.with_name(value)


def evaluate_tone_calibration(
    dataset_path: Path | str,
    prediction_run_path: Path | str,
    calibration_input_path: Path | str,
) -> ToneCalibrationReport:
    dataset_root = Path(dataset_path)
    run_path = Path(prediction_run_path)
    input_path = Path(calibration_input_path)
    metrics = evaluate_tone_classification(dataset_root, run_path)
    run = _load_json(run_path)
    record = _load_json(input_path)
    fields = {
        "schema_version", "calibration_evaluation_id", "prediction_run_id",
        "dataset_version", "split", "generated_at", "bin_count",
        "prediction_run_sha256", "predictions_sha256", "dataset_manifest_sha256",
        "dataset_cases_sha256", "probabilities_file", "probabilities_sha256",
    }
    if not isinstance(record, dict) or set(record) != fields or record.get(
        "schema_version"
    ) != INPUT_SCHEMA:
        raise EvaluationDatasetError("unsupported or invalid tone calibration input")
    expected = {
        "prediction_run_id": metrics.prediction_run_id,
        "dataset_version": metrics.dataset_version,
        "split": metrics.split,
        "bin_count": 10,
        "prediction_run_sha256": _sha(run_path),
        "predictions_sha256": run["predictions_sha256"],
        "dataset_manifest_sha256": _sha(dataset_root / "manifest.json"),
        "dataset_cases_sha256": _sha(dataset_root / "cases.jsonl"),
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise EvaluationDatasetError("tone calibration input differs from frozen run or dataset")
    evaluation_id = record.get("calibration_evaluation_id")
    if not isinstance(evaluation_id, str) or not evaluation_id.strip():
        raise EvaluationDatasetError("tone calibration input requires calibration_evaluation_id")
    try:
        generated = datetime.fromisoformat(str(record.get("generated_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError("tone calibration input has invalid generated_at") from exc
    if generated.tzinfo is None:
        raise EvaluationDatasetError("tone calibration input generated_at requires timezone")
    probabilities_path = _local_file(input_path, record.get("probabilities_file"))
    if record.get("probabilities_sha256") != _sha(probabilities_path):
        raise EvaluationDatasetError("tone calibration probabilities_sha256 mismatch")

    selected = {
        case["case_id"]: case
        for case in _load_cases(dataset_root / "cases.jsonl")
        if case["split"] == metrics.split
    }
    prediction_path = run_path.with_name(run["predictions_file"])
    predictions = {row["case_id"]: row for row in _load_cases(prediction_path)}
    probability_rows = _load_cases(probabilities_path)
    by_id: dict[str, dict] = {}
    labels = tuple(sorted(POLARITIES))
    for row in probability_rows:
        if set(row) != {"case_id", "content_sha256", "probabilities"}:
            raise EvaluationDatasetError("tone calibration probability row has invalid fields")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or case_id in by_id or case_id not in selected:
            raise EvaluationDatasetError("tone calibration case is duplicate, unknown or in another split")
        if row.get("content_sha256") != selected[case_id]["content_sha256"]:
            raise EvaluationDatasetError(f"tone calibration case {case_id} content hash differs")
        prediction = predictions.get(case_id)
        probabilities = row.get("probabilities")
        if prediction is None or prediction["predicted_polarity"] == ABSTAIN:
            if probabilities is not None:
                raise EvaluationDatasetError(
                    f"tone calibration case {case_id} cannot score an abstained or missing prediction"
                )
        else:
            if not isinstance(probabilities, dict) or set(probabilities) != set(labels):
                raise EvaluationDatasetError(f"tone calibration case {case_id} requires all probabilities")
            values = list(probabilities.values())
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
                for value in values
            ) or not math.isclose(sum(values), 1.0, rel_tol=0.0, abs_tol=1e-9):
                raise EvaluationDatasetError(f"tone calibration case {case_id} probabilities are invalid")
            maximum = max(values)
            winners = [label for label in labels if probabilities[label] == maximum]
            if winners != [prediction["predicted_polarity"]]:
                raise EvaluationDatasetError(f"tone calibration case {case_id} argmax differs from prediction")
            confidence = prediction["raw_confidence"]
            if confidence is None or not math.isclose(confidence, maximum, rel_tol=0.0, abs_tol=1e-12):
                raise EvaluationDatasetError(f"tone calibration case {case_id} confidence differs from probabilities")
        by_id[case_id] = row
    if set(by_id) != set(selected):
        raise EvaluationDatasetError("tone calibration probabilities must cover every split case")

    scored: list[tuple[float, bool, float]] = []
    for case_id, case in selected.items():
        probabilities = by_id[case_id]["probabilities"]
        if probabilities is None:
            continue
        gold = case["annotation"]["labels"]["tone"]["polarity"]
        predicted = predictions[case_id]["predicted_polarity"]
        confidence = probabilities[predicted]
        brier = sum(
            (probabilities[label] - (1.0 if label == gold else 0.0)) ** 2
            for label in labels
        )
        scored.append((confidence, predicted == gold, brier))
    bins = []
    weighted_gap = 0.0
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        members = [
            item for item in scored
            if lower <= item[0] < upper or (index == 9 and item[0] == 1.0)
        ]
        if members:
            mean_confidence = sum(item[0] for item in members) / len(members)
            accuracy = sum(item[1] for item in members) / len(members)
            gap = abs(accuracy - mean_confidence)
            weighted_gap += len(members) * gap
        else:
            mean_confidence = accuracy = gap = None
        bins.append(ReliabilityBin(lower, upper, len(members), mean_confidence, accuracy, gap))
    total = len(selected)
    scored_count = len(scored)
    brier_score = sum(item[2] for item in scored) / scored_count if scored else None
    ece = weighted_gap / scored_count if scored else None
    checks = {
        "blind_test_split": metrics.split == "test",
        "publishable_gold_and_quality_run": metrics.quality_claim_allowed,
        "heldout_support_at_least_200": total >= MIN_HELDOUT,
        "complete_probability_coverage": scored_count == total,
        "ece_at_most_0_10": ece is not None and ece <= MAX_ECE,
    }
    claim_allowed = all(
        checks[key]
        for key in (
            "blind_test_split",
            "publishable_gold_and_quality_run",
            "heldout_support_at_least_200",
            "complete_probability_coverage",
        )
    )
    warnings = []
    if not claim_allowed:
        warnings.append("calibration claim blocked: requires at least 200 complete publishable blind-test cases")
    if not checks["ece_at_most_0_10"]:
        warnings.append("ECE admission threshold is not met")
    warnings.append("this report evaluates raw probabilities; it does not create a calibration mapping")
    return ToneCalibrationReport(
        REPORT_VERSION,
        metrics.dataset_version,
        metrics.prediction_run_id,
        evaluation_id,
        metrics.split,
        total,
        scored_count,
        total - scored_count,
        scored_count / total,
        brier_score,
        ece,
        tuple(bins),
        checks,
        claim_allowed,
        claim_allowed and checks["ece_at_most_0_10"],
        tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate frozen tone probability calibration")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--calibration-input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_tone_calibration(args.dataset, args.run, args.calibration_input)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if report.calibration_admission_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
