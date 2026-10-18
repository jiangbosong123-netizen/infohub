import tempfile
import unittest
from pathlib import Path

from app import database, db_admin


class CollectionPeriodsMigrationTests(unittest.TestCase):
    def test_migration_61_dates_each_company_from_its_own_feed(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "app.db"
        with database.get_db(path) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:60])
            db.execute("""INSERT INTO sources(id,key,name,channel,type) VALUES
                          (1,'googlenews-acme','G','stock','googlenews'),(2,'nasdaq-cik9','N','stock','rss'),
                          (3,'wire','W','stock','rss')""")
            db.execute("""INSERT INTO companies(id,slug,name,market,aliases,origin,covered) VALUES
                          (1,'acme','Acme','US','[]','watchlist',1),(2,'cik9','Nine','US','[]','coverage',1),
                          (3,'cik8','Eight','US','[]','coverage',0)""")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at) VALUES
                          (1,1,'https://e.test/1','A','stock','2026-09-11T21:43:00+00:00','2026-09-11T21:43:00+00:00'),
                          (2,3,'https://e.test/2','B','stock','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')""")
            # fetch_log keeps 14 days: the feed's first item is older than its first logged run
            db.execute("INSERT INTO fetch_log(source_id,ran_at,ok,new_items) VALUES(1,'2026-10-02T20:12:15+00:00',1,0)")
            db.execute("INSERT INTO fetch_log(source_id,ran_at,ok,new_items) VALUES(2,'2026-10-15T15:59:42+00:00',1,0)")
        with database.get_db(path) as db:
            self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:61]), (61,))
            rows = {row[0]: (row[1], row[2]) for row in db.execute(
                "SELECT c.slug, p.started_at, p.basis FROM company_coverage_periods p JOIN companies c ON c.id=p.company_id")}
            gaps = db.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0]
        self.assertEqual(rows, {"acme": ("2026-09-11T21:43:00+00:00", "first_feed_fetch"),
                                "cik9": ("2026-10-15T15:59:42+00:00", "first_feed_fetch")})  # cik8 is not covered
        self.assertEqual(gaps, 0)  # the measured gaps belong to the production dataset only


if __name__ == "__main__":
    unittest.main()
