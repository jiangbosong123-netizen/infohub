import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database, db_admin

LAST_FETCH = "SELECT MAX(fetched_at) AS m FROM items"


class ItemsFetchedIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"

    def seed(self, db):
        db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
        db.executemany(
            """INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
               VALUES(?,1,?,'T','ai','2026-09-20T00:00:00+00:00',?)""",
            [(n, f"https://e.test/{n}", f"2026-09-{10 + n % 17:02d}T0{n % 10}:00:00+00:00")
             for n in range(1, 200)],
        )

    def test_last_fetch_reads_the_index_instead_of_scanning_items(self):
        db_admin.migrate_database(self.path)
        with database.get_db(self.path) as db:
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + LAST_FETCH))
        self.assertIn("idx_items_fetched", plan)
        self.assertNotIn("SCAN items", plan)

    def test_migration_48_adds_only_the_index_and_keeps_the_answer(self):
        with database.get_db(self.path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:47])
            self.seed(db)
            before = db.execute(LAST_FETCH).fetchone()[0]
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.applied_versions, tuple(range(48, db_admin.CURRENT_SCHEMA_VERSION + 1)))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 47)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(LAST_FETCH).fetchone()[0], before)
            self.assertEqual(before, "2026-09-26T09:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
