import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import cli
from app import config, database
from app.ingest import PayloadIntegrityError, verify_payload
from app.report_generation import prepare_report_generation, record_report_response
from app.report_inputs import freeze_calendar_daily
from app.report_llm_publish import publish_reviewed_report
from app.report_review import record_manual_review, review_preview
from app.report_versions import publish_structured_report
from app.report_query import published_calendar_report


T0 = "2026-09-19T08:00:00Z"
T1 = "2026-09-19T08:01:00Z"


class ReportGenerationRecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, name, value in (
            (database, "DB_PATH", root / "app.db"),
            (config, "DB_PATH", root / "app.db"),
            (config, "BLOB_PATH", root / "blobs"),
            (config, "APP_TZ", ZoneInfo("Europe/London")),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/one','Evidence','ai',80,1,
                                 '2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""")
        self.snapshot = freeze_calendar_daily("2026-09-18")["snapshot_id"]

    def _prepare(self):
        return prepare_report_generation(
            snapshot_id=self.snapshot, provider="local", requested_model="test-model",
            prompt_template_id="daily-v1", prompt_template_text="Template {{ items }}",
            rendered_prompt="Template: evidence input 1", parameters={"temperature": 0}, now=T0,
        )

    def _valid_response(self):
        return json.dumps({
            "schema_version": "infohub.report-draft/1.0", "date": "2026-09-18",
            "sections": [{"channel": "ai", "claims": [
                {"text": "存在一篇报道", "input_ordinals": [0]},
            ]}],
        }, ensure_ascii=False)

    def test_prompt_and_valid_response_are_immutable_and_idempotent(self):
        prepared = self._prepare()
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(self._prepare()["status"], "already_prepared")
        first = record_report_response(run_id=prepared["run_id"], response=self._valid_response(),
                                       resolved_model="test-model", started_at=T0, finished_at=T1)
        self.assertEqual(first["status"], "valid_draft")
        self.assertEqual(record_report_response(run_id=prepared["run_id"], response=self._valid_response(),
                                                resolved_model="test-model", started_at=T0, finished_at=T1)["attempt_id"],
                         first["attempt_id"])
        with database.get_db() as db:
            run = db.execute("SELECT * FROM report_generation_runs").fetchone()
            attempt = db.execute("SELECT * FROM report_generation_attempts").fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_generation_attempts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 0)
            self.assertEqual(verify_payload(run["rendered_prompt_ref"], run["rendered_prompt_sha256"]).read_text(),
                             "Template: evidence input 1")
            self.assertEqual(verify_payload(attempt["raw_response_ref"], attempt["raw_response_sha256"]).read_text(),
                             self._valid_response())
            self.assertEqual(json.loads(attempt["validation_report_json"])["citation_count"], 1)
            self.assertEqual(attempt["usage_status"], "unknown")

    def test_invalid_response_is_retained_but_not_validated(self):
        run_id = self._prepare()["run_id"]
        invalid = record_report_response(run_id=run_id, response=b'{bad-json',
                                         resolved_model="test-model", started_at=T0, finished_at=T1)
        self.assertEqual(invalid["status"], "invalid_draft")
        with database.get_db() as db:
            attempt = db.execute("SELECT * FROM report_generation_attempts").fetchone()
            self.assertIsNone(attempt["validated_draft_json"])
            self.assertEqual(json.loads(attempt["validation_report_json"])["error_code"], "invalid_json")
            self.assertEqual(verify_payload(attempt["raw_response_ref"], attempt["raw_response_sha256"]).read_bytes(),
                             b'{bad-json')

    def test_wrong_channel_draft_is_invalid_and_prompt_corruption_blocks_recording(self):
        run_id = self._prepare()["run_id"]
        wrong = self._valid_response().replace('"ai"', '"stock"')
        result = record_report_response(run_id=run_id, response=wrong,
                                        resolved_model="test-model", started_at=T0, finished_at=T1)
        self.assertEqual(result["status"], "invalid_draft")
        with database.get_db() as db:
            run = db.execute("SELECT * FROM report_generation_runs").fetchone()
        verify_payload(run["rendered_prompt_ref"], run["rendered_prompt_sha256"]).write_text("corrupt")
        with self.assertRaises(PayloadIntegrityError):
            record_report_response(run_id=run_id, response=self._valid_response(),
                                   resolved_model="test-model", started_at=T0, finished_at=T1)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_generation_attempts").fetchone()[0], 1)

    def test_manual_review_binds_exact_draft_and_preserves_decision(self):
        run_id = self._prepare()["run_id"]
        attempt = record_report_response(run_id=run_id, response=self._valid_response(),
                                         resolved_model="test-model", started_at=T0, finished_at=T1)
        preview = review_preview(attempt["attempt_id"])
        self.assertEqual(preview["claims"][0]["sources"][0]["source_url"], "https://example.test/one")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            record_manual_review(attempt_id=attempt["attempt_id"], decision="approved",
                                 expected_digest="0" * 64, reviewer_id="operator", reason="checked source")
        recorded = record_manual_review(attempt_id=attempt["attempt_id"], decision="approved",
                                        expected_digest=preview["review_digest"], reviewer_id="operator",
                                        reason="checked source")
        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(record_manual_review(attempt_id=attempt["attempt_id"], decision="approved",
                                              expected_digest=preview["review_digest"], reviewer_id="operator",
                                              reason="checked source")["status"], "already_reviewed")
        with self.assertRaisesRegex(ValueError, "different review"):
            record_manual_review(attempt_id=attempt["attempt_id"], decision="rejected",
                                 expected_digest=preview["review_digest"], reviewer_id="operator", reason="changed")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_generation_reviews").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 0)

    def test_invalid_draft_can_only_be_rejected(self):
        run_id = self._prepare()["run_id"]
        attempt = record_report_response(run_id=run_id, response="{bad-json",
                                         resolved_model="test-model", started_at=T0, finished_at=T1)
        preview = review_preview(attempt["attempt_id"])
        self.assertEqual(preview["claims"], [])
        with self.assertRaisesRegex(ValueError, "cannot be approved"):
            record_manual_review(attempt_id=attempt["attempt_id"], decision="approved",
                                 expected_digest=preview["review_digest"], reviewer_id="operator", reason="no")
        self.assertEqual(record_manual_review(attempt_id=attempt["attempt_id"], decision="rejected",
                                              expected_digest=preview["review_digest"], reviewer_id="operator",
                                              reason="invalid JSON")["decision"], "rejected")

    def test_review_cli_rejects_web_role(self):
        with patch.object(config, "PROCESS_ROLE", "web"):
            with self.assertRaisesRegex(config.RuntimeConfigurationError, "maintenance role"):
                cli.cmd_report_review_preview("attempt")
            with self.assertRaisesRegex(config.RuntimeConfigurationError, "maintenance role"):
                cli.cmd_report_review("attempt", "approved", "0" * 64, "reason")
            with self.assertRaisesRegex(config.RuntimeConfigurationError, "maintenance role"):
                cli.cmd_report_publish_reviewed("review")

    def _approved_review(self):
        run_id = self._prepare()["run_id"]
        attempt = record_report_response(run_id=run_id, response=self._valid_response(),
                                         resolved_model="test-model", started_at=T0, finished_at=T1)
        preview = review_preview(attempt["attempt_id"])
        return record_manual_review(attempt_id=attempt["attempt_id"], decision="approved",
                                    expected_digest=preview["review_digest"], reviewer_id="operator",
                                    reason="read original source")["review_id"]

    def test_approved_draft_can_replace_structured_fallback_once(self):
        fallback = publish_structured_report(self.snapshot)
        self.assertEqual(fallback["status"], "published")
        review_id = self._approved_review()
        published = publish_reviewed_report(review_id)
        self.assertEqual((published["status"], published["version"]), ("published", 2))
        self.assertEqual(publish_reviewed_report(review_id)["status"], "already_exists")
        with database.get_db() as db:
            report = published_calendar_report(db, "2026-09-18")
            self.assertEqual(report["mode"], "llm")
            self.assertEqual(report["citation_count"], 1)
            self.assertIn("存在一篇报道", report["content"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_reports").fetchone()[0], 0)

    def test_rejected_or_legacy_date_is_not_published(self):
        run_id = self._prepare()["run_id"]
        attempt = record_report_response(run_id=run_id, response=self._valid_response(),
                                         resolved_model="test-model", started_at=T0, finished_at=T1)
        with self.assertRaisesRegex(ValueError, "missing"):
            publish_reviewed_report("unknown-review")
        preview = review_preview(attempt["attempt_id"])
        rejected = record_manual_review(attempt_id=attempt["attempt_id"], decision="rejected",
                                        expected_digest=preview["review_digest"], reviewer_id="operator",
                                        reason="claim not supported")
        with self.assertRaisesRegex(ValueError, "no valid approval"):
            publish_reviewed_report(rejected["review_id"])
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 0)

    def test_existing_legacy_report_is_preserved_after_approval(self):
        review_id = self._approved_review()
        with database.get_db() as db:
            db.execute("INSERT INTO daily_reports(date,content,created_at) VALUES('2026-09-18','old report',?)", (T0,))
        self.assertEqual(publish_reviewed_report(review_id)["status"], "legacy_preserved")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "old report")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 0)

    def test_second_approved_draft_cannot_displace_existing_llm(self):
        first_review = self._approved_review()
        first = publish_reviewed_report(first_review)
        other = prepare_report_generation(
            snapshot_id=self.snapshot, provider="local", requested_model="test-model",
            prompt_template_id="daily-v1", prompt_template_text="Template {{ items }}",
            rendered_prompt="Template: evidence input 1", parameters={"temperature": 1}, now=T0,
        )
        attempt = record_report_response(run_id=other["run_id"], response=self._valid_response(),
                                         resolved_model="test-model", started_at=T0, finished_at=T1)
        preview = review_preview(attempt["attempt_id"])
        second_review = record_manual_review(
            attempt_id=attempt["attempt_id"], decision="approved",
            expected_digest=preview["review_digest"], reviewer_id="operator",
            reason="read original source",
        )
        protected = publish_reviewed_report(second_review["review_id"])
        self.assertEqual(protected["status"], "preserved_llm")
        self.assertEqual(protected["version_id"], first["version_id"])


if __name__ == "__main__":
    unittest.main()
