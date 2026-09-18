from __future__ import annotations

"""Deterministic, metadata-only sampling plans for private evaluation annotation."""

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SCHEMA_VERSION = "evaluation-sampling-plan-v1"
FORBIDDEN_EXPORT_FIELDS = {
    "title", "title_en", "title_zh", "summary", "raw_summary", "text", "url",
    "database_path", "payload_ref",
}
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_SPACE = re.compile(r"\s+")


class EvaluationSamplingError(RuntimeError):
    pass


@dataclass(frozen=True)
class SamplingArtifacts:
    manifest_path: Path
    candidates_path: Path
    report_path: Path
    candidate_pool: int
    selected: int


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _language(title: str, title_en: str, title_zh: str) -> str:
    # Legacy rows keep the source title in ``title`` and presentation translations
    # in title_en/title_zh. Prefer the source field so a translated display title
    # does not silently change the source language stratum.
    sample = (title or "").strip()
    if sample and _CJK.search(sample):
        return "zh"
    if sample and any(ch.isalpha() for ch in sample):
        return "en"
    sample = (title_zh or title_en or "").strip()
    if sample and _CJK.search(sample):
        return "zh"
    if sample and any(ch.isalpha() for ch in sample):
        return "en"
    return "und"


def _document_kind(source_key: str, source_type: str, event_type: str, url: str) -> str:
    value = " ".join((source_key, source_type, event_type, url)).lower()
    if "sec.gov" in value or "sec" in source_key.lower() or "filing" in value:
        return "sec_filing"
    if "hkex" in value or "announcement" in value:
        return "exchange_announcement"
    if "fast" in value or "快讯" in value or "brief" in value:
        return "news_brief"
    return "article"


def _time_bucket(value: str) -> str:
    match = re.match(r"^(\d{4})-(\d{2})", value or "")
    if not match:
        return "unknown"
    month = int(match.group(2))
    return f"{match.group(1)}-Q{(month - 1) // 3 + 1}" if 1 <= month <= 12 else "unknown"


def _normalized_title(value: str) -> str:
    return _SPACE.sub(" ", re.sub(r"[^\w\u3400-\u9fff]+", " ", value.lower())).strip()


def _distribution(rows: list[dict], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row[key]) for row in rows).items()))


def _assert_safe(row: dict) -> None:
    leaked = FORBIDDEN_EXPORT_FIELDS.intersection(row)
    if leaked:
        raise EvaluationSamplingError(f"candidate export contains forbidden fields: {sorted(leaked)}")
    serialized = _json(row)
    if "file:/" in serialized or "sqlite:/" in serialized:
        raise EvaluationSamplingError("candidate export contains a local path")


