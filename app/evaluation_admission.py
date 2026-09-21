from __future__ import annotations

"""Freeze a private sampling plan into an unlabeled, leakage-aware dataset."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .evaluation import EvaluationDatasetError, validate_evaluation_dataset
from .evaluation_sampling import (
    FORBIDDEN_EXPORT_FIELDS, SCHEMA_VERSION as SAMPLING_SCHEMA,
    _connect_read_only, _json, _load_pool, _sha,
)

ADMISSION_VERSION = "evaluation-admission-v3"
SPLIT_TARGETS = {"train": 0.60, "dev": 0.20, "test": 0.20}
TARGET_PLAN = {"documents": 600, "event_groups": 150,
               "impact_annotations": 300, "security_cases": 50}
SPLITS = ("train", "dev", "test")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AdmissionReport:
    status: str
    candidates: int
    components: int
    split_counts: dict[str, int]
    source_database_verified: bool
    holdout_status: str
    publishable_gold: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_text(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationDatasetError(f"candidate requires {key}")
    return value


def _load_candidates(path: Path) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationDatasetError(f"cannot read candidates: {exc}") from exc
    ids: set[str] = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationDatasetError(f"invalid candidate JSON at line {number}") from exc
        if not isinstance(row, dict):
            raise EvaluationDatasetError(f"candidate line {number} is not an object")
        if FORBIDDEN_EXPORT_FIELDS.intersection(row) or "text" in row:
            raise EvaluationDatasetError(f"candidate line {number} contains restricted fields")
        for key in ("candidate_id", "document_ref", "object_ref", "content_sha256",
                    "event_group_ref", "origin_group_ref", "source_ref", "language"):
            _require_text(row, key)
        if row["candidate_id"] in ids:
            raise EvaluationDatasetError("duplicate candidate_id")
        ids.add(row["candidate_id"])
        if not _SHA256.fullmatch(row["content_sha256"]):
            raise EvaluationDatasetError("candidate has invalid content_sha256")
        if not row["object_ref"].startswith("private-db:"):
            raise EvaluationDatasetError("candidate object_ref must be a private-db reference")
        if row.get("annotation_state") != "unlabeled":
            raise EvaluationDatasetError("sampling input must remain unlabeled")
        rows.append(row)
    if not rows:
        raise EvaluationDatasetError("sampling plan has no candidates")
    return rows


def _components(rows: list[dict]) -> list[list[dict]]:
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        parent[find(left)] = find(right)

    owners: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for key in ("event_group_ref", "origin_group_ref", "content_sha256", "document_ref"):
            value = (key, row[key])
            if value in owners:
                union(index, owners[value])
            else:
                owners[value] = index
    groups: dict[int, list[dict]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[find(index)].append(row)
    return list(groups.values())


def _verify_source_database(rows: list[dict], manifest: dict, database: Path) -> None:
    seed = _require_text(manifest, "seed")
    db = _connect_read_only(database)
    try:
        pool = _load_pool(db, seed=seed)
        schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
        max_item_id = int(db.execute("SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0])
    finally:
        db.close()
    fingerprint = _sha(_json({
        "schema_version": schema_version, "items": len(pool), "max_item_id": max_item_id,
        "content_hashes": [row["content_sha256"] for row in pool],
    }))
    if fingerprint != manifest.get("database_snapshot_fingerprint"):
        raise EvaluationDatasetError("source database differs from sampling snapshot")
    by_id = {row["candidate_id"]: row for row in pool}
    if any(by_id.get(row["candidate_id"]) != row for row in rows):
        raise EvaluationDatasetError("candidate metadata or content differs from source database")


def _split_components(components: list[list[dict]], seed: str) -> dict[str, str]:
    """Keep connected evidence together while approaching SPEC's 60/20/20 split."""
    total = sum(map(len, components))
    targets = {name: total * share for name, share in SPLIT_TARGETS.items()}
    counts: Counter[str] = Counter()
    assignment: dict[str, str] = {}
    ordered = sorted(components, key=lambda group: _digest(
        (seed + ":" + min(row["candidate_id"] for row in group)).encode("utf-8")))
    for group in ordered:
        split = max(SPLITS, key=lambda name: (targets[name] - counts[name], -SPLITS.index(name)))
        for row in group:
            assignment[row["candidate_id"]] = split
        counts[split] += len(group)
    return assignment


