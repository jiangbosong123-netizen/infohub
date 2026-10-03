from __future__ import annotations

"""Freeze one blind single-owner label batch (D23 single-owner-v1) into a new private dataset."""

import argparse
import json
from pathlib import Path

from . import evaluation_review_intake, impact_review_intake, tone_review_intake
from .evaluation import EvaluationDatasetError
from .review_intake import OwnerLabelIntakeReport, ReviewIntakeTask, run_owner_label_intake

TASKS: dict[str, ReviewIntakeTask] = {
    task.task_id: task
    for task in (
        evaluation_review_intake.TASK,
        tone_review_intake.TASK,
        impact_review_intake.TASK,
    )
}


def import_owner_label_batch(
    task: str,
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> OwnerLabelIntakeReport:
    if task not in TASKS:
        raise EvaluationDatasetError(f"unknown owner label task {task!r}")
    return run_owner_label_intake(
        TASKS[task], source, batch, output, dataset_version=dataset_version
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a blind single-owner label batch")
    parser.add_argument("--task", required=True, choices=sorted(TASKS))
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    args = parser.parse_args()
    report = import_owner_label_batch(
        args.task, args.dataset, args.batch, args.output, dataset_version=args.dataset_version
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
