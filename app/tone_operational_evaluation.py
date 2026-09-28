from __future__ import annotations

"""Evaluate complete tone attempt cost, latency and structural reliability."""

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_cases, _load_json
from .tone_evaluation_metrics import evaluate_tone_classification

INPUT_SCHEMA = "tone-operational-run-v1"
REPORT_VERSION = "tone-operational-report-v1"
ATTEMPT_KINDS = {"primary", "retry", "repair"}
ATTEMPT_STATUSES = {"succeeded", "failed"}
USAGE_STATUSES = {"reported", "estimated", "unknown"}
MAX_ATTEMPTS = 3
MIN_FIRST_SCHEMA_RATE = 0.99


@dataclass(frozen=True)
class ToneOperationalReport:
    report_version: str
    operational_run_id: str
    prediction_run_id: str
    dataset_version: str
    split: str
    policy_id: str
    cases: int
    attempts: int
    failed_attempts: int
    recovered_cases: int
    unknown_usage_attempts: int
    first_attempt_schema_valid: int
    final_schema_valid: int
    first_attempt_schema_rate: float
    final_schema_rate: float
    total_input_tokens: int
    total_output_tokens: int
    total_cost_microusd: int
    cost_microusd_per_1000_input_tokens: float | None
    cost_microusd_per_100_cases: float
    latency_ms_p50: int
    latency_ms_p95: int
    checks: dict[str, bool]
    admission_ready: bool
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
        raise EvaluationDatasetError("tone operational attempts_file must be local")
    return parent.with_name(value)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise EvaluationDatasetError(f"tone operational attempt requires {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError(f"tone operational attempt has invalid {field}") from exc
    if parsed.tzinfo is None:
        raise EvaluationDatasetError(f"tone operational attempt {field} requires timezone")
    return parsed


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationDatasetError(f"tone operational attempt has invalid {field}")
    return value


def _positive_limit(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvaluationDatasetError(f"tone operational policy requires positive {field}")
    return value


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def evaluate_tone_operations(
    dataset_path: Path | str,
    prediction_run_path: Path | str,
    operational_run_path: Path | str,
) -> ToneOperationalReport:
    root = Path(dataset_path)
    prediction_path = Path(prediction_run_path)
    operations_path = Path(operational_run_path)
    metrics = evaluate_tone_classification(root, prediction_path)
    prediction_run = _load_json(prediction_path)
    record = _load_json(operations_path)
    fields = {
        "schema_version", "operational_run_id", "prediction_run_id", "dataset_version",
        "split", "generated_at", "prediction_run_sha256", "predictions_sha256",
        "dataset_manifest_sha256", "dataset_cases_sha256", "attempts_file",
        "attempts_sha256", "policy_id", "max_cost_microusd_per_1000_input_tokens",
        "max_cost_microusd_per_100_cases", "max_p95_latency_ms",
    }
    if not isinstance(record, dict) or set(record) != fields or record.get("schema_version") != INPUT_SCHEMA:
        raise EvaluationDatasetError("unsupported or invalid tone operational run")
    expected = {
        "prediction_run_id": metrics.prediction_run_id,
        "dataset_version": metrics.dataset_version,
        "split": metrics.split,
        "prediction_run_sha256": _sha(prediction_path),
        "predictions_sha256": prediction_run["predictions_sha256"],
        "dataset_manifest_sha256": _sha(root / "manifest.json"),
        "dataset_cases_sha256": _sha(root / "cases.jsonl"),
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise EvaluationDatasetError("tone operational run differs from frozen prediction or dataset")
    for field in ("operational_run_id", "policy_id"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise EvaluationDatasetError(f"tone operational run requires {field}")
    _timestamp(record.get("generated_at"), "generated_at")
    cost_per_1000_limit = _positive_limit(
        record.get("max_cost_microusd_per_1000_input_tokens"),
        "max_cost_microusd_per_1000_input_tokens",
    )
    cost_per_100_limit = _positive_limit(
        record.get("max_cost_microusd_per_100_cases"),
        "max_cost_microusd_per_100_cases",
    )
    latency_limit = _positive_limit(record.get("max_p95_latency_ms"), "max_p95_latency_ms")
    attempts_path = _local_file(operations_path, record.get("attempts_file"))
    if record.get("attempts_sha256") != _sha(attempts_path):
        raise EvaluationDatasetError("tone operational attempts_sha256 mismatch")

    selected = {
        case["case_id"] for case in _load_cases(root / "cases.jsonl")
        if case["split"] == metrics.split
    }
    row_fields = {
        "case_id", "attempt_number", "attempt_kind", "status", "started_at",
        "finished_at", "schema_valid", "usage_status", "input_tokens",
        "output_tokens", "cost_microusd",
    }
    by_case: dict[str, list[dict]] = {case_id: [] for case_id in selected}
    seen: set[tuple[str, int]] = set()
    latencies = []
    total_input = total_output = total_cost = unknown_usage = failed = 0
    for row in _load_cases(attempts_path):
        if set(row) != row_fields:
            raise EvaluationDatasetError("tone operational attempt has invalid fields")
        case_id = row.get("case_id")
        number = row.get("attempt_number")
        if (
            not isinstance(case_id, str)
            or case_id not in selected
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or (case_id, number) in seen
        ):
            raise EvaluationDatasetError("tone operational attempt is duplicate, unknown or invalid")
        seen.add((case_id, number))
        kind, status = row.get("attempt_kind"), row.get("status")
        if kind not in ATTEMPT_KINDS or status not in ATTEMPT_STATUSES:
            raise EvaluationDatasetError("tone operational attempt kind or status is invalid")
        if not isinstance(row.get("schema_valid"), bool):
            raise EvaluationDatasetError("tone operational attempt schema_valid must be boolean")
        if status == "failed" and row["schema_valid"]:
            raise EvaluationDatasetError("failed tone attempt cannot be schema valid")
        started = _timestamp(row.get("started_at"), "started_at")
        finished = _timestamp(row.get("finished_at"), "finished_at")
        latency = (finished - started).total_seconds() * 1000
        if latency < 0 or not latency.is_integer():
            raise EvaluationDatasetError("tone operational attempt latency must be nonnegative whole milliseconds")
        latencies.append(int(latency))
        usage = row.get("usage_status")
        if usage not in USAGE_STATUSES:
            raise EvaluationDatasetError("tone operational attempt usage_status is invalid")
        usage_values = (row.get("input_tokens"), row.get("output_tokens"), row.get("cost_microusd"))
        if usage == "unknown":
            if any(value is not None for value in usage_values):
                raise EvaluationDatasetError("unknown tone usage must not contain token or cost values")
            unknown_usage += 1
        else:
            input_tokens = _nonnegative_int(usage_values[0], "input_tokens")
            output_tokens = _nonnegative_int(usage_values[1], "output_tokens")
            cost = _nonnegative_int(usage_values[2], "cost_microusd")
            total_input += input_tokens
            total_output += output_tokens
            total_cost += cost
        if status == "failed":
            failed += 1
        by_case[case_id].append(row)
    if not latencies:
        raise EvaluationDatasetError("tone operational run has no attempts")

    first_valid = final_valid = recovered = 0
    for case_id, attempts in by_case.items():
        attempts.sort(key=lambda row: row["attempt_number"])
        numbers = [row["attempt_number"] for row in attempts]
        if not attempts or numbers != list(range(1, len(attempts) + 1)) or len(attempts) > MAX_ATTEMPTS:
            raise EvaluationDatasetError(f"tone operational case {case_id} attempt sequence is invalid")
        if attempts[0]["attempt_kind"] != "primary" or any(
            row["attempt_kind"] == "primary" for row in attempts[1:]
        ):
            raise EvaluationDatasetError(f"tone operational case {case_id} primary attempt is invalid")
        if sum(row["attempt_kind"] == "repair" for row in attempts) > 1:
            raise EvaluationDatasetError(f"tone operational case {case_id} has more than one repair")
        for previous, current in zip(attempts, attempts[1:]):
            if previous["status"] == "succeeded" and previous["schema_valid"]:
                raise EvaluationDatasetError(
                    f"tone operational case {case_id} continues after a valid result"
                )
            expected_kind = (
                "retry" if previous["status"] == "failed" else "repair"
            )
            if current["attempt_kind"] != expected_kind:
                raise EvaluationDatasetError(
                    f"tone operational case {case_id} retry or repair kind is invalid"
                )
            if _timestamp(current["started_at"], "started_at") < _timestamp(
                previous["finished_at"], "finished_at"
            ):
                raise EvaluationDatasetError(
                    f"tone operational case {case_id} attempts overlap or run backwards"
                )
        first_valid += int(attempts[0]["status"] == "succeeded" and attempts[0]["schema_valid"])
        has_valid = attempts[-1]["status"] == "succeeded" and attempts[-1]["schema_valid"]
        final_valid += int(has_valid)
        recovered += int(has_valid and len(attempts) > 1)

    cases = len(selected)
    first_rate = first_valid / cases
    final_rate = final_valid / cases
    p50, p95 = _nearest_rank(latencies, 0.50), _nearest_rank(latencies, 0.95)
    cost_per_1000 = total_cost * 1000 / total_input if total_input else None
    cost_per_100 = total_cost * 100 / cases
    checks = {
        "blind_test_split": metrics.split == "test",
        "publishable_gold_and_quality_run": metrics.quality_claim_allowed,
        "all_cases_have_attempts": len(by_case) == cases and all(by_case.values()),
        "final_schema_valid_rate_is_1_00": final_rate == 1.0,
        "first_attempt_schema_valid_rate_at_least_0_99": first_rate >= MIN_FIRST_SCHEMA_RATE,
        "all_usage_known": unknown_usage == 0,
        "input_tokens_nonzero": total_input > 0,
        "cost_per_1000_input_within_policy": (
            cost_per_1000 is not None and cost_per_1000 <= cost_per_1000_limit
        ),
        "cost_per_100_cases_within_policy": cost_per_100 <= cost_per_100_limit,
        "p95_latency_within_policy": p95 <= latency_limit,
    }
    warnings = []
    if not all(checks.values()):
        warnings.append("operational admission blocked; failed attempts and retries remain in every denominator")
    warnings.append("policy identity and budget ownership require release-review verification")
    return ToneOperationalReport(
        REPORT_VERSION,
        record["operational_run_id"],
        metrics.prediction_run_id,
        metrics.dataset_version,
        metrics.split,
        record["policy_id"],
        cases,
        len(seen),
        failed,
        recovered,
        unknown_usage,
        first_valid,
        final_valid,
        first_rate,
        final_rate,
        total_input,
        total_output,
        total_cost,
        cost_per_1000,
        cost_per_100,
        p50,
        p95,
        checks,
        all(checks.values()),
        tuple(warnings),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate frozen tone operational attempts")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--operations", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_tone_operations(args.dataset, args.run, args.operations)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if report.admission_ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
