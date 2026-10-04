from __future__ import annotations

"""Impact-specific annotation contract layered on the generic evaluation dataset."""

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
from .impact_contracts import ASPECTS, DIRECTIONS, HORIZONS, TARGET_TYPES

IMPACT_EVALUATION_CONTRACT = "infohub.impact-evaluation/1.0"
IMPACT_ANNOTATION_SCHEMA = "infohub.impact-annotation/1.0"
IMPACT_OUTPUT_SCHEMA = "impact/1.0"
IMPACT_VOCABULARY_VERSION = "impact-vocabulary-v1"

EXPECTED_STATUSES = {"needs_review", "insufficient_evidence"}
EVIDENCE_JUDGMENTS = {"supported", "conflicting", "insufficient"}
INTENSITY_BANDS = {"zero", "weak", "moderate", "strong", "unknown"}
PHENOMENA = {
    "conditional_plan",
    "conflicting_evidence",
    "cross_entity",
    "direct_effect",
    "insufficient_evidence",
    "numeric_revision",
    "prompt_injection",
    "unknown_direction",
}
REQUIRED_PHENOMENA = tuple(sorted(PHENOMENA))
MINIMUM_TARGETS = {
    "documents": 300,
    "event_groups": 150,
    "assessments": 300,
    "english_cases": 100,
    "chinese_cases": 100,
    "security_cases": 50,
    "adjudicated_cases": 300,
    "source_kinds": 3,
    "insufficient_or_conflicting_cases": 100,
}
MINIMUM_PHENOMENON_CASES = 30
HASH_RE = re.compile(r"[0-9a-f]{64}")
DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


@dataclass(frozen=True)
class ImpactEvaluationReport:
    dataset_version: str
    cases: int
    assessments: int
    split_counts: dict[str, int]
    language_counts: dict[str, int]
    status_counts: dict[str, int]
    direction_counts: dict[str, int]
    evidence_judgment_counts: dict[str, int]
    phenomenon_counts: dict[str, int]
    source_kind_counts: dict[str, int]
    annotation_state_counts: dict[str, int]
    target_gaps: dict[str, int]
    publishable_impact_gold: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _error(case_id: str, message: str) -> EvaluationDatasetError:
    return EvaluationDatasetError(f"impact case {case_id} {message}")


