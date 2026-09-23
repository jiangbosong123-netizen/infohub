import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, db_admin
from app.api_auth import create_consumer, issue_api_key
from app.topic_statistics import advance_topic_statistics
from app.web.routes import app


NOW = "2026-09-23T17:00:00.000000Z"


class ApiTopicTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "topics.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        db_admin.migrate_database(self.path)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "topic-test", actor="test")
            self.key = issue_api_key(
                db, consumer, {"read:catalog"}, expires_at=now + timedelta(days=1),
                actor="test",
            )
            wrong = create_consumer(db, "topic-wrong", actor="test")
            self.wrong_key = issue_api_key(
                db, wrong, {"read:events"}, expires_at=now + timedelta(days=1),
                actor="test",
            )
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            self._topic(db, dataset, "topic-a", "tv-a", "alpha", "technology")
            self._topic(db, dataset, "topic-b", "tv-b", "beta", "macro")
            self._topic(db, dataset, "topic-c", "tv-c", "gamma", "technology")
        advance_topic_statistics(25)
        advance_topic_statistics(25)
        self.db_patch = patch("app.web.routes.get_db", lambda: database.get_db(self.path))
        self.auth_patch = patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path))
        self.flag_patch = patch("app.config.API_CATALOG_ENABLED", True)
        for mocked in (self.db_patch, self.auth_patch, self.flag_patch):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app)

    def _topic(self, db, dataset, topic_id, version_id, slug, group):
        db.execute(
            "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES(?,?,'active',?)",
            (topic_id, dataset, NOW),
        )
        db.execute(
            """INSERT INTO topic_versions(
                   id,topic_id,version,slug,name,group_key,description,rules_json,
                   rules_hash,version_sha256,status,available_at)
               VALUES(?,?,1,?,?,?,'','{}',?,?,'active',?)""",
            (version_id, topic_id, slug, slug.title(), group, "a" * 64, "b" * 64, NOW),
        )
        db.execute(
            "UPDATE topic_catalog SET current_version_id=? WHERE id=?", (version_id, topic_id)
        )

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def test_list_is_publication_bound_typed_and_paginated(self):
        first = self.client.get(
            "/api/v1/topics?limit=1&group=technology", headers=self.headers()
        )
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["api_version"], "v1")
        self.assertEqual(body["pagination"]["consistency"], "publication")
        self.assertEqual(body["publication"]["version"], 1)
        self.assertTrue(body["publication"]["count_policy"]["unreviewed_assignments_excluded"])
        self.assertEqual(body["data"][0]["id"], "topic-a")
        self.assertEqual((body["data"][0]["document_count"], body["data"][0]["event_count"]), (0, 0))
        second = self.client.get(
            "/api/v1/topics",
            params={"limit": 1, "group": "technology", "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual([row["id"] for row in second.json()["data"]], ["topic-c"])
        self.assertIsNone(second.json()["pagination"]["next_cursor"])

    def test_detail_etag_not_found_and_dirty_fail_closed(self):
        detail = self.client.get("/api/v1/topics/topic-a", headers=self.headers())
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["version_id"], "tv-a")
        self.assertEqual(
            self.client.get(
                "/api/v1/topics/topic-a",
                headers={**self.headers(), "If-None-Match": detail.headers["etag"]},
            ).status_code,
            304,
        )
        missing = self.client.get("/api/v1/topics/missing", headers=self.headers())
        self.assertEqual((missing.status_code, missing.json()["error"]["code"]),
                         (404, "resource_not_found"))
        with database.get_db(self.path) as db:
            db.execute(
                "INSERT INTO topic_statistics_dirty(topic_id,reason,queued_at) VALUES('topic-a','fixture',?)",
                (NOW,),
            )
        unavailable = self.client.get("/api/v1/topics", headers=self.headers())
        self.assertEqual((unavailable.status_code, unavailable.json()["error"]["code"]),
                         (503, "not_ready"))

    def test_cursor_cannot_cross_a_new_publication(self):
        first = self.client.get("/api/v1/topics?limit=1", headers=self.headers())
        cursor = first.json()["pagination"]["next_cursor"]
        with database.get_db(self.path) as db:
            dataset = db.execute("SELECT dataset_id FROM dataset_state").fetchone()[0]
            self._topic(db, dataset, "topic-d", "tv-d", "delta", "research")
        advance_topic_statistics(25)
        advance_topic_statistics(25)
        stale = self.client.get(
            "/api/v1/topics", params={"limit": 1, "cursor": cursor},
            headers=self.headers(),
        )
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]),
                         (400, "filter_mismatch"))

    def test_auth_flag_and_parameters_are_enforced(self):
        self.assertEqual(self.client.get("/api/v1/topics").status_code, 401)
        denied = self.client.get("/api/v1/topics", headers=self.headers(self.wrong_key))
        self.assertEqual((denied.status_code, denied.json()["error"]["code"]),
                         (403, "insufficient_scope"))
        for url in (
            "/api/v1/topics?unknown=1", "/api/v1/topics?limit=01",
            "/api/v1/topics?limit=1&limit=2", "/api/v1/topics?group=unknown",
            "/api/v1/topics/topic-a?version=tv-a",
        ):
            with self.subTest(url=url):
                response = self.client.get(url, headers=self.headers())
                self.assertEqual((response.status_code, response.json()["error"]["code"]),
                                 (422, "invalid_parameter"))
        with patch("app.config.API_CATALOG_ENABLED", False):
            disabled = self.client.get("/api/v1/topics", headers=self.headers())
        self.assertEqual((disabled.status_code, disabled.json()["error"]["code"]),
                         (503, "not_ready"))

    def test_openapi_declares_topic_contract(self):
        schema = app.openapi()
        listing = schema["paths"]["/api/v1/topics"]["get"]
        detail = schema["paths"]["/api/v1/topics/{id}"]["get"]
        self.assertEqual(listing["x-required-scopes"], ["read:catalog"])
        self.assertEqual(detail["x-required-scopes"], ["read:catalog"])
        self.assertEqual(
            {parameter["name"] for parameter in listing["parameters"]},
            {"limit", "cursor", "group"},
        )
        self.assertEqual(
            detail["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/TopicResponse"},
        )


if __name__ == "__main__":
    unittest.main()
