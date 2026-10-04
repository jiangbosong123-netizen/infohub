from __future__ import annotations

"""Intra-annotator agreement for the D23 delayed blind owner recheck.

A single owner cannot be checked against a second person, so D23 substitutes a seeded sample
of the test split relabelled blind by the same owner at least seven days later. This module
reports that sample's progress and the same-owner Cohen kappa on the task's key label, and
decides whether the recheck is complete enough to unblock *experimental* claims.
"""

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .agreement_stats import CategoricalAgreement, categorical_agreement, empty_confusion
from .evaluation import (
    OWNER_RECHECK_MIN_CASES,
    OWNER_RECHECK_MIN_GAP,
    OWNER_RECHECK_VERSION,
    EvaluationDatasetError,
    _load_cases,
    _load_json,
    _time,
    owner_protocol,
    owner_recheck_sample,
)
from .impact_contracts import DIRECTIONS
from .tone_contracts import POLARITIES

KAPPA_TARGET = 0.70
KEY_LABELS: dict[str, tuple[str, tuple[str, ...], Callable[[dict], str]]] = {
    "relevance": (
        "relevance", ("not_relevant", "relevant", "unknown"), lambda labels: labels["relevance"],
    ),
    "tone": ("tone.polarity", tuple(sorted(POLARITIES)), lambda labels: labels["tone"]["polarity"]),
    "impact": (
        "impact.direction", tuple(sorted(DIRECTIONS)), lambda labels: labels["impact"][0]["direction"],
    ),
}


@dataclass(frozen=True)
class OwnerRecheckReport:
    recheck_version: str
    task: str
    dataset_version: str
    owner_id: str | None
    key_label: str
    sample_size: int
    sample_not_owner_labeled: int
    rechecked: int
    eligible_now: int
    waiting_for_gap: int
    agreement: CategoricalAgreement
    kappa_target: float
    disagreements: int
    unresolved_disagreements: int
    complete: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def owner_recheck_report(
    dataset_path: Path | str, task: str, *, now: datetime | None = None
) -> OwnerRecheckReport:
    """Read-only. The dataset must already have passed its task validator."""
    if task not in KEY_LABELS:
        raise EvaluationDatasetError(f"unknown owner recheck task {task!r}")
    key_name, classes, key = KEY_LABELS[task]
    root = Path(dataset_path)
    manifest = _load_json(root / "manifest.json")
    cases = _load_cases(root / "cases.jsonl")
    protocol = owner_protocol(manifest)
    by_id = {case["case_id"]: case for case in cases}
    sample = owner_recheck_sample(cases)
    now = now or datetime.now(timezone.utc)
    confusion = empty_confusion(classes)
    not_labeled = rechecked = eligible = waiting = disagreements = 0
    for case_id in sample:
        annotation = by_id[case_id]["annotation"]
        if annotation["state"] != "owner_labeled":
            not_labeled += 1
            continue
        recheck = annotation.get("owner_recheck")
        if recheck is None:
            if now - _time(annotation["owner_label"]["recorded_at"], case_id) >= OWNER_RECHECK_MIN_GAP:
                eligible += 1
            else:
                waiting += 1
            continue
        first, second = key(annotation["owner_label"]["labels"]), key(recheck["labels"])
        if first not in confusion or second not in confusion:
            raise EvaluationDatasetError(f"case {case_id} has a {key_name} outside the task vocabulary")
        confusion[first][second] += 1
        rechecked += 1
        disagreements += int(recheck["labels"] != annotation["owner_label"]["labels"])
    agreement = categorical_agreement(confusion, rechecked)
    # Resolutions are not supported yet, so every disagreement stays unresolved.
    unresolved = disagreements
    blockers = []
    if protocol is None:
        blockers.append("dataset has no single-owner annotation protocol")
    if not sample:
        blockers.append("dataset has no test split to recheck")
    if not_labeled:
        blockers.append(f"{not_labeled} sampled test cases are not owner-labeled yet")
    if rechecked < len(sample):
        blockers.append(f"{len(sample) - rechecked} sampled cases still need a recheck")
    if rechecked < OWNER_RECHECK_MIN_CASES:
        blockers.append(f"recheck needs at least {OWNER_RECHECK_MIN_CASES} pairs")
    if rechecked and agreement.cohen_kappa is None:
        blockers.append("same-owner kappa is undefined because one class was used throughout")
    elif agreement.cohen_kappa is not None and agreement.cohen_kappa < KAPPA_TARGET:
        blockers.append("same-owner kappa is below 0.70; revise the label definition first")
    if unresolved:
        blockers.append(f"{unresolved} recheck disagreements await an owner resolution")
    return OwnerRecheckReport(
        recheck_version=OWNER_RECHECK_VERSION,
        task=task,
        dataset_version=str(manifest.get("dataset_version")),
        owner_id=protocol["owner_id"] if protocol else None,
        key_label=key_name,
        sample_size=len(sample),
        sample_not_owner_labeled=not_labeled,
        rechecked=rechecked,
        eligible_now=eligible,
        waiting_for_gap=waiting,
        agreement=agreement,
        kappa_target=KAPPA_TARGET,
        disagreements=disagreements,
        unresolved_disagreements=unresolved,
        complete=not blockers,
        blockers=tuple(blockers),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="D23 owner recheck progress and same-owner kappa")
    parser.add_argument("--task", required=True, choices=sorted(KEY_LABELS))
    parser.add_argument("--dataset", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(owner_recheck_report(args.dataset, args.task).to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
