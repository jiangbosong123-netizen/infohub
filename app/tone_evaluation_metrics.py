from __future__ import annotations

"""Deterministic polarity metrics for frozen tone prediction runs."""

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_cases, _load_json
from .evaluation_metrics import ClassMetrics, _wilson
from .tone_contracts import POLARITIES
from .tone_evaluation import (
    TONE_EVALUATION_CONTRACT,
    TONE_OUTPUT_SCHEMA,
    TONE_VOCABULARY_VERSION,
    validate_tone_evaluation_dataset,
)

RUN_SCHEMA = "tone-prediction-run-v1"
METRICS_VERSION = "tone-classification-metrics-v1"
ABSTAIN = "__abstain__"
EVALUATION_SPLITS = {"dev", "test", "security"}


@dataclass(frozen=True)
class ToneSliceMetrics:
    total: int
    covered: int
    correct: int
    accuracy: float
    macro_f1: float
    unknown_support: int
    unknown_recall: float | None
    low_support: bool


@dataclass(frozen=True)
class ToneClassificationReport:
    metrics_version: str
    dataset_version: str
    prediction_run_id: str
    split: str
    total: int
    correct: int
    accuracy: float
    accuracy_wilson_95: tuple[float, float]
    covered: int
    coverage: float
    abstained: int
    missing_predictions: int
    macro_f1: float
    unknown_recall: float | None
    per_class: tuple[ClassMetrics, ...]
    confusion: dict[str, dict[str, int]]
    slices: dict[str, dict[str, ToneSliceMetrics]]
    quality_claim_allowed: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prediction_path(run_path: Path, value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or value in {".", ".."}
        or "\\" in value
    ):
        raise EvaluationDatasetError("tone predictions_file must be a local filename")
    return run_path.with_name(value)


