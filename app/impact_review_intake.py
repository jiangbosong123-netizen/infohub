from __future__ import annotations

"""Import one independent human impact-review batch into a new private dataset."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .impact_evaluation import _validate_impact_label, validate_impact_evaluation_dataset
from .review_intake import ReviewIntakeTask, review_intake_cli, run_review_intake

BATCH_VERSION = "human-impact-review-batch-v1"
TASK = ReviewIntakeTask(
    name="impact",
    batch_version=BATCH_VERSION,
    manifest_prefix="impact_review_batch",
    validate_dataset=validate_impact_evaluation_dataset,
    is_publishable=lambda report: report.publishable_impact_gold,
    validate_labels=_validate_impact_label,
    evidence_review_key="impact_evidence_review",
    task_id="impact",
)


@dataclass(frozen=True)
class ImpactReviewIntakeReport:
    source_dataset_version: str
    dataset_version: str
    reviewed_cases: int
    one_review_cases: int
    two_review_cases: int
    adjudicated_cases: int
    publishable_impact_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def import_impact_review_batch(
    source: Path | str,
    batch: Path | str,
    output: Path | str,
    *,
    dataset_version: str,
) -> ImpactReviewIntakeReport:
    counts = run_review_intake(TASK, source, batch, output, dataset_version=dataset_version)
    return ImpactReviewIntakeReport(
        source_dataset_version=counts.source_dataset_version,
        dataset_version=counts.dataset_version,
        reviewed_cases=counts.reviewed_cases,
        one_review_cases=counts.one_review_cases,
        two_review_cases=counts.two_review_cases,
        adjudicated_cases=counts.adjudicated_cases,
        publishable_impact_gold=counts.publishable,
    )


def main() -> int:
    return review_intake_cli(
        "Import a private human impact review batch", import_impact_review_batch
    )


if __name__ == "__main__":
    raise SystemExit(main())
