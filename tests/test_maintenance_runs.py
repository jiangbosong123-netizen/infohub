import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.curation_import_audit import audit_curation_import
from app.legacy_backfill import backfill_legacy_batch
from app.maintenance_runs import (
    run_legacy_backfill,
    run_legacy_curation_import,
    run_projection_builders,
)


def seed_legacy_items(test: unittest.TestCase, count: int) -> None:
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    for target, name, value in (
        (database, "DB_PATH", root / "app.db"),
        (config, "DB_PATH", root / "app.db"),
        (config, "BLOB_PATH", root / "blobs"),
    ):
        item = patch.object(target, name, value)
        item.start()
        test.addCleanup(item.stop)
    database.init_schema()
    with database.get_db() as db:
        db.execute("""INSERT INTO sources(id,key,name,channel,tier,type)
                      VALUES(1,'fixture','Fixture','ai','media','rss')""")
        for item_id in range(1, count + 1):
            db.execute("""INSERT INTO items(
                          id,source_id,url,title,title_zh,summary,raw_summary,channel,
                          score,tmt,reason,ai_cat,companies,official,published_at,fetched_at)
                          VALUES(?,1,?,?,?,'摘要','Source excerpt','ai',80,1,'理由','product','[]',0,
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""",
                       (item_id, f"https://example.test/{item_id}", f"Launch {item_id}", f"发布 {item_id}"))


class BackfillRunTests(unittest.TestCase):
    def test_one_command_run_maps_every_batch_once(self):
        seed_legacy_items(self, 7)
        report = run_legacy_backfill(2)
        self.assertEqual(report.status, "completed")
        self.assertEqual((report.items_total, report.items_mapped, report.unexplained_items), (7, 7, 0))
        self.assertEqual(run_legacy_backfill(2).batch_processed, 0)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 7)


class MaintenanceRunTests(unittest.TestCase):
    def setUp(self):
        seed_legacy_items(self, 3)
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
