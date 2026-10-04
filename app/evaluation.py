from __future__ import annotations

"""Versioned evaluation datasets, leakage checks, and honest coverage reports."""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

ALLOWED_SPLITS = {"train", "dev", "test", "security"}
ALLOWED_ANNOTATION_STATES = {
    "unlabeled", "single_annotator", "adjudicated", "synthetic_fixture",
    "owner_labeled", "algorithm_labeled",
}
# D23 single-owner-v1: owner labels are experimental truth; algorithm (silver) labels never are.
OWNER_PROTOCOL_VERSION = "single-owner-v1"
OWNER_LABEL_FIELDS = frozenset({
    "owner_id", "source", "blind", "model_assistance", "content_sha256", "recorded_at", "labels",
})
LABELER_FIELDS = frozenset({
    "labeler_id", "labeler_version", "config_sha256", "content_sha256", "generated_at",
})
SILVER_SPLITS = {"train", "dev"}
# D23 delayed blind recheck: a seeded, recomputable sample of the test split, relabelled by
# the same owner at least seven days after the first label.
OWNER_RECHECK_VERSION = "owner-recheck-v1"
OWNER_RECHECK_MIN_GAP = timedelta(days=7)
OWNER_RECHECK_MIN_CASES = 30
OWNER_RECHECK_FRACTION = 0.10
# A resolution is the owner's reasoned final decision after seeing both labels (not blind).
OWNER_RESOLUTION_FIELDS = frozenset({
    "owner_id", "source", "model_assistance", "content_sha256", "recorded_at", "labels", "reason",
})
GOLD_STATES = {"adjudicated", "synthetic_fixture"}
SCHEMA_VERSION = "evaluation-dataset-v1"
MINIMUM_GOLD_TARGETS = {"documents": 600, "event_groups": 150,
                        "impact_annotations": 300, "security_cases": 50}
HOLDOUT_CHECKS = (
    "origin_and_translation_groups", "announcement_revisions",
    "source_independence", "time_window", "training_exclusion", "usage_rights",
)


class EvaluationDatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class EvaluationReport:
    dataset_version: str
    cases: int
    documents: int
    event_groups: int
    impact_annotations: int
    security_cases: int
    split_counts: dict[str, int]
    annotation_state_counts: dict[str, int]
    target_gaps: dict[str, int]
    publishable_gold: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDatasetError(f"cannot read JSON {path}: {exc}") from exc


