import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db_admin


NOW = "2026-09-19T09:00:00Z"
HASH = "a" * 64


class ReportGenerationSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"

    def _input(self, db):
        dataset = db.execute("SELECT dataset_id FROM dataset_state WHERE singleton=1").fetchone()[0]
        db.execute("""INSERT INTO report_input_snapshots(
            id,dataset_id,report_key,report_type,report_date,window_start,window_end,
            window_basis,timezone,as_of,manifest_json,manifest_sha256,input_count,created_at)
            VALUES('input-one',?,'calendar_daily:2026-09-18:UTC','calendar_daily',
                   '2026-09-18','2026-09-18T00:00:00Z','2026-09-19T00:00:00Z',
                   'calendar_day','UTC',?,'{}',?,0,?)""", (dataset, NOW, HASH, NOW))
        return dataset

    def _run(self, db, dataset):
        db.execute("""INSERT INTO report_generation_runs(
            id,dataset_id,input_snapshot_id,provider,requested_model,prompt_template_id,
            prompt_sha256,rendered_prompt_ref,rendered_prompt_sha256,parameters_json,prepared_at)
            VALUES('run-one',?,'input-one','local','model-v1','daily-v1',?,
                   'sha256/prompt',?,'{}',?)""", (dataset, HASH, HASH, NOW))

    def _attempt(self, db, *, attempt_id, number, status):
        valid = status == "valid_draft"
        db.execute("""INSERT INTO report_generation_attempts(
            id,run_id,attempt_number,status,resolved_model,raw_response_ref,
            raw_response_sha256,validated_draft_json,validation_report_json,
            usage_status,started_at,finished_at,recorded_at)
            VALUES(?,'run-one',?,?,'model-v1','sha256/response',?,?,'{}',
                   'unknown',?,?,?)""",
                   (attempt_id, number, status, HASH, "{}" if valid else None, NOW, NOW, NOW))

    def _version(self, db, dataset, *, attempt_id):
        db.execute("""INSERT INTO report_versions(
            id,dataset_id,report_key,version,input_snapshot_id,mode,content,
            content_sha256,citations_json,coverage_json,provider,model,
            prompt_template_id,prompt_sha256,generated_at,available_at,generation_attempt_id)
            VALUES('llm-one',?,'calendar_daily:2026-09-18:UTC',1,'input-one',
                   'llm','draft',?,'[]','{}','local','model-v1','daily-v1',?,?,?,?)""",
                   (dataset, HASH, HASH, NOW, NOW, attempt_id))

    def test_migration_preserves_legacy_and_adds_only_empty_generation_tables(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:19])
            db.execute("INSERT INTO daily_reports(date,content,created_at) VALUES('2026-09-18','old text',?)", (NOW,))
            dataset = self._input(db)
            db.execute("""INSERT INTO report_versions(
                id,dataset_id,report_key,version,input_snapshot_id,mode,content,
                content_sha256,citations_json,coverage_json,provider,model,
                prompt_template_id,prompt_sha256,generated_at,available_at)
                VALUES('preexisting-llm',?,'calendar_daily:2026-09-18:UTC',1,
                       'input-one','llm','preexisting text',?,'[]','{}',
                       'legacy-provider','legacy-model','legacy-prompt',?,?,?)""",
                       (dataset, HASH, HASH, NOW, NOW))
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.applied_versions, tuple(range(20, db_admin.CURRENT_SCHEMA_VERSION + 1)))
        self.assertEqual(db_admin.verify_database(self.path, require_current=True).schema_version, db_admin.CURRENT_SCHEMA_VERSION)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "old text")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_generation_runs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_generation_attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT content,generation_attempt_id FROM report_versions").fetchone(),
                             ("preexisting text", None))

    def test_llm_version_requires_valid_matching_immutable_attempt(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            dataset = self._input(db)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "review provenance"):
                self._version(db, dataset, attempt_id=None)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "identity mismatch"):
                self._run(db, "wrong-dataset")
            self._run(db, dataset)
            self._attempt(db, attempt_id="invalid", number=1, status="invalid_draft")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "only a valid report draft"):
                db.execute("""INSERT INTO report_generation_reviews(
                    id,attempt_id,decision,review_type,reviewer_id,reason,draft_sha256,reviewed_at)
                    VALUES('bad-review','invalid','approved','manual_source_check',
                           'operator','source checked',?,?)""", (HASH, NOW))
            db.execute("""INSERT INTO report_generation_reviews(
                id,attempt_id,decision,review_type,reviewer_id,reason,draft_sha256,reviewed_at)
                VALUES('reject-one','invalid','rejected','manual_source_check',
                       'operator','unsupported claim',?,?)""", (HASH, NOW))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "review provenance"):
                self._version(db, dataset, attempt_id="invalid")
            self._attempt(db, attempt_id="valid", number=2, status="valid_draft")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "review provenance"):
                self._version(db, dataset, attempt_id="valid")
            db.execute("""INSERT INTO report_generation_reviews(
                id,attempt_id,decision,review_type,reviewer_id,reason,draft_sha256,reviewed_at)
                VALUES('review-one','valid','approved','manual_source_check',
                       'operator','source checked',?,?)""", (HASH, NOW))
            self._version(db, dataset, attempt_id="valid")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute("UPDATE report_generation_attempts SET status='failed' WHERE id='valid'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute("DELETE FROM report_generation_runs WHERE id='run-one'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute("UPDATE report_generation_reviews SET decision='rejected' WHERE id='review-one'")
            self.assertEqual(db.execute("SELECT generation_attempt_id FROM report_versions").fetchone()[0], "valid")


if __name__ == "__main__":
    unittest.main()
