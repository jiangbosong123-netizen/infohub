from __future__ import annotations

"""Freeze one blind single-owner label or recheck batch (D23 single-owner-v1) into a new dataset,
or re-declare its label definition on the owner's decision."""

import argparse
import json
from pathlib import Path

from . import evaluation_review_intake, impact_review_intake, tone_review_intake
from .evaluation import EvaluationDatasetError
from .relevance_definition import DEFINITIONS as RELEVANCE_DEFINITIONS
from .review_intake import (
    OwnerLabelIntakeReport,
    ReviewIntakeTask,
    run_owner_label_intake,
    run_owner_recheck_intake,
    run_owner_redefinition,
    run_owner_resolution_intake,
)

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


def import_owner_recheck_batch(
    task: str,
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> dict:
    if task not in TASKS:
        raise EvaluationDatasetError(f"unknown owner label task {task!r}")
    return run_owner_recheck_intake(
        TASKS[task], source, batch, output, dataset_version=dataset_version
    )


def import_owner_resolution_batch(
    task: str,
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> dict:
    if task not in TASKS:
        raise EvaluationDatasetError(f"unknown owner label task {task!r}")
    return run_owner_resolution_intake(
        TASKS[task], source, batch, output, dataset_version=dataset_version
    )


def redefine_owner_dataset(
    task: str,
    source: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
    definition: str,
    decision: str,
    decided_at: str,
) -> dict:
    if task not in TASKS:
        raise EvaluationDatasetError(f"unknown owner label task {task!r}")
    if task == "relevance" and definition not in RELEVANCE_DEFINITIONS:
        raise EvaluationDatasetError(f"unknown relevance definition {definition!r}")
    return run_owner_redefinition(
        TASKS[task], source, output, dataset_version=dataset_version,
        definition=definition, decision=decision, decided_at=decided_at,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a blind single-owner label batch")
    parser.add_argument("--task", required=True, choices=sorted(TASKS))
    parser.add_argument("--kind", default="label",
                        choices=["label", "recheck", "resolution", "redefine"])
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--batch", type=Path, help="required except for --kind redefine")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--definition", help="redefine: the new label definition")
    parser.add_argument("--decision", help="redefine: the owner's decision, e.g. its record id")
    parser.add_argument("--decided-at", help="redefine: date of the owner's decision")
    args = parser.parse_args()
    if args.kind == "redefine":
        if not (args.definition and args.decision and args.decided_at):
            parser.error("--kind redefine needs --definition, --decision and --decided-at")
        result = redefine_owner_dataset(
            args.task, args.dataset, args.output, dataset_version=args.dataset_version,
            definition=args.definition, decision=args.decision, decided_at=args.decided_at,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.batch is None:
        parser.error("--batch is required")
    if args.kind == "resolution":
        result = import_owner_resolution_batch(
            args.task, args.dataset, args.batch, args.output, dataset_version=args.dataset_version
        )
    elif args.kind == "recheck":
        result = import_owner_recheck_batch(
            args.task, args.dataset, args.batch, args.output, dataset_version=args.dataset_version
        )
    else:
        result = import_owner_label_batch(
            args.task, args.dataset, args.batch, args.output, dataset_version=args.dataset_version
        ).to_dict()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