def _load_cases(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationDatasetError(f"cannot read cases: {exc}") from exc
    return _parse_cases(text)


def _parse_cases(text: str) -> list[dict]:
    cases = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationDatasetError(f"invalid case JSON at line {number}") from exc
        if not isinstance(value, dict):
            raise EvaluationDatasetError(f"case at line {number} is not an object")
        cases.append(value)
    return cases


def _require_text(value: object, field: str, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationDatasetError(f"case {case_id} requires {field}")
    return value


def _review_time(value: object, case_id: str) -> None:
    stamp = _require_text(value, "recorded_at", case_id)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError(f"case {case_id} has invalid review time") from exc
    if parsed.tzinfo is None:
        raise EvaluationDatasetError(f"case {case_id} review time must include timezone")


def _validate_human_reviews(case_id: str, annotation: dict, digest: str,
                            *, require_two: bool) -> set[str]:
    reviews = annotation.get("reviews")
    if not isinstance(reviews, list) or not 1 <= len(reviews) <= 2 or (require_two and len(reviews) != 2):
        raise EvaluationDatasetError(f"case {case_id} requires {'two' if require_two else 'one or two'} independent human reviews")
    reviewers: set[str] = set()
    for review in reviews:
        if not isinstance(review, dict):
            raise EvaluationDatasetError(f"case {case_id} has invalid review")
        reviewer = _require_text(review.get("reviewer_id"), "reviewer_id", case_id)
        if reviewer in reviewers or review.get("source") != "human" or review.get("independent") is not True:
            raise EvaluationDatasetError(f"case {case_id} requires distinct independent human reviewers")
        if review.get("content_sha256") != digest or not isinstance(review.get("labels"), dict) or not review["labels"]:
            raise EvaluationDatasetError(f"case {case_id} review lacks frozen-content labels")
        _review_time(review.get("recorded_at"), case_id)
        reviewers.add(reviewer)
    return reviewers


def _validate_adjudication(case_id: str, annotation: dict, digest: str) -> None:
    reviewers = _validate_human_reviews(case_id, annotation, digest, require_two=True)
    decision = annotation.get("adjudication")
    if not isinstance(decision, dict):
        raise EvaluationDatasetError(f"case {case_id} requires a human adjudication")
    adjudicator = _require_text(decision.get("adjudicator_id"), "adjudicator_id", case_id)
    if (adjudicator in reviewers or decision.get("source") != "human"
            or decision.get("content_sha256") != digest
            or decision.get("labels") != annotation.get("labels")):
        raise EvaluationDatasetError(f"case {case_id} has invalid adjudication provenance")
    _review_time(decision.get("recorded_at"), case_id)


def owner_protocol(manifest: dict) -> dict[str, str] | None:
    """Return the declared single-owner protocol (owner and label definition), if any."""
    protocol = manifest.get("annotation_protocol")
    if protocol is None:
        return None
    if (
        not isinstance(protocol, dict)
        or set(protocol) != {"version", "owner_id", "label_definition"}
        or protocol.get("version") != OWNER_PROTOCOL_VERSION
    ):
        raise EvaluationDatasetError("manifest has invalid annotation_protocol")
    return {
        "owner_id": _require_text(protocol.get("owner_id"), "owner_id", "annotation_protocol"),
        "label_definition": _require_text(
            protocol.get("label_definition"), "label_definition", "annotation_protocol"
        ),
    }


def owner_protocol_id(manifest: dict) -> str | None:
    """Return the declared single-owner annotator, or None when the protocol is absent."""
    protocol = owner_protocol(manifest)
    return protocol["owner_id"] if protocol else None


def owner_recheck_sample(cases: Iterable[dict]) -> list[str]:
    """Return the fixed recheck sample: max(30, 10%) of test cases ranked by a seeded hash."""
    test_ids = sorted(case["case_id"] for case in cases if case.get("split") == "test")
    size = min(len(test_ids), max(OWNER_RECHECK_MIN_CASES, math.ceil(len(test_ids) * OWNER_RECHECK_FRACTION)))
    ranked = sorted(
        test_ids,
        key=lambda case_id: hashlib.sha256(f"{OWNER_RECHECK_VERSION}:{case_id}".encode("utf-8")).hexdigest(),
    )
    return ranked[:size]


def _time(value: object, case_id: str) -> datetime:
    _review_time(value, case_id)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _validate_owner_recheck(case_id: str, annotation: dict, digest: str, owner_id: str,
                            in_sample: bool) -> None:
    recheck = annotation["owner_recheck"]
    if not in_sample:
        raise EvaluationDatasetError(f"case {case_id} is not in the {OWNER_RECHECK_VERSION} sample")
    if not isinstance(recheck, dict) or set(recheck) != OWNER_LABEL_FIELDS:
        raise EvaluationDatasetError(f"case {case_id} has invalid owner_recheck provenance")
    if (
        recheck["owner_id"] != owner_id
        or recheck["source"] != "human"
        or recheck["blind"] is not True
        or recheck["model_assistance"] is not False
    ):
        raise EvaluationDatasetError(
            f"case {case_id} owner recheck must be blind, human and by the declared owner"
        )
    if recheck["content_sha256"] != digest:
        raise EvaluationDatasetError(f"case {case_id} owner recheck lacks frozen-content binding")
    if not isinstance(recheck["labels"], dict) or not recheck["labels"]:
        raise EvaluationDatasetError(f"case {case_id} owner recheck has no labels")
    first = _time(annotation["owner_label"]["recorded_at"], case_id)
    if _time(recheck["recorded_at"], case_id) - first < OWNER_RECHECK_MIN_GAP:
        raise EvaluationDatasetError(
            f"case {case_id} owner recheck must follow the first label by at least 7 days"
        )


def _validate_owner_label(case_id: str, annotation: dict, digest: str, owner_id: str | None) -> None:
    if owner_id is None:
        raise EvaluationDatasetError(
            f"case {case_id} owner label requires manifest annotation_protocol {OWNER_PROTOCOL_VERSION}"
        )
    record = annotation.get("owner_label")
    if not isinstance(record, dict) or set(record) != OWNER_LABEL_FIELDS:
        raise EvaluationDatasetError(f"case {case_id} has invalid owner_label provenance")
    if (
        record["owner_id"] != owner_id
        or record["source"] != "human"
        or record["blind"] is not True
        or record["model_assistance"] is not False
    ):
        raise EvaluationDatasetError(
            f"case {case_id} owner label must be blind, human and by the declared owner"
        )
    if record["content_sha256"] != digest:
        raise EvaluationDatasetError(f"case {case_id} owner label lacks frozen-content binding")
    _review_time(record["recorded_at"], case_id)
    if not isinstance(record["labels"], dict) or not record["labels"]:
        raise EvaluationDatasetError(f"case {case_id} owner label has no labels")
    resolution = annotation.get("owner_resolution")
    final = resolution.get("labels") if isinstance(resolution, dict) else record["labels"]
    if final != annotation["labels"]:
        source = "owner resolution" if resolution is not None else "owner label"
        raise EvaluationDatasetError(f"case {case_id} {source} does not match final labels")


def _validate_owner_resolution(case_id: str, annotation: dict, digest: str, owner_id: str) -> None:
    resolution = annotation["owner_resolution"]
    recheck = annotation.get("owner_recheck")
    if not isinstance(recheck, dict):
        raise EvaluationDatasetError(f"case {case_id} owner resolution requires a recheck")
    if recheck["labels"] == annotation["owner_label"]["labels"]:
        raise EvaluationDatasetError(f"case {case_id} owner resolution is only for recheck disagreements")
    if not isinstance(resolution, dict) or set(resolution) != OWNER_RESOLUTION_FIELDS:
        raise EvaluationDatasetError(f"case {case_id} has invalid owner_resolution provenance")
    if (
        resolution["owner_id"] != owner_id
        or resolution["source"] != "human"
        or resolution["model_assistance"] is not False
    ):
        raise EvaluationDatasetError(f"case {case_id} owner resolution must be human, by the declared owner")
    if resolution["content_sha256"] != digest:
        raise EvaluationDatasetError(f"case {case_id} owner resolution lacks frozen-content binding")
    if not isinstance(resolution["labels"], dict) or not resolution["labels"]:
        raise EvaluationDatasetError(f"case {case_id} owner resolution has no labels")
    reason = resolution["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise EvaluationDatasetError(f"case {case_id} owner resolution requires a reason (1-1000 chars)")
    if _time(resolution["recorded_at"], case_id) < _time(recheck["recorded_at"], case_id):
        raise EvaluationDatasetError(f"case {case_id} owner resolution predates its recheck")


def _validate_silver_label(case_id: str, split: str, annotation: dict, digest: str) -> None:
    if split not in SILVER_SPLITS:
        raise EvaluationDatasetError(f"case {case_id} algorithm labels may only enter train/dev")
    if annotation.get("generated_by_model") is not True:
        raise EvaluationDatasetError(f"case {case_id} algorithm labels must set generated_by_model")
    labeler = annotation.get("labeler")
    if not isinstance(labeler, dict) or set(labeler) != LABELER_FIELDS:
        raise EvaluationDatasetError(f"case {case_id} has invalid labeler provenance")
    _require_text(labeler["labeler_id"], "labeler_id", case_id)
    _require_text(labeler["labeler_version"], "labeler_version", case_id)
    if not isinstance(labeler["config_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", labeler["config_sha256"]) is None:
        raise EvaluationDatasetError(f"case {case_id} labeler requires config_sha256")
    if labeler["content_sha256"] != digest:
        raise EvaluationDatasetError(f"case {case_id} algorithm label lacks frozen-content binding")
    _review_time(labeler["generated_at"], case_id)
    if not annotation["labels"]:
        raise EvaluationDatasetError(f"case {case_id} has empty algorithm labels")


def _verified_holdout(manifest: dict, cases: list[dict], root: Path | None = None) -> bool:
    review = manifest.get("holdout_review")
    if not isinstance(review, dict) or review.get("status") != "verified":
        return False
    if not isinstance(review.get("reviewer_id"), str) or not review["reviewer_id"].strip():
        return False
    if (review.get("protocol_version") != "holdout-review-v1"
            or not isinstance(review.get("checks"), dict)
            or any(review["checks"].get(key) is not True for key in HOLDOUT_CHECKS)
            or any(not isinstance(review.get(key), str)
                   or re.fullmatch(r"[0-9a-f]{64}", review[key]) is None
                   for key in ("source_manifest_sha256", "source_cases_sha256", "review_record_sha256"))):
        return False
    if root is not None:
        try:
            record_bytes = (root / "holdout-review.json").read_bytes()
            record = json.loads(record_bytes)
        except (OSError, json.JSONDecodeError):
            return False
        if (hashlib.sha256(record_bytes).hexdigest() != review["review_record_sha256"]
                or not isinstance(record, dict)
                or record.get("schema_version") != "holdout-review-v1"
                or record.get("source") != "human"
                or record.get("model_assistance") is not False
                or not isinstance(record.get("inspection_notes"), str)
                or not record["inspection_notes"].strip()
                or any(record.get(key) != review.get(key) for key in (
                    "reviewer_id", "recorded_at", "heldout_after", "heldout_source_refs", "checks"))
                or record.get("source_manifest_sha256") != review["source_manifest_sha256"]
                or record.get("source_cases_sha256") != review["source_cases_sha256"]):
            return False
    try:
        _review_time(review.get("recorded_at"), "holdout_review")
    except EvaluationDatasetError:
        return False
    sources = review.get("heldout_source_refs")
    cutoff_text = review.get("heldout_after")
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(value, str) or not value for value in sources)
            or not isinstance(cutoff_text, str)):
        return False
    try:
        cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if cutoff.tzinfo is None:
        return False
    natural = [case for case in cases if case["split"] != "security"]
    source_cases = [case for case in natural if case.get("source_kind") in sources]
    if not source_cases or any(case["split"] != "test" for case in source_cases):
        return False
    recent = []
    for case in natural:
        value = case.get("published_at")
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamp.tzinfo is None:
            return False
        if stamp >= cutoff:
            recent.append(case)
    if not recent or any(case["split"] != "test" for case in recent):
        return False
    return True


def validate_evaluation_dataset(path: Path | str) -> EvaluationReport:
    root = Path(path)
    manifest = _load_json(root / "manifest.json")
    cases = _load_cases(root / "cases.jsonl")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise EvaluationDatasetError("unsupported evaluation dataset schema")
    dataset_version = _require_text(manifest.get("dataset_version"), "dataset_version", "manifest")
    targets = manifest.get("target_plan")
    if not isinstance(targets, dict):
        raise EvaluationDatasetError("manifest target_plan must be an object")

    owner_id = owner_protocol_id(manifest)
    recheck_sample = set(owner_recheck_sample(cases))
    ids: set[str] = set()
    hashes: dict[str, str] = {}
    group_splits: dict[tuple[str, str], str] = {}
    split_counts = {name: 0 for name in sorted(ALLOWED_SPLITS)}
    states: dict[str, int] = {}
    event_groups: set[str] = set()
    documents: set[str] = set()
    impact_count = 0
    security_count = 0
    language_counts: dict[str, int] = {}
    warnings: list[str] = []

    for case in cases:
        case_id = _require_text(case.get("case_id"), "case_id", "unknown")
        if case_id in ids:
            raise EvaluationDatasetError(f"duplicate case_id {case_id}")
        ids.add(case_id)
        split = case.get("split")
        if split not in ALLOWED_SPLITS:
            raise EvaluationDatasetError(f"case {case_id} has invalid split")
        split_counts[split] += 1
        if split == "security":
            security_count += 1
        event_group = _require_text(case.get("event_group_id"), "event_group_id", case_id)
        origin_group = _require_text(case.get("origin_group_id"), "origin_group_id", case_id)
        event_groups.add(event_group)
        for kind, group in (("event", event_group), ("origin", origin_group)):
            previous = group_splits.setdefault((kind, group), split)
            if previous != split:
                raise EvaluationDatasetError(
                    f"{kind} group {group} leaks across {previous} and {split}"
                )
        document_ref = _require_text(case.get("document_ref"), "document_ref", case_id)
        documents.add(document_ref)
        language = _require_text(case.get("language"), "language", case_id)
        language_counts[language] = language_counts.get(language, 0) + 1
        storage = case.get("text_storage")
        if storage == "synthetic_embedded":
            text = _require_text(case.get("text"), "text", case_id)
            digest = _require_text(case.get("content_sha256"), "content_sha256", case_id)
            if _sha_text(text) != digest:
                raise EvaluationDatasetError(f"case {case_id} content hash mismatch")
        elif storage == "restricted_reference":
            if case.get("text") is not None:
                raise EvaluationDatasetError(f"restricted case {case_id} embeds text")
            _require_text(case.get("object_ref"), "object_ref", case_id)
            digest = _require_text(case.get("content_sha256"), "content_sha256", case_id)
        else:
            raise EvaluationDatasetError(f"case {case_id} has unsupported text_storage")
        previous_content_split = group_splits.setdefault(("content", digest), split)
        if previous_content_split != split:
            raise EvaluationDatasetError(
                f"content hash {digest} leaks across {previous_content_split} and {split}"
            )
        previous_document = hashes.setdefault(document_ref, digest)
        if previous_document != digest:
            raise EvaluationDatasetError(f"document {document_ref} has conflicting hashes")
        annotation = case.get("annotation")
        if not isinstance(annotation, dict):
            raise EvaluationDatasetError(f"case {case_id} requires annotation")
        state = annotation.get("state")
        if state not in ALLOWED_ANNOTATION_STATES:
            raise EvaluationDatasetError(f"case {case_id} has invalid annotation state")
        states[state] = states.get(state, 0) + 1
        labels = annotation.get("labels")
        if not isinstance(labels, dict):
            raise EvaluationDatasetError(f"case {case_id} labels must be an object")
        if state == "unlabeled" and labels:
            raise EvaluationDatasetError(f"case {case_id} has labels while marked unlabeled")
        if annotation.get("generated_by_model") and state in {"single_annotator", "adjudicated", "owner_labeled"}:
            raise EvaluationDatasetError(f"case {case_id} cannot use model output as gold")
        # Tiers never mix: provenance of one tier on a case of another is a hidden upgrade path.
        if state != "owner_labeled" and any(
            key in annotation for key in ("owner_label", "owner_recheck", "owner_resolution")
        ):
            raise EvaluationDatasetError(f"case {case_id} carries owner_label outside owner tier")
        if state != "algorithm_labeled" and "labeler" in annotation:
            raise EvaluationDatasetError(f"case {case_id} carries labeler outside silver tier")
        if state in {"owner_labeled", "algorithm_labeled"}:
            if storage != "restricted_reference":
                raise EvaluationDatasetError(f"case {case_id} {state} requires a restricted real-data reference")
            if annotation.get("reviews") or annotation.get("adjudication"):
                raise EvaluationDatasetError(f"case {case_id} mixes {state} with multi-reviewer provenance")
        if state == "owner_labeled":
            _validate_owner_label(case_id, annotation, digest, owner_id)
            if "owner_recheck" in annotation:
                _validate_owner_recheck(case_id, annotation, digest, owner_id, case_id in recheck_sample)
            if "owner_resolution" in annotation:
                _validate_owner_resolution(case_id, annotation, digest, owner_id)
        if state == "algorithm_labeled":
            _validate_silver_label(case_id, split, annotation, digest)
        if state == "single_annotator":
            if labels:
                raise EvaluationDatasetError(f"case {case_id} provisional reviews cannot supply gold labels")
            _validate_human_reviews(case_id, annotation, digest, require_two=False)
        if state == "adjudicated":
            if storage != "restricted_reference":
                raise EvaluationDatasetError(f"case {case_id} synthetic text cannot become gold")
            if not labels:
                raise EvaluationDatasetError(f"case {case_id} has no adjudicated labels")
            _validate_adjudication(case_id, annotation, digest)
        impact = labels.get("impact")
        if state in GOLD_STATES and isinstance(impact, list):
            impact_count += len(impact)

    target_values = {}
    for key, minimum in MINIMUM_GOLD_TARGETS.items():
        declared = targets.get(key, minimum)
        if type(declared) is not int or declared < 0:
            raise EvaluationDatasetError(f"manifest has invalid target {key}")
        target_values[key] = max(minimum, declared)
    actual = {
        "documents": len(documents), "event_groups": len(event_groups),
        "impact_annotations": impact_count, "security_cases": security_count,
    }
    gaps = {key: max(0, target_values[key] - actual[key]) for key in target_values}
    publishable = (
        not any(gaps.values())
        and states.get("adjudicated", 0) == len(cases)
        and len(cases) > 0
        and language_counts.get("en", 0) >= 200
        and language_counts.get("zh", 0) >= 200
        and all(split_counts[name] > 0 for name in ALLOWED_SPLITS)
        and manifest.get("source_database_verified_at_admission") is True
        and _verified_holdout(manifest, cases, root)
    )
    if not publishable:
        warnings.append("dataset is not publishable gold; target size and/or adjudication is incomplete")
    if states.get("synthetic_fixture"):
        warnings.append("synthetic fixtures validate tooling only and do not measure model quality")
    if not _verified_holdout(manifest, cases, root):
        warnings.append("time/source blind holdout is not verified")
    return EvaluationReport(
        dataset_version=dataset_version, cases=len(cases), documents=len(documents),
        event_groups=len(event_groups), impact_annotations=impact_count,
        security_cases=security_count, split_counts=split_counts,
        annotation_state_counts=states, target_gaps=gaps,
        publishable_gold=publishable, warnings=tuple(warnings),
    )


def write_evaluation_report(dataset_path: Path | str, output: Path | str) -> EvaluationReport:
    report = validate_evaluation_dataset(dataset_path)
    Path(output).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
