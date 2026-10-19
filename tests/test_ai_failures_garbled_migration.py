import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database, db_admin


class AiFailuresGarbledMigrationTests(unittest.TestCase):
    def test_migration_62_keeps_the_rows_and_accepts_garbled(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "app.db"
        with database.get_db(path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:61])
            db.execute("INSERT INTO ai_failures_daily(day,task,kind,calls) VALUES('2026-10-18','score','content_filter',6)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO ai_failures_daily(day,task,kind,calls) VALUES('2026-10-18','score','garbled',1)")
        with database.get_db(path) as db:
            self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:62]), (62,))
            db.execute("INSERT INTO ai_failures_daily(day,task,kind,calls) VALUES('2026-10-19','score','garbled',3)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO ai_failures_daily(day,task,kind,calls) VALUES('2026-10-19','score','typo',1)")
            rows = db.execute("SELECT day,task,kind,calls FROM ai_failures_daily ORDER BY day").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("2026-10-18", "score", "content_filter", 6), ("2026-10-19", "score", "garbled", 3)])


if __name__ == "__main__":
    unittest.main()
