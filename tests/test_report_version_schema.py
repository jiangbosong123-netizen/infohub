import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db_admin


NOW = "2026-09-19T09:00:00+00:00"
START = "2026-09-18T00:00:00+00:00"
END = "2026-09-19T00:00:00+00:00"
ZERO_HASH = "0" * 64


class ReportVersionSchemaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"

    def _snapshot(self, db, *, snapshot_id="input-one", count=0):
        dataset_id = db.execute("SELECT dataset_id FROM dataset_state WHERE singleton=1").fetchone()[0]
        db.execute("""INSERT INTO report_input_snapshots(
            id,dataset_id,report_key,report_type,report_date,window_start,window_end,
            window_basis,timezone,as_of,manifest_json,manifest_sha256,input_count,created_at)
            VALUES(?,?,?,'calendar_daily','2026-09-18',?,?,'calendar_day','UTC',?,
                   '{}',?,?,?)""",
            (snapshot_id, dataset_id, "calendar_daily:2026-09-18:UTC", START, END,
             NOW, ZERO_HASH, count, NOW))
        return dataset_id

    def _version(self, db, dataset_id, *, version=1, prior=None, snapshot_id="input-one"):
        db.execute("""INSERT INTO report_versions(
            id,dataset_id,report_key,version,input_snapshot_id,mode,content,
            content_sha256,citations_json,coverage_json,generated_at,available_at,
            supersedes_version_id)
            VALUES(?,?,?, ?,?,'structured_fallback','report',?,'[]','{}',?,?,?)""",
            (f"version-{version}", dataset_id, "calendar_daily:2026-09-18:UTC", version,
             snapshot_id, hashlib.sha256(b"report").hexdigest(), NOW, NOW, prior))

    def test_migration_adds_empty_version_tables_without_rewriting_legacy_report(self):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:18])
            db.execute("INSERT INTO daily_reports(date,content,created_at) VALUES('2026-09-18','old text',?)", (NOW,))
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.applied_versions, tuple(range(19, db_admin.CURRENT_SCHEMA_VERSION + 1)))
        self.assertEqual(db_admin.verify_database(self.path, require_current=True).schema_version, db_admin.CURRENT_SCHEMA_VERSION)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT content FROM daily_reports").fetchone()[0], "old text")
            for table in ("report_input_snapshots", "report_input_members", "report_versions", "report_publications"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_versions_are_append_only_and_publication_must_match_identity(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            dataset_id = self._snapshot(db)
            self._version(db, dataset_id)
            self._version(db, dataset_id, version=2, prior="version-1")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute("UPDATE report_versions SET content='replaced' WHERE id='version-1'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute("DELETE FROM report_input_snapshots WHERE id='input-one'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "identity mismatch"):
                db.execute("INSERT INTO report_publications VALUES(?,?,?,?)",
                           (dataset_id, "other-report", "version-2", NOW))
            db.execute("INSERT INTO report_publications VALUES(?,?,?,?)",
                       (dataset_id, "calendar_daily:2026-09-18:UTC", "version-2", NOW))
            self.assertEqual(db.execute("SELECT current_version_id FROM report_publications").fetchone()[0], "version-2")

    def test_incomplete_input_and_wrong_predecessor_cannot_be_published(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA foreign_keys=ON")
            dataset_id = self._snapshot(db, count=1)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "complete matching input"):
                self._version(db, dataset_id)
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/1','Headline','ai',?,?)""", (START, NOW))
            db.execute("INSERT INTO report_input_members VALUES('input-one',0,1,NULL,?)", (ZERO_HASH,))
            self._version(db, dataset_id)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "input is closed"):
                db.execute("INSERT INTO report_input_members VALUES('input-one',1,1,NULL,?)", (ZERO_HASH,))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "prior version"):
                self._version(db, dataset_id, version=2, prior=None)


if __name__ == "__main__":
    unittest.main()
