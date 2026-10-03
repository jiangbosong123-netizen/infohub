from __future__ import annotations

"""Verify tone quote locators against immutable, hash-checked raw CAS payloads."""

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping

from .analysis_runs import AnalysisRunError
from .ingest import PayloadIntegrityError, verify_payload
from .tone_contracts import TONE_SCHEMA_VERSION

TONE_QUOTE_VALIDATOR_VERSION = "tone-quote-validator-v2"
# Quotes must be source text. Connector payloads keep the source record under /source_record.
# Flat candidate payloads (scraped generated_metadata and legacy excerpts) carry publisher text
# only in title and summary; their other fields are derived, and a legacy excerpt's
# extra.legacy_snapshot preserves old model output that must never pass as a quote.
FLAT_SOURCE_TEXT_POINTERS = frozenset({"/title", "/summary"})
_BAD_POINTER_ESCAPE = re.compile(r"~(?![01])")
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def _is_source_text(payload: object, pointer: str) -> bool:
    if isinstance(payload, Mapping) and "source_record" in payload:
        return pointer.startswith("/source_record/")
    return pointer in FLAT_SOURCE_TEXT_POINTERS


def _resolve_pointer(payload: object, pointer: str) -> object:
    if not pointer.startswith("/") or _BAD_POINTER_ESCAPE.search(pointer):
        raise AnalysisRunError("tone evidence json_pointer is invalid")
    current = payload
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise AnalysisRunError("tone evidence json_pointer does not exist")
            current = current[token]
        elif isinstance(current, list):
            if not _ARRAY_INDEX.fullmatch(token):
                raise AnalysisRunError("tone evidence json_pointer array index is invalid")
            index = int(token)
            if index >= len(current):
                raise AnalysisRunError("tone evidence json_pointer does not exist")
            current = current[index]
        else:
            raise AnalysisRunError("tone evidence json_pointer traverses a scalar")
    return current


def verify_tone_quotes(
    db: sqlite3.Connection, *, schema_version: str, data: Mapping[str, object]
) -> dict:
    if schema_version != TONE_SCHEMA_VERSION:
        return {"validator_version": None, "status": "not_applicable", "spans": []}
    assessments = data.get("assessments")
    if not isinstance(assessments, list):
        return {"validator_version": TONE_QUOTE_VALIDATOR_VERSION, "status": "not_applicable", "spans": []}
    cached: dict[str, tuple[object, str]] = {}
    verified = []
    for assessment in assessments:
        if not isinstance(assessment, Mapping):
            continue
        evidence = assessment.get("evidence")
        if not isinstance(evidence, list):
            continue
        for span in evidence:
            if not isinstance(span, Mapping):
                continue
            evidence_id = span["evidence_id"]
            locator = span["locator"]
            if evidence_id not in cached:
                row = db.execute(
                    """SELECT payload_ref,payload_sha256,size_bytes,media_type,encoding
                       FROM raw_records WHERE id=?""",
                    (evidence_id,),
                ).fetchone()
                if row is None:
                    raise AnalysisRunError("tone evidence raw record does not exist")
                if row["media_type"] != "application/json" or row["encoding"] != "utf-8":
                    raise AnalysisRunError("tone evidence payload is not canonical UTF-8 JSON")
                try:
                    path = verify_payload(row["payload_ref"], row["payload_sha256"])
                    payload_bytes = path.read_bytes()
                except (PayloadIntegrityError, OSError) as exc:
                    raise AnalysisRunError("tone evidence payload is missing or corrupt") from exc
                if (
                    len(payload_bytes) != row["size_bytes"]
                    or hashlib.sha256(payload_bytes).hexdigest() != row["payload_sha256"]
                ):
                    raise AnalysisRunError("tone evidence payload does not match its ledger")
                try:
                    payload = json.loads(payload_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AnalysisRunError("tone evidence payload is not valid UTF-8 JSON") from exc
                cached[evidence_id] = (payload, row["payload_sha256"])
            payload, payload_sha256 = cached[evidence_id]
            if not _is_source_text(payload, locator["json_pointer"]):
                raise AnalysisRunError("tone evidence must quote source text, not generated or derived fields")
            value = _resolve_pointer(payload, locator["json_pointer"])
            if not isinstance(value, str):
                raise AnalysisRunError("tone evidence json_pointer must resolve to a string")
            start = locator["start_offset"]
            end = locator["end_offset"]
            if end > len(value) or value[start:end] != span["quote"]:
                raise AnalysisRunError("tone evidence quote does not match the frozen payload")
            verified.append({
                "evidence_id": evidence_id,
                "payload_sha256": payload_sha256,
                "json_pointer": locator["json_pointer"],
                "start_offset": start,
                "end_offset": end,
                "quote_sha256": hashlib.sha256(span["quote"].encode("utf-8")).hexdigest(),
            })
    return {
        "validator_version": TONE_QUOTE_VALIDATOR_VERSION,
        "status": "passed",
        "offset_unit": "unicode_code_point",
        "spans": verified,
    }


__all__ = ["TONE_QUOTE_VALIDATOR_VERSION", "verify_tone_quotes"]