def _validate_run(run: object, run_path: Path, dataset_root: Path, dataset_version: str) -> Path:
    fields = {
        "schema_version", "prediction_run_id", "dataset_version", "task",
        "task_contract", "output_schema_version", "vocabulary_version", "split",
        "method_id", "method_version", "method_config_sha256", "generated_at",
        "predictions_file", "dataset_manifest_sha256", "dataset_cases_sha256",
        "predictions_sha256", "abstain_label",
    }
    if not isinstance(run, dict) or set(run) != fields or run.get("schema_version") != RUN_SCHEMA:
        raise EvaluationDatasetError("unsupported or invalid tone prediction run manifest")
    expected = {
        "dataset_version": dataset_version,
        "task": "tone.polarity",
        "task_contract": TONE_EVALUATION_CONTRACT,
        "output_schema_version": TONE_OUTPUT_SCHEMA,
        "vocabulary_version": TONE_VOCABULARY_VERSION,
        "abstain_label": ABSTAIN,
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise EvaluationDatasetError("tone prediction run contract differs from dataset")
    if run.get("split") not in EVALUATION_SPLITS:
        raise EvaluationDatasetError("tone prediction run requires dev, test or security split")
    for key in ("prediction_run_id", "method_id", "method_version"):
        if not isinstance(run.get(key), str) or not run[key].strip():
            raise EvaluationDatasetError(f"tone prediction run requires {key}")
    try:
        generated = datetime.fromisoformat(str(run.get("generated_at", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError("tone prediction run has invalid generated_at") from exc
    if generated.tzinfo is None:
        raise EvaluationDatasetError("tone prediction run generated_at requires timezone")
    config_hash = run.get("method_config_sha256")
    if not isinstance(config_hash, str) or len(config_hash) != 64:
        raise EvaluationDatasetError("tone prediction run requires method_config_sha256")
    try:
        int(config_hash, 16)
    except ValueError as exc:
        raise EvaluationDatasetError("tone prediction run has invalid method_config_sha256") from exc
    prediction_path = _prediction_path(run_path, run["predictions_file"])
    expected_hashes = {
        "dataset_manifest_sha256": _sha(dataset_root / "manifest.json"),
        "dataset_cases_sha256": _sha(dataset_root / "cases.jsonl"),
        "predictions_sha256": _sha(prediction_path),
    }
    if any(run.get(key) != value for key, value in expected_hashes.items()):
        raise EvaluationDatasetError("tone prediction run hash binding differs")
    return prediction_path


def _score(actual: list[str], predicted: list[str]) -> tuple[
    tuple[ClassMetrics, ...], dict[str, dict[str, int]], float, float | None
]:
    gold_labels = tuple(sorted(POLARITIES))
    columns = (*gold_labels, ABSTAIN)
    confusion = {actual_label: {label: 0 for label in columns} for actual_label in gold_labels}
    for actual_label, predicted_label in zip(actual, predicted):
        confusion[actual_label][predicted_label] += 1
    per_class = []
    for label in gold_labels:
        true_positive = confusion[label][label]
        support = sum(confusion[label].values())
        predicted_count = sum(confusion[gold][label] for gold in gold_labels)
        precision = true_positive / predicted_count if predicted_count else None
        recall = true_positive / support if support else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else (0.0 if support else None)
        )
        per_class.append(
            ClassMetrics(label, support, predicted_count, true_positive, precision, recall, f1)
        )
    macro_f1 = sum(item.f1 or 0.0 for item in per_class) / len(per_class)
    unknown = next(item for item in per_class if item.label == "unknown")
    return tuple(per_class), confusion, macro_f1, unknown.recall


def _slice(cases: list[dict], predicted_by_id: dict[str, str]) -> ToneSliceMetrics:
    actual = [case["annotation"]["labels"]["tone"]["polarity"] for case in cases]
    predicted = [predicted_by_id[case["case_id"]] for case in cases]
    per_class, _, macro_f1, unknown_recall = _score(actual, predicted)
    covered = sum(label != ABSTAIN for label in predicted)
    correct = sum(a == p for a, p in zip(actual, predicted))
    unknown_support = next(item.support for item in per_class if item.label == "unknown")
    return ToneSliceMetrics(
        total=len(cases),
        covered=covered,
        correct=correct,
        accuracy=correct / len(cases),
        macro_f1=macro_f1,
        unknown_support=unknown_support,
        unknown_recall=unknown_recall,
        low_support=len(cases) < 30,
    )


def evaluate_tone_classification(
    dataset_path: Path | str, prediction_run_path: Path | str
) -> ToneClassificationReport:
    root = Path(dataset_path)
    dataset = validate_tone_evaluation_dataset(root)
    run_path = Path(prediction_run_path)
    run = _load_json(run_path)
    prediction_path = _validate_run(run, run_path, root, dataset.dataset_version)
    selected = [case for case in _load_cases(root / "cases.jsonl") if case["split"] == run["split"]]
    if not selected:
        raise EvaluationDatasetError(f"tone dataset has no {run['split']} cases")
    selected_by_id = {case["case_id"]: case for case in selected}
    by_id: dict[str, str] = {}
    for row in _load_cases(prediction_path):
        if set(row) != {"case_id", "content_sha256", "predicted_polarity", "raw_confidence"}:
            raise EvaluationDatasetError("tone prediction row has invalid fields")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or case_id in by_id or case_id not in selected_by_id:
            raise EvaluationDatasetError("tone prediction case is duplicate, unknown or in another split")
        if row.get("content_sha256") != selected_by_id[case_id]["content_sha256"]:
            raise EvaluationDatasetError(f"tone prediction case {case_id} content hash differs")
        predicted = row.get("predicted_polarity")
        if predicted not in POLARITIES | {ABSTAIN}:
            raise EvaluationDatasetError(f"tone prediction case {case_id} polarity is invalid")
        confidence = row.get("raw_confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise EvaluationDatasetError(f"tone prediction case {case_id} confidence is invalid")
        by_id[case_id] = predicted
    missing = len(selected_by_id) - len(by_id)
    predicted_by_id = {case_id: by_id.get(case_id, ABSTAIN) for case_id in selected_by_id}
    actual = [case["annotation"]["labels"]["tone"]["polarity"] for case in selected]
    predicted = [predicted_by_id[case["case_id"]] for case in selected]
    per_class, confusion, macro_f1, unknown_recall = _score(actual, predicted)

    slices: dict[str, dict[str, ToneSliceMetrics]] = {}
    for dimension, values in (
        ("language", sorted({case["language"] for case in selected})),
        ("source_kind", sorted({case["source_kind"] for case in selected})),
        (
            "phenomenon",
            sorted({item for case in selected for item in case["annotation"]["labels"]["tone"]["phenomena"]}),
        ),
    ):
        slices[dimension] = {}
        for value in values:
            subset = [
                case
                for case in selected
                if (
                    value in case["annotation"]["labels"]["tone"]["phenomena"]
                    if dimension == "phenomenon"
                    else case[dimension] == value
                )
            ]
            slices[dimension][value] = _slice(subset, predicted_by_id)

    total = len(selected)
    correct = sum(a == p for a, p in zip(actual, predicted))
    abstained = sum(label == ABSTAIN for label in predicted)
    covered = total - abstained
    quality_allowed = (
        run["split"] == "test"
        and dataset.publishable_tone_gold
        and missing == 0
        and covered > 0
        and all(case["annotation"]["state"] == "adjudicated" for case in selected)
    )
    warnings = []
    if run["split"] == "security":
        warnings.append("security cases are reported separately from natural-distribution accuracy")
    if any(metric.low_support for group in slices.values() for metric in group.values()):
        warnings.append("one or more slices have support below 30; do not generalize point estimates")
    if not quality_allowed:
        warnings.append("tone quality claim blocked: requires publishable private gold and complete blind-test predictions")
    return ToneClassificationReport(
        metrics_version=METRICS_VERSION,
        dataset_version=dataset.dataset_version,
        prediction_run_id=run["prediction_run_id"],
        split=run["split"],
        total=total,
        correct=correct,
        accuracy=correct / total,
        accuracy_wilson_95=_wilson(correct, total),
        covered=covered,
        coverage=covered / total,
        abstained=abstained,
        missing_predictions=missing,
        macro_f1=macro_f1,
        unknown_recall=unknown_recall,
        per_class=per_class,
        confusion=confusion,
        slices=slices,
        quality_claim_allowed=quality_allowed,
        warnings=tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure a frozen tone polarity prediction run")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_tone_classification(args.dataset, args.run)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
