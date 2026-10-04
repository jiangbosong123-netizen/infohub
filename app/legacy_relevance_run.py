from __future__ import annotations

"""Turn the legacy stored ``items.tmt`` field into a prediction run for owner-tier evaluation.

The legacy AI pipeline stores ``tmt = official OR watched-company link OR LLM judgment``
(``app/ai/pipeline.py`` ``_keep_tmt``). Every prediction is therefore tagged with a slice:
``llm_only`` rows carry the model's own judgment, ``policy_forced`` rows were kept by the
product rule regardless of the model, and ``unscored`` rows were never processed (abstain).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .evaluation import EvaluationDatasetError, _load_cases, validate_evaluation_dataset
from .evaluation_metrics import write_classification_report
from .evaluation_sampling import legacy_content_sha256, legacy_item_content

METHOD_ID = "legacy-tmt"
METHOD_VERSION = "keep-tmt-v1"
ABSTAIN = "__unscored__"
OBJECT_REF = re.compile(r"private-db:items/([1-9][0-9]*)")
METHOD_CONFIG = {
    "field": "items.tmt",
    "writer": "app/ai/pipeline.py _keep_tmt",
    "stored_rule": "official OR watched company link OR LLM tmt",
    "mapping": {"1": "relevant", "0": "not_relevant", "null": ABSTAIN},
    "slices": {
        "llm_only": "not official and no watched company: stored value is the LLM judgment",
        "policy_forced": "official or watched company: stored value forced to relevant",
        "unscored": "never processed by the legacy AI pipeline",
    },
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_private(path: Path) -> None:
    repository = Path(__file__).parents[1].resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(repository) and not resolved.is_relative_to(
        repository / "evaluation" / "private"
    ):
        raise EvaluationDatasetError("run output must be outside the repository or under evaluation/private")


def build_legacy_tmt_run(
    dataset: Path | str,
    database: Path | str,
    output: Path | str,
    *,
    split: str = "test",
) -> dict:
    dataset, database, output = Path(dataset), Path(database), Path(output)
    _require_private(output)
    if output.exists():
        raise EvaluationDatasetError("run output already exists")
    if not output.parent.is_dir():
        raise EvaluationDatasetError("run output parent directory must already exist")
    if not database.is_file():
        raise EvaluationDatasetError("database does not exist")
    report = validate_evaluation_dataset(dataset)
    manifest_bytes = (dataset / "manifest.json").read_bytes()
    cases_bytes = (dataset / "cases.jsonl").read_bytes()
    cases = [case for case in _load_cases(dataset / "cases.jsonl") if case["split"] == split]
    if not cases:
        raise EvaluationDatasetError(f"dataset has no cases in {split} split")

    rows = []
    db = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        for case in cases:
            match = OBJECT_REF.fullmatch(str(case.get("object_ref") or ""))
            if match is None:
                raise EvaluationDatasetError(f"case {case['case_id']} is not a legacy item reference")
            item = db.execute(
                "SELECT title,raw_summary,tmt,official,companies FROM items WHERE id=?",
                (int(match.group(1)),),
            ).fetchone()
            if item is None:
                raise EvaluationDatasetError(f"case {case['case_id']} item is missing from the database")
            # Predictions must come from the same snapshot the owner judged.
            if legacy_content_sha256(legacy_item_content(item)) != case["content_sha256"]:
                raise EvaluationDatasetError(f"case {case['case_id']} content differs from the database")
            forced = bool(item["official"]) or (item["companies"] or "[]") != "[]"
            if item["tmt"] is None:
                label, tags = ABSTAIN, ["unscored"]
            else:
                label = "relevant" if item["tmt"] else "not_relevant"
                tags = ["policy_forced" if forced else "llm_only"]
            rows.append({"case_id": case["case_id"], "predicted_label": label, "slices": tags})
    finally:
        db.close()

    predictions = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")
    config = {**METHOD_CONFIG, "source_database_sha256": _file_sha(database)}
    run = {
        "schema_version": "prediction-run-v2",
        "prediction_run_id": f"{METHOD_ID}-on-{report.dataset_version}-{split}",
        "dataset_version": report.dataset_version,
        "task": "relevance",
        "label_path": "relevance",
        "split": split,
        "method_id": METHOD_ID,
        "method_version": METHOD_VERSION,
        "method_config": config,
        "method_config_sha256": _sha(json.dumps(config, sort_keys=True).encode("utf-8")),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "predictions_file": "predictions.jsonl",
        "abstain_labels": [ABSTAIN],
        "dataset_manifest_sha256": _sha(manifest_bytes),
        "dataset_cases_sha256": _sha(cases_bytes),
        "predictions_sha256": _sha(predictions),
    }
    staging = Path(tempfile.mkdtemp(prefix="infohub-legacy-run-", dir=output.parent))
    try:
        (staging / "predictions.jsonl").write_bytes(predictions)
        (staging / "run.json").write_text(
            json.dumps(run, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        metrics = write_classification_report(dataset, staging / "run.json", staging / "report.json")
        os.rename(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return metrics.to_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the legacy stored tmt field")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--split", default="test", choices=["train", "dev", "test", "security"])
    args = parser.parse_args()
    report = build_legacy_tmt_run(args.dataset, args.database, args.output, split=args.split)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
