from __future__ import annotations

"""Controlled import of validated tone release decisions into an immutable ledger."""

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from . import db_admin
from .database import get_db
from .evaluation import EvaluationDatasetError
from .timeutil import format_utc, parse_utc, utc_now
from .tone_release_admission import validate_tone_release_bundle
from .tone_release_decision import record_tone_release_decision


class ToneReleaseImportError(RuntimeError):
    """The controlled record cannot be admitted without weakening provenance."""


@dataclass(frozen=True)
class ToneReleaseAdmission:
    admission_id: str
    decision_id: str
    bundle_id: str
    dataset_version: str
    decision: str
    approval_candidate: bool
    record_sha256: str
    imported_by: str
    imported_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_candidate(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToneReleaseImportError("cannot read tone release decision record") from exc
    if not isinstance(value, dict):
        raise ToneReleaseImportError("tone release decision record must be an object")
    return value


def import_tone_release_admission(
    db: sqlite3.Connection,
    bundle_path: Path | str,
    registry_path: Path | str,
    decision_path: Path | str,
    record_path: Path | str,
    *,
    imported_by: str,
    now: str | None = None,
) -> ToneReleaseAdmission:
    """Revalidate all files and append one decision without enabling publication."""
    actor = imported_by.strip() if isinstance(imported_by, str) else ""
    if not actor or len(actor) > 200:
        raise ToneReleaseImportError("controlled import requires a bounded imported_by actor")
    bundle_path = Path(bundle_path)
    try:
        bundle = validate_tone_release_bundle(bundle_path)
        expected = record_tone_release_decision(
            bundle_path, Path(registry_path), Path(decision_path)
        )
    except EvaluationDatasetError as exc:
        raise ToneReleaseImportError(str(exc)) from exc
    candidate = _load_candidate(Path(record_path))
    expected_payload = json.loads(_canonical(expected.to_dict()))
    if candidate != expected_payload:
        raise ToneReleaseImportError(
            "tone release decision record differs from revalidated source inputs"
        )
    record_json = _canonical(candidate)
    bundle_json = _canonical(bundle)
    record_sha256 = _sha_text(record_json)
    try:
        imported_at = format_utc(parse_utc(now or utc_now()))
    except (TypeError, ValueError) as exc:
        raise ToneReleaseImportError("controlled import time requires a timezone") from exc
    admission = ToneReleaseAdmission(
        expected.record_id,
        expected.decision_id,
        expected.bundle_id,
        bundle["dataset_version"],
        expected.decision,
        expected.approval_candidate_for_controlled_import,
        record_sha256,
        actor,
        imported_at,
    )
    db.execute("SAVEPOINT tone_release_admission_import")
    try:
        conflict = db.execute(
            """SELECT decision_id,bundle_id FROM tone_release_admissions
               WHERE id=? OR decision_id=? OR bundle_id=? OR record_sha256=?""",
            (expected.record_id, expected.decision_id, expected.bundle_id, record_sha256),
        ).fetchone()
        if conflict is not None:
            raise ToneReleaseImportError(
                "tone release decision or evidence bundle is already admitted"
            )
        db.execute(
            """INSERT INTO tone_release_admissions(
                   id,record_version,decision_id,bundle_id,bundle_sha256,
                   registry_version,registry_sha256,dataset_version,
                   baseline_test_run_id,candidate_dev_run_id,candidate_test_run_id,
                   candidate_security_run_id,calibration_version,operational_run_id,
                   approver_id,decision,reason,decided_at,policy_id,
                   approval_candidate,record_json,record_sha256,bundle_json,
                   imported_by,imported_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                expected.record_id, expected.record_version, expected.decision_id,
                expected.bundle_id, expected.bundle_sha256, expected.registry_version,
                expected.registry_sha256, bundle["dataset_version"],
                bundle["baseline_test_run_id"], bundle["candidate_dev_run_id"],
                bundle["candidate_test_run_id"], bundle["candidate_security_run_id"],
                bundle["calibration_version"], bundle["operational_run_id"],
                expected.approver_id, expected.decision, expected.reason,
                expected.recorded_at, expected.policy_id,
                int(expected.approval_candidate_for_controlled_import), record_json,
                record_sha256, bundle_json, actor, imported_at,
            ),
        )
        db.execute("RELEASE SAVEPOINT tone_release_admission_import")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT tone_release_admission_import")
        db.execute("RELEASE SAVEPOINT tone_release_admission_import")
        raise
    return admission


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Revalidate and append one tone release admission"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--decision", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--imported-by", required=True)
    args = parser.parse_args()
    db_admin.verify_database(args.database, require_current=True)
    with get_db(args.database) as db:
        admitted = import_tone_release_admission(
            db, args.bundle, args.registry, args.decision, args.record,
            imported_by=args.imported_by,
        )
    print(json.dumps(admitted.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
