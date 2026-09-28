from __future__ import annotations

"""Fit versioned temperature scaling on dev and evaluate it once on test."""

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_cases, _load_json
from .tone_calibration_evaluation import (
    MAX_ECE,
    MIN_HELDOUT,
    ToneCalibrationReport,
    evaluate_tone_calibration,
)
from .tone_contracts import POLARITIES
from .tone_evaluation import validate_tone_evaluation_dataset

REPORT_VERSION = "tone-temperature-calibration-v1"
ALGORITHM = "multiclass-temperature-scaling-v1"
MIN_TEMPERATURE = 0.05
MAX_TEMPERATURE = 20.0
OPTIMIZATION_STEPS = 96
PROBABILITY_FLOOR = 1e-15


@dataclass(frozen=True)
class CalibrationMeasurements:
    fit_cases: int
    test_cases: int
    temperature: float
    dev_nll_before: float
    dev_nll_after: float
    test_brier_before: float
    test_brier_after: float
    test_ece_before: float
    test_ece_after: float


@dataclass(frozen=True)
class TemperatureCalibrationReport:
    report_version: str
    algorithm: str
    calibration_version: str
    dataset_version: str
    method_id: str
    method_version: str
    method_config_sha256: str
    fit_prediction_run_id: str
    test_prediction_run_id: str
    fit_input_sha256: str
    test_input_sha256: str
    measurements: CalibrationMeasurements
    reliability_bins: tuple[dict[str, float | int | None], ...]
    checks: dict[str, bool]
    admission_ready: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_validated_probabilities(
    dataset_root: Path,
    run_path: Path,
    input_path: Path,
) -> tuple[ToneCalibrationReport, dict, list[tuple[str, dict[str, float]]]]:
    report = evaluate_tone_calibration(dataset_root, run_path, input_path)
    run = _load_json(run_path)
    calibration_input = _load_json(input_path)
    probability_path = input_path.with_name(calibration_input["probabilities_file"])
    probability_by_id = {
        row["case_id"]: row["probabilities"] for row in _load_cases(probability_path)
    }
    rows = []
    for case in _load_cases(dataset_root / "cases.jsonl"):
        if case["split"] != report.split:
            continue
        probabilities = probability_by_id[case["case_id"]]
        if probabilities is None:
            raise EvaluationDatasetError(
                f"temperature calibration requires complete probabilities for {report.split}"
            )
        rows.append((case["annotation"]["labels"]["tone"]["polarity"], probabilities))
    return report, run, rows


def _scaled(probabilities: dict[str, float], temperature: float) -> dict[str, float]:
    weights = {
        label: math.exp(math.log(max(probabilities[label], PROBABILITY_FLOOR)) / temperature)
        for label in POLARITIES
    }
    denominator = sum(weights.values())
    return {label: weights[label] / denominator for label in sorted(POLARITIES)}


def _nll(rows: list[tuple[str, dict[str, float]]], temperature: float) -> float:
    return -sum(
        math.log(max(_scaled(probabilities, temperature)[gold], PROBABILITY_FLOOR))
        for gold, probabilities in rows
    ) / len(rows)


def _fit_temperature(rows: list[tuple[str, dict[str, float]]]) -> float:
    left = math.log(MIN_TEMPERATURE)
    right = math.log(MAX_TEMPERATURE)
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    x1 = right - ratio * (right - left)
    x2 = left + ratio * (right - left)
    f1 = _nll(rows, math.exp(x1))
    f2 = _nll(rows, math.exp(x2))
    for _ in range(OPTIMIZATION_STEPS):
        if f1 <= f2:
            right, x2, f2 = x2, x1, f1
            x1 = right - ratio * (right - left)
            f1 = _nll(rows, math.exp(x1))
        else:
            left, x1, f1 = x1, x2, f2
            x2 = left + ratio * (right - left)
            f2 = _nll(rows, math.exp(x2))
    return math.exp((left + right) / 2.0)


def _calibration_metrics(
    rows: list[tuple[str, dict[str, float]]], temperature: float
) -> tuple[float, float, tuple[dict[str, float | int | None], ...]]:
    observations = []
    for gold, probabilities in rows:
        calibrated = _scaled(probabilities, temperature)
        predicted = max(sorted(POLARITIES), key=lambda label: calibrated[label])
        confidence = calibrated[predicted]
        brier = sum(
            (calibrated[label] - (1.0 if label == gold else 0.0)) ** 2
            for label in POLARITIES
        )
        observations.append((confidence, predicted == gold, brier))
    bins = []
    weighted_gap = 0.0
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        members = [
            item for item in observations
            if lower <= item[0] < upper or (index == 9 and item[0] == 1.0)
        ]
        if members:
            mean_confidence = sum(item[0] for item in members) / len(members)
            accuracy = sum(item[1] for item in members) / len(members)
            gap = abs(accuracy - mean_confidence)
            weighted_gap += len(members) * gap
        else:
            mean_confidence = accuracy = gap = None
        bins.append({
            "lower": lower,
            "upper": upper,
            "count": len(members),
            "mean_confidence": mean_confidence,
            "accuracy": accuracy,
            "calibration_gap": gap,
        })
    return (
        sum(item[2] for item in observations) / len(observations),
        weighted_gap / len(observations),
        tuple(bins),
    )