def _exact(data: object, keys: set[str], case_id: str, field: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise _error(case_id, f"requires {field} object")
    if set(data) != keys:
        raise _error(case_id, f"has invalid {field} fields")
    return data


def _text(
    value: object, case_id: str, field: str, maximum: int, *, nullable: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(case_id, f"has invalid {field}")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise _error(case_id, f"has oversized {field}")
    return cleaned


def _string_list(
    value: object, case_id: str, field: str, *, maximum: int, sorted_unique: bool
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise _error(case_id, f"has invalid {field}")
    cleaned = [item.strip() for item in value]
    if len(cleaned) != len(set(cleaned)):
        raise _error(case_id, f"contains duplicate {field}")
    if sorted_unique and cleaned != sorted(cleaned):
        raise _error(case_id, f"requires sorted {field}")
    return cleaned


def _validate_event_evidence(case: dict) -> dict[str, tuple[str, bool]]:
    case_id = case["case_id"]
    records = case.get("event_evidence")
    if not isinstance(records, list) or not records:
        raise _error(case_id, "requires event_evidence")
    roles: dict[str, tuple[str, bool]] = {}
    for value in records:
        evidence = _exact(
            value,
            {
                "evidence_id", "role", "quote", "quote_sha256", "start_offset",
                "end_offset", "offset_unit", "payload_kind", "truncated",
            },
            case_id,
            "event evidence",
        )
        evidence_id = _text(evidence["evidence_id"], case_id, "evidence_id", 128)
        if evidence_id in roles:
            raise _error(case_id, "contains duplicate evidence_id")
        role = evidence["role"]
        if role not in {"supports", "contradicts"}:
            raise _error(case_id, "has unsupported evidence role")
        if evidence["offset_unit"] != "unicode_code_point":
            raise _error(case_id, "requires unicode_code_point evidence offsets")
        payload_kind = _text(
            evidence["payload_kind"], case_id, "evidence payload_kind", 40
        )
        if type(evidence["truncated"]) is not bool:
            raise _error(case_id, "has invalid evidence truncated flag")
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
        roles[evidence_id] = (
            role,
            payload_kind != "generated_metadata" and not evidence["truncated"],
        )
    return roles


def _validate_event_facts(
    case: dict, roles: Mapping[str, tuple[str, bool]]
) -> list[dict]:
    case_id = case["case_id"]
    records = case.get("event_facts")
    if not isinstance(records, list) or len(records) > 20:
        raise _error(case_id, "has invalid event_facts")
    clean: list[dict] = []
    ids: set[str] = set()
    for value in records:
        fact = _exact(
            value,
            {"fact_id", "kind", "metric", "value", "unit", "period", "evidence_id"},
            case_id,
            "event fact",
        )
        fact_id = _text(fact["fact_id"], case_id, "fact_id", 128)
        if fact_id in ids:
            raise _error(case_id, "contains duplicate fact_id")
        ids.add(fact_id)
        kind = fact["kind"]
        if kind not in {"actual", "prior", "revised"}:
            raise _error(case_id, "has unsupported event fact kind")
        value_text = _text(fact["value"], case_id, "fact value", 100)
        if DECIMAL_RE.fullmatch(value_text) is None:
            raise _error(case_id, "requires decimal-string event fact values")
        evidence_id = _text(fact["evidence_id"], case_id, "fact evidence_id", 128)
        if roles.get(evidence_id) != ("supports", True):
            raise _error(case_id, "event fact requires usable direct support")
        clean.append({
            "fact_id": fact_id,
            "kind": kind,
            "metric": _text(fact["metric"], case_id, "fact metric", 100),
            "value": value_text,
            "unit": _text(fact["unit"], case_id, "fact unit", 80),
            "period": _text(fact["period"], case_id, "fact period", 80),
            "evidence_id": evidence_id,
        })
    return clean


def _validate_target(value: object, case_id: str) -> None:
    target = _exact(value, {"entity_id", "type"}, case_id, "target")
    _text(target["entity_id"], case_id, "target entity_id", 128)
    if target["type"] not in TARGET_TYPES:
        raise _error(case_id, "has unsupported target type")


def _validate_horizon(value: object, case_id: str) -> None:
    horizon = _exact(value, {"bucket", "min_days", "max_days"}, case_id, "horizon")
    bucket = horizon["bucket"]
    if bucket not in HORIZONS:
        raise _error(case_id, "has unsupported horizon bucket")
    expected = HORIZONS[bucket]
    for actual, boundary in zip(
        (horizon["min_days"], horizon["max_days"]), expected, strict=True
    ):
        if (boundary is None and actual is not None) or (
            boundary is not None and type(actual) is not int
        ):
            raise _error(case_id, "has invalid horizon boundary types")
    if (horizon["min_days"], horizon["max_days"]) != expected:
        raise _error(case_id, "horizon boundaries do not match the bucket")


def _validate_impact_label(
    case: dict, labels: object | None = None
) -> tuple[str, str, str, tuple[str, ...]]:
    case_id = case["case_id"]
    if labels is None:
        labels = case["annotation"]["labels"]
    if not isinstance(labels, Mapping) or set(labels) != {"impact"}:
        raise _error(case_id, "must contain exactly one impact label")
    values = labels["impact"]
    if not isinstance(values, list) or len(values) != 1:
        raise _error(case_id, "requires exactly one impact assessment")
    impact = _exact(
        values[0],
        {
            "event_version_ref", "expected_status", "target", "aspect", "horizon",
            "direction", "intensity_band", "evidence_judgment", "evidence_ids",
            "contradicting_evidence_ids", "mechanism", "assumptions", "phenomena",
            "uncertainty_reason",
        },
        case_id,
        "label",
    )
    event_version_ref = _text(
        impact["event_version_ref"], case_id, "event_version_ref", 128
    )
    if event_version_ref != case.get("event_version_ref"):
        raise _error(case_id, "event_version_ref does not match the frozen case")
    status = impact["expected_status"]
    if status not in EXPECTED_STATUSES:
        raise _error(case_id, "has unsupported expected_status")
    _validate_target(impact["target"], case_id)
    if impact["aspect"] not in ASPECTS:
        raise _error(case_id, "has unsupported aspect")
    _validate_horizon(impact["horizon"], case_id)
    direction = impact["direction"]
    if direction not in DIRECTIONS:
        raise _error(case_id, "has unsupported direction")
    band = impact["intensity_band"]
    if band not in INTENSITY_BANDS:
        raise _error(case_id, "has unsupported intensity_band")
    judgment = impact["evidence_judgment"]
    if judgment not in EVIDENCE_JUDGMENTS:
        raise _error(case_id, "has unsupported evidence_judgment")
    supporting = _string_list(
        impact["evidence_ids"], case_id, "evidence_ids", maximum=20, sorted_unique=True
    )
    contradicting = _string_list(
        impact["contradicting_evidence_ids"], case_id,
        "contradicting_evidence_ids", maximum=20, sorted_unique=True,
    )
    if set(supporting) & set(contradicting):
        raise _error(case_id, "supporting and contradicting evidence must be disjoint")
    roles = _validate_event_evidence(case)
    event_facts = _validate_event_facts(case, roles)
    if any(roles.get(item) != ("supports", True) for item in supporting):
        raise _error(case_id, "supporting evidence is not usable direct support")
    if any(roles.get(item) != ("contradicts", True) for item in contradicting):
        raise _error(case_id, "contradicting evidence is not a usable direct contradiction")
    assumptions = _string_list(
        impact["assumptions"], case_id, "assumptions", maximum=20, sorted_unique=False
    )
    if any(len(item) > 500 for item in assumptions):
        raise _error(case_id, "has oversized assumption")
    phenomena = _string_list(
        impact["phenomena"], case_id, "phenomena", maximum=len(PHENOMENA),
        sorted_unique=True,
    )
    if not phenomena or any(item not in PHENOMENA for item in phenomena):
        raise _error(case_id, "requires controlled phenomena")
    uncertainty = _text(
        impact["uncertainty_reason"], case_id, "uncertainty_reason", 500, nullable=True
    )
    mechanism = _text(
        impact["mechanism"], case_id, "mechanism", 1_000, nullable=True
    )
    if (status == "insufficient_evidence") != ("insufficient_evidence" in phenomena):
        raise _error(case_id, "status and insufficient_evidence phenomenon disagree")
    if (direction == "unknown") != ("unknown_direction" in phenomena):
        raise _error(case_id, "direction and unknown_direction phenomenon disagree")
    if (judgment == "conflicting") != ("conflicting_evidence" in phenomena):
        raise _error(case_id, "judgment and conflicting_evidence phenomenon disagree")
    revision_groups: dict[tuple[str, str, str], set[str]] = {}
    for fact in event_facts:
        key = (fact["metric"], fact["unit"], fact["period"])
        revision_groups.setdefault(key, set()).add(fact["kind"])
    has_revision_pair = any(
        {"prior", "revised"}.issubset(kinds) for kinds in revision_groups.values()
    )
    if has_revision_pair != ("numeric_revision" in phenomena):
        raise _error(case_id, "numeric_revision requires matching prior and revised facts")
    if "prompt_injection" in phenomena and status != "insufficient_evidence":
        raise _error(case_id, "prompt injection cannot support an impact assessment")
    if status == "insufficient_evidence":
        if (
            direction != "unknown" or band != "unknown" or judgment != "insufficient"
            or mechanism is not None or assumptions or uncertainty is None
            or "insufficient_evidence" not in phenomena
            or "unknown_direction" not in phenomena
        ):
            raise _error(case_id, "has invalid insufficient_evidence semantics")
    else:
        if not supporting or mechanism is None or judgment == "insufficient":
            raise _error(case_id, "needs_review requires direct support and a mechanism")
        if judgment == "conflicting" and (
            not contradicting or "conflicting_evidence" not in phenomena
        ):
            raise _error(case_id, "conflicting judgment requires contradiction evidence")
        if judgment == "supported" and contradicting:
            raise _error(case_id, "supported judgment cannot carry contradiction evidence")
        if direction == "unknown":
            if band != "unknown" or uncertainty is None or "unknown_direction" not in phenomena:
                raise _error(case_id, "unknown direction requires unknown intensity and a reason")
        else:
            if uncertainty is not None:
                raise _error(case_id, "known direction cannot carry an uncertainty reason")
            if direction == "neutral" and band != "zero":
                raise _error(case_id, "neutral direction requires zero intensity")
            if direction != "neutral" and band in {"zero", "unknown"}:
                raise _error(case_id, "directional or mixed impact requires nonzero intensity")
    if "prompt_injection" in phenomena and case.get("split") != "security":
        raise _error(case_id, "prompt_injection must remain in the security split")
    return status, direction, judgment, tuple(phenomena)


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise EvaluationDatasetError(f"impact manifest has invalid target {field}")
    return value


def _verified_private_evidence(manifest: dict, root: Path) -> bool:
    review = manifest.get("impact_evidence_review")
    if not isinstance(review, Mapping) or review.get("status") != "verified":
        return False
    required = {
        "protocol_version": "impact-evidence-review-v1",
        "source": "human",
        "model_assistance": False,
        "all_cases_verified": True,
        "event_roles_verified": True,
        "normalized_artifacts_verified": True,
    }
    if any(review.get(key) != value for key, value in required.items()):
        return False
    text_fields = ("verifier_id", "recorded_at", "cases_sha256", "review_record_sha256")
    if any(not isinstance(review.get(key), str) or not review[key].strip() for key in text_fields):
        return False
    if any(HASH_RE.fullmatch(review[key]) is None for key in ("cases_sha256", "review_record_sha256")):
        return False
    if type(review.get("assessments_verified")) is not int or review["assessments_verified"] <= 0:
        return False
    try:
        stamp = datetime.fromisoformat(review["recorded_at"].replace("Z", "+00:00"))
        record_bytes = (root / "impact-evidence-review.json").read_bytes()
        record = json.loads(record_bytes)
    except (ValueError, OSError, json.JSONDecodeError):
        return False
    if stamp.tzinfo is None:
        return False
    if review["cases_sha256"] != hashlib.sha256((root / "cases.jsonl").read_bytes()).hexdigest():
        return False
    if review["review_record_sha256"] != hashlib.sha256(record_bytes).hexdigest():
        return False
    if not isinstance(record, Mapping) or not isinstance(record.get("inspection_notes"), str):
        return False
    if not record["inspection_notes"].strip():
        return False
    return all(record.get(key) == review.get(key) for key in required | {
        "verifier_id": None, "recorded_at": None, "cases_sha256": None,
        "assessments_verified": None,
    })


def validate_impact_evaluation_dataset(path: Path | str) -> ImpactEvaluationReport:
    """Validate frozen impact annotations without treating fixtures as quality gold."""
    root = Path(path)
    generic = validate_evaluation_dataset(root)
    manifest = _load_json(root / "manifest.json")
    cases = _load_cases(root / "cases.jsonl")
    expected = {
        "task_contract": IMPACT_EVALUATION_CONTRACT,
        "annotation_schema_version": IMPACT_ANNOTATION_SCHEMA,
        "impact_output_schema_version": IMPACT_OUTPUT_SCHEMA,
        "impact_vocabulary_version": IMPACT_VOCABULARY_VERSION,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            raise EvaluationDatasetError(f"impact manifest requires {field}={value}")
    plan = manifest.get("impact_target_plan")
    required_plan = {
        "documents", "event_groups", "assessments", "english_cases", "chinese_cases",
        "security_cases", "adjudicated_cases", "source_kinds",
        "insufficient_or_conflicting_cases", "required_phenomena",
    }
    if (
        not isinstance(plan, Mapping) or set(plan) != required_plan
        or not isinstance(plan["required_phenomena"], Mapping)
    ):
        raise EvaluationDatasetError("impact manifest has invalid impact_target_plan fields")
    required_slices = plan["required_phenomena"]
    if set(required_slices) != set(REQUIRED_PHENOMENA):
        raise EvaluationDatasetError("impact manifest must declare every required phenomenon")

    languages: dict[str, int] = {}
    statuses = {name: 0 for name in sorted(EXPECTED_STATUSES)}
    directions = {name: 0 for name in sorted(DIRECTIONS)}
    judgments = {name: 0 for name in sorted(EVIDENCE_JUDGMENTS)}
    phenomena = {name: 0 for name in REQUIRED_PHENOMENA}
    sources: dict[str, int] = {}
    restricted = 0
    assessments = 0
    for case in cases:
        case_id = case["case_id"]
        _text(case.get("event_version_ref"), case_id, "event_version_ref", 128)
        annotation = case["annotation"]
        state = annotation["state"]
        if state == "unlabeled" and annotation.get("reviews"):
            raise _error(case_id, "cannot attach reviews while marked unlabeled")
        if state == "synthetic_fixture" and (
            annotation.get("reviews") or annotation.get("adjudication")
        ):
            raise _error(case_id, "synthetic fixtures cannot carry human gold provenance")
        evidence_roles = _validate_event_evidence(case)
        _validate_event_facts(case, evidence_roles)
        if state in {"synthetic_fixture", "adjudicated"}:
            status, direction, judgment, case_phenomena = _validate_impact_label(case)
            assessments += 1
            statuses[status] += 1
            directions[direction] += 1
            judgments[judgment] += 1
            for item in case_phenomena:
                phenomena[item] += 1
        for review in annotation.get("reviews", []):
            _validate_impact_label(case, review["labels"])
        if state in {"owner_labeled", "algorithm_labeled"}:
            # Owner and silver labels follow the same contract but never count as gold assessments.
            _validate_impact_label(case)
        for key in ("owner_label", "owner_recheck"):
            # After a resolution the first label may differ from the final one; both stay checked.
            if isinstance(annotation.get(key), dict):
                _validate_impact_label(case, annotation[key].get("labels"))
        language = case["language"]
        languages[language] = languages.get(language, 0) + 1
        source = case.get("source_kind")
        if not isinstance(source, str) or not source:
            raise _error(case_id, "requires source_kind")
        sources[source] = sources.get(source, 0) + 1
        if case["text_storage"] == "restricted_reference":
            restricted += 1

    target_actual = {
        "documents": generic.documents,
        "event_groups": generic.event_groups,
        "assessments": assessments,
        "english_cases": languages.get("en", 0),
        "chinese_cases": languages.get("zh", 0),
        "security_cases": generic.security_cases,
        "adjudicated_cases": generic.annotation_state_counts.get("adjudicated", 0),
        "source_kinds": len([name for name in sources if name != "synthetic"]),
        "insufficient_or_conflicting_cases": (
            statuses["insufficient_evidence"] + judgments["conflicting"]
        ),
    }
    gaps = {
        key: max(
            0,
            max(MINIMUM_TARGETS[key], _positive_int(plan[key], key)) - actual,
        )
        for key, actual in target_actual.items()
    }
    for name in REQUIRED_PHENOMENA:
        gaps[f"phenomenon:{name}"] = max(
            0,
            max(
                MINIMUM_PHENOMENON_CASES,
                _positive_int(required_slices[name], f"required_phenomena.{name}"),
            )
            - phenomena[name],
        )
    all_adjudicated = generic.annotation_state_counts.get("adjudicated", 0) == len(cases)
    holdout_verified = _verified_holdout(manifest, cases, root)
    private_evidence_verified = _verified_private_evidence(manifest, root)
    evidence_review = manifest.get("impact_evidence_review")
    if not isinstance(evidence_review, Mapping):
        evidence_review = {}
    publishable = (
        bool(cases)
        and not any(gaps.values())
        and all_adjudicated
        and restricted == len(cases)
        and all(generic.split_counts[name] > 0 for name in ALLOWED_SPLITS)
        and manifest.get("source_database_verified_at_admission") is True
        and holdout_verified
        and private_evidence_verified
        and evidence_review.get("assessments_verified") == assessments
    )
    warnings: list[str] = []
    if generic.annotation_state_counts.get("synthetic_fixture"):
        warnings.append("synthetic impact fixtures validate the contract only; they are not quality gold")
    if not publishable:
        warnings.append("impact quality claims remain blocked until the private adjudicated plan passes")
    if not holdout_verified:
        warnings.append("time/source blind holdout is not verified")
    if not private_evidence_verified:
        warnings.append("private event evidence roles and normalized artifacts are not verified")
    return ImpactEvaluationReport(
        dataset_version=generic.dataset_version,
        cases=len(cases),
        assessments=assessments,
        split_counts=generic.split_counts,
        language_counts=dict(sorted(languages.items())),
        status_counts=statuses,
        direction_counts=directions,
        evidence_judgment_counts=judgments,
        phenomenon_counts=phenomena,
        source_kind_counts=dict(sorted(sources.items())),
        annotation_state_counts=generic.annotation_state_counts,
        target_gaps=gaps,
        publishable_impact_gold=publishable,
        warnings=tuple(warnings),
    )


def write_impact_evaluation_report(
    dataset_path: Path | str, output_path: Path | str
) -> ImpactEvaluationReport:
    report = validate_impact_evaluation_dataset(dataset_path)
    Path(output_path).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report
