from __future__ import annotations

"""Import one independent human tone-review batch into a new private dataset."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .review_intake import ReviewIntakeTask, review_intake_cli, run_review_intake
from .tone_evaluation import _validate_tone_label, validate_tone_evaluation_dataset

BATCH_VERSION = "human-tone-review-batch-v1"
TASK = ReviewIntakeTask(
    name="tone",
    batch_version=BATCH_VERSION,
    manifest_prefix="tone_review_batch",
    validate_dataset=validate_tone_evaluation_dataset,
    is_publishable=lambda report: report.publishable_tone_gold,
    validate_labels=_validate_tone_label,
    evidence_review_key="tone_evidence_review",
    task_id="tone",
)


@dataclass(frozen=True)
class ToneReviewIntakeReport:
    source_dataset_version: str
    dataset_version: str
    reviewed_cases: int
    one_review_cases: int
    two_review_cases: int
    adjudicated_cases: int
    publishable_tone_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def import_tone_review_batch(
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> ToneReviewIntakeReport:
    counts = run_review_intake(TASK, source, batch, output, dataset_version=dataset_version)
    return ToneReviewIntakeReport(
        source_dataset_version=counts.source_dataset_version,
        dataset_version=counts.dataset_version,
        reviewed_cases=counts.reviewed_cases,
        one_review_cases=counts.one_review_cases,
        two_review_cases=counts.two_review_cases,
        adjudicated_cases=counts.adjudicated_cases,
        publishable_tone_gold=counts.publishable,
    )


def main() -> int:
    return review_intake_cli("Import a private human tone review batch", import_tone_review_batch)


if __name__ == "__main__":
    raise SystemExit(main())
