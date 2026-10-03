from __future__ import annotations

"""Tone-specific annotation contract layered on the generic evaluation dataset."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .evaluation import (
    ALLOWED_SPLITS,
    EvaluationDatasetError,
    _load_cases,
    _load_json,
    _verified_holdout,
    validate_evaluation_dataset,
)
from .tone_contracts import ASPECTS, POLARITIES, SPEAKER_KINDS, TARGET_TYPES

TONE_EVALUATION_CONTRACT = "infohub.tone-evaluation/1.0"
TONE_ANNOTATION_SCHEMA = "infohub.tone-annotation/1.0"
TONE_OUTPUT_SCHEMA = "infohub.tone/1.1"
TONE_VOCABULARY_VERSION = "tone-vocabulary-v1"

INTENSITY_BANDS = {"zero", "weak", "moderate", "strong", "unknown"}
PHENOMENA = {
    "direct",
    "negation",
    "quotation",
    "reported_speech",
    "sarcasm",
    "mixed",
    "ambiguous_target",
    "prompt_injection",
}
REQUIRED_PHENOMENA = tuple(sorted(PHENOMENA))
HASH_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ToneEvaluationReport:
    dataset_version: str
    cases: int
    assessments: int
    split_counts: dict[str, int]
    language_counts: dict[str, int]
    polarity_counts: dict[str, int]
    phenomenon_counts: dict[str, int]
    source_kind_counts: dict[str, int]
    annotation_state_counts: dict[str, int]
    target_gaps: dict[str, int]
    publishable_tone_gold: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _error(case_id: str, message: str) -> EvaluationDatasetError:
    return EvaluationDatasetError(f"tone case {case_id} {message}")


def _exact(data: object, keys: set[str], case_id: str, field: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise _error(case_id, f"requires {field} object")
    if set(data) != keys:
        raise _error(case_id, f"has invalid {field} fields")
    return data


def _nullable_text(
    value: object, case_id: str, field: str, maximum: int | None = None
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(case_id, f"has invalid {field}")
    cleaned = value.strip()
    if maximum is not None and len(cleaned) > maximum:
        raise _error(case_id, f"has oversized {field}")
    return cleaned


def _validate_speaker(value: object, case_id: str) -> None:
    speaker = _exact(value, {"kind", "entity_id", "label"}, case_id, "speaker")
    kind = speaker["kind"]
    if kind not in SPEAKER_KINDS:
        raise _error(case_id, "has unsupported speaker kind")
    entity_id = _nullable_text(speaker["entity_id"], case_id, "speaker entity_id", 128)
    label = _nullable_text(speaker["label"], case_id, "speaker label", 200)
    if kind == "unknown" and entity_id is not None:
        raise _error(case_id, "cannot assign an entity to an unknown speaker")
    if kind != "unknown" and entity_id is None and label is None:
        raise _error(case_id, "requires a speaker entity_id or label")


def _validate_target(value: object, case_id: str, polarity: str) -> None:
    if value is None:
        if polarity != "unknown":
            raise _error(case_id, "requires unknown polarity when target is unresolved")
        return
    target = _exact(value, {"entity_id", "type"}, case_id, "target")
    if target["type"] not in TARGET_TYPES:
        raise _error(case_id, "has unsupported target type")
    _nullable_text(target["entity_id"], case_id, "target entity_id", 128)
    if target["entity_id"] is None:
        raise _error(case_id, "requires target entity_id")


def _validate_evidence(case: dict, value: object, case_id: str) -> None:
    evidence = _exact(
        value,
        {"quote", "quote_sha256", "start_offset", "end_offset", "offset_unit"},
        case_id,
        "evidence",
    )
    if evidence["offset_unit"] != "unicode_code_point":
        raise _error(case_id, "requires unicode_code_point evidence offsets")
    start, end = evidence["start_offset"], evidence["end_offset"]
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise _error(case_id, "has invalid evidence offsets")
    digest = evidence["quote_sha256"]
    if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
        raise _error(case_id, "has invalid quote_sha256")
    quote = evidence["quote"]
    if case["text_storage"] == "synthetic_embedded":
        if not isinstance(quote, str) or not quote or len(quote) > 2_000:
            raise _error(case_id, "requires an embedded synthetic quote")
        if end > len(case["text"]) or case["text"][start:end] != quote:
            raise _error(case_id, "evidence quote does not match the frozen text")
        if hashlib.sha256(quote.encode("utf-8")).hexdigest() != digest:
            raise _error(case_id, "quote hash does not match the frozen text")
    elif quote is not None:
        raise _error(case_id, "must not embed a quote for restricted text")


def _validate_tone_label(
    case: dict, labels: object | None = None
) -> tuple[str, tuple[str, ...]]:
    case_id = case["case_id"]
    if labels is None:
        labels = case["annotation"]["labels"]
    if not isinstance(labels, Mapping):
        raise _error(case_id, "requires labels object")
    if set(labels) != {"tone"}:
        raise _error(case_id, "must contain exactly one tone label")
    tone = _exact(
        labels["tone"],
        {
            "speaker",
            "target",
            "aspect",
            "polarity",
            "intensity_band",
            "evidence",
            "phenomena",
            "uncertainty_reason",
        },
        case_id,
        "label",
    )
    polarity = tone["polarity"]
    if polarity not in POLARITIES:
        raise _error(case_id, "has unsupported polarity")
    if tone["aspect"] not in ASPECTS:
        raise _error(case_id, "has unsupported aspect")
    band = tone["intensity_band"]
    if band not in INTENSITY_BANDS:
        raise _error(case_id, "has unsupported intensity band")
    if polarity == "unknown":
        if band != "unknown" or _nullable_text(
            tone["uncertainty_reason"], case_id, "uncertainty_reason", 500
        ) is None:
            raise _error(case_id, "unknown polarity requires unknown intensity and a reason")
    else:
        if tone["uncertainty_reason"] is not None:
            raise _error(case_id, "known polarity cannot carry an uncertainty reason")
        if polarity == "neutral" and band != "zero":
            raise _error(case_id, "neutral polarity requires zero intensity")
        if polarity != "neutral" and band in {"zero", "unknown"}:
            raise _error(case_id, "directional or mixed polarity requires nonzero intensity")
    _validate_speaker(tone["speaker"], case_id)
    _validate_target(tone["target"], case_id, polarity)
    evidence = tone["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise _error(case_id, "requires at least one evidence span")
    for span in evidence:
        _validate_evidence(case, span, case_id)
    signatures = [
        (span["start_offset"], span["end_offset"], span["quote_sha256"])
        for span in evidence
    ]
    if len(signatures) != len(set(signatures)):
        raise _error(case_id, "contains duplicate evidence spans")
    phenomena = tone["phenomena"]
    if (
        not isinstance(phenomena, list)
        or not phenomena
        or any(item not in PHENOMENA for item in phenomena)
        or phenomena != sorted(set(phenomena))
    ):
        raise _error(case_id, "requires unique sorted controlled phenomena")
    if polarity == "mixed" and "mixed" not in phenomena:
        raise _error(case_id, "mixed polarity requires the mixed phenomenon")
    if tone["target"] is None and "ambiguous_target" not in phenomena:
        raise _error(case_id, "unresolved target requires the ambiguous_target phenomenon")
    return polarity, tuple(phenomena)


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise EvaluationDatasetError(f"tone manifest has invalid target {field}")
    return value


def _verified_private_evidence(manifest: dict, root: Path) -> bool:
    review = manifest.get("tone_evidence_review")
    if not isinstance(review, Mapping) or review.get("status") != "verified":
        return False
    required = {
        "protocol_version": "tone-evidence-review-v1",
        "source": "human",
        "model_assistance": False,
        "all_cases_verified": True,
        "quote_hash_and_offsets_verified": True,
        "normalized_artifacts_verified": True,
    }
    if any(review.get(key) != value for key, value in required.items()):
        return False
    for key in (
        "verifier_id",
        "recorded_at",
        "cases_sha256",
        "review_record_sha256",
        "artifact_manifest_sha256",
        "artifacts_sha256",
    ):
        if not isinstance(review.get(key), str) or not review[key].strip():
            return False
    if any(
        HASH_RE.fullmatch(review[key]) is None
        for key in (
            "cases_sha256",
            "review_record_sha256",
            "artifact_manifest_sha256",
            "artifacts_sha256",
        )
    ):
        return False
    if (
        type(review.get("labels_verified")) is not int
        or review["labels_verified"] <= 0
        or type(review.get("spans_verified")) is not int
        or review["spans_verified"] <= 0
        or not isinstance(review.get("normalizer_versions"), list)
        or not review["normalizer_versions"]
        or any(not isinstance(item, str) or not item.strip() for item in review["normalizer_versions"])
        or review["normalizer_versions"] != sorted(set(review["normalizer_versions"]))
    ):
        return False
    try:
        stamp = datetime.fromisoformat(review["recorded_at"].replace("Z", "+00:00"))
        record_bytes = (root / "tone-evidence-review.json").read_bytes()
        record = json.loads(record_bytes)
    except (ValueError, OSError, json.JSONDecodeError):
        return False
    if stamp.tzinfo is None:
        return False
    if review["cases_sha256"] != hashlib.sha256((root / "cases.jsonl").read_bytes()).hexdigest():
        return False
    if review["review_record_sha256"] != hashlib.sha256(record_bytes).hexdigest():
        return False
    if (
        not isinstance(record, Mapping)
        or not isinstance(record.get("inspection_notes"), str)
        or not record["inspection_notes"].strip()
    ):
        return False
    return all(record.get(key) == review.get(key) for key in required | {
        "verifier_id": None,
        "recorded_at": None,
        "cases_sha256": None,
        "artifact_manifest_sha256": None,
        "artifacts_sha256": None,
    })


def validate_tone_evaluation_dataset(path: Path | str) -> ToneEvaluationReport:
    """Validate a frozen tone dataset without treating synthetic fixtures as quality gold."""
    root = Path(path)
    generic = validate_evaluation_dataset(root)
    manifest = _load_json(root / "manifest.json")
    cases = _load_cases(root / "cases.jsonl")
    expected = {
        "task_contract": TONE_EVALUATION_CONTRACT,
        "annotation_schema_version": TONE_ANNOTATION_SCHEMA,
        "tone_output_schema_version": TONE_OUTPUT_SCHEMA,
        "tone_vocabulary_version": TONE_VOCABULARY_VERSION,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise EvaluationDatasetError(f"tone manifest requires {field}={value}")
    plan = manifest.get("tone_target_plan")
    if not isinstance(plan, Mapping):
        raise EvaluationDatasetError("tone manifest requires tone_target_plan")
    required_plan = {
        "documents",
        "assessments",
        "english_cases",
        "chinese_cases",
        "security_cases",
        "adjudicated_cases",
        "source_kinds",
        "required_phenomena",
    }
    if set(plan) != required_plan or not isinstance(plan["required_phenomena"], Mapping):
        raise EvaluationDatasetError("tone manifest has invalid tone_target_plan fields")
    required_slices = plan["required_phenomena"]
    if set(required_slices) != set(REQUIRED_PHENOMENA):
        raise EvaluationDatasetError("tone manifest must declare every required phenomenon")

    languages: dict[str, int] = {}
    polarities = {name: 0 for name in sorted(POLARITIES)}
    phenomena = {name: 0 for name in REQUIRED_PHENOMENA}
    sources: dict[str, int] = {}
    restricted = 0
    assessments = 0
    for case in cases:
        annotation = case["annotation"]
        state = annotation["state"]
        if state == "unlabeled" and annotation.get("reviews"):
            raise _error(case["case_id"], "cannot attach reviews while marked unlabeled")
        if state == "synthetic_fixture" and (
            annotation.get("reviews") or annotation.get("adjudication")
        ):
            raise _error(case["case_id"], "synthetic fixtures cannot carry human gold provenance")
        if state in {"synthetic_fixture", "adjudicated"}:
            polarity, case_phenomena = _validate_tone_label(case)
            assessments += 1
        else:
            polarity, case_phenomena = None, ()
        for review in annotation.get("reviews", []):
            _validate_tone_label(case, review["labels"])
        if state in {"owner_labeled", "algorithm_labeled"}:
            # Owner and silver labels follow the same contract but never count as gold assessments.
            _validate_tone_label(case)
        language = case["language"]
        languages[language] = languages.get(language, 0) + 1
        if polarity is not None:
            polarities[polarity] += 1
        for item in case_phenomena:
            phenomena[item] += 1
        source = case.get("source_kind")
        if not isinstance(source, str) or not source:
            raise _error(case["case_id"], "requires source_kind")
        sources[source] = sources.get(source, 0) + 1
        if case["text_storage"] == "restricted_reference":
            restricted += 1

    target_actual = {
        "documents": generic.documents,
        "assessments": assessments,
        "english_cases": languages.get("en", 0),
        "chinese_cases": languages.get("zh", 0),
        "security_cases": generic.security_cases,
        "adjudicated_cases": generic.annotation_state_counts.get("adjudicated", 0),
        "source_kinds": len([name for name in sources if name != "synthetic"]),
    }
    gaps = {
        key: max(0, _positive_int(plan[key], key) - actual)
        for key, actual in target_actual.items()
    }
    for name in REQUIRED_PHENOMENA:
        gaps[f"phenomenon:{name}"] = max(
            0,
            _positive_int(required_slices[name], f"required_phenomena.{name}")
            - phenomena[name],
        )
    all_adjudicated = generic.annotation_state_counts.get("adjudicated", 0) == len(cases)
    publishable = (
        bool(cases)
        and not any(gaps.values())
        and all_adjudicated
        and restricted == len(cases)
        and all(generic.split_counts[name] > 0 for name in ALLOWED_SPLITS)
        and manifest.get("source_database_verified_at_admission") is True
        and _verified_holdout(manifest, cases, root)
        and _verified_private_evidence(manifest, root)
    )
    warnings: list[str] = []
    if generic.annotation_state_counts.get("synthetic_fixture"):
        warnings.append("synthetic tone fixtures validate the contract only; they are not quality gold")
    if not publishable:
        warnings.append("tone quality claims remain blocked until the private adjudicated plan passes")
    if not _verified_holdout(manifest, cases, root):
        warnings.append("time/source blind holdout is not verified")
    if not _verified_private_evidence(manifest, root):
        warnings.append("private quote hashes, offsets and normalized artifacts are not verified")
    return ToneEvaluationReport(
        dataset_version=generic.dataset_version,
        cases=len(cases),
        assessments=assessments,
        split_counts=generic.split_counts,
        language_counts=dict(sorted(languages.items())),
        polarity_counts=polarities,
        phenomenon_counts=phenomena,
        source_kind_counts=dict(sorted(sources.items())),
        annotation_state_counts=generic.annotation_state_counts,
        target_gaps=gaps,
        publishable_tone_gold=publishable,
        warnings=tuple(warnings),
    )


def write_tone_evaluation_report(
    dataset_path: Path | str, output_path: Path | str
) -> ToneEvaluationReport:
    report = validate_tone_evaluation_dataset(dataset_path)
    Path(output_path).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
