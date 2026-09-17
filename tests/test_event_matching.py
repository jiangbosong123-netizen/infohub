import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import config, database
from app.db_admin import DatabaseVerificationError, verify_database
from app.catalog import sync_identity_catalog
from app.crawler import runner
from app.event_matching import (
    MATCHER_VERSION,
    EventProposal,
    assess_event_match,
    record_candidate_matches,
)
from app.ingest import begin_ingest_run, observe_candidate
from app.timeutil import utc_now


def candidate(
    *, title, event_type, entities=("entity_openai",), facts=(), event_time_start=None
):
    return {
        "title": title,
        "event_type": event_type,
        "primary_entity_ids": tuple(entities),
        "object_entity_ids": (),
        "facts": tuple(facts),
        "event_time_start": event_time_start,
        "event_time_end": None,
    }


class EventMatchingRuleTests(unittest.TestCase):
    def proposal(self, title, event_type, *, entities=("entity_openai",), facts=()):
        return EventProposal(
            document_version_id="document_version_fixture", title=title,
            event_type=event_type, primary_entity_ids=tuple(entities),
            facts=tuple(facts),
        )

    def test_different_earnings_periods_are_a_hard_negative(self):
        result = assess_event_match(
            self.proposal("OpenAI reports Q2 2026 earnings", "earnings"),
            candidate(title="OpenAI reports Q1 2026 earnings", event_type="earnings"),
        )
        self.assertEqual(result.decision, "no_match")
        self.assertEqual(result.features["hard_negative"], "reporting periods conflict")

    def test_same_earnings_period_can_link(self):
        result = assess_event_match(
            self.proposal("OpenAI posts results for Q2 2026", "earnings"),
            candidate(title="OpenAI reports Q2 2026 earnings", event_type="earnings"),
        )
        self.assertEqual(result.decision, "candidate_link")
        self.assertIn("reporting period", result.reason)

    def test_different_model_versions_are_a_hard_negative(self):
        result = assess_event_match(
            self.proposal("OpenAI launches GPT-4.2 coding model", "model_release"),
            candidate(
                title="OpenAI launches GPT-4.1 coding model", event_type="model_release"
            ),
        )
        self.assertEqual(result.decision, "no_match")
        self.assertEqual(
            result.features["hard_negative"], "product or model versions conflict"
        )

    def test_denial_and_announcement_are_separate_events(self):
        result = assess_event_match(
            self.proposal("OpenAI denies acquisition talks", "ma"),
            candidate(title="OpenAI announces acquisition talks", event_type="ma"),
        )
        self.assertEqual(result.decision, "no_match")
        self.assertIn("denial", result.reason)

    def test_different_reports_of_same_structured_fact_can_link(self):
        fact = {
            "subject_ids": ["entity_openai"], "predicate": "revenue",
            "value": 420, "currency": "USD", "period": "2026-Q2",
            "modality": "asserted", "evidence_ids": ["source-a"],
        }
        second = {**fact, "evidence_ids": ["source-b"], "fact_id": "other-id"}
        result = assess_event_match(
            self.proposal("OpenAI quarterly filing", "earnings", facts=(fact,)),
            candidate(
                title="Technology company reports revenue", event_type="earnings",
                facts=(second,),
            ),
        )
        self.assertEqual(result.decision, "candidate_link")
        self.assertTrue(result.features["shared_fact_signatures"])
        self.assertEqual(result.features["score_kind"], "ranking_not_probability")

    def test_disjoint_entities_are_a_hard_negative(self):
        result = assess_event_match(
            self.proposal("OpenAI announces a model", "model_release"),
            candidate(
                title="OpenAI announces a model", event_type="model_release",
                entities=("entity_anthropic",),
            ),
        )
        self.assertEqual(result.decision, "no_match")
        self.assertEqual(result.features["hard_negative"], "primary entities are disjoint")


class EventMatchingPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app.db"
        self.blobs = Path(self.temp.name) / "blobs"
        for item in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "BLOB_PATH", self.blobs),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """INSERT INTO companies(slug,name,name_zh,market,aliases)
                   VALUES('openai','OpenAI','OpenAI','PRIVATE','[]')"""
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','ai','media','rss','https://example.com/feed')"""
            )
            sync_identity_catalog(db)
        self.source = {
            "key": "fixture", "name": "Fixture", "channel": "ai", "tier": "media",
            "type": "rss", "url": "https://example.com/feed", "interval_minutes": 30,
        }
        self.number = 0

    def document(self, title: str) -> str:
        self.number += 1
        observed = datetime(2026, 9, 17, 11, self.number, tzinfo=timezone.utc).isoformat()
        candidate_item = {
            "url": f"https://example.com/{self.number}", "title": title,
            "summary": "Two independently structured facts.",
            "published_at": observed, "companies": ["openai"],
            "observed_at": observed,
            "source_record": {"id": self.number, "title": title},
            "payload_kind": "feed_entry",
        }
        run = begin_ingest_run(self.source, started_at=observed)
        observation = observe_candidate(run, candidate_item, ordinal=0, observed_at=observed)
        self.assertTrue(runner.insert_item("fixture", candidate_item, observation=observation))
        with database.get_db() as db:
            return db.execute(
                """SELECT document.current_version_id
                   FROM documents AS document
                   WHERE document.legacy_item_id=(SELECT MAX(id) FROM items)"""
            ).fetchone()[0]

    def event(self, title: str, event_type: str, fact: dict) -> str:
        now = utc_now()
        event_id = f"event_{uuid4().hex}"
        version_id = f"event_version_{uuid4().hex}"
        payload = {
            "title": title, "event_type": event_type,
            "primary_entities": ["entity_openai"], "facts": [fact],
        }
        version_sha = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with database.get_db() as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO events(
                       id,dataset_id,first_seen_at,latest_report_at,last_fact_change_at,status
                   ) VALUES(?,?,?,?,?,'candidate')""",
                (event_id, dataset_id, now, now, now),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,previous_version_id,schema_version,title,event_type,
                       event_time_start,event_time_end,time_precision,primary_entities_json,
                       object_entities_json,facts_json,topics_json,knowledge_status,
                       version_sha256,available_at,created_by,method_version
                   ) VALUES(?,?,1,NULL,'event-candidate-v1',?,?,NULL,NULL,'unknown',
                            ?,'[]',?,'[]','reported',?,?, 'test','test-v1')""",
                (
                    version_id, event_id, title, event_type,
                    json.dumps(["entity_openai"]), json.dumps([fact]), version_sha, now,
                ),
            )
            db.execute(
                "UPDATE events SET current_version_id=? WHERE id=?", (version_id, event_id)
            )
        return version_id

    def test_one_document_can_link_multiple_events_and_rerun_is_idempotent(self):
        document_version_id = self.document("OpenAI quarterly update and GPT-5.1 launch")
        revenue = {
            "subject_ids": ["entity_openai"], "predicate": "revenue",
            "value": 500, "currency": "USD", "period": "2026-Q2",
            "modality": "asserted",
        }
        release = {
            "subject_ids": ["entity_openai"], "predicate": "released_model",
            "object": {"name": "GPT", "model_version": "5.1"},
            "modality": "announced",
        }
        earnings_version = self.event("OpenAI Q2 2026 results", "earnings", revenue)
        model_version = self.event("OpenAI launches GPT-5.1", "model_release", release)
        proposal = EventProposal(
            document_version_id=document_version_id,
            title="OpenAI quarterly update and GPT-5.1 launch",
            event_type="other", primary_entity_ids=("entity_openai",),
            facts=(revenue, release),
        )
        with database.get_db() as db:
            first = record_candidate_matches(
                db, proposal, [earnings_version, model_version]
            )
            second = record_candidate_matches(
                db, proposal, [earnings_version, model_version]
            )
            links = db.execute(
                """SELECT link.role,event.status,decision.review_status,
                          decision.matcher_version,decision.features_json
                   FROM document_event_links AS link
                   JOIN events AS event ON event.id=link.event_id
                   JOIN match_decisions AS decision ON decision.id=link.decision_id
                   ORDER BY link.event_id"""
            ).fetchall()
        self.assertEqual(first.candidate_links_created, 2)
        self.assertEqual(first.evidence_links_created, 2)
        self.assertEqual(second.candidate_links_created, 0)
        self.assertEqual(second.decisions_created, 0)
        self.assertEqual(len(links), 2)
        self.assertTrue(all(row["role"] == "candidate" for row in links))
        self.assertTrue(all(row["status"] == "candidate" for row in links))
        self.assertTrue(all(row["review_status"] == "pending" for row in links))
        self.assertTrue(all(row["matcher_version"] == MATCHER_VERSION for row in links))
        self.assertTrue(all("ranking_not_probability" in row["features_json"] for row in links))
        self.assertEqual(verify_database(self.path, require_current=True).integrity, "ok")

    def test_all_hard_negatives_record_new_candidate_without_link(self):
        document_version_id = self.document("OpenAI reports Q2 2026 earnings")
        old_fact = {
            "subject_ids": ["entity_openai"], "predicate": "revenue",
            "value": 100, "period": "2026-Q1", "modality": "asserted",
        }
        version_id = self.event("OpenAI reports Q1 2026 earnings", "earnings", old_fact)
        proposal = EventProposal(
            document_version_id=document_version_id,
            title="OpenAI reports Q2 2026 earnings", event_type="earnings",
            primary_entity_ids=("entity_openai",),
        )
        with database.get_db() as db:
            report = record_candidate_matches(db, proposal, [version_id])
            decisions = db.execute(
                "SELECT decision,review_status FROM match_decisions ORDER BY decision"
            ).fetchall()
            links = db.execute("SELECT COUNT(*) FROM document_event_links").fetchone()[0]
        self.assertTrue(report.new_candidate_decision_created)
        self.assertEqual(links, 0)
        self.assertEqual(
            [tuple(row) for row in decisions],
            [("new_candidate", "pending"), ("no_match", "pending")],
        )

    def test_empty_retrieval_records_new_candidate_decision(self):
        document_version_id = self.document("OpenAI begins a new research program")
        proposal = EventProposal(
            document_version_id=document_version_id,
            title="OpenAI begins a new research program", event_type="research_result",
            primary_entity_ids=("entity_openai",),
        )
        with database.get_db() as db:
            first = record_candidate_matches(db, proposal, [])
            second = record_candidate_matches(db, proposal, [])
            decisions = db.execute(
                "SELECT decision,candidate_event_versions_json FROM match_decisions"
            ).fetchall()
        self.assertTrue(first.new_candidate_decision_created)
        self.assertFalse(second.new_candidate_decision_created)
        self.assertEqual([tuple(row) for row in decisions], [("new_candidate", "[]")])

    def test_database_verification_rejects_unresolvable_match_inputs(self):
        document_version_id = self.document("OpenAI begins a new research program")
        with database.get_db() as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,
                       score,decision,reason,review_status,available_at
                   ) VALUES('bad',?,'bad',?,'[]',?,'{}',NULL,'new_candidate',
                            'bad fixture','pending',?)""",
                (dataset_id, json.dumps([document_version_id, "missing"]), MATCHER_VERSION, utc_now()),
            )
        with self.assertRaisesRegex(DatabaseVerificationError, "match_decisions=1"):
            verify_database(self.path, require_current=True)


if __name__ == "__main__":
    unittest.main()
