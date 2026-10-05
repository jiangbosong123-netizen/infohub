import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import database


class ConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        with database.get_db(self.path) as db:
            db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id REFERENCES parent(id))")

    def count(self) -> int:
        with sqlite3.connect(self.path) as other:
            return other.execute("SELECT COUNT(*) FROM parent").fetchone()[0]

    def assertClosed(self, conn):
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_outside_a_scope_every_block_closes_its_connection(self):
        with database.get_db(self.path) as first:
            pass
        with database.get_db(self.path) as second:
            pass
        self.assertIsNot(first, second)
        self.assertClosed(first)
        self.assertClosed(second)

    def test_scope_reuses_committed_connections_and_closes_them_at_the_end(self):
        with database.reused_connections():
            with database.get_db(self.path) as first:
                first.execute("INSERT INTO parent VALUES(1)")
            self.assertEqual(self.count(), 1)  # committed before the next block
            self.assertFalse(first.in_transaction)
            with database.get_db(self.path) as second:
                self.assertEqual(second.execute("SELECT COUNT(*) FROM parent").fetchone()[0], 1)
            self.assertIs(first, second)
        self.assertClosed(first)

    def test_failed_block_rolls_back_before_its_connection_is_reused(self):
        with database.reused_connections():
            with self.assertRaises(RuntimeError):
                with database.get_db(self.path) as first:
                    first.execute("INSERT INTO parent VALUES(1)")
                    raise RuntimeError("boom")
            with database.get_db(self.path) as second:
                self.assertEqual(second.execute("SELECT COUNT(*) FROM parent").fetchone()[0], 0)
            self.assertIs(first, second)
        self.assertEqual(self.count(), 0)

    def test_failed_commit_leaves_nothing_behind_for_the_next_block(self):
        with database.reused_connections():
            with self.assertRaises(sqlite3.IntegrityError):
                with database.get_db(self.path) as broken:
                    broken.execute("PRAGMA defer_foreign_keys=ON")
                    broken.execute("INSERT INTO child VALUES(1, 99)")
            with database.get_db(self.path) as fresh:
                self.assertFalse(fresh.in_transaction)
                self.assertEqual(fresh.execute("SELECT COUNT(*) FROM child").fetchone()[0], 0)
                fresh.execute("INSERT INTO parent VALUES(1)")
        self.assertEqual(self.count(), 1)

    def test_connection_still_in_a_transaction_is_never_kept(self):
        with database.reused_connections():
            conn = database.get_db(self.path)
            conn.execute("BEGIN")
            self.assertFalse(database._keep_idle(conn))
            conn.rollback()
            self.assertTrue(database._keep_idle(conn))
            self.assertFalse(database._keep_idle(conn))  # already idle: never listed twice

    def test_each_checkout_starts_with_the_standard_settings(self):
        with database.reused_connections():
            with database.get_db(self.path) as first:
                first.row_factory = None
                first.execute("PRAGMA foreign_keys=OFF")
                first.execute("PRAGMA busy_timeout=1")
            with database.get_db(self.path) as second:
                self.assertIs(second, first)
                self.assertIs(second.row_factory, sqlite3.Row)
                self.assertEqual(second.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(second.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
                self.assertEqual(second.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_nested_blocks_never_share_a_connection_and_idle_ones_are_bounded(self):
        with database.reused_connections():
            opened = []
            def nest(depth):
                with database.get_db(self.path) as db:
                    self.assertNotIn(db, opened)
                    opened.append(db)
                    if depth:
                        nest(depth - 1)
            nest(database.REUSE_IDLE_LIMIT + 1)
            closed = [db for db in opened if self._is_closed(db)]
            self.assertEqual(len(closed), 2)
            with database.get_db(self.path) as again:
                self.assertIn(again, opened)
                self.assertFalse(self._is_closed(again))
        self.assertTrue(all(self._is_closed(db) for db in opened))

    def test_inner_scope_shares_the_outer_one(self):
        with database.reused_connections():
            with database.reused_connections():
                with database.get_db(self.path) as first:
                    pass
            self.assertFalse(self._is_closed(first))
            with database.get_db(self.path) as second:
                self.assertIs(first, second)
        self.assertClosed(first)

    def test_scope_is_per_thread(self):
        seen = []
        def other_thread():
            with database.get_db(self.path) as db:
                pass
            seen.append(self._is_closed(db))
        with database.reused_connections():
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join()
        self.assertEqual(seen, [True])

    def test_connection_used_after_the_scope_is_closed_on_exit(self):
        with database.reused_connections():
            late = database.get_db(self.path)
        with late:
            late.execute("INSERT INTO parent VALUES(1)")
        self.assertClosed(late)
        self.assertEqual(self.count(), 1)

    @staticmethod
    def _is_closed(conn) -> bool:
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return True
        return False


if __name__ == "__main__":
    unittest.main()
