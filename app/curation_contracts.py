from __future__ import annotations

"""Strict task contracts and an honest adapter for the legacy curation projection."""

import json
from collections.abc import Mapping

from .analysis_runs import AnalysisRunError

CURATION_PIPELINE_VERSION = "curation-contracts-v1"
CURATION_SCHEMAS = {
    "translation": "infohub.translation/1.0",
    "relevance": "infohub.relevance/1.0",
    "summarization": "infohub.summarization/1.0",
    "importance": "infohub.importance/1.0",
}
AI_CATEGORIES = {"model", "product", "industry", "paper", "opinion"}
PUBLISHABLE_STATUSES = {"valid", "needs_review"}


def _json_copy(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AnalysisRunError("curation data is not valid JSON") from exc


def _text(value: object, field: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise AnalysisRunError(f"{field} must be a string")
    cleaned = value.strip()
    if required and not cleaned:
        raise AnalysisRunError(f"{field} is required")
    if len(cleaned) > maximum:
        raise AnalysisRunError(f"{field} exceeds {maximum} characters")
    return cleaned


def _exact_keys(data: Mapping[str, object], required: set[str], optional: set[str] = set()) -> None:
    missing = required - set(data)
    unknown = set(data) - required - optional
    if missing:
        raise AnalysisRunError(f"curation data is missing fields: {sorted(missing)}")
    if unknown:
        raise AnalysisRunError(f"curation data has unknown fields: {sorted(unknown)}")


def _nonpublishable_data(data: Mapping[str, object]) -> dict:
    _exact_keys(data, {"reason_code"})
    return {"reason_code": _text(data["reason_code"], "reason_code", 100)}


def _translation(data: Mapping[str, object]) -> dict:
    _exact_keys(
        data,
        {"translated_title", "source_language", "target_language", "transformation"},
        {"legacy_provenance"},
    )
    source_language = _text(data["source_language"], "source_language", 20)
    if source_language not in {"zh", "en", "und"}:
        raise AnalysisRunError("translation source_language is unsupported")
    if data["target_language"] != "zh-Hans":
        raise AnalysisRunError("translation target_language must be zh-Hans")
    transformation = _text(data["transformation"], "transformation", 20)
    if transformation not in {"translation", "refinement", "unknown"}:
        raise AnalysisRunError("translation transformation is unsupported")
    result = {
        "translated_title": _text(data["translated_title"], "translated_title", 120),
        "source_language": source_language,
        "target_language": "zh-Hans",
        "transformation": transformation,
    }
    if "legacy_provenance" in data:
        result["legacy_provenance"] = _text(data["legacy_provenance"], "legacy_provenance", 40)
    return result


def _relevance(data: Mapping[str, object]) -> dict:
    _exact_keys(data, {"label", "rationale", "ai_category", "policy_override"})
    label = _text(data["label"], "label", 20)
    if label not in {"relevant", "not_relevant"}:
        raise AnalysisRunError("publishable relevance label must be relevant or not_relevant")
    category = data["ai_category"]
    if category is not None and category not in AI_CATEGORIES:
        raise AnalysisRunError("relevance ai_category is unsupported")
    if type(data["policy_override"]) is not bool:
        raise AnalysisRunError("relevance policy_override must be a boolean")
    return {
        "label": label,
        "rationale": _text(data["rationale"], "rationale", 200, required=False),
        "ai_category": category,
        "policy_override": data["policy_override"],
    }


def _summarization(data: Mapping[str, object], allowed_evidence: set[str]) -> dict:
    _exact_keys(data, {"summary", "claims"}, {"legacy_provenance"})
    claims = data["claims"]
    if not isinstance(claims, list) or not claims:
        raise AnalysisRunError("summarization claims must be a non-empty list")
    clean_claims = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise AnalysisRunError("summarization claim must be an object")
        _exact_keys(claim, {"text", "evidence_ids"})
        evidence = claim["evidence_ids"]
        if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) for x in evidence):
            raise AnalysisRunError("summarization claim evidence_ids must be a non-empty string list")
        unknown = set(evidence) - allowed_evidence
        if unknown:
            raise AnalysisRunError(f"summarization claim references unknown evidence IDs: {sorted(unknown)}")
        clean_claims.append({
            "text": _text(claim["text"], "claim text", 500),
            "evidence_ids": list(evidence),
        })
    result = {
        "summary": _text(data["summary"], "summary", 500),
        "claims": clean_claims,
    }
    if "legacy_provenance" in data:
        result["legacy_provenance"] = _text(data["legacy_provenance"], "legacy_provenance", 40)
    return result


def _importance(data: Mapping[str, object]) -> dict:
    _exact_keys(data, {"score", "rationale", "scale"})
    score = data["score"]
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise AnalysisRunError("importance score must be an integer from 0 to 100")
    if data["scale"] != "editorial_importance_not_probability":
        raise AnalysisRunError("importance scale must identify editorial importance")
    return {
        "score": score,
        "rationale": _text(data["rationale"], "rationale", 200, required=False),
        "scale": data["scale"],
    }


