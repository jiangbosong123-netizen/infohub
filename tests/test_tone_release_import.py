import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.tone_release_admission import assess_tone_release_evidence
from app.tone_release_decision import record_tone_release_decision
from app.tone_release_import import (
    ToneReleaseImportError,
    import_tone_release_admission,
)
from tests.test_tone_release_admission import hashes, reports


NOW = "2026-09-29T01:00:00.000000Z"


class ToneReleaseImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database_path = self.root / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.database_path),
            patch.object(config, "DB_PATH", self.database_path),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        self.bundle_path = self._write_bundle()
        self.registry_path = self._write_registry()
        self.decision_path = self._write_decision()
        self.record_path = self._write_record()

    def _write_bundle(self, *, ready=True) -> Path:
        values = list(reports())
        if not ready:
            values[0].publishable_tone_gold = False
        bundle = assess_tone_release_evidence(*values, artifact_sha256=hashes())
        path = self.root / "bundle.json"
        path.write_text(json.dumps(bundle.to_dict(), sort_keys=True))
        return path

    def _write_registry(self) -> Path:
        registry = {
            "schema_version": "tone-release-approver-registry-v1",
            "registry_version": "tone-approvers-v1",
            "generated_at": "2026-09-28T23:00:00Z",
            "approvers": [{
                "approver_id": "release-owner", "status": "active",
                "scopes": ["tone:release:approve"],
                "valid_from": "2026-09-01T00:00:00Z", "valid_until": None,
            }],
        }
        path = self.root / "registry.json"
        path.write_text(json.dumps(registry, sort_keys=True))
        return path

    def _write_decision(self, **changes) -> Path:
        bundle = json.loads(self.bundle_path.read_text())
        registry = json.loads(self.registry_path.read_text())
        decision = {
            "schema_version": "tone-release-decision-v1",
            "decision_id": "tone-decision-v1",
            "bundle_id": bundle["bundle_id"],
            "bundle_sha256": hashlib.sha256(self.bundle_path.read_bytes()).hexdigest(),
            "registry_version": registry["registry_version"],
            "registry_sha256": hashlib.sha256(self.registry_path.read_bytes()).hexdigest(),
            "approver_id": "release-owner", "decision": "approve",
            "reason": "All frozen evidence and rollback steps were independently reviewed.",
            "recorded_at": "2026-09-29T00:00:00Z", "source": "human",
            "model_assistance": False,
            "blind_holdout_exclusion_acknowledged": True,
            "budget_policy_id": bundle["measurements"]["policy_id"],
            "rollback_plan_acknowledged": True,
        }
        decision.update(changes)
        path = self.root / f"decision-{decision['decision_id']}.json"
        path.write_text(json.dumps(decision, sort_keys=True))
        return path

    def _write_record(self) -> Path:
        record = record_tone_release_decision(
            self.bundle_path, self.registry_path, self.decision_path
        )
        path = self.root / f"record-{record.decision_id}.json"
        path.write_text(json.dumps(record.to_dict(), sort_keys=True))
        return path

    def _import(self):
        with database.get_db() as db:
            return import_tone_release_admission(
                db, self.bundle_path, self.registry_path, self.decision_path,
                self.record_path, imported_by="release-operator", now=NOW,
            )

    def test_exact_approval_is_appended_without_publication(self):
        admitted = self._import()
        self.assertTrue(admitted.approval_candidate)
        with database.get_db() as db:
            row = db.execute("SELECT * FROM tone_release_admissions").fetchone()
            self.assertEqual(row["id"], admitted.admission_id)
            self.assertEqual(row["decision"], "approve")
            self.assertEqual(row["approval_candidate"], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM analysis_publications"
            ).fetchone()[0], 0)
        self.assertEqual(
            db_admin.verify_database(self.database_path, require_current=True).schema_version,
            40,
        )

    def test_candidate_record_is_recomputed_and_tampering_fails(self):
        record = json.loads(self.record_path.read_text())
        record["approver_id"] = "forged-operator"
        self.record_path.write_text(json.dumps(record, sort_keys=True))
        with self.assertRaisesRegex(ToneReleaseImportError, "differs"):
            self._import()
        with database.get_db() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tone_release_admissions"
            ).fetchone()[0], 0)

    def test_duplicate_decision_and_conflicting_bundle_are_rejected(self):
        self._import()
        with self.assertRaisesRegex(ToneReleaseImportError, "already admitted"):
            self._import()
        self.decision_path = self._write_decision(
            decision_id="different-decision", decision="reject",
            reason="A conflicting later opinion must use a new evidence bundle.",
            blind_holdout_exclusion_acknowledged=False,
            rollback_plan_acknowledged=False,
        )
        self.record_path = self._write_record()
        with self.assertRaisesRegex(ToneReleaseImportError, "already admitted"):
            self._import()

    def test_rejection_is_audited_but_never_an_approval_candidate(self):
        self.bundle_path = self._write_bundle(ready=False)
        self.decision_path = self._write_decision(
            decision="reject", reason="Quality evidence is incomplete.",
            blind_holdout_exclusion_acknowledged=False,
            rollback_plan_acknowledged=False,
        )
        self.record_path = self._write_record()
        admitted = self._import()
        self.assertFalse(admitted.approval_candidate)
        with database.get_db() as db:
            self.assertEqual(db.execute(
                "SELECT approval_candidate FROM tone_release_admissions"
            ).fetchone()[0], 0)

    def test_ledger_rows_are_immutable(self):
        self._import()
        with database.get_db() as db:
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE tone_release_admissions SET reason='rewritten'")
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM tone_release_admissions")

    def test_migration_40_preserves_schema_39_database(self):
        predecessor = self.root / "schema39.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:39])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (40,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 39)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tone_release_admissions"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
