import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.topic_statistics import advance_topic_statistics
from app.topic_statistics_query import (
    TopicStatisticsNotFound,
    TopicStatisticsUnavailable,
    published_topic_statistic,
    published_topic_statistics,
)


NOW = "2026-09-23T16:00:00.000000Z"


class TopicStatisticsQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        with database.get_db() as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute(
                "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES('t',?,'active',?)",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO topic_versions(
                       id,topic_id,version,slug,name,group_key,description,rules_json,
                       rules_hash,version_sha256,status,available_at)
                   VALUES('tv','t',1,'topic','Topic','technology','','{}',?,?,'active',?)""",
                ("a" * 64, "b" * 64, NOW),
            )
            db.execute("UPDATE topic_catalog SET current_version_id='tv' WHERE id='t'")

    def _publish(self):
        first = advance_topic_statistics(25)
        self.assertEqual(first.status, "building")
        return advance_topic_statistics(25)

    def test_unpublished_and_dirty_projections_fail_closed(self):
        with database.get_db() as db:
            with self.assertRaises(TopicStatisticsUnavailable):
                published_topic_statistics(db)
        ready = self._publish()
        with database.get_db() as db:
            publication = published_topic_statistics(db)
            self.assertEqual(publication.build_id, ready.build_id)
            db.execute(
                """INSERT OR REPLACE INTO topic_statistics_dirty(topic_id,reason,queued_at)
                   VALUES('t','fixture',?)""",
                (NOW,),
            )
            with self.assertRaisesRegex(TopicStatisticsUnavailable, "pending"):
                published_topic_statistics(db)

    def test_complete_zero_is_explicit_and_includes_policy_versions(self):
        ready = self._publish()
        with database.get_db() as db:
            publication = published_topic_statistics(db)
            self.assertEqual(publication.publication_id, ready.publication_id)
            self.assertEqual(publication.assignment_policy_version, "effective-review-v1")
            self.assertEqual(publication.event_policy_version, "current-stable-event-v1")
            self.assertEqual(len(publication.topics), 1)
            topic = publication.topics[0]
            self.assertEqual((topic.topic_id, topic.document_count, topic.event_count), ("t", 0, 0))
            same_publication, by_slug = published_topic_statistic(db, slug="topic")
            self.assertEqual(same_publication.publication_id, publication.publication_id)
            self.assertEqual(by_slug.topic_version_id, "tv")
            with self.assertRaises(TopicStatisticsNotFound):
                published_topic_statistic(db, topic_id="missing")

    def test_inconsistent_membership_fails_closed(self):
        self._publish()
        with database.get_db() as db:
            db.execute("DROP TRIGGER topic_statistics_versions_no_update")
            db.execute("UPDATE topic_statistics_versions SET document_count=1")
            with self.assertRaisesRegex(TopicStatisticsUnavailable, "membership"):
                published_topic_statistics(db)

    def test_detail_requires_exactly_one_identifier(self):
        self._publish()
        with database.get_db() as db:
            with self.assertRaises(ValueError):
                published_topic_statistic(db)
            with self.assertRaises(ValueError):
                published_topic_statistic(db, topic_id="t", slug="topic")


if __name__ == "__main__":
    unittest.main()
