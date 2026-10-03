from __future__ import annotations

"""Import one independent human relevance-review batch into a new private dataset."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .evaluation import EvaluationDatasetError, validate_evaluation_dataset
from .review_intake import ReviewIntakeTask, review_intake_cli, run_review_intake

BATCH_VERSION = "human-relevance-review-batch-v1"
RELEVANCE_LABELS = {"relevant", "not_relevant", "unknown"}


def _validate_relevance_label(case: dict, labels: object) -> None:
    if labels not in ({"relevance": label} for label in RELEVANCE_LABELS):
        raise EvaluationDatasetError(f"review case {case['case_id']} has invalid relevance label")


TASK = ReviewIntakeTask(
    name="",
    batch_version=BATCH_VERSION,
    manifest_prefix="review_batch",
    validate_dataset=validate_evaluation_dataset,
    is_publishable=lambda report: report.publishable_gold,
    validate_labels=_validate_relevance_label,
)


@dataclass(frozen=True)
class ReviewIntakeReport:
    source_dataset_version: str
    dataset_version: str
    reviewed_cases: int
    one_review_cases: int
    two_review_cases: int
    adjudicated_cases: int
    publishable_gold: bool

    def to_dict(self) -> dict:
        return asdict(self)


def import_review_batch(source: Path | str, batch: Path | str, output: Path | str,
                        *, dataset_version: str) -> ReviewIntakeReport:
    counts = run_review_intake(TASK, source, batch, output, dataset_version=dataset_version)
    return ReviewIntakeReport(
        source_dataset_version=counts.source_dataset_version,
        dataset_version=counts.dataset_version,
        reviewed_cases=counts.reviewed_cases,
        one_review_cases=counts.one_review_cases,
        two_review_cases=counts.two_review_cases,
        adjudicated_cases=counts.adjudicated_cases,
        publishable_gold=counts.publishable,
    )


def main() -> int:
    return review_intake_cli("Import a private human relevance review batch", import_review_batch)


if __name__ == "__main__":
    raise SystemExit(main())
