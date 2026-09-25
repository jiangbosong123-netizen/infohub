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
from app.event_admission import (
    EventAdmissionError,
    admitted_event,
    admission_preview,
    record_admission_review,
)
from app.ingest import begin_ingest_run, observe_candidate


NOW = "2026-09-25T21:00:00.000000Z"


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


class EventAdmissionTests(unittest.TestCase):
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
                   VALUES('fixture','Fixture','ai','media','rss','https://example.test/feed')"""
            )
            sync_identity_catalog(db)
        source = {
            "key": "fixture", "name": "Fixture", "channel": "ai", "tier": "media",
            "type": "rss", "url": "https://example.test/feed", "interval_minutes": 30,
        }
        observed = datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc).isoformat()
        item = {
            "url": "https://example.test/report", "title": "OpenAI reports an update",
            "summary": "Source evidence.", "published_at": observed,
            "companies": ["openai"], "observed_at": observed,
            "source_record": {"id": "report", "title": "OpenAI reports an update"},
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
                     ON input.version_id=document.current_version_id AND input.role='primary'"""
            ).fetchone()
            self.document_version_id = row["current_version_id"]
            self.raw_record_id = row["raw_record_id"]
            self.entity_id = db.execute(
                "SELECT entity_id FROM legacy_company_entities"
            ).fetchone()[0]

    def create_event(self, knowledge_status="reported"):
        semantic = {
            "schema_version": "event-v1", "title": "OpenAI reports an update",
            "event_type": "product_update", "event_time_start": None,
            "event_time_end": None, "time_precision": "unknown",
            "primary_entities": [self.entity_id], "object_entities": [],
            "facts": [], "topics": [], "knowledge_status": knowledge_status,
            "created_by": "fixture", "method_version": "fixture-v1",
        }
        digest = hashlib.sha256(canonical(semantic).encode()).hexdigest()
        with database.get_db() as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            suffix = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] + 1
            event_id, version_id = f"event-{suffix}", f"event-version-{suffix}"
            db.execute(
                """INSERT INTO events(id,dataset_id,first_seen_at,latest_report_at,status)
                   VALUES(?,?,?,?,'candidate')""",
                (event_id, dataset, NOW, NOW),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,time_precision,
                       primary_entities_json,object_entities_json,facts_json,topics_json,
                       knowledge_status,version_sha256,available_at,created_by,method_version)
                   VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id, event_id, semantic["schema_version"], semantic["title"],
                    semantic["event_type"], semantic["time_precision"],
                    canonical(semantic["primary_entities"]), "[]", "[]", "[]",
                    knowledge_status, digest, NOW, "fixture", "fixture-v1",
                ),
            )
            db.execute(
                "UPDATE events SET current_version_id=? WHERE id=?", (version_id, event_id)
            )
            decision_id = f"decision-{suffix}"
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,score,
                       decision,reason,review_status,available_at)
                   VALUES(?,?,?,?,?,?,?,?,'candidate_link','fixture','accepted',?)""",
                (
                    decision_id, dataset, f"fixture-{suffix}",
                    canonical([self.document_version_id]), canonical([version_id]),
                    "fixture-v1", "{}", 1.0, NOW,
                ),
            )
            db.execute(
                """INSERT INTO document_event_links(
                       id,document_version_id,event_id,event_version_id,role,decision_id,available_at)
                   VALUES(?,?,?,?, 'primary',?,?)""",
                (
                    f"link-{suffix}", self.document_version_id, event_id, version_id,
                    decision_id, NOW,
                ),
            )
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,role,available_at)
                   VALUES(?,?,?,?, 'supports',?)""",
                (
                    f"evidence-{suffix}", version_id, self.document_version_id,
                    self.raw_record_id, NOW,
                ),
            )
        return event_id, version_id

    def test_unknown_candidate_cannot_be_published_as_reported(self):
        _, version_id = self.create_event("unknown")
        with database.get_db() as db:
            with self.assertRaisesRegex(EventAdmissionError, "knowledge status"):
                record_admission_review(
                    db, event_version_id=version_id, decision="reported",
                    expected_previous_review_id=None, reviewer_id="reviewer",
                    reason="Unknown legacy projection.", now=NOW,
                )
            rejected = record_admission_review(
                db, event_version_id=version_id, decision="rejected",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="Unknown legacy projection.", now=NOW,
            )
        self.assertEqual(rejected.current_decision, "rejected")

    def test_reported_admission_is_append_only_and_serves_current_version(self):
        event_id, version_id = self.create_event()
        with database.get_db() as db:
            preview = admission_preview(db, version_id)
            self.assertEqual(preview.metrics["accepted_links"], 1)
            self.assertEqual(preview.metrics["supporting_evidence"], 1)
            reviewed = record_admission_review(
                db, event_version_id=version_id, decision="reported",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="Evidence and match reviewed.", now=NOW,
            )
            admitted = admitted_event(db, event_id)
            self.assertEqual(admitted.public_state, "reported")
            self.assertEqual(admitted.review_id, reviewed.current_review_id)
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE event_admission_reviews SET reason='rewrite'")
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM event_admission_reviews")

    def test_stale_reviewer_and_changed_evidence_fail_closed(self):
        event_id, version_id = self.create_event()
        with database.get_db() as db:
            first = record_admission_review(
                db, event_version_id=version_id, decision="reported",
                expected_previous_review_id=None, reviewer_id="reviewer",
                reason="Initial review.", now=NOW,
            )
            with self.assertRaisesRegex(EventAdmissionError, "refresh"):
                record_admission_review(
                    db, event_version_id=version_id, decision="rejected",
                    expected_previous_review_id=None, reviewer_id="stale",
                    reason="Stale decision.", now=NOW,
                )
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,role,available_at)
                   VALUES('later-context',?,?,?,'context',?)""",
                (version_id, self.document_version_id, self.raw_record_id, NOW),
            )
            with self.assertRaisesRegex(EventAdmissionError, "stale"):
                admitted_event(db, event_id)
            second = record_admission_review(
                db, event_version_id=version_id, decision="reported",
                expected_previous_review_id=first.current_review_id,
                reviewer_id="reviewer", reason="Reviewed expanded evidence.", now=NOW,
            )
            self.assertEqual(second.current_review_version, 2)

    def test_corroborated_and_confirmed_have_stricter_evidence_rules(self):
        _, version_id = self.create_event("corroborated")
        with database.get_db() as db:
            with self.assertRaisesRegex(EventAdmissionError, "two verified"):
                record_admission_review(
                    db, event_version_id=version_id, decision="corroborated",
                    expected_previous_review_id=None, reviewer_id="reviewer",
                    reason="One source is insufficient.", now=NOW,
                )
        _, confirmed_version = self.create_event("confirmed_by_primary")
        with database.get_db() as db:
            with self.assertRaisesRegex(EventAdmissionError, "primary entity publisher"):
                record_admission_review(
                    db, event_version_id=confirmed_version, decision="confirmed",
                    expected_previous_review_id=None, reviewer_id="reviewer",
                    reason="No verified primary attribution.", now=NOW,
                )

    def test_migration_34_preserves_schema_33_database(self):
        predecessor = self.path.parent / "schema33.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:33])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (34,))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 33)
        with database.get_db(predecessor) as db:
            columns = {row["name"] for row in db.execute(
                "PRAGMA table_info(event_admission_reviews)"
            )}
            self.assertIn("policy_version", columns)


if __name__ == "__main__":
    unittest.main()