def _published_at(row: dict) -> datetime | None:
    value = row.get("published_at")
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvaluationDatasetError("candidate published_at must be a timestamp or null")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationDatasetError("candidate published_at is invalid") from exc
    if stamp.tzinfo is None:
        raise EvaluationDatasetError("candidate published_at requires timezone")
    return stamp.astimezone(timezone.utc)


def _blind_holdout_split(components: list[list[dict]], seed: str) -> tuple[dict[str, str], dict]:
    """Reserve a recent window and one source; never separate connected evidence."""
    total = sum(map(len, components))
    if total < 100:
        raise EvaluationDatasetError("blind holdout requires at least 100 candidates")
    target_test = round(total * SPLIT_TARGETS["test"])
    max_test = round(total * 0.25)
    min_recent = max(10, round(total * 0.05))
    max_recent = round(target_test * 0.75)
    timestamps = [[_published_at(row) for row in group] for group in components]
    dates = sorted({stamp.date() for group in timestamps for stamp in group if stamp}, reverse=True)
    recent_groups: set[int] | None = None
    cutoff = None
    for day in dates:
        selected = {index for index, group in enumerate(timestamps)
                    if any(stamp and stamp.date() >= day for stamp in group)}
        count = sum(len(components[index]) for index in selected)
        if min_recent <= count <= max_recent:
            recent_groups, cutoff = selected, day
            break
        if count > max_recent:
            break
    if recent_groups is None or cutoff is None:
        raise EvaluationDatasetError("no bounded recent window fits the blind-test budget")

    source_groups: dict[str, set[int]] = defaultdict(set)
    source_documents: Counter[str] = Counter()
    for index, group in enumerate(components):
        for row in group:
            source_groups[row["source_ref"]].add(index)
            source_documents[row["source_ref"]] += 1
    eligible = []
    for source, indices in source_groups.items():
        selected = recent_groups | indices
        count = sum(len(components[index]) for index in selected)
        if (max(5, round(total * 0.01)) <= source_documents[source] <= round(total * 0.05)
                and indices - recent_groups and count <= target_test):
            eligible.append((abs(count - round(total * 0.12)),
                             _digest((seed + ":source:" + source).encode()), source, selected))
    if not eligible:
        raise EvaluationDatasetError("no source fits the blind-test budget")
    _, _, heldout_source, test_groups = min(eligible)

    ordered = sorted(range(len(components)), key=lambda index: _digest(
        (seed + ":" + min(row["candidate_id"] for row in components[index])).encode()))
    test_count = sum(len(components[index]) for index in test_groups)
    for index in ordered:
        if test_count >= target_test:
            break
        if index not in test_groups and test_count + len(components[index]) <= max_test:
            test_groups.add(index)
            test_count += len(components[index])
    if test_count < target_test:
        raise EvaluationDatasetError("connected groups cannot fill the blind-test budget")

    dev_groups: set[int] = set()
    dev_count = 0
    target_dev = round(total * SPLIT_TARGETS["dev"])
    for index in ordered:
        if dev_count >= target_dev:
            break
        if index not in test_groups:
            dev_groups.add(index)
            dev_count += len(components[index])
    if not dev_groups or len(test_groups) + len(dev_groups) == len(components):
        raise EvaluationDatasetError("blind split leaves no training or development components")
    assignment = {}
    for index, group in enumerate(components):
        split = "test" if index in test_groups else "dev" if index in dev_groups else "train"
        for row in group:
            assignment[row["candidate_id"]] = split
    cutoff_text = cutoff.isoformat() + "T00:00:00Z"
    recent_documents = sum(1 for group in components for row in group
                           if (stamp := _published_at(row)) is not None
                           and stamp.date() >= cutoff)
    source_count = source_documents[heldout_source]
    if any(assignment[row["candidate_id"]] != "test" for group in components for row in group
           if row["source_ref"] == heldout_source
           or ((stamp := _published_at(row)) is not None and stamp.date() >= cutoff)):
        raise EvaluationDatasetError("blind holdout leaked outside test")
    return assignment, {
        "status": "pending_human_review", "heldout_after": cutoff_text,
        "heldout_source_refs": [heldout_source],
        "recent_documents": recent_documents, "source_documents": source_count,
        "test_documents": test_count,
    }


