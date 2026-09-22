import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database, db_admin
from app.api_auth import create_consumer, issue_api_key
from app.api_cursor import (
    CursorEpochChanged,
    CursorExpired,
    CursorFilterMismatch,
    decode_cursor,
    encode_cursor,
)
from app.web.routes import app


class ApiCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "catalog.db"
        db_admin.migrate_database(self.path)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            self.consumer_id = create_consumer(db, "catalog-test", actor="test")
            self.key = issue_api_key(
                db, self.consumer_id, {"read:catalog"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            second_consumer = create_consumer(db, "catalog-other", actor="test")
            self.other_key = issue_api_key(
                db, second_consumer, {"read:catalog"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            for entity_id, entity_type, name, status in (
                ("entity_a", "organization", "Alpha Holdings", "active"),
                ("entity_b", "security", "Beta Shares", "active"),
                ("entity_c", "person", "Casey Example", "inactive"),
            ):
                version_id = entity_id + "_v1"
                db.execute(
                    """INSERT INTO entities(id,dataset_id,type,status,created_at)
                       VALUES(?,?,?,?, '2026-09-22T10:00:00.000000Z')""",
                    (entity_id, dataset_id, entity_type, status),
                )
                db.execute(
                    """INSERT INTO entity_versions(
                           id,entity_id,version,type,canonical_name,status,attributes_json,
                           version_sha256,available_at,created_by)
                       VALUES(?,?,1,?,?,?,'{}',?, '2026-09-22T10:00:00.000000Z','test')""",
                    (version_id, entity_id, entity_type, name, status, "a" * 64),
                )
                db.execute(
                    "UPDATE entities SET current_version_id=? WHERE id=?",
                    (version_id, entity_id),
                )
            db.execute(
                """INSERT INTO entity_aliases(
                       id,entity_id,alias,alias_key,match_mode,ambiguity,status,
                       assertion_sha256,available_at)
                   VALUES('alias_a','entity_a','Alpha','alpha','exact','unique','active',?,
                          '2026-09-22T10:00:00.000000Z')""",
                ("b" * 64,),
            )
            db.execute(
                """INSERT INTO entity_identifiers(
                       id,entity_id,namespace,value,qualifier_json,verification_status,
                       assertion_sha256,available_at)
                   VALUES('identifier_b','entity_b','ticker','BETA','{"exchange":"XNAS"}',
                          'verified',?,'2026-09-22T10:00:00.000000Z')""",
                ("c" * 64,),
            )
            db.execute(
                """INSERT INTO entity_relations(
                       id,from_entity_id,to_entity_id,relation,verification_status,
                       available_at)
                   VALUES('relation_ba','entity_b','entity_a','issues','verified',
                          '2026-09-22T10:00:00.000000Z')"""
            )
        self.db_patch = patch("app.web.routes.get_db", lambda: database.get_db(self.path))
        self.auth_db_patch = patch(
            "app.web.v1_auth.get_db", lambda: database.get_db(self.path)
        )
        self.flag_patch = patch("app.config.API_CATALOG_ENABLED", True)
        self.db_patch.start()
        self.auth_db_patch.start()
        self.flag_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.auth_db_patch.stop)
        self.addCleanup(self.flag_patch.stop)
        self.client = TestClient(app)

    def headers(self, key=None):
        selected = key or self.key
        return {"Authorization": f"Bearer {selected.token}"}

    def test_entity_list_is_typed_and_stably_paginated(self):
        first = self.client.get("/api/v1/entities?limit=2", headers=self.headers())
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["api_version"], "v1")
        self.assertEqual(body["schema_version"], "1.0.0")
        self.assertEqual(body["request_id"], first.headers["X-Request-ID"])
        self.assertIsNone(body["knowledge_cutoff"])
        self.assertEqual([row["id"] for row in body["data"]], ["entity_a", "entity_b"])
        self.assertEqual(body["data"][0]["aliases"], ["Alpha"])
        self.assertEqual(body["data"][1]["identifiers"][0]["exchange"], "XNAS")
        self.assertEqual(body["data"][1]["relations"][0]["target"],
                         {"id": "entity_a", "version_id": "entity_a_v1"})
        cursor = body["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)

        second = self.client.get(
            "/api/v1/entities", params={"limit": 2, "cursor": cursor},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual([row["id"] for row in second.json()["data"]], ["entity_c"])
        self.assertEqual(second.json()["data"][0]["status"], "retired")
        self.assertIsNone(second.json()["pagination"]["next_cursor"])

    def test_cursor_is_bound_to_filters_key_epoch_and_expiry(self):
        with database.get_db(self.path) as db:
            principal = db.execute(
                "SELECT authz_version FROM api_consumers WHERE id=?", (self.consumer_id,)
            ).fetchone()[0]
            from app.api_auth import authenticate_api_key
            authenticated = authenticate_api_key(db, self.key.token)
            self.assertEqual(authenticated.authz_version, principal)
            epoch = db.execute(
                "SELECT current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            token = encode_cursor(
                db, authenticated, resource="entities", filters={"q": None, "type": None},
                last_id="entity_a", dataset_epoch=epoch, now=1000,
            )
            with self.assertRaises(CursorFilterMismatch):
                decode_cursor(
                    db, authenticated, token, resource="entities",
                    filters={"q": "Alpha", "type": None}, dataset_epoch=epoch, now=1001,
                )
            with self.assertRaises(CursorEpochChanged):
                decode_cursor(
                    db, authenticated, token, resource="entities",
                    filters={"q": None, "type": None}, dataset_epoch="different", now=1001,
                )
            with self.assertRaises(CursorExpired):
                decode_cursor(
                    db, authenticated, token, resource="entities",
                    filters={"q": None, "type": None}, dataset_epoch=epoch, now=2000,
                )

        live_cursor = self.client.get(
            "/api/v1/entities?limit=1", headers=self.headers()
        ).json()["pagination"]["next_cursor"]
        wrong_key = self.client.get(
            "/api/v1/entities", params={"limit": 1, "cursor": live_cursor},
            headers=self.headers(self.other_key),
        )
        self.assertEqual(wrong_key.status_code, 400)
        self.assertEqual(wrong_key.json()["error"]["code"], "invalid_cursor")

    def test_filters_and_query_validation_are_fail_closed(self):
        filtered = self.client.get(
            "/api/v1/entities", params={"q": "Alpha", "type": "organization"},
            headers=self.headers(),
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([row["id"] for row in filtered.json()["data"]], ["entity_a"])
        for path in (
            "/api/v1/entities?unknown=1",
            "/api/v1/entities?limit=01",
            "/api/v1/entities?limit=1&limit=2",
            "/api/v1/entities?type=company",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "invalid_parameter")

    def test_default_flag_and_unsupported_identity_keep_endpoint_unavailable(self):
        with patch("app.config.API_CATALOG_ENABLED", False):
            disabled = self.client.get("/api/v1/entities", headers=self.headers())
        self.assertEqual(disabled.status_code, 503)
        self.assertEqual(disabled.json()["error"]["code"], "not_ready")

        with database.get_db(self.path) as db:
            db.execute("UPDATE entities SET status='restricted' WHERE id='entity_c'")
        unavailable = self.client.get("/api/v1/entities", headers=self.headers())
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["error"]["code"], "not_ready")


if __name__ == "__main__":
    unittest.main()
