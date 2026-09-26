import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.catalog import sync_identity_catalog
from app.crawler import runner
from app.event_admission import EventAdmissionError, record_admission_review
from app.event_match_reviews import (
    EventMatchReviewError,
    effective_match_status,
    record_match_review,
    review_queue,
)
from app.ingest import begin_ingest_run, observe_candidate


NOW = "2026-09-26T12:00:00.000000Z"


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


class EventMatchReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        self.blobs = Path(temporary.name) / "blobs"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "BLOB_PATH", self.blobs),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """INSERT INTO companies(slug,name,name_zh,market,aliases)
                   VALUES('openai','OpenAI','OpenAI','PRIVATE','[]')"""
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','ai','media','rss',
                          'https://example.test/feed')"""
            )
            sync_identity_catalog(db)
        observed = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc).isoformat()
        source = {
            "key": "fixture", "name": "Fixture", "channel": "ai",
            "tier": "media", "type": "rss", "url": "https://example.test/feed",
            "interval_minutes": 30,
        }
        item = {
            "url": "https://example.test/report", "title": "OpenAI update",
            "summary": "Source evidence.", "published_at": observed,
            "companies": ["openai"], "observed_at": observed,
            "source_record": {"id": "report", "title": "OpenAI update"},
            "payload_kind": "feed_entry",
        }
        run = begin_ingest_run(source, started_at=observed)
        observation = observe_candidate(run, item, ordinal=0, observed_at=observed)
        self.assertTrue(runner.insert_item("fixture", item, observation=observation))
        with database.get_db() as db:
            row = db.execute(
                """SELECT document.current_version_id,input.raw_record_id
                   FROM documents AS document
                   JOIN document_version_inputs AS input
                     ON input.version_id=document.current_version_id
                    AND input.role='primary'"""
            ).fetchone()
            self.document_version_id = row["current_version_id"]
            self.raw_record_id = row["raw_record_id"]
            entity_id = db.execute(
                "SELECT entity_id FROM legacy_company_entities"
            ).fetchone()[0]
            semantic = {
                "schema_version": "event-v1", "title": "OpenAI update",
                "event_type": "product_update", "event_time_start": None,
                "event_time_end": None, "time_precision": "unknown",
                "primary_entities": [entity_id], "object_entities": [],
                "facts": [], "topics": [], "knowledge_status": "reported",
                "created_by": "fixture", "method_version": "fixture-v1",
            }
            digest = hashlib.sha256(canonical(semantic).encode()).hexdigest()
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            self.event_id, self.event_version_id = "event-1", "event-version-1"
            db.execute(
                """INSERT INTO events(
                       id,dataset_id,first_seen_at,latest_report_at,status)
                   VALUES(?,?,?,?, 'candidate')""",
                (self.event_id, dataset, NOW, NOW),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,time_precision,
                       primary_entities_json,object_entities_json,facts_json,topics_json,
                       knowledge_status,version_sha256,available_at,created_by,method_version)
                   VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.event_version_id, self.event_id, semantic["schema_version"],
                    semantic["title"], semantic["event_type"], semantic["time_precision"],
                    canonical(semantic["primary_entities"]), "[]", "[]", "[]",
                    semantic["knowledge_status"], digest, NOW, "fixture", "fixture-v1",
                ),
            )
            db.execute(
                "UPDATE events SET current_version_id=? WHERE id=?",
                (self.event_version_id, self.event_id),
            )
            self.decision_id = "decision-1"
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,score,
                       decision,reason,review_status,available_at)
                   VALUES(?,?,?,?,?,?,?,?,'candidate_link','fixture','pending',?)""",
                (
                    self.decision_id, dataset, "fixture-1",
                    canonical([self.document_version_id]),
                    canonical([self.event_version_id]), "fixture-v1", "{}", 0.9, NOW,
                ),
            )
            db.execute(
                """INSERT INTO document_event_links(
                       id,document_version_id,event_id,event_version_id,role,
                       decision_id,available_at)
                   VALUES('link-1',?,?,?,'primary',?,?)""",
                (
                    self.document_version_id, self.event_id, self.event_version_id,
                    self.decision_id, NOW,
                ),
            )
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,role,available_at)
                   VALUES('evidence-1',?,?,?,'supports',?)""",
                (
                    self.event_version_id, self.document_version_id,
                    self.raw_record_id, NOW,
                ),
            )

    def accept(self, previous=None):
        with database.get_db() as db:
            return record_match_review(
                db, decision_id=self.decision_id, decision="accepted",
                expected_previous_review_id=previous,
                evidence_ids=(self.raw_record_id,), reviewer_id="reviewer",
                reason="Evidence supports this event link.", now=NOW,
            )

    def test_queue_is_stable_and_new_decisions_are_queued(self):
        with database.get_db() as db:
            first = review_queue(db)
            self.assertEqual([row.decision_id for row in first], [self.decision_id])
            sequence = first[0].queue_sequence
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,
                       decision,reason,review_status,available_at)
                   VALUES('decision-2',?,'fixture-2','[]','[]','fixture-v1','{}',
                          'new_candidate','fixture','pending',?)""",
                (dataset, NOW),
            )
            after = review_queue(db, after_sequence=sequence)
            self.assertEqual([row.decision_id for row in after], ["decision-2"])

    def test_review_is_append_only_and_uses_optimistic_concurrency(self):
        first = self.accept()
        self.assertEqual(first.current_decision, "accepted")
        with database.get_db() as db:
            self.assertEqual(effective_match_status(db, self.decision_id), "accepted")
            self.assertEqual(review_queue(db), ())
            with self.assertRaisesRegex(EventMatchReviewError, "refresh"):
                record_match_review(
                    db, decision_id=self.decision_id, decision="rejected",
                    expected_previous_review_id=None,
                    evidence_ids=(self.raw_record_id,), reviewer_id="stale",
                    reason="Stale writer.", now=NOW,
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE event_match_reviews SET reason='rewrite'")
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM event_match_reviews")

    def test_review_rejects_evidence_outside_decision_inputs(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(EventMatchReviewError, "decision input"):
                record_match_review(
                    db, decision_id=self.decision_id, decision="accepted",
                    expected_previous_review_id=None, evidence_ids=("raw-missing",),
                    reviewer_id="reviewer", reason="Invalid evidence.", now=NOW,
                )

    def test_event_admission_uses_latest_human_match_review(self):
        with database.get_db() as db:
            with self.assertRaisesRegex(EventAdmissionError, "pending candidate links"):
                record_admission_review(
                    db, event_version_id=self.event_version_id, decision="reported",
                    expected_previous_review_id=None, reviewer_id="reviewer",
                    reason="Should wait for link review.", now=NOW,
                )
        self.accept()
        with database.get_db() as db:
            admitted = record_admission_review(
                db, event_version_id=self.event_version_id, decision="reported",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="Link and evidence reviewed.", now=NOW,
            )
            self.assertEqual(admitted.current_decision, "reported")

    def test_migration_35_preserves_schema_34_database(self):
        predecessor = self.path.parent / "schema34.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:34])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (35, 36, 37))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 34)
        with database.get_db(predecessor) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("event_match_review_queue", tables)
            self.assertIn("event_match_reviews", tables)


if __name__ == "__main__":
    unittest.main()
