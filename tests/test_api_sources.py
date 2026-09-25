import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, db_admin
from app.api_auth import create_consumer, issue_api_key
from app.web.routes import app


NOW = "2026-09-25T12:00:00.000000Z"
LATER = "2026-09-25T12:01:00.000000Z"


class ApiSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "sources.db"
        db_admin.migrate_database(self.path)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "source-test", actor="test")
            self.key = issue_api_key(
                db, consumer, {"read:catalog"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            wrong = create_consumer(db, "source-wrong", actor="test")
            self.wrong_key = issue_api_key(
                db, wrong, {"read:items"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO companies(id,slug,name,market) VALUES(1,'acme','Acme','US')"""
            )
            db.execute(
                """INSERT INTO entities(id,dataset_id,type,status,created_at)
                   VALUES('entity-acme',?,'organization','active',?)""",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO entity_versions(
                       id,entity_id,version,type,canonical_name,status,attributes_json,
                       version_sha256,available_at,created_by)
                   VALUES('entity-acme-v1','entity-acme',1,'organization','Acme','active',
                          '{}',?,?, 'test')""",
                ("a" * 64, NOW),
            )
            db.execute(
                "UPDATE entities SET current_version_id='entity-acme-v1' WHERE id='entity-acme'"
            )
            db.execute(
                """INSERT INTO legacy_company_entities(
                       company_id,entity_id,legacy_sha256,available_at)
                   VALUES(1,'entity-acme',?,?)""",
                ("b" * 64, NOW),
            )
            self._source(db, 1, "alpha-feed", "Mutable Legacy Name", "robot", "info", "rss",
                         "https://alpha.example/feed?token=[redacted]", "", 30, True)
            self._source(db, 2, "beta-sec", "Beta SEC", "stock", "official", "sec",
                         "https://data.example/submissions", "acme", 10, True)
            self._source(db, 3, "retired-feed", "Retired", "robot", "info", "rss",
                         "https://retired.example/feed", "", 60, False)
            self._config(db, 1, 1, "alpha-feed", "Alpha Old", "ai", "media", "rss",
                         "https://old.example/feed", "", 60, NOW)
            self._config(db, 1, 2, "alpha-feed", "Alpha Feed", "ai", "media", "rss",
                         "https://alpha.example/feed?token=[redacted]", "", 30, LATER)
            self._config(db, 2, 1, "beta-sec", "Beta SEC", "stock", "official", "sec",
                         "https://data.example/submissions", "acme", 10, NOW)
            self._config(db, 3, 1, "retired-feed", "Retired", "robot", "info", "rss",
                         "https://retired.example/feed", "", 60, NOW)
        patches = (
            patch("app.web.routes.get_db", lambda: database.get_db(self.path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path)),
            patch.object(config, "API_CATALOG_ENABLED", True),
        )
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app)

    def _source(self, db, source_id, key, name, channel, tier, kind, url,
                company_slug, interval, enabled):
        db.execute(
            """INSERT INTO sources(
                   id,key,name,channel,tier,type,url,company_slug,enabled,interval_minutes)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (source_id, key, name, channel, tier, kind, url, company_slug,
             int(enabled), interval),
        )

    def _config(self, db, source_id, version, key, name, channel, tier, kind,
                url, company_slug, interval, available_at):
        value = {
            "channel": channel, "company_slug": company_slug,
            "interval_minutes": interval, "key": key, "name": name,
            "tier": tier, "type": kind, "url": url,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        db.execute(
            """INSERT INTO source_config_versions(
                   id,source_id,version,config_json,config_hash,available_at)
               VALUES(?,?,?,?,?,?)""",
            (f"source-config-{source_id}-{version}", source_id, version,
             encoded, digest, available_at),
        )

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def test_list_uses_latest_frozen_config_and_stable_pagination(self):
        first = self.client.get("/api/v1/sources?limit=1", headers=self.headers())
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["schema_version"], "1.0.0")
        self.assertEqual(body["request_id"], first.headers["X-Request-ID"])
        self.assertIsNone(body["knowledge_cutoff"])
        source = body["data"][0]
        self.assertEqual(source["id"], "alpha-feed")
        self.assertEqual(source["name"], "Alpha Feed")
        self.assertEqual(source["origin_host"], "alpha.example")
        self.assertNotIn("token", json.dumps(source))
        self.assertEqual(source["collection_interval_seconds"], 1800)
        self.assertEqual(source["configuration"]["version"], 2)
        cursor = body["pagination"]["next_cursor"]
        second = self.client.get(
            "/api/v1/sources", params={"limit": 1, "cursor": cursor},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual([row["id"] for row in second.json()["data"]], ["beta-sec"])
        self.assertEqual(
            second.json()["data"][0]["collection_subject_entity_id"], "entity-acme"
        )
        self.assertIsNone(second.json()["pagination"]["next_cursor"])

    def test_filters_are_cursor_bound_and_validation_is_strict(self):
        filtered = self.client.get(
            "/api/v1/sources?channel=stock&tier=official&q=Beta",
            headers=self.headers(),
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([row["id"] for row in filtered.json()["data"]], ["beta-sec"])
        cursor = self.client.get(
            "/api/v1/sources?limit=1", headers=self.headers()
        ).json()["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)
        mismatch = self.client.get(
            "/api/v1/sources", params={"limit": 1, "channel": "stock", "cursor": cursor},
            headers=self.headers(),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["error"]["code"], "filter_mismatch")
        for path in (
            "/api/v1/sources?unknown=1",
            "/api/v1/sources?limit=01",
            "/api/v1/sources?channel=finance",
            "/api/v1/sources?tier=unknown",
            "/api/v1/sources?q=" + "x" * 201,
            "/api/v1/sources?channel=ai&channel=stock",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "invalid_parameter")

    def test_detail_etag_scope_and_active_only_boundary(self):
        detail = self.client.get("/api/v1/sources/beta-sec", headers=self.headers())
        self.assertEqual(detail.status_code, 200)
        etag = detail.headers["etag"]
        cached = self.client.get(
            "/api/v1/sources/beta-sec",
            headers={**self.headers(), "If-None-Match": etag},
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.headers["etag"], etag)
        self.assertEqual(
            self.client.get("/api/v1/sources/retired-feed", headers=self.headers()).status_code,
            404,
        )
        under_scoped = self.client.get(
            "/api/v1/sources", headers=self.headers(self.wrong_key)
        )
        self.assertEqual(under_scoped.status_code, 403)
        self.assertEqual(under_scoped.json()["error"]["code"], "insufficient_scope")

    def test_missing_or_invalid_active_snapshot_fails_closed(self):
        with database.get_db(self.path) as db:
            self._source(db, 4, "missing", "Missing", "ai", "info", "rss",
                         "https://missing.example/feed", "", 30, True)
        unavailable = self.client.get("/api/v1/sources", headers=self.headers())
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["error"]["code"], "not_ready")
        with database.get_db(self.path) as db:
            db.execute("UPDATE sources SET enabled=0 WHERE key='missing'")
            self._source(db, 5, "invalid", "Invalid", "ai", "info", "rss",
                         "https://invalid.example/feed", "", 30, True)
            db.execute(
                """INSERT INTO source_config_versions(
                       id,source_id,version,config_json,config_hash,available_at)
                   VALUES('source-config-invalid',5,1,'{}',?,?)""",
                ("f" * 64, NOW),
            )
        invalid = self.client.get("/api/v1/sources/invalid", headers=self.headers())
        self.assertEqual(invalid.status_code, 503)
        self.assertEqual(invalid.json()["error"]["code"], "not_ready")


if __name__ == "__main__":
    unittest.main()
