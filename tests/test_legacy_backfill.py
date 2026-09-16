import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.crawler import runner
from app.ingest import begin_ingest_run, finish_ingest_run, observe_candidate
from app.legacy_backfill import backfill_legacy_batch
from app.source_time import parse_source_time


class LegacyBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db_path = root / "app.db"
        self.blob_path = root / "blobs"
        for item in (
            patch.object(database, "DB_PATH", self.db_path),
            patch.object(config, "DB_PATH", self.db_path),
            patch.object(config, "BLOB_PATH", self.blob_path),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                "INSERT INTO sources(id,key,name,channel,tier,type) "
                "VALUES(1,'one','One','stock','media','rss')"
            )
            db.execute(
                "INSERT INTO sources(id,key,name,channel,tier,type) "
                "VALUES(2,'two','Two','ai','info','html')"
            )

    def _item(
        self,
        item_id: int,
        source_id: int,
        *,
        title: str,
        summary: str,
        url: str,
        fetched_at: str = "2026-09-10T09:01:00+00:00",
    ) -> None:
        with database.get_db() as db:
            db.execute(
                """INSERT INTO items(
                       id,source_id,url,title,summary,raw_summary,channel,companies,
                       published_at,fetched_at
                   ) VALUES(?,?,?,?,?,?,?,'[]','2026-09-10T09:00:00+00:00',?)""",
                (item_id, source_id, url, title, summary, summary, "stock", fetched_at),
            )

    def _run_to_completion(self, batch_size=1):
        reports = []
        for _ in range(30):
            result = backfill_legacy_batch(batch_size)
            reports.append(result)
            if result.status == "completed":
                return reports
        self.fail("backfill did not complete")

    def _legacy_fingerprints(self):
        result = {}
        with database.get_db() as db:
            for table in ("items", "item_discoveries", "daily_reports"):
                rows = [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                result[table] = hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, default=str).encode()
                ).hexdigest()
        return result

    def test_maps_items_discoveries_and_reports_without_rewriting_legacy(self):
        self._item(1, 1, title="Original", summary="Excerpt", url="https://example.com/a")
        self._item(2, 1, title="", summary="", url="", fetched_at="not-a-time")
        self._item(3, 2, title="HTML", summary="", url="https://example.com/c")
        with database.get_db() as db:
            db.execute(
                """INSERT INTO item_discoveries(item_id,source_id,first_seen_at,last_seen_at)
                   VALUES(1,1,'2026-09-10T09:01:00+00:00','2026-09-10T10:00:00+00:00')"""
            )
            db.execute(
                """INSERT INTO item_discoveries(item_id,source_id,first_seen_at,last_seen_at)
                   VALUES(1,2,'2026-09-10T09:02:00+00:00','2026-09-10T11:00:00+00:00')"""
            )
            db.execute(
                """INSERT INTO daily_reports(id,date,content,created_at)
                   VALUES(7,'2026-09-10','# Legacy report','2026-09-11T01:00:00+00:00')"""
            )
        before = self._legacy_fingerprints()
        reports = self._run_to_completion(batch_size=1)
        after = self._legacy_fingerprints()
        self.assertEqual(before, after)
        self.assertEqual(reports[-1].items_total, 3)
        self.assertEqual(reports[-1].items_mapped, 3)
        self.assertEqual(reports[-1].discoveries_total, 2)
        self.assertEqual(reports[-1].discoveries_mapped, 2)
        self.assertEqual(reports[-1].reports_mapped, 1)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0], 3)
            versions = db.execute(
                """SELECT title_original,published_at,time_status,content_origin,
                          content_extent,availability_basis,point_in_time_eligible
                   FROM document_versions ORDER BY document_id"""
            ).fetchall()
            empty = db.execute(
                """SELECT version.* FROM document_versions AS version
                   JOIN documents AS document ON document.current_version_id=version.id
                   WHERE document.legacy_item_id=2"""
            ).fetchone()
            report = db.execute("SELECT * FROM legacy_report_identities").fetchone()
            raw = db.execute(
                "SELECT observed_at,ingested_at FROM raw_records ORDER BY id LIMIT 1"
            ).fetchone()
            locator_count = db.execute(
                "SELECT COUNT(*) FROM document_locators WHERE document_id=(SELECT id FROM documents WHERE legacy_item_id=1)"
            ).fetchone()[0]
        self.assertTrue(all(row["published_at"] is None for row in versions))
        self.assertTrue(all(row["time_status"] == "legacy_unverified" for row in versions))
        self.assertTrue(all(row["content_origin"] == "legacy_unknown" for row in versions))
        self.assertTrue(all(row["availability_basis"] == "legacy_unknown" for row in versions))
        self.assertTrue(all(row["point_in_time_eligible"] == 0 for row in versions))
        self.assertEqual(empty["content_extent"], "none")
        self.assertEqual(locator_count, 2)
        self.assertEqual(report["status"], "legacy_unverified")
        self.assertGreater(raw["ingested_at"], raw["observed_at"])

    def test_failure_after_observation_resumes_without_duplicate_evidence_or_version(self):
        self._item(1, 1, title="Original", summary="Excerpt", url="https://example.com/a")
        failed = False

        def interrupt(_item_id):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("simulated interruption")

        with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
            backfill_legacy_batch(10, after_observation=interrupt)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM legacy_backfill_state").fetchone()[0],
                "failed",
            )

        final = self._run_to_completion(batch_size=10)[-1]
        self.assertEqual(final.status, "completed")
        with database.get_db() as db:
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM raw_records),
                   (SELECT COUNT(*) FROM raw_observations),
                   (SELECT COUNT(*) FROM documents),
                   (SELECT COUNT(*) FROM document_versions),
                   (SELECT COUNT(*) FROM legacy_object_mappings WHERE resource_type='item')"""
            ).fetchone())
        self.assertEqual(counts, (1, 1, 1, 1, 1))

    def test_cutoff_excludes_rows_created_after_backfill_started(self):
        self._item(1, 1, title="Old", summary="", url="https://example.com/old")
        first = backfill_legacy_batch(1)
        self.assertEqual(first.cutoff_item_id, 1)
        self._item(2, 1, title="New", summary="", url="https://example.com/new")
        final = self._run_to_completion(batch_size=1)[-1]
        self.assertEqual(final.items_total, 1)
        self.assertEqual(final.items_mapped, 1)
        with database.get_db() as db:
            self.assertIsNone(db.execute(
                """SELECT 1 FROM legacy_object_mappings
                   WHERE resource_type='item' AND legacy_key='2'"""
            ).fetchone())

    def test_second_completed_run_is_a_noop(self):
        self._item(1, 1, title="Old", summary="", url="https://example.com/old")
        self._run_to_completion(batch_size=10)
        with database.get_db() as db:
            before = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM raw_observations),
                   (SELECT COUNT(*) FROM document_versions),
                   (SELECT COUNT(*) FROM legacy_object_mappings)"""
            ).fetchone())
        result = backfill_legacy_batch(10)
        with database.get_db() as db:
            after = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM raw_observations),
                   (SELECT COUNT(*) FROM document_versions),
                   (SELECT COUNT(*) FROM legacy_object_mappings)"""
            ).fetchone())
        self.assertEqual(result.status, "completed")
        self.assertEqual(before, after)

    def test_resume_after_run_finished_before_partition_checkpoint(self):
        self._item(1, 1, title="Old", summary="", url="https://example.com/old")
        self._run_to_completion(batch_size=10)
        with database.get_db() as db:
            run_id = db.execute(
                "SELECT id FROM ingest_runs WHERE trace_id LIKE 'legacy-backfill-%'"
            ).fetchone()[0]
            db.execute(
                """UPDATE legacy_backfill_sources
                   SET active_run_id=?,last_item_id=0,processed_count=0,status='running'
                   WHERE source_id=1""",
                (run_id,),
            )
            db.execute(
                """UPDATE legacy_backfill_state
                   SET status='running',finished_at=NULL WHERE singleton=1"""
            )
        result = self._run_to_completion(batch_size=10)[-1]
        self.assertEqual(result.status, "completed")
        with database.get_db() as db:
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM raw_observations),
                   (SELECT COUNT(*) FROM document_versions),
                   (SELECT COUNT(*) FROM legacy_object_mappings WHERE resource_type='item')"""
            ).fetchone())
        self.assertEqual(counts, (1, 1, 1))

    def test_existing_evidence_version_is_not_replaced_by_legacy_snapshot(self):
        self._item(1, 1, title="Legacy", summary="Old", url="https://example.com/a")
        source = {
            "key": "one", "name": "One", "channel": "stock", "tier": "media",
            "type": "rss", "url": "https://example.com/feed", "interval_minutes": 10,
        }
        candidate = {
            "url": "https://example.com/a", "title": "Current", "summary": "Verified",
            "published_at": "2026-09-16T07:00:00Z",
            "observed_at": "2026-09-16T08:00:00+00:00",
            "source_time_values": [parse_source_time(
                "2026-09-16T07:00:00Z", field_path="entry.published",
                role="published", interpretation="fixture",
            ).to_dict()],
            "source_record": {"title": "Current", "summary": "Verified"},
            "payload_kind": "feed_entry",
        }
        run = begin_ingest_run(source, trace_id="current-evidence")
        observation = observe_candidate(run, candidate, ordinal=0)
        self.assertFalse(runner.insert_item("one", candidate, observation=observation))
        finish_ingest_run(
            run, status="succeeded", raw_count=1, accepted_count=1,
            duplicate_count=1, rejected_count=0, byte_count=observation.size_bytes,
        )
        with database.get_db() as db:
            original = db.execute(
                "SELECT current_version_id FROM documents WHERE legacy_item_id=1"
            ).fetchone()[0]

        self._run_to_completion(batch_size=10)
        with database.get_db() as db:
            document = db.execute(
                "SELECT current_version_id FROM documents WHERE legacy_item_id=1"
            ).fetchone()
            version_count = db.execute(
                "SELECT COUNT(*) FROM document_versions"
            ).fetchone()[0]
            input_count = db.execute(
                "SELECT COUNT(*) FROM document_version_inputs WHERE version_id=?",
                (original,),
            ).fetchone()[0]
        self.assertEqual(document["current_version_id"], original)
        self.assertEqual(version_count, 1)
        self.assertEqual(input_count, 2)


if __name__ == "__main__":
    unittest.main()
