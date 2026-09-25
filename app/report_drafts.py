from __future__ import annotations

"""Validate a model's cited draft before any generation or publication path uses it.

This checks references and shape, not whether a sentence is entailed by its source.
"""

from .report_versions import CHANNELS, _label, _source_url

DRAFT_SCHEMA = "infohub.report-draft/1.0"
MAX_CLAIMS_PER_CHANNEL = 12
MAX_REFERENCES_PER_CLAIM = 3
MAX_CLAIM_CHARS = 280


def _fields(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} has unsupported or missing fields")
    return value


def render_validated_draft(manifest: dict, draft: dict) -> tuple[str, list[dict], dict]:
    """Render controlled Markdown from a closed, source-indexed draft contract."""
    if manifest.get("schema_version") != "infohub.report-input/1.0" or manifest.get("report_type") != "calendar_daily":
        raise ValueError("unsupported report input schema")
    materials = manifest.get("items")
    if not isinstance(materials, list) or not materials:
        raise ValueError("report input has no materials")
    draft = _fields(draft, {"schema_version", "date", "sections"}, "draft")
    if draft["schema_version"] != DRAFT_SCHEMA or draft["date"] != manifest.get("date"):
        raise ValueError("draft schema or date does not match the input")
    sections = draft["sections"]
    if not isinstance(sections, list) or not 1 <= len(sections) <= len(CHANNELS):
        raise ValueError("draft must contain one to three sections")

    allowed_channels = dict(CHANNELS)
    seen_channels: set[str] = set()
    lines = [f"# 行业日报 · {_label(manifest['date'])}", "",
             "模型分析；来源引用已做结构校验，结论仍需结合原文核实。", ""]
    citations: list[dict] = []
    counts: dict[str, int] = {}
    for section in sections:
        section = _fields(section, {"channel", "claims"}, "section")
        channel = section["channel"]
        if not isinstance(channel, str) or channel not in allowed_channels or channel in seen_channels:
            raise ValueError("section channel is unsupported or repeated")
        seen_channels.add(channel)
        claims = section["claims"]
        if not isinstance(claims, list) or not 1 <= len(claims) <= MAX_CLAIMS_PER_CHANNEL:
            raise ValueError("section claim count is outside the supported range")
        counts[channel] = len(claims)
        lines.extend((f"## {allowed_channels[channel]}", ""))
        for claim in claims:
            claim = _fields(claim, {"text", "input_ordinals"}, "claim")
            message = claim["text"]
            if (not isinstance(message, str) or not message.strip()
                    or len(message) > MAX_CLAIM_CHARS
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in message)
                    or "http://" in message.lower() or "https://" in message.lower()):
                raise ValueError("claim text is empty, too long or contains unsupported content")
            ordinals = claim["input_ordinals"]
            if not isinstance(ordinals, list) or not 1 <= len(ordinals) <= MAX_REFERENCES_PER_CLAIM:
                raise ValueError("claim requires one to three source references")
            if len(set(map(str, ordinals))) != len(ordinals):
                raise ValueError("claim repeats a source reference")
            sources: list[tuple[int, dict, str]] = []
            for ordinal in ordinals:
                if type(ordinal) is not int or not 0 <= ordinal < len(materials):
                    raise ValueError("claim references an unknown input ordinal")
                material = materials[ordinal]
                if not isinstance(material, dict) or material.get("channel") != channel:
                    raise ValueError("claim source does not belong to its channel")
                sources.append((ordinal, material, _source_url(material.get("url"))))
            markers: list[str] = []
            links: list[str] = []
            for ordinal, material, url in sources:
                number = len(citations) + 1
                citations.append({
                    "number": number,
                    "input_ordinal": ordinal,
                    "legacy_item_id": material["item_id"],
                    "document_version_id": material["document_version_id"],
                    "source_url": url,
                })
                markers.append(f"【{number}】")
                links.append(f"【{number}】[{_label(material['source_name'])}](<{url}>)")
            lines.append(f"- {_label(message.strip())}{''.join(markers)}")
            lines.append(f"  来源：{' · '.join(links)}")
        lines.append("")
    coverage = {
        "schema_version": "infohub.report-coverage/1.0",
        "selection": manifest["coverage"],
        "reported_by_channel": counts,
        "citation_count": len(citations),
        "point_in_time_status": manifest["point_in_time_status"],
        "method": "model_draft_reference_validation",
        "claim_entailment_status": "not_automatically_verified",
    }
    return "\n".join(lines).rstrip() + "\n", citations, coverage
