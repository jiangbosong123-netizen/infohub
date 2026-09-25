import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.legacy_compare import compare_legacy_snapshots


class LegacyCompareTests(unittest.TestCase):
    def test_added_column_is_ignored_but_original_value_change_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "before.db", Path(directory) / "after.db"
            with sqlite3.connect(old) as db:
                db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,title TEXT NOT NULL)")
                db.execute("INSERT INTO items VALUES(1,'original')")
            shutil.copyfile(old, new)
            with sqlite3.connect(new) as db:
                db.execute("ALTER TABLE items ADD COLUMN migration_note TEXT")
                db.execute("UPDATE items SET migration_note='new' WHERE id=1")
            same = compare_legacy_snapshots(old, new)
            self.assertEqual(same["status"], "ok")
            self.assertEqual(same["tables"]["items"]["rows_after"], 1)
            with sqlite3.connect(new) as db:
                db.execute("UPDATE items SET title='changed' WHERE id=1")
            different = compare_legacy_snapshots(old, new)
            self.assertEqual(different["status"], "failed")
            self.assertEqual(different["tables"]["items"]["status"], "changed")

    def test_missing_table_and_same_path_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "before.db", Path(directory) / "after.db"
            with sqlite3.connect(old) as db:
                db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY,title TEXT)")
            with sqlite3.connect(new):
                pass
            self.assertEqual(compare_legacy_snapshots(old, new)["tables"]["items"]["status"],
                             "missing_after")
            with self.assertRaises(ValueError):
                compare_legacy_snapshots(old, old)


if __name__ == "__main__":
    unittest.main()
