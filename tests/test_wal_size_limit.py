import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database


class WalSizeLimitTests(unittest.TestCase):
    def test_every_connection_limits_the_wal(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with database.get_db(Path(temporary.name) / "t.db") as db:
            limit = db.execute("PRAGMA journal_size_limit").fetchone()[0]
        self.assertEqual(limit, database.JOURNAL_SIZE_LIMIT_BYTES)
        self.assertEqual(limit, 64 * 1024 * 1024)

    def test_a_wal_grown_under_a_long_read_shrinks_back(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "t.db"
        with patch.object(database, "JOURNAL_SIZE_LIMIT_BYTES", 1024 * 1024):
            with database.get_db(path) as db:
                db.execute("CREATE TABLE t(x BLOB)")
            # The last connection to close deletes the WAL; a busy host always has one open.
            keeper = database.get_db(path)
            self.addCleanup(keeper.close)
            reader = database.get_db(path)
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM t").fetchone()  # pins the WAL like a backup
            for _ in range(60):
                with database.get_db(path) as db:
                    db.execute("INSERT INTO t VALUES(randomblob(100000))")
            peak = os.path.getsize(f"{path}-wal")
            reader.rollback()
            reader.close()
            with database.get_db(path) as db:
                db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            for _ in range(3):
                with database.get_db(path) as db:
                    db.execute("INSERT INTO t VALUES(randomblob(1000))")
        self.assertGreater(peak, 5 * 1024 * 1024)
        self.assertLessEqual(os.path.getsize(f"{path}-wal"), 1024 * 1024 + 64 * 1024)


if __name__ == "__main__":
    unittest.main()
