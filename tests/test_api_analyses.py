import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database
from app.analysis_results import publish_analysis_result
from app.api_auth import create_consumer, issue_api_key
from app.curation_search import advance_search_index
from app.web.routes import app
from tests.test_analysis_runs import AnalysisRunTests, T0


class ApiAnalysisTests(unittest.TestCase):
    def setUp(self):
        fixture = AnalysisRunTests("test_validated_result_publishes_atomically_and_idempotently")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        first_job, first_run, first_attempt = fixture.completed_attempt("api-first")
        first_output = {
            "schema_version": "summary/1.0",
            "subject": {"type": "document", "version_id": fixture.doc},
            "status": "valid", "evidence_ids": [fixture.raw],
            "data": {"summary": "First evidence-backed summary", "raw_confidence": 0.8},
        }
        publish_analysis_result(
            job_id=first_job.id, lease_token=first_job.lease_token,
            expected_input_version=fixture.doc, run_id=first_run.id,
            attempt_id=first_attempt.id, validated_output=first_output,
            review_status="unreviewed", evidence_status="supported",
            idempotency_key="result:api-first", now=T0,
        )
        # Consume the derived search refresh marker exactly as the worker does before
        # publishing another summarization for the same document.
        for _ in range(3):
            if advance_search_index(limit=10).dirty_remaining == 0:
                break
        second_job, second_run, second_attempt = fixture.completed_attempt("api-second")
        second_output = {
            "schema_version": "summary/1.0",
            "subject": {"type": "document", "version_id": fixture.doc},
            "status": "valid", "evidence_ids": [fixture.raw],
            "data": {"summary": "Current evidence-backed summary", "raw_confidence": 0.9},
        }
        publish_analysis_result(
            job_id=second_job.id, lease_token=second_job.lease_token,
            expected_input_version=fixture.doc, run_id=second_run.id,
            attempt_id=second_attempt.id, validated_output=second_output,
            review_status="accepted", evidence_status="supported",
            idempotency_key="result:api-second", now=T0,
        )
        with database.get_db(fixture.path) as db:
            self.first_id = db.execute(
                "SELECT id FROM analysis_results WHERE run_id=?", (first_run.id,)
            ).fetchone()[0]
            self.current_id = db.execute(
                "SELECT id FROM analysis_results WHERE run_id=?", (second_run.id,)
            ).fetchone()[0]
            reader = create_consumer(db, "analysis-reader", actor="test")
            self.key = issue_api_key(
                db, reader, {"read:analyses"},
                expires_at=datetime.now(timezone.utc) + timedelta(days=1), actor="test",
            )
            wrong = create_consumer(db, "analysis-wrong", actor="test")
            self.wrong_key = issue_api_key(
                db, wrong, {"read:events"},
                expires_at=datetime.now(timezone.utc) + timedelta(days=1), actor="test",
            )
        for mocked in (
            patch("app.web.routes.get_db", lambda: database.get_db(fixture.path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(fixture.path)),
            patch.object(config, "API_ANALYSES_ENABLED", True),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app)

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def test_detail_exposes_auditable_contract_without_sensitive_attempt_data(self):
        response = self.client.get(
            f"/api/v1/analyses/{self.current_id}", headers=self.headers()
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], "1.0.0")
        data = body["data"]
        self.assertEqual(data["task_type"], "summarization")
        self.assertEqual(data["review_status"], "accepted")
        self.assertEqual(data["processing_state"], "complete")
        self.assertFalse(data["stale"])
        self.assertEqual(data["evidence_refs"], [self.fixture.raw])
        self.assertEqual(data["input_refs"][0]["evidence_id"], self.fixture.raw)
        self.assertEqual(len(data["parameters_hash"]), 64)
        for secret in ("raw_response_ref", "provider_request_id", "cost_microusd"):
            self.assertNotIn(secret, data)
        cached = self.client.get(
            f"/api/v1/analyses/{self.current_id}",
            headers={**self.headers(), "If-None-Match": response.headers["etag"]},
        )
        self.assertEqual(cached.status_code, 304)
        old = self.client.get(f"/api/v1/analyses/{self.first_id}", headers=self.headers())
        self.assertTrue(old.json()["data"]["stale"])

    def test_auth_missing_invalid_parameters_and_switch_fail_closed(self):
        path = f"/api/v1/analyses/{self.current_id}"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.headers(self.wrong_key)).status_code, 403)
        self.assertEqual(
            self.client.get("/api/v1/analyses/missing", headers=self.headers()).status_code,
            404,
        )
        self.assertEqual(self.client.get(path + "?as_of=now", headers=self.headers()).status_code, 422)
        with patch.object(config, "API_ANALYSES_ENABLED", False):
            self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 503)

    def test_openapi_declares_analysis_scope(self):
        schema = self.client.get("/openapi.json").json()
        contract = schema["paths"]["/api/v1/analyses/{id}"]["get"]
        self.assertEqual(contract["x-required-scopes"], ["read:analyses"])


if __name__ == "__main__":
    unittest.main()
