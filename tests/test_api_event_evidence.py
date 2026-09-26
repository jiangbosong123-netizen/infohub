import unittest
from unittest.mock import patch

from app import config, database
from app.event_admission import record_admission_review
from tests.test_api_events import ApiEventTests, NOW


class ApiEventEvidenceTests(unittest.TestCase):
    def setUp(self):
        ApiEventTests.setUp(self)

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.evidence_key).token}"}

    def test_release_bound_evidence_is_typed_filtered_and_paginated(self):
        first = self.client.get(
            "/api/v1/events/event-2/evidence", params={"limit": 1},
            headers=self.headers(),
        )
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["schema_version"], "1.0.0")
        self.assertEqual(body["pagination"]["consistency"], "release")
        self.assertEqual(body["event"]["version_id"], "event-version-2")
        self.assertEqual(body["release"]["policy_version"], "event-release-v1")
        evidence = body["data"][0]
        self.assertEqual(evidence["document"]["version_id"], self.document_version_id)
        self.assertEqual(evidence["raw_record"]["id"], self.raw_record_id)
        self.assertEqual(len(evidence["raw_record"]["payload_sha256"]), 64)
        self.assertNotIn("payload_ref", evidence["raw_record"])
        self.assertNotIn("request_url", evidence["raw_record"])
        second = self.client.get(
            "/api/v1/events/event-2/evidence",
            params={"limit": 1, "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["data"][0]["id"], "event-evidence-3")
        filtered = self.client.get(
            "/api/v1/events/event-2/evidence", params={"role": "context"},
            headers=self.headers(),
        )
        self.assertEqual(
            [item["id"] for item in filtered.json()["data"]], ["event-evidence-3"]
        )

    def test_scope_not_found_parameters_switch_and_stale_release_fail_closed(self):
        path = "/api/v1/events/event-2/evidence"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.headers(self.key)).status_code, 403)
        self.assertEqual(
            self.client.get("/api/v1/events/missing/evidence", headers=self.headers()).status_code,
            404,
        )
        for invalid in (
            f"{path}?unknown=1", f"{path}?limit=01", f"{path}?limit=1&limit=2",
            f"{path}?role=unknown", f"{path}?fact_id=",
        ):
            self.assertEqual(self.client.get(invalid, headers=self.headers()).status_code, 422)
        with patch.object(config, "API_EVENTS_ENABLED", False):
            self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 503)
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
        stale = self.client.get(path, headers=self.headers())
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (503, "not_ready"))

    def test_openapi_declares_evidence_scope(self):
        schema = self.client.get("/openapi.json").json()
        contract = schema["paths"]["/api/v1/events/{id}/evidence"]["get"]
        self.assertEqual(contract["x-required-scopes"], ["read:evidence"])


if __name__ == "__main__":
    unittest.main()
