import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.jobs import claim_job, enqueue_job, fail_job
from app.timeutil import utc_now
from app.legacy_backfill import backfill_legacy_batch
from app.legacy_curation_import import (
    JOB_KIND, LegacyCurationImportError, enqueue_legacy_curation_batch,
    enqueue_legacy_curation_item,
    import_claimed_legacy_curation, process_one_legacy_curation_import,
)


class LegacyCurationImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, name, value in (
            (database, "DB_PATH", root / "app.db"),
            (config, "DB_PATH", root / "app.db"),
            (config, "BLOB_PATH", root / "blobs"),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                          VALUES(1,'fixture','Fixture','ai','media','rss')""")
            db.execute("""INSERT INTO items(
                          id,source_id,url,title,title_zh,summary,raw_summary,channel,
                          score,tmt,reason,ai_cat,companies,official,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/a','Company launches product',
                          '公司发布产品','公司发布一款产品。','Company launches a product.',
                          'ai',80,1,'值得关注','product','[]',0,
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
        for _ in range(5):
            if backfill_legacy_batch(10).status == "completed":
                break

    def test_four_tasks_publish_once_and_preserve_legacy_fields(self):
        before = None
        with database.get_db() as db:
            before = tuple(db.execute("SELECT title_zh,summary,score,tmt FROM items WHERE id=1").fetchone())
        jobs = enqueue_legacy_curation_item(1)
        self.assertEqual(len(jobs), 4)
        self.assertEqual([job.id for job in jobs], [job.id for job in enqueue_legacy_curation_item(1)])
        results = []
        for _ in range(4):
            result = process_one_legacy_curation_import(worker_id="fixture-importer")
            self.assertIsNotNone(result)
            results.append(result)
        self.assertIsNone(process_one_legacy_curation_import(worker_id="fixture-importer"))
        self.assertEqual({result.task_type for result in results},
                         {"translation", "relevance", "summarization", "importance"})
        with database.get_db() as db:
            self.assertEqual(tuple(db.execute("SELECT title_zh,summary,score,tmt FROM items WHERE id=1").fetchone()), before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_attempts").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_publication_versions").fetchone()[0], 4)
            rows = db.execute("""SELECT v.task_type,v.evidence_status,r.validated_output_json
                    FROM analysis_publication_versions v JOIN analysis_results r ON r.id=v.result_id""").fetchall()
            self.assertTrue(all(row["evidence_status"] == "partial" for row in rows))
            self.assertTrue(all(json.loads(row["validated_output_json"])["status"] == "needs_review" for row in rows))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_attempt_authorizations WHERE reserved_cost_microusd=0 AND decision='allowed'").fetchone()[0],4)

    def test_missing_snapshot_and_stale_lease_cannot_publish(self):
        with self.assertRaisesRegex(LegacyCurationImportError, "no current frozen"):
            enqueue_legacy_curation_item(999)
        enqueue_legacy_curation_item(1)
        job = claim_job(worker_id="fixture-importer", kinds=(JOB_KIND,), lease_seconds=300)
        self.assertIsNotNone(job)
        with database.get_db() as db:
            db.execute("UPDATE jobs SET lease_token='different' WHERE id=?", (job.id,))
        with self.assertRaisesRegex(Exception, "lease"):
            import_claimed_legacy_curation(job)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0], 0)

    def test_failure_after_attempt_retries_without_second_attempt(self):
        enqueue_legacy_curation_item(1)
        job = claim_job(worker_id="fixture-importer", kinds=(JOB_KIND,), lease_seconds=300)
        with patch("app.legacy_curation_import.publish_analysis_result", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                import_claimed_legacy_curation(job)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_attempts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0], 0)
        fail_job(job.id, job.lease_token, error_code="interrupted", retry_at=utc_now())
        with database.get_db() as db:
            db.execute("UPDATE jobs SET next_attempt_at='2099-01-01T00:00:00.000000Z' WHERE id<>?", (job.id,))
        retried = claim_job(worker_id="fixture-importer", kinds=(JOB_KIND,), lease_seconds=300)
        self.assertEqual(retried.id, job.id)
        imported = import_claimed_legacy_curation(retried)
        self.assertEqual(imported.status, "needs_review")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_attempts").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0], 1)

    def test_paged_enqueue_can_resume_without_duplicate_jobs(self):
        first = enqueue_legacy_curation_batch(after_item_id=0, limit=1)
        self.assertEqual(first["next_after_item_id"], 1)
        self.assertEqual(first["jobs_ensured"], 4)
        self.assertEqual(enqueue_legacy_curation_batch(after_item_id=1, limit=1)["items_seen"], 0)
        enqueue_legacy_curation_batch(after_item_id=0, limit=1)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind=?", (JOB_KIND,)).fetchone()[0], 4)

    def test_existing_publication_is_not_overwritten_by_later_import_job(self):
        original = enqueue_legacy_curation_item(1)[0]
        job = claim_job(worker_id="fixture-importer", kinds=(JOB_KIND,), lease_seconds=300)
        self.assertEqual(job.id, original.id)
        published = import_claimed_legacy_curation(job)
        replay = enqueue_job(
            kind=JOB_KIND, idempotency_key="separate-operator-replay",
            subject_id=job.subject_id, input_version=job.input_version,
            payload=job.payload,
        )
        with database.get_db() as db:
            db.execute("UPDATE jobs SET next_attempt_at='2099-01-01T00:00:00.000000Z' WHERE id NOT IN (?,?)", (job.id, replay.id))
        claimed = claim_job(worker_id="fixture-importer", kinds=(JOB_KIND,), lease_seconds=300)
        skipped = import_claimed_legacy_curation(claimed)
        self.assertEqual(skipped.status, "already_published")
        self.assertEqual(skipped.publication_id, published.publication_id)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_publication_versions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
