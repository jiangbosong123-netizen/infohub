import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database
from app.api_auth import create_consumer, issue_api_key
from app.event_admission import record_admission_review
from app.event_dataset_release import record_release_review
from app.event_match_reviews import record_match_review
from app.event_review_sample_gate import record_sample_evaluation
from app.event_review_sampling import create_sample_batch, sample_queue
from app.web.routes import app
from tests import test_event_review_sampling as sampling_fixture


NOW = sampling_fixture.NOW
canonical = sampling_fixture.canonical


class ApiEventTests(unittest.TestCase):
    def setUp(self):
        sampling_fixture.EventReviewSamplingTests.setUp(self)
        with database.get_db(self.path) as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            entity_id = db.execute("SELECT entity_id FROM legacy_company_entities").fetchone()[0]
            semantic = {
                "schema_version": "event-v1", "title": "OpenAI reports earnings",
                "event_type": "earnings", "event_time_start": "2026-09-26T00:00:00Z",
                "event_time_end": None, "time_precision": "day",
                "primary_entities": [entity_id], "object_entities": [],
                "facts": [], "topics": [], "knowledge_status": "reported",
                "created_by": "fixture", "method_version": "fixture-v1",
            }
            digest = hashlib.sha256(canonical(semantic).encode()).hexdigest()
            db.execute(
                """INSERT INTO events(id,dataset_id,first_seen_at,latest_report_at,status)
                   VALUES('event-2',?,?,?,'candidate')""",
                (dataset, NOW, NOW),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,event_time_start,
                       time_precision,primary_entities_json,object_entities_json,facts_json,
                       topics_json,knowledge_status,version_sha256,available_at,created_by,
                       method_version)
                   VALUES('event-version-2','event-2',1,?,?,?,?,?,?,'[]','[]','[]',?,?,?,?,?)""",
                (
                    semantic["schema_version"], semantic["title"], semantic["event_type"],
                    semantic["event_time_start"], semantic["time_precision"],
                    canonical(semantic["primary_entities"]), semantic["knowledge_status"],
                    digest, NOW, "fixture", "fixture-v1",
                ),
            )
            db.execute(
                "UPDATE events SET current_version_id='event-version-2' WHERE id='event-2'"
            )
            db.execute(
                """INSERT INTO match_decisions(
                       id,dataset_id,decision_key,input_versions_json,
                       candidate_event_versions_json,matcher_version,features_json,score,
                       decision,reason,review_status,available_at)
                   VALUES('decision-7',?,'fixture-7',?,?,'matcher-v2','{}',0.95,
                          'candidate_link','fixture','pending',?)""",
                (
                    dataset, canonical([self.document_version_id]),
                    canonical(["event-version-2"]), NOW,
                ),
            )
            db.execute(
                """INSERT INTO document_event_links(
                       id,document_version_id,event_id,event_version_id,role,decision_id,available_at)
                   VALUES('link-2',?,'event-2','event-version-2','primary','decision-7',?)""",
                (self.document_version_id, NOW),
            )
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,role,available_at)
                   VALUES('event-evidence-2','event-version-2',?,?,'supports',?)""",
                (self.document_version_id, self.raw_record_id, NOW),
            )
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,fact_id,role,available_at)
                   VALUES('event-evidence-3','event-version-2',?,?,NULL,'context',?)""",
                (self.document_version_id, self.raw_record_id, NOW),
            )
            batch = create_sample_batch(
                db, seed="api-events", per_stratum_limit=250,
                created_by="api-fixture", now=NOW,
            )
            for item in sample_queue(db, batch.batch_id, limit=250):
                record_match_review(
                    db, decision_id=item.decision_id, decision="accepted",
                    expected_previous_review_id=None,
                    evidence_ids=(self.raw_record_id,), reviewer_id="api-fixture",
                    reason="Synthetic API quality review.", now=NOW,
                )
            evaluation = record_sample_evaluation(
                db, batch_id=batch.batch_id, decision="approved",
                expected_previous_evaluation_id=None,
                minimum_decided_bps=10_000, minimum_stratum_decided_bps=10_000,
                minimum_acceptance_bps=10_000,
                minimum_stratum_acceptance_bps=10_000,
                evaluator_id="api-fixture", reason="Synthetic quality approval.", now=NOW,
            )
            for version_id in (self.event_version_id, "event-version-2"):
                record_admission_review(
                    db, event_version_id=version_id, decision="reported",
                    expected_previous_review_id=None, reviewer_id="api-fixture",
                    reason="Synthetic API event admission.", now=NOW,
                )
            record_release_review(
                db, sample_evaluation_id=evaluation.current_evaluation_id,
                decision="approved", expected_previous_review_id=None,
                reviewer_id="api-fixture", reason="Synthetic API event release.", now=NOW,
            )
            consumer = create_consumer(db, "event-test", actor="test")
            self.key = issue_api_key(
                db, consumer, {"read:events"},
                expires_at=datetime.now(timezone.utc) + timedelta(days=1), actor="test",
            )
            wrong = create_consumer(db, "event-wrong", actor="test")
            self.wrong_key = issue_api_key(
                db, wrong, {"read:items"},
                expires_at=datetime.now(timezone.utc) + timedelta(days=1), actor="test",
            )
            evidence = create_consumer(db, "event-evidence-test", actor="test")
            self.evidence_key = issue_api_key(
                db, evidence, {"read:evidence"},
                expires_at=datetime.now(timezone.utc) + timedelta(days=1), actor="test",
            )
            self.entity_id = entity_id
        for mocked in (
            patch("app.web.routes.get_db", lambda: database.get_db(self.path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path)),
            patch.object(config, "API_EVENTS_ENABLED", True),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app)

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def test_list_is_release_bound_typed_filtered_and_paginated(self):
        first = self.client.get("/api/v1/events?limit=1", headers=self.headers())
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["schema_version"], "1.0.0")
        self.assertEqual(body["pagination"]["consistency"], "release")
        self.assertEqual(body["release"]["policy_version"], "event-release-v1")
        self.assertEqual(body["data"][0]["admission"]["public_state"], "reported")
        second = self.client.get(
            "/api/v1/events", params={"limit": 1, "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual([item["id"] for item in second.json()["data"]], ["event-2"])
        filtered = self.client.get(
            "/api/v1/events",
            params={"type": "earnings", "state": "reported", "entity_id": self.entity_id,
                    "q": "earnings"},
            headers=self.headers(),
        )
        self.assertEqual([item["id"] for item in filtered.json()["data"]], ["event-2"])

    def test_detail_etag_auth_and_unreleased_not_found(self):
        detail = self.client.get("/api/v1/events/event-2", headers=self.headers())
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["version"]["event_type"], "earnings")
        cached = self.client.get(
            "/api/v1/events/event-2",
            headers={**self.headers(), "If-None-Match": detail.headers["etag"]},
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(
            self.client.get("/api/v1/events/missing", headers=self.headers()).status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/v1/events").status_code, 401)
        self.assertEqual(
            self.client.get("/api/v1/events", headers=self.headers(self.wrong_key)).status_code,
            403,
        )

    def test_stale_release_invalid_parameters_and_switch_fail_closed(self):
        for path in (
            "/api/v1/events?unknown=1", "/api/v1/events?limit=01",
            "/api/v1/events?limit=1&limit=2", "/api/v1/events?type=unknown",
            "/api/v1/events?state=candidate", "/api/v1/events?entity_id=",
            "/api/v1/events/event-2?version=1",
        ):
            self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 422)
        with patch.object(config, "API_EVENTS_ENABLED", False):
            self.assertEqual(
                self.client.get("/api/v1/events", headers=self.headers()).status_code, 503
            )
        with database.get_db(self.path) as db:
            admission = db.execute(
                """SELECT id FROM event_admission_reviews
                   WHERE event_version_id='event-version-2' ORDER BY version DESC LIMIT 1"""
            ).fetchone()[0]
            record_admission_review(
                db, event_version_id="event-version-2", decision="reported",
                expected_previous_review_id=admission, reviewer_id="api-fixture",
                reason="Makes the frozen release stale.", now=NOW,
            )
        stale = self.client.get("/api/v1/events", headers=self.headers())
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (503, "not_ready"))

    def test_openapi_declares_event_contract(self):
        schema = self.client.get("/openapi.json").json()
        listing = schema["paths"]["/api/v1/events"]["get"]
        detail = schema["paths"]["/api/v1/events/{id}"]["get"]
        self.assertEqual(listing["x-required-scopes"], ["read:events"])
        self.assertEqual(detail["x-required-scopes"], ["read:events"])


if __name__ == "__main__":
    unittest.main()