def fit_and_evaluate_temperature_calibration(
    dataset_path: Path | str,
    fit_run_path: Path | str,
    fit_input_path: Path | str,
    test_run_path: Path | str,
    test_input_path: Path | str,
) -> TemperatureCalibrationReport:
    root = Path(dataset_path)
    fit_run_path = Path(fit_run_path)
    fit_input_path = Path(fit_input_path)
    test_run_path = Path(test_run_path)
    test_input_path = Path(test_input_path)
    dataset = validate_tone_evaluation_dataset(root)
    fit_report, fit_run, fit_rows = _load_validated_probabilities(
        root, fit_run_path, fit_input_path
    )
    test_report, test_run, test_rows = _load_validated_probabilities(
        root, test_run_path, test_input_path
    )
    if fit_report.split != "dev" or test_report.split != "test":
        raise EvaluationDatasetError("temperature calibration requires dev fit and independent test")
    identity_fields = ("dataset_version", "method_id", "method_version", "method_config_sha256")
    if any(fit_run[field] != test_run[field] for field in identity_fields):
        raise EvaluationDatasetError("temperature calibration run identity differs between dev and test")
    if fit_run["prediction_run_id"] == test_run["prediction_run_id"]:
        raise EvaluationDatasetError("temperature calibration requires distinct dev and test runs")

    temperature = _fit_temperature(fit_rows)
    dev_nll_before = _nll(fit_rows, 1.0)
    dev_nll_after = _nll(fit_rows, temperature)
    test_brier_after, test_ece_after, bins = _calibration_metrics(test_rows, temperature)
    if test_report.multiclass_brier_score is None or test_report.expected_calibration_error is None:
        raise EvaluationDatasetError("temperature calibration requires scored test probabilities")
    version_payload = {
        "algorithm": ALGORITHM,
        "dataset_version": dataset.dataset_version,
        "method_id": fit_run["method_id"],
        "method_version": fit_run["method_version"],
        "method_config_sha256": fit_run["method_config_sha256"],
        "fit_prediction_run_sha256": _sha(fit_run_path),
        "fit_input_sha256": _sha(fit_input_path),
        "temperature": format(temperature, ".17g"),
    }
    version_hash = hashlib.sha256(
        json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    checks = {
        "publishable_private_gold": dataset.publishable_tone_gold,
        "fit_split_is_dev": fit_report.split == "dev",
        "test_split_is_independent_test": test_report.split == "test",
        "fit_support_at_least_200": fit_report.total >= MIN_HELDOUT,
        "test_support_at_least_200": test_report.total >= MIN_HELDOUT,
        "complete_fit_probability_coverage": fit_report.scored == fit_report.total,
        "complete_test_probability_coverage": test_report.scored == test_report.total,
        "test_quality_claim_allowed": test_report.checks["publishable_gold_and_quality_run"],
        "fit_nll_not_worse": dev_nll_after <= dev_nll_before + 1e-12,
        "test_brier_not_worse": test_brier_after <= test_report.multiclass_brier_score + 1e-12,
        "test_ece_not_worse": test_ece_after <= test_report.expected_calibration_error + 1e-12,
        "test_ece_at_most_0_10": test_ece_after <= MAX_ECE,
    }
    admission_ready = all(checks.values())
    warnings = []
    if not admission_ready:
        warnings.append("calibration admission remains blocked until every dev/test and quality check passes")
    warnings.append("test gold evaluates the frozen mapping only and is never used to fit temperature")
    warnings.append("this report does not enable calibrated confidence or change a publication pointer")
    return TemperatureCalibrationReport(
        REPORT_VERSION,
        ALGORITHM,
        f"tone-temperature-{version_hash[:20]}",
        dataset.dataset_version,
        fit_run["method_id"],
        fit_run["method_version"],
        fit_run["method_config_sha256"],
        fit_run["prediction_run_id"],
        test_run["prediction_run_id"],
        _sha(fit_input_path),
        _sha(test_input_path),
        CalibrationMeasurements(
            len(fit_rows),
            len(test_rows),
            temperature,
            dev_nll_before,
            dev_nll_after,
            test_report.multiclass_brier_score,
            test_brier_after,
            test_report.expected_calibration_error,
            test_ece_after,
        ),
        bins,
        checks,
        admission_ready,
        tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit dev-only tone temperature and evaluate on test")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--fit-run", required=True, type=Path)
    parser.add_argument("--fit-input", required=True, type=Path)
    parser.add_argument("--test-run", required=True, type=Path)
    parser.add_argument("--test-input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = fit_and_evaluate_temperature_calibration(
        args.dataset, args.fit_run, args.fit_input, args.test_run, args.test_input
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if report.admission_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
