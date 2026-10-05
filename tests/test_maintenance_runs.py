import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.curation_import_audit import audit_curation_import
from app.legacy_backfill import backfill_legacy_batch
from app.maintenance_runs import run_legacy_curation_import, run_projection_builders


class MaintenanceRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, name, value in (
            (database, "DB_PATH", root / "app.db"),
            (config, "DB_PATH", root / "app.db"),
            (config, "BLOB_PATH", root / "blobs"),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                          VALUES(1,'fixture','Fixture','ai','media','rss')""")
            for item_id in (1, 2, 3):
                db.execute("""INSERT INTO items(
                              id,source_id,url,title,title_zh,summary,raw_summary,channel,
                              score,tmt,reason,ai_cat,companies,official,published_at,fetched_at)
                              VALUES(?,1,?,?,?,'摘要','Source excerpt','ai',80,1,'理由','product','[]',0,
                              '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""",
                           (item_id, f"https://example.test/{item_id}", f"Launch {item_id}", f"发布 {item_id}"))
        for _ in range(10):
            if backfill_legacy_batch(10).status == "completed":
                break

    def test_curation_run_imports_everything_once_and_reports_nothing_left(self):
        seen = []
        report = run_legacy_curation_import(progress=seen.append)
        self.assertEqual((report["items_seen"], report["jobs_ensured"], report["processed"]), (3, 12, 12))
        self.assertTrue(report["complete"])
        self.assertEqual(report["remaining"], {})
        self.assertEqual(seen[0]["phase"], "enqueue")
        self.assertEqual(audit_curation_import(config.DB_PATH)["status"], "ok")
        again = run_legacy_curation_import()
        self.assertEqual((again["jobs_ensured"], again["processed"], again["complete"]), (12, 0, True))

    def test_builders_run_until_idle_and_repeat_is_a_no_op(self):
        run_legacy_curation_import()
        report = run_projection_builders()
        self.assertTrue(report["complete"])
        for name in ("search", "hot", "topic_statistics"):
            self.assertNotEqual(report["builders"][name]["status"], "building")
            self.assertFalse(report["builders"][name]["dirty_remaining"])
        again = run_projection_builders()
        self.assertTrue(again["complete"])
        self.assertTrue(all(entry["calls"] == 1 for entry in again["builders"].values()))


if __name__ == "__main__":
    unittest.main()
