import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.db_admin import verify_database
from app.topic_assignment_reviews import record_topic_assignment_review
from app.topic_statistics import advance_topic_statistics


NOW = "2026-09-23T15:00:00.000000Z"


class TopicStatisticsBuilderTests(unittest.TestCase):
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
            self.dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')"
            )
            self._topic(db, "topic-a", "topic-version-a", "a")
            self._topic(db, "topic-b", "topic-version-b", "b")
            self._document(db, 1, "document-a", "document-version-a")
            self._document(db, 2, "document-b", "document-version-b")
            self._document(db, 3, "document-c", "document-version-c")
            self._assignment(db, "accepted-a", "document-version-a", "topic-version-a", "accepted")
            self._assignment(db, "candidate-a", "document-version-a", "topic-version-a", "candidate")
            self._assignment(db, "candidate-b", "document-version-b", "topic-version-a", "candidate")
            self._assignment(db, "accepted-c", "document-version-c", "topic-version-a", "accepted")
            accepted = record_topic_assignment_review(
                db, assignment_id="candidate-a", decision="accepted",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="Confirmed topic.", now=NOW,
            )
            record_topic_assignment_review(
                db, assignment_id="accepted-c", decision="rejected",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="False positive.", now=NOW,
            )
            self.accepted_review_id = accepted.current_review_id
            self._event(db, "event-a", "event-version-a", "active", "topic-version-a")
            self._event(db, "event-candidate", "event-version-candidate", "candidate", "topic-version-a")

    def _topic(self, db, topic_id, version_id, slug):
        db.execute(
            "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES(?,?,'active',?)",
            (topic_id, self.dataset, NOW),
        )
        db.execute(
            """INSERT INTO topic_versions(
                   id,topic_id,version,slug,name,group_key,description,rules_json,
                   rules_hash,version_sha256,status,available_at)
               VALUES(?,?,1,?,?,'technology','','{}',?,?,'active',?)""",
            (version_id, topic_id, slug, slug.upper(), "a" * 64, "b" * 64, NOW),
        )
        db.execute(
            "UPDATE topic_catalog SET current_version_id=? WHERE id=?",
            (version_id, topic_id),
        )

    def _document(self, db, item_id, document_id, version_id):
        db.execute(
            """INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
               VALUES(?,1,?,?,'ai',?,?)""",
            (item_id, f"https://example.test/{item_id}", document_id, NOW, NOW),
        )
        db.execute(
            """INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at)
               VALUES(?,?,?,'article',?)""",
            (document_id, self.dataset, item_id, NOW),
        )
        db.execute(
            """INSERT INTO document_versions(
                   id,document_id,version,normalizer_version,normalized_at,title_original,
                   language,text,content_sha256,version_sha256,canonical_url,source_id,
                   published_precision,time_status,time_rule_version,tzdb_version,
                   content_origin,content_extent,truncated,extraction_status,correction_kind,
                   available_at,availability_basis,point_in_time_eligible)
               VALUES(?,?,1,'v1',?,?,'en','',?,?,?,1,'unknown','legacy_unverified',
                      'legacy','unknown','legacy_unknown','none',0,'not_attempted','initial',
                      ?,'legacy_unknown',0)""",
            (
                version_id, document_id, NOW, document_id,
                hashlib.sha256(document_id.encode()).hexdigest(),
                hashlib.sha256(version_id.encode()).hexdigest(),
                f"https://example.test/{item_id}", NOW,
            ),
        )
        db.execute(
            "UPDATE documents SET current_version_id=? WHERE id=?", (version_id, document_id)
        )

    def _assignment(self, db, assignment_id, document_version_id, topic_version_id, status):
        db.execute(
            """INSERT INTO document_topic_assignments(
                   id,document_version_id,topic_version_id,method,method_version,
                   status,available_at)
               VALUES(?,?,?,'fixture','fixture-v1',?,?)""",
            (assignment_id, document_version_id, topic_version_id, status, NOW),
        )

    def _event(self, db, event_id, version_id, status, topic_version_id):
        db.execute(
            """INSERT INTO events(id,dataset_id,first_seen_at,latest_report_at,status)
               VALUES(?,?,?,?,?)""",
            (event_id, self.dataset, NOW, NOW, status),
        )
        payload = json.dumps([topic_version_id], separators=(",", ":"))
        db.execute(
            """INSERT INTO event_versions(
                   id,event_id,version,schema_version,title,event_type,time_precision,
                   primary_entities_json,object_entities_json,facts_json,topics_json,
                   knowledge_status,version_sha256,available_at,created_by,method_version)
               VALUES(?,?,1,'event-v1',?,'other','unknown','[]','[]','[]',?,
                      'reported',?,?, 'fixture','fixture-v1')""",
            (version_id, event_id, event_id, payload, "c" * 64, NOW),
        )
        db.execute(
            "UPDATE events SET current_version_id=? WHERE id=?", (version_id, event_id)
        )

    def _complete(self, limit=25):
        reports = []
        while not reports or reports[-1].status == "building":
            reports.append(advance_topic_statistics(limit))
        return reports

    def test_build_is_resumable_deduplicated_and_published_atomically(self):
        first = advance_topic_statistics(1)
        self.assertEqual((first.status, first.processed, first.topic_count), ("building", 1, 1))
        reports = self._complete(1)
        final = reports[-1]
        self.assertEqual((final.status, final.topic_count, final.publication_version), ("ready", 2, 1))
        with database.get_db() as db:
            statistic = db.execute(
                """SELECT statistic.* FROM topic_statistics_versions AS statistic
                   JOIN topic_versions AS topic ON topic.id=statistic.topic_version_id
                   WHERE statistic.build_id=? AND topic.topic_id='topic-a'""",
                (final.build_id,),
            ).fetchone()
            self.assertEqual((statistic["document_count"], statistic["event_count"]), (1, 1))
            members = db.execute(
                """SELECT member_type,resource_id,version_id,provenance_json
                   FROM topic_statistics_members WHERE statistics_id=? ORDER BY ordinal""",
                (statistic["id"],),
            ).fetchall()
            self.assertEqual(
                [(row["member_type"], row["resource_id"]) for row in members],
                [("document", "document-a"), ("event", "event-a")],
            )
            provenance = json.loads(members[0]["provenance_json"])
            self.assertEqual(len(provenance["accepted_assignments"]), 2)
            self.assertIn(
                self.accepted_review_id,
                [row["review_id"] for row in provenance["accepted_assignments"]],
            )
            empty = db.execute(
                """SELECT document_count,event_count FROM topic_statistics_versions
                   WHERE build_id=? AND topic_version_id='topic-version-b'""",
                (final.build_id,),
            ).fetchone()
            self.assertEqual(tuple(empty), (0, 0))
        verify_database(self.path, require_current=True)

    def test_change_behind_cursor_fails_build_and_next_build_recovers(self):
        old = advance_topic_statistics(1)
        with database.get_db() as db:
            self._assignment(
                db, "late-accepted", "document-version-b", "topic-version-a", "accepted"
            )
        advance_topic_statistics(1)
        failed = advance_topic_statistics(1)
        self.assertEqual(failed.status, "failed")
        self.assertIn("inputs changed during build", failed.error_detail)
        final = self._complete(2)[-1]
        self.assertEqual((final.status, final.publication_version), ("ready", 1))
        self.assertNotEqual(final.build_id, old.build_id)
        with database.get_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM topic_statistics_builds WHERE status='failed'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM topic_statistics_publications").fetchone()[0],
                1,
            )

    def test_ready_build_is_idempotent_until_inputs_change(self):
        ready = self._complete(25)[-1]
        repeated = advance_topic_statistics(25)
        self.assertEqual(repeated.build_id, ready.build_id)
        self.assertEqual(repeated.publication_id, ready.publication_id)
        with database.get_db() as db:
            record_topic_assignment_review(
                db, assignment_id="candidate-b", decision="accepted",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="Now confirmed.", now=NOW,
            )
        revised = self._complete(25)[-1]
        self.assertEqual(revised.publication_version, 2)
        self.assertNotEqual(revised.build_id, ready.build_id)


if __name__ == "__main__":
    unittest.main()
