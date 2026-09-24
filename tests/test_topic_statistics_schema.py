import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin


NOW = "2026-09-23T14:00:00.000000Z"


class TopicStatisticsSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start(); self.addCleanup(mocked.stop)
        database.init_schema()

    def test_schema_starts_empty_and_catalog_changes_are_dirty(self):
        with database.get_db(self.path) as db:
            state = db.execute(
                "SELECT status,current_build_id,current_publication_id FROM topic_statistics_state"
            ).fetchone()
            self.assertEqual(tuple(state), ("empty", None, None))
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute("INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES('t',?,'active',?)",
                       (dataset, NOW))
            self.assertEqual(
                tuple(db.execute("SELECT topic_id,reason FROM topic_statistics_dirty").fetchone()),
                ("t", "topic_insert"),
            )

    def test_published_build_and_members_are_immutable(self):
        with database.get_db(self.path) as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute("INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES('t',?,'active',?)",
                       (dataset, NOW))
            db.execute("""INSERT INTO topic_versions(id,topic_id,version,slug,name,group_key,
                description,rules_json,rules_hash,version_sha256,status,available_at)
                VALUES('tv','t',1,'topic','Topic','technology','','{}',?,?,'active',?)""",
                       ('a'*64, 'b'*64, NOW))
            db.execute("UPDATE topic_catalog SET current_version_id='tv' WHERE id='t'")
            db.execute("""INSERT INTO topic_statistics_builds(
                id,dataset_id,status,assignment_policy_version,event_policy_version,started_at,updated_at)
                VALUES('build',?,'building','effective-review-v1','current-stable-event-v1',?,?)""",
                       (dataset, NOW, NOW))
            db.execute("""INSERT INTO topic_statistics_versions(
                id,build_id,topic_version_id,document_count,event_count,input_manifest_sha256,counted_at)
                VALUES('stats','build','tv',1,0,?,?)""", ('c'*64, NOW))
            db.execute("""INSERT INTO topic_statistics_members(
                statistics_id,ordinal,member_type,resource_id,version_id,provenance_json,member_sha256)
                VALUES('stats',0,'document','d','dv','{}',?)""", ('d'*64,))
            db.execute("UPDATE topic_statistics_builds SET status='ready',topic_count=1,updated_at=?,finished_at=? WHERE id='build'",
                       (NOW, NOW))
            db.execute("""INSERT INTO topic_statistics_publications(
                id,version,build_id,published_at) VALUES('publication',1,'build',?)""", (NOW,))
            db.execute("""UPDATE topic_statistics_state SET status='ready',
                current_build_id='build',current_publication_id='publication',updated_at=?""",
                       (NOW,))
            with self.assertRaisesRegex(Exception, "building run"):
                db.execute("""INSERT INTO topic_statistics_members(
                    statistics_id,ordinal,member_type,resource_id,version_id,provenance_json,member_sha256)
                    VALUES('stats',1,'event','e','ev','{}',?)""", ('e'*64,))
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE topic_statistics_versions SET document_count=2")
        db_admin.verify_database(self.path, require_current=True)

    def test_migration_28_preserves_version_27_database(self):
        predecessor = self.path.parent / "predecessor.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:27])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('s','S','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (28, 29, 30, 31, 32))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 27)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "s")


if __name__ == "__main__":
    unittest.main()