def _connect_read_only(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise EvaluationSamplingError("database does not exist")
    db = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    required = {"items", "sources", "story_items", "item_companies", "companies", "item_topics"}
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = required - present
    if missing:
        db.close()
        raise EvaluationSamplingError(f"database is missing required tables: {sorted(missing)}")
    return db


def _load_pool(db: sqlite3.Connection, *, seed: str) -> list[dict]:
    company_rows: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in db.execute(
        """SELECT link.item_id,company.slug,company.market,company.cik
           FROM item_companies AS link JOIN companies AS company ON company.id=link.company_id
           ORDER BY link.item_id,company.slug"""
    ):
        company_rows[row["item_id"]].append(row)
    topic_rows: dict[int, list[str]] = defaultdict(list)
    for row in db.execute("SELECT item_id,topic_slug FROM item_topics ORDER BY item_id,topic_slug"):
        topic_rows[row["item_id"]].append(row["topic_slug"])
    story_rows = {
        row["item_id"]: row["story_id"]
        for row in db.execute("SELECT item_id,story_id FROM story_items")
    }
    company_story_groups: dict[str, set[str]] = defaultdict(set)
    for item_id, companies in company_rows.items():
        story = story_rows.get(item_id)
        if story:
            for company in companies:
                company_story_groups[company["slug"]].add(story)

    rows: list[dict] = []
    query = """SELECT item.*,source.key AS source_key,source.name AS source_name,
                      source.type AS source_type,source.tier AS source_tier
               FROM items AS item JOIN sources AS source ON source.id=item.source_id
               ORDER BY item.id"""
    for item in db.execute(query):
        item_id = int(item["id"])
        title = str(item["title"] or "")
        raw_text = str(item["raw_summary"] if item["raw_summary"] is not None else item["summary"] or "")
        canonical = _canonical_url(str(item["url"] or ""))
        language = _language(title, str(item["title_en"] or ""), str(item["title_zh"] or ""))
        kind = _document_kind(item["source_key"], item["source_type"], str(item["event_type"] or ""), canonical)
        companies = company_rows.get(item_id, [])
        company_refs = [row["slug"] for row in companies]
        us_linked = any(
            str(row["market"] or "").lower() in {"us", "nasdaq", "nyse", "amex"}
            or bool(str(row["cik"] or "").strip())
            for row in companies
        )
        story = story_rows.get(item_id)
        normalized_title = _normalized_title(title)
        event_basis = f"legacy-story:{story}" if story else f"document:{item_id}"
        origin_basis = canonical or normalized_title or f"document:{item_id}"
        hard_negative = bool(company_refs) and any(
            len(company_story_groups[company]) >= 2 for company in company_refs
        )
        row = {
            "candidate_id": f"candidate-{_sha(f'{seed}:{item_id}')[:20]}",
            "document_ref": f"legacy-item:{item_id}",
            "object_ref": f"private-db:items/{item_id}",
            "content_sha256": _sha(_json({"title": title, "text": raw_text})),
            "source_ref": str(item["source_key"]),
            "source_tier": str(item["source_tier"]),
            "language": language,
            "document_kind": kind,
            "time_bucket": _time_bucket(str(item["published_at"] or "")),
            "published_at": str(item["published_at"] or "") or None,
            "content_extent": "excerpt" if raw_text.strip() else "title_only",
            "official": bool(item["official"]),
            "event_group_ref": f"event-{_sha(event_basis)[:20]}",
            "origin_group_ref": f"origin-{_sha(origin_basis)[:20]}",
            "origin_group_basis": "canonical_url",
            "company_refs": company_refs,
            "topic_refs": topic_rows.get(item_id, []),
            "us_market_linked": us_linked,
            "sec_related": kind == "sec_filing",
            "hard_negative_pool": hard_negative,
            "selection_key": _sha(f"{seed}:{item['source_key']}:{language}:{kind}:{item_id}"),
            "annotation_state": "unlabeled",
        }
        _assert_safe(row)
        rows.append(row)
    return rows


def _round_robin(pool: list[dict], target: int, used: set[str]) -> list[dict]:
    strata: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for row in pool:
        if row["candidate_id"] in used:
            continue
        key = (
            row["source_ref"], row["language"], row["document_kind"],
            row["time_bucket"], "official" if row["official"] else "non_official",
        )
        strata[key].append(row)
    for values in strata.values():
        values.sort(key=lambda row: row["selection_key"])
    selected: list[dict] = []
    ordered_keys = sorted(strata, key=lambda key: _sha("|".join(key)))
    available = sum(len(values) for values in strata.values())
    while len(selected) < min(target, available):
        progressed = False
        for key in ordered_keys:
            if strata[key] and len(selected) < target:
                selected.append(strata[key].pop(0))
                progressed = True
        if not progressed:
            break
    return sorted(selected, key=lambda row: row["selection_key"])


def _select(pool: list[dict], target: int) -> list[dict]:
    selected: list[dict] = []
    used: set[str] = set()
    language_floor = min(200, target // 2)
    for language in ("zh", "en"):
        part = _round_robin(
            [row for row in pool if row["language"] == language], language_floor, used
        )
        selected.extend(part)
        used.update(row["candidate_id"] for row in part)
    remainder = _round_robin(pool, target - len(selected), used)
    selected.extend(remainder)
    return sorted(selected, key=lambda row: row["selection_key"])


def _coverage(rows: list[dict]) -> dict:
    return {
        "documents": len(rows),
        "event_groups": len({row["event_group_ref"] for row in rows}),
        "origin_groups": len({row["origin_group_ref"] for row in rows}),
        "languages": _distribution(rows, "language"),
        "document_kinds": _distribution(rows, "document_kind"),
        "source_refs": _distribution(rows, "source_ref"),
        "source_tiers": _distribution(rows, "source_tier"),
        "time_buckets": _distribution(rows, "time_bucket"),
        "content_extents": _distribution(rows, "content_extent"),
        "official": sum(1 for row in rows if row["official"]),
        "sec_related": sum(1 for row in rows if row["sec_related"]),
        "us_market_linked": sum(1 for row in rows if row["us_market_linked"]),
        "hard_negative_pool": sum(1 for row in rows if row["hard_negative_pool"]),
    }


def build_sampling_plan(
    database: Path | str,
    output_dir: Path | str,
    *,
    target: int = 600,
    seed: str = "infohub-evaluation-v1",
) -> SamplingArtifacts:
    if target <= 0:
        raise EvaluationSamplingError("target must be positive")
    database = Path(database)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    db = _connect_read_only(database)
    try:
        pool = _load_pool(db, seed=seed)
        schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        max_item_id = int(db.execute("SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0])
    finally:
        db.close()
    selected = _select(pool, target)
    pool_coverage = _coverage(pool)
    selected_coverage = _coverage(selected)
    snapshot_fingerprint = _sha(_json({
        "schema_version": schema_version,
        "items": len(pool),
        "max_item_id": max_item_id,
        "content_hashes": [row["content_sha256"] for row in pool],
    }))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "sampling_version": "p12c-v1",
        "seed": seed,
        "target_documents": target,
        "database_snapshot_fingerprint": snapshot_fingerprint,
        "database_schema_version": schema_version,
        "selection_method": "deterministic language floors then round-robin over source/language/kind/time/official strata",
        "language_floor": min(200, target // 2),
        "privacy": {
            "contains_text": False,
            "contains_titles": False,
            "contains_urls": False,
            "contains_local_paths": False,
            "restricted_content_remains_in_private_database": True,
        },
        "label_policy": "legacy AI/editorial fields are sampling signals only and are not gold labels",
        "group_policy": {
            "event": "hashed legacy story identity; unclustered documents remain separate",
            "origin": "hashed canonical URL; syndicated and translated origin detection remains incomplete",
        },
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "sampling_version": "p12c-v1",
        "database_snapshot_fingerprint": snapshot_fingerprint,
        "pool": pool_coverage,
        "selected": selected_coverage,
        "target_gaps": {
            "documents": max(0, target - len(selected)),
            "zh_documents": max(0, 200 - selected_coverage["languages"].get("zh", 0)),
            "en_documents": max(0, 200 - selected_coverage["languages"].get("en", 0)),
            "event_groups": max(0, 150 - selected_coverage["event_groups"]),
            "hard_negative_groups": max(0, 40 - len({
                row["event_group_ref"] for row in selected if row["hard_negative_pool"]
            })),
        },
        "warnings": [
            "candidate metadata is unlabeled and cannot be used to claim NLP quality",
            "language, document kind, US-market links, and hard-negative status are sampling heuristics",
            "canonical URL origin groups do not yet detect all syndication or translation relationships",
            "company links in the legacy database are not gold entity labels",
        ],
    }
    manifest_path = output / "manifest.json"
    candidates_path = output / "candidates.jsonl"
    report_path = output / "report.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    candidates_path.write_text("".join(_json(row) + "\n" for row in selected), encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SamplingArtifacts(manifest_path, candidates_path, report_path, len(pool), len(selected))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a metadata-only InfoHub evaluation sampling plan")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", type=int, default=600)
    parser.add_argument("--seed", default="infohub-evaluation-v1")
    args = parser.parse_args()
    artifacts = build_sampling_plan(args.database, args.output, target=args.target, seed=args.seed)
    print(json.dumps({**asdict(artifacts), "manifest_path": str(artifacts.manifest_path), "candidates_path": str(artifacts.candidates_path), "report_path": str(artifacts.report_path)}, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