def admit_sampling_plan(input_dir: Path | str, output_dir: Path | str,
                        *, dataset_version: str, seed: str = "infohub-admission-v1",
                        database: Path | str | None = None,
                        split_policy: str = "balanced") -> AdmissionReport:
    source = Path(input_dir)
    target = Path(output_dir)
    if source.resolve() == target.resolve():
        raise EvaluationDatasetError("input and output directories must differ")
    if not dataset_version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset_version):
        raise EvaluationDatasetError("dataset_version must be a simple nonempty identifier")
    if not seed:
        raise EvaluationDatasetError("seed must be nonempty")
    if split_policy not in {"balanced", "blind-holdout"}:
        raise EvaluationDatasetError("unsupported split_policy")
    repository = Path(__file__).parents[1].resolve()
    if target.resolve().is_relative_to(repository) and not target.resolve().is_relative_to(
            repository / "evaluation" / "private"):
        raise EvaluationDatasetError("repository output must be under evaluation/private")
    if target.exists() and any(target.iterdir()):
        raise EvaluationDatasetError("output directory must be empty; dataset versions are immutable")
    try:
        manifest_bytes = (source / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDatasetError(f"cannot read sampling manifest: {exc}") from exc
    if manifest.get("schema_version") != SAMPLING_SCHEMA:
        raise EvaluationDatasetError("unsupported sampling plan schema")
    rows = _load_candidates(source / "candidates.jsonl")
    target_count = manifest.get("target_documents")
    if type(target_count) is not int or len(rows) > target_count:
        raise EvaluationDatasetError("candidate count exceeds or invalidates sampling target")
    if database is not None:
        _verify_source_database(rows, manifest, Path(database))
    components = _components(rows)
    assignment, holdout_plan = (
        (_split_components(components, seed), None) if split_policy == "balanced"
        else _blind_holdout_split(components, seed)
    )
    cases = [
        {"case_id": row["candidate_id"], "document_ref": row["document_ref"],
         "object_ref": row["object_ref"], "content_sha256": row["content_sha256"],
         "text_storage": "restricted_reference", "event_group_id": row["event_group_ref"],
         "origin_group_id": row["origin_group_ref"], "language": row["language"],
         "source_kind": row["source_ref"], "time_bucket": row.get("time_bucket"),
         "published_at": row.get("published_at"),
         "us_market_linked": bool(row.get("us_market_linked")),
         "sec_related": bool(row.get("sec_related")),
         "split": assignment[row["candidate_id"]],
         "annotation": {"state": "unlabeled", "generated_by_model": False, "labels": {}}}
        for row in sorted(rows, key=lambda item: item["candidate_id"])
    ]
    output_manifest = {
        "schema_version": "evaluation-dataset-v1", "dataset_version": dataset_version,
        "annotation_schema_version": "annotation-v1", "purpose": "private unlabeled human annotation intake",
        "target_plan": TARGET_PLAN,
        "admission_version": ADMISSION_VERSION,
        "sampling_manifest_sha256": _digest(manifest_bytes),
        "sampling_candidates_sha256": _digest((source / "candidates.jsonl").read_bytes()),
        "source_snapshot_fingerprint": manifest.get("database_snapshot_fingerprint"),
        "source_database_verified_at_admission": database is not None,
        "split_policy": split_policy,
        "split_target_ratios": SPLIT_TARGETS,
        "split_seed": seed,
    }
    if holdout_plan is not None:
        output_manifest["holdout_review"] = {
            "status": "pending", "heldout_after": holdout_plan["heldout_after"],
            "heldout_source_refs": holdout_plan["heldout_source_refs"],
        }
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(output_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (target / "cases.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in cases),
        encoding="utf-8")
    validated = validate_evaluation_dataset(target)
    report = AdmissionReport(
        status="unlabeled", candidates=len(rows), components=len(components),
        split_counts=validated.split_counts, source_database_verified=database is not None,
        holdout_status="planned_unreviewed" if holdout_plan else "not_planned",
        publishable_gold=validated.publishable_gold,
        warnings=("No human labels or model quality claims are available.",
                  "Syndicated/translated origin links and blind time/source holdouts still need human review.",
                  *(("Source database was not supplied; frozen candidate hashes were not independently verified.",)
                    if database is None else ())))
    (target / "admission-report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    if holdout_plan is not None:
        (target / "holdout-plan.json").write_text(
            json.dumps(holdout_plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze an unlabeled private evaluation dataset")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--database", type=Path,
                        help="Read-only source database for snapshot and candidate verification")
    parser.add_argument("--seed", default="infohub-admission-v1")
    parser.add_argument("--split-policy", choices=("balanced", "blind-holdout"),
                        default="balanced")
    args = parser.parse_args()
    print(json.dumps(admit_sampling_plan(args.plan, args.output,
                                        dataset_version=args.dataset_version,
                                        seed=args.seed, database=args.database,
                                        split_policy=args.split_policy).to_dict(),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
