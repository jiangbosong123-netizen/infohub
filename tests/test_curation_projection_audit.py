import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app.curation_hot_metrics import advance_hot_metrics
from app.curation_projection_audit import audit_curation_projections
from app.curation_search import advance_search_index


class CurationProjectionAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        db_patch = patch.object(database, "DB_PATH", self.path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(id,key,name,channel,tier,type) VALUES(1,'test','Test','ai','media','rss')")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/1','Original','ai',
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:01:00+00:00')""")
            db.execute("""INSERT INTO stories(id,channel,title,url,first_at,last_at,anchor_item_id)
                          VALUES('story-1','ai','Original','https://example.test/1',
                          '2026-09-10T09:00:00+00:00','2026-09-10T09:00:00+00:00',1)""")
            db.execute("INSERT INTO story_items(story_id,item_id) VALUES('story-1',1)")

    def test_ready_then_dirty_then_refreshed(self):
        self.assertEqual(audit_curation_projections(self.path)["status"], "failed")
        while advance_search_index(10).status != "ready":
            pass
        while advance_hot_metrics(10).status != "ready":
            pass
        ready = audit_curation_projections(self.path)
        self.assertEqual(ready["status"], "ok")
        self.assertEqual(ready["search"]["stale_publication_pointers"], 0)
        with database.get_db() as db:
            db.execute("UPDATE items SET title='Updated' WHERE id=1")
        dirty = audit_curation_projections(self.path)
        self.assertEqual(dirty["status"], "failed")
        self.assertEqual(dirty["reasons"], ["search_projection_not_current", "hot_projection_not_current"])
        self.assertEqual(dirty["search"]["dirty"], 1)
        self.assertEqual(dirty["hot"]["dirty"], 1)
        advance_search_index(10)
        advance_hot_metrics(10)
        self.assertEqual(audit_curation_projections(self.path)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
