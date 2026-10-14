import contextlib
import importlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parents[1] / "deploy" / "windows")


def load_copier():
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    return importlib.import_module("copy_data")


class CopyDataTests(unittest.TestCase):
    def setUp(self):
        self.copier = load_copier()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.source, self.target = Path(folder.name) / "from", Path(folder.name) / "to"
        self.source.mkdir()
        self.target.mkdir()
        db = sqlite3.connect(self.source / "app.db")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE schema_migrations(version INTEGER)")
        db.execute("INSERT INTO schema_migrations VALUES (55), (56)")
        db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT)")
        db.executemany("INSERT INTO items(title) VALUES (?)", [(f"t{n}",) for n in range(50)])
        db.commit()
        db.close()  # the last connection checkpoints and removes the WAL
        (self.source / "blobs" / "ab").mkdir(parents=True)
        (self.source / "blobs" / "ab" / "abcdef").write_bytes(b"evidence" * 1000)
        (self.source / "backups" / "nightly").mkdir(parents=True)  # empty folders are kept too
        (self.source / "runtime").mkdir()
        (self.source / "runtime" / "worker-heartbeat.json").write_text("{}", encoding="ascii")

    def run_copy(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = self.copier.main([str(self.source), str(self.target)])
        self.assertTrue(output.getvalue().isascii())
        return status, json.loads(output.getvalue())

    def listing(self, root):
        return sorted((path.relative_to(root).as_posix(), path.is_dir() or path.read_bytes())
                      for path in root.rglob("*"))

    def test_a_clean_copy_is_identical_and_verified(self):
        before = self.listing(self.source)
        status, report = self.run_copy()
        self.assertEqual((status, report["verdict"], report["problems"]), (0, "copy verified", []))
        self.assertEqual(report["files"], 3)
        self.assertEqual(report["database"], {"integrity": "ok", "schema_version": 56, "items": 50})
        self.assertEqual(self.listing(self.target), before)
        self.assertEqual(self.listing(self.source), before)  # the source is only read

    def test_a_legacy_database_reports_schema_zero(self):
        db = sqlite3.connect(self.source / "app.db")
        db.execute("DROP TABLE schema_migrations")
        db.commit()
        db.close()
        status, report = self.run_copy()
        self.assertEqual((status, report["database"]["schema_version"]), (0, 0))

    def test_it_refuses_a_target_that_is_not_empty(self):
        (self.target / "app.db").write_bytes(b"older copy")
        status, report = self.run_copy()
        self.assertEqual((status, report["verdict"]), (1, "COPY FAILED"))
        self.assertIn("not empty", report["problems"][0])
        self.assertEqual((self.target / "app.db").read_bytes(), b"older copy")

    def test_it_refuses_a_database_that_was_not_closed_cleanly(self):
        (self.source / "app.db-wal").write_bytes(b"\0" * 32)
        status, report = self.run_copy()
        self.assertEqual(status, 1)
        self.assertIn("not closed cleanly", report["problems"][0])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_it_refuses_symbolic_links(self):
        (self.source / "blobs" / "link").symlink_to(self.source / "app.db")
        status, report = self.run_copy()
        self.assertEqual(status, 1)
        self.assertIn("symbolic link", report["problems"][0])

    def test_verification_catches_a_damaged_or_missing_copy(self):
        copied = self.copier.copy_tree(self.source, self.target)
        self.assertEqual(self.copier.verify_copy(self.source, self.target, copied), [])
        blob = self.target / "blobs" / "ab" / "abcdef"
        blob.write_bytes(b"x" + blob.read_bytes()[1:])
        (self.target / "runtime" / "worker-heartbeat.json").unlink()
        (self.target / "stray").write_bytes(b"")
        self.assertEqual(self.copier.verify_copy(self.source, self.target, copied), [
            "file missing from the copy: runtime/worker-heartbeat.json",
            "file only in the copy: stray",
            "copy differs from the source: blobs/ab/abcdef",
        ])

    def test_verification_catches_a_source_that_changed_during_the_copy(self):
        copied = self.copier.copy_tree(self.source, self.target)
        heartbeat = self.source / "runtime" / "worker-heartbeat.json"
        heartbeat.write_text('{"state": "running"}', encoding="ascii")
        os.utime(heartbeat, ns=(1, 2))
        self.assertEqual(self.copier.verify_copy(self.source, self.target, copied),
                         ["source file changed during the copy: runtime/worker-heartbeat.json"])


if __name__ == "__main__":
    unittest.main()