def validate_curation_data(
    *, task_type: str, schema_version: str, status: str,
    data: object, allowed_evidence: set[str],
) -> dict:
    """Validate registered curation schemas; leave unrelated future schemas untouched."""
    expected = CURATION_SCHEMAS.get(task_type)
    registered = schema_version in set(CURATION_SCHEMAS.values())
    if not registered:
        return _json_copy(data)  # type: ignore[return-value]
    if schema_version != expected:
        raise AnalysisRunError("curation schema does not match its task type")
    if not isinstance(data, Mapping):
        raise AnalysisRunError("curation data must be an object")
    if status not in PUBLISHABLE_STATUSES:
        return _nonpublishable_data(data)
    if task_type == "translation":
        return _translation(data)
    if task_type == "relevance":
        return _relevance(data)
    if task_type == "summarization":
        return _summarization(data, allowed_evidence)
    if task_type == "importance":
        return _importance(data)
    raise AnalysisRunError("unsupported curation contract")


def _legacy_status_data(value: object, *, missing: str, refused: bool = False) -> tuple[str, dict]:
    if refused:
        return "refused", {"reason_code": "legacy_content_filter_sentinel"}
    if value is None or value == "":
        return "insufficient_evidence", {"reason_code": missing}
    return "needs_review", {}


def adapt_legacy_curation(
    snapshot: Mapping[str, object], *, subject_version_id: str, evidence_id: str,
) -> dict[str, dict]:
    """Map legacy columns without presenting them as newly validated model output."""
    subject = {"type": "document", "version_id": subject_version_id}
    evidence_ids = [evidence_id]
    title = str(snapshot.get("title") or "")
    title_zh = str(snapshot.get("title_zh") or "")
    source_language = "zh" if any("\u3400" <= char <= "\u9fff" for char in title) else "en" if title else "und"
    translation_status, translation_data = _legacy_status_data(
        title_zh, missing="legacy_translation_missing", refused=title_zh == "-",
    )
    if translation_status == "needs_review":
        translation_data = {
            "translated_title": title_zh,
            "source_language": source_language,
            "target_language": "zh-Hans",
            "transformation": "refinement" if source_language == "zh" else "translation",
            "legacy_provenance": "compound_curation_unknown",
        }

    tmt = snapshot.get("tmt")
    relevance_status, relevance_data = _legacy_status_data(tmt, missing="legacy_relevance_missing")
    if relevance_status == "needs_review":
        if type(tmt) is not int or tmt not in {0, 1}:
            raise AnalysisRunError("legacy tmt must be 0, 1, or null")
        category = snapshot.get("ai_cat") or None
        category = category if category in AI_CATEGORIES else None
        companies = snapshot.get("companies")
        no_companies = companies is None or companies == "" or companies == "[]" or companies == []
        policy_override = bool(snapshot.get("official")) or not no_companies
        relevance_data = {
            "label": "relevant" if tmt else "not_relevant",
            "rationale": str(snapshot.get("reason") or "")[:200],
            "ai_category": category,
            "policy_override": policy_override,
        }

    summary = str(snapshot.get("summary") or "")
    raw_summary = snapshot.get("raw_summary")
    summary_status, summary_data = _legacy_status_data(summary, missing="legacy_summary_missing")
    if summary_status == "needs_review":
        provenance = "unknown" if raw_summary is None else (
            "distinct_from_original" if summary != str(raw_summary) else "same_as_original"
        )
        summary_data = {
            "summary": summary,
            "claims": [{"text": summary, "evidence_ids": evidence_ids}],
            "legacy_provenance": provenance,
        }

    score = snapshot.get("score")
    importance_status, importance_data = _legacy_status_data(
        score, missing="legacy_importance_missing", refused=score == -1,
    )
    if importance_status == "needs_review":
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise AnalysisRunError("legacy score must be -1, 0..100, or null")
        importance_data = {
            "score": score,
            "rationale": str(snapshot.get("reason") or "")[:200],
            "scale": "editorial_importance_not_probability",
        }

    values = {
        "translation": (translation_status, translation_data),
        "relevance": (relevance_status, relevance_data),
        "summarization": (summary_status, summary_data),
        "importance": (importance_status, importance_data),
    }
    envelopes = {}
    for task_type, (status, data) in values.items():
        envelope = {
            "schema_version": CURATION_SCHEMAS[task_type],
            "subject": subject,
            "status": status,
            "evidence_ids": evidence_ids,
            "data": data,
        }
        envelope["data"] = validate_curation_data(
            task_type=task_type, schema_version=envelope["schema_version"],
            status=status, data=data, allowed_evidence=set(evidence_ids),
        )
        envelopes[task_type] = envelope
    return envelopes
