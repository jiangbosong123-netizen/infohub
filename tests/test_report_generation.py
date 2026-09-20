import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import config, database
from app.ingest import PayloadIntegrityError, verify_payload
from app.report_generation import prepare_report_generation, record_report_response
from app.report_inputs import freeze_calendar_daily


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


if __name__ == "__main__":
    unittest.main()
