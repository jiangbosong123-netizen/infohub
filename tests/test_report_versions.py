import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import config, database
from app.report_inputs import freeze_calendar_daily
from app.report_versions import publish_structured_report


def clock(hour: int) -> datetime:
    return datetime(2026, 9, 19, hour, tzinfo=timezone.utc)


class ReportPublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        for target, name, value in (
            (database, "DB_PATH", self.path),
            (config, "DB_PATH", self.path),
            (config, "APP_TZ", ZoneInfo("Europe/London")),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,summary,channel,score,tmt,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/article','Original','Legacy AI summary',
                                 'ai',80,1,'2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""")
            db.execute("""INSERT INTO daily_reports(date,content,created_at)
                          VALUES('2026-09-18','legacy report','2026-09-19T07:00:00Z')""")

    def test_published_report_has_exact_citation_and_is_idempotent(self):
        snapshot = freeze_calendar_daily("2026-09-18", now=clock(8))
        first = publish_structured_report(snapshot["snapshot_id"])
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["version"], 1)
        self.assertEqual(publish_structured_report(snapshot["snapshot_id"])["status"], "already_exists")
        with database.get_db() as db:
            row = db.execute("SELECT * FROM report_versions").fetchone()
            citations = json.loads(row["citations_json"])
            self.assertEqual(len(citations), 1)
            self.assertEqual((citations[0]["input_ordinal"], citations[0]["legacy_item_id"]), (0, 1))
            self.assertEqual(citations[0]["source_url"], "https://example.test/article")
            self.assertIn("https://example.test/article", row["content"])
            self.assertNotIn("Legacy AI summary", row["content"])
            self.assertEqual(json.loads(row["coverage_json"])["point_in_time_status"], "legacy_mutable_unverified")
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "legacy report")
            self.assertEqual(db.execute("SELECT current_version_id FROM report_publications").fetchone()[0], row["id"])

    def test_late_material_creates_revision_and_old_snapshot_cannot_republish(self):
        old = freeze_calendar_daily("2026-09-18", now=clock(8))
        prior = publish_structured_report(old["snapshot_id"])
        with database.get_db() as db:
            db.execute("""INSERT INTO items(id,source_id,url,title,summary,channel,score,tmt,published_at,fetched_at)
                          VALUES(2,1,'https://example.test/late','Late report','Late evidence',
                                 'ai',95,1,'2026-09-18T13:00:00Z','2026-09-19T08:30:00Z')""")
        new = freeze_calendar_daily("2026-09-18", now=clock(9))
        revised = publish_structured_report(new["snapshot_id"])
        self.assertEqual((revised["status"], revised["version"]), ("published", 2))
        with database.get_db() as db:
            versions = db.execute("SELECT id,content,supersedes_version_id FROM report_versions ORDER BY version").fetchall()
            self.assertEqual(len(versions), 2)
            self.assertEqual(versions[1]["supersedes_version_id"], prior["version_id"])
            self.assertIn("Original", versions[0]["content"])
            self.assertNotIn("Late report", versions[0]["content"])
            self.assertIn("Late report", versions[1]["content"])
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "legacy report")
        # A later request for an old, already published input never moves the pointer.
        self.assertEqual(publish_structured_report(old["snapshot_id"])["status"], "already_exists")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT current_version_id FROM report_publications").fetchone()[0],
                             revised["version_id"])

    def test_fallback_cannot_replace_published_llm(self):
        old = freeze_calendar_daily("2026-09-18", now=clock(8))
        first = publish_structured_report(old["snapshot_id"])
        newer = freeze_calendar_daily("2026-09-18", now=clock(9))
        with database.get_db() as db:
            snapshot = db.execute("SELECT dataset_id,report_key FROM report_input_snapshots WHERE id=?",
                                  (newer["snapshot_id"],)).fetchone()
            db.execute("""INSERT INTO report_generation_runs(
                id,dataset_id,input_snapshot_id,provider,requested_model,prompt_template_id,
                prompt_sha256,rendered_prompt_ref,rendered_prompt_sha256,parameters_json,prepared_at)
                VALUES('test-run',?,?,'test','test-model','report-v1',?,
                       'test/prompt',?,'{}','2026-09-19T08:45:00Z')""",
                       (snapshot["dataset_id"], newer["snapshot_id"], "b" * 64, "c" * 64))
            db.execute("""INSERT INTO report_generation_attempts(
                id,run_id,attempt_number,status,resolved_model,raw_response_ref,
                raw_response_sha256,validated_draft_json,validation_report_json,
                usage_status,started_at,finished_at,recorded_at)
                VALUES('test-attempt','test-run',1,'valid_draft','test-model',
                       'test/response',?,'{}','{}','unknown',
                       '2026-09-19T08:45:00Z','2026-09-19T08:46:00Z','2026-09-19T08:46:00Z')""",
                       ("d" * 64,))
            db.execute("""INSERT INTO report_versions(
                          id,dataset_id,report_key,version,input_snapshot_id,mode,content,
                          content_sha256,citations_json,coverage_json,provider,model,
                          prompt_template_id,prompt_sha256,generated_at,available_at,
                          supersedes_version_id,generation_attempt_id)
                          VALUES('llm-version',?,?,2,?,'llm','validated LLM report',?,
                                 '[]','{}','test','test-model','report-v1',?,
                                 '2026-09-19T09:00:00Z','2026-09-19T09:00:00Z',?,'test-attempt')""",
                       (snapshot["dataset_id"], snapshot["report_key"], newer["snapshot_id"],
                        "a" * 64, "b" * 64, first["version_id"]))
            db.execute("UPDATE report_publications SET current_version_id='llm-version' WHERE report_key=?",
                       (snapshot["report_key"],))
        latest = freeze_calendar_daily("2026-09-18", now=clock(10))
        skipped = publish_structured_report(latest["snapshot_id"])
        self.assertEqual(skipped["status"], "preserved_llm")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT current_version_id FROM report_publications").fetchone()[0], "llm-version")

    def test_bad_snapshot_url_aborts_without_version(self):
        with database.get_db() as db:
            db.execute("UPDATE items SET url='javascript:alert(1)' WHERE id=1")
        snapshot = freeze_calendar_daily("2026-09-18", now=clock(8))
        with self.assertRaisesRegex(ValueError, "HTTP"):
            publish_structured_report(snapshot["snapshot_id"])
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_versions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
