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
                db, authenticated, resource="entities", filters={
                    "q": None, "type": None, "identifier_namespace": None,
                    "identifier_value": None, "exchange": None,
                },
                last_id="entity_a", dataset_epoch=epoch, now=1000,
            )
            with self.assertRaises(CursorFilterMismatch):
                decode_cursor(
                    db, authenticated, token, resource="entities",
                    filters={
                        "q": "Alpha", "type": None, "identifier_namespace": None,
                        "identifier_value": None, "exchange": None,
                    }, dataset_epoch=epoch, now=1001,
                )
            with self.assertRaises(CursorEpochChanged):
                decode_cursor(
                    db, authenticated, token, resource="entities",
                    filters={
                        "q": None, "type": None, "identifier_namespace": None,
                        "identifier_value": None, "exchange": None,
                    }, dataset_epoch="different", now=1001,
                )
            with self.assertRaises(CursorExpired):
                decode_cursor(
                    db, authenticated, token, resource="entities",
                    filters={
                        "q": None, "type": None, "identifier_namespace": None,
                        "identifier_value": None, "exchange": None,
                    }, dataset_epoch=epoch, now=2000,
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
        self.assertEqual(self.client.get(
            "/api/v1/entities", params={"q": "x" * 200}, headers=self.headers()
        ).status_code, 200)
        too_long = self.client.get(
            "/api/v1/entities", params={"q": "x" * 201}, headers=self.headers()
        )
        self.assertEqual(too_long.status_code, 422)
        self.assertEqual(too_long.json()["error"]["code"], "invalid_parameter")
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

    def test_exact_identifier_lookup_returns_candidates_and_binds_cursor(self):
        with database.get_db(self.path) as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            for entity_id, exchange in (("entity_d", "XNAS"), ("entity_e", "XNYS")):
                db.execute(
                    """INSERT INTO entities(id,dataset_id,type,status,created_at)
                       VALUES(?,?,'security','active','2026-09-22T10:00:00.000000Z')""",
                    (entity_id, dataset_id),
                )
                db.execute(
                    """INSERT INTO entity_versions(
                           id,entity_id,version,type,canonical_name,status,attributes_json,
                           version_sha256,available_at,created_by)
                       VALUES(?,?,1,'security',?,'active','{}',?,
                              '2026-09-22T10:00:00.000000Z','test')""",
                    (entity_id + "_v1", entity_id, entity_id.upper(), entity_id[-1] * 64),
                )
                db.execute(
                    "UPDATE entities SET current_version_id=? WHERE id=?",
                    (entity_id + "_v1", entity_id),
                )
                db.execute(
                    """INSERT INTO entity_identifiers(
                           id,entity_id,namespace,value,qualifier_json,verification_status,
                           assertion_sha256,available_at)
                       VALUES(?,?, 'exchange_ticker','DUAL',?,'verified',?,
                              '2026-09-22T10:00:00.000000Z')""",
                    (
                        "identifier_" + entity_id, entity_id,
                        '{"exchange":"' + exchange + '"}', entity_id[-1] * 64,
                    ),
                )
            db.execute(
                """INSERT INTO entity_identifiers(
                       id,entity_id,namespace,value,qualifier_json,verification_status,
                       assertion_sha256,available_at)
                   VALUES('identifier_b_exchange','entity_b','exchange_ticker','DUAL',
                          '{"exchange":"XNAS"}','verified',?,
                          '2026-09-22T10:00:00.000000Z')""",
                ("9" * 64,),
            )
            db.execute(
                """INSERT INTO entity_identifiers(
                       id,entity_id,namespace,value,verification_status,
                       assertion_sha256,available_at)
                   VALUES('identifier_a_unverified','entity_a','cik','0000000001',
                          'legacy_unverified',?,'2026-09-22T10:00:00.000000Z')""",
                ("8" * 64,),
            )

        ticker = self.client.get(
            "/api/v1/entities",
            params={"identifier_namespace": "ticker", "identifier_value": "beta"},
            headers=self.headers(),
        )
        self.assertEqual(ticker.status_code, 200)
        self.assertEqual([row["id"] for row in ticker.json()["data"]], ["entity_b"])

        candidates = self.client.get(
            "/api/v1/entities",
            params={
                "identifier_namespace": "exchange_ticker",
                "identifier_value": "dual", "exchange": "xnas", "limit": 1,
            },
            headers=self.headers(),
        )
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual([row["id"] for row in candidates.json()["data"]], ["entity_b"])
        cursor = candidates.json()["pagination"]["next_cursor"]
        self.assertIsNotNone(cursor)
        next_candidate = self.client.get(
            "/api/v1/entities",
            params={
                "identifier_namespace": "exchange_ticker",
                "identifier_value": "DUAL", "exchange": "XNAS", "limit": 1,
                "cursor": cursor,
            },
            headers=self.headers(),
        )
        self.assertEqual(
            [row["id"] for row in next_candidate.json()["data"]], ["entity_d"]
        )
        wrong_exchange = self.client.get(
            "/api/v1/entities",
            params={
                "identifier_namespace": "exchange_ticker",
                "identifier_value": "DUAL", "exchange": "XNYS", "limit": 1,
                "cursor": cursor,
            },
            headers=self.headers(),
        )
        self.assertEqual(wrong_exchange.status_code, 400)
        self.assertEqual(wrong_exchange.json()["error"]["code"], "filter_mismatch")
        unverified = self.client.get(
            "/api/v1/entities",
            params={"identifier_namespace": "cik", "identifier_value": "0000000001"},
            headers=self.headers(),
        )
        self.assertEqual(unverified.status_code, 200)
        self.assertEqual(unverified.json()["data"], [])

        invalid = (
            {"identifier_namespace": "ticker"},
            {"identifier_value": "BETA"},
            {"exchange": "XNAS"},
            {"identifier_namespace": "ticker", "identifier_value": "BETA", "exchange": "XNAS"},
            {"identifier_namespace": "exchange_ticker", "identifier_value": "DUAL"},
        )
        for params in invalid:
            with self.subTest(params=params):
                response = self.client.get(
                    "/api/v1/entities", params=params, headers=self.headers()
                )
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

    def test_entity_detail_current_exact_version_and_logical_as_of(self):
        with database.get_db(self.path) as db:
            db.execute(
                """INSERT INTO entity_versions(
                       id,entity_id,version,previous_version_id,type,canonical_name,status,
                       attributes_json,version_sha256,available_at,created_by)
                   VALUES('entity_a_v2','entity_a',2,'entity_a_v1','organization',
                          'Alpha Group','active','{}',?,'2026-09-23T10:00:00.000000Z','test')""",
                ("d" * 64,),
            )
            db.execute(
                "UPDATE entities SET current_version_id='entity_a_v2' WHERE id='entity_a'"
            )
            db.execute(
                """INSERT INTO entity_aliases(
                       id,entity_id,alias,alias_key,match_mode,ambiguity,status,
                       assertion_sha256,available_at)
                   VALUES('alias_a_late','entity_a','Alpha Group','alpha group','exact',
                          'unique','active',?,'2026-09-23T10:00:00.000000Z')""",
                ("e" * 64,),
            )

        current = self.client.get("/api/v1/entities/entity_a", headers=self.headers())
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["data"]["version_id"], "entity_a_v2")
        self.assertEqual(current.json()["data"]["canonical_name"], "Alpha Group")
        self.assertEqual(current.json()["data"]["aliases"], ["Alpha", "Alpha Group"])
        self.assertIsNone(current.json()["knowledge_cutoff"])
        self.assertTrue(current.headers["ETag"].startswith('"'))

        exact = self.client.get(
            "/api/v1/entities/entity_a?version_id=entity_a_v1", headers=self.headers()
        )
        self.assertEqual(exact.status_code, 200)
        self.assertEqual(exact.json()["data"]["canonical_name"], "Alpha Holdings")
        self.assertEqual(exact.json()["data"]["aliases"], ["Alpha"])
        self.assertIsNone(exact.json()["knowledge_cutoff"])

        historical = self.client.get(
            "/api/v1/entities/entity_a?as_of=2026-09-22T12:00:00%2B00:00",
            headers=self.headers(),
        )
        self.assertEqual(historical.status_code, 200)
        self.assertEqual(historical.json()["data"]["version_id"], "entity_a_v1")
        self.assertEqual(historical.json()["data"]["aliases"], ["Alpha"])
        self.assertEqual(historical.json()["knowledge_cutoff"], {
            "basis": "logical_as_of",
            "as_of": "2026-09-22T12:00:00.000000Z",
            "checkpoint_id": None,
            "dataset_epoch": historical.json()["dataset_epoch"],
            "high_water": None,
            "observed_at": None,
            "clock_status": "unknown",
        })
        current_relation = self.client.get(
            "/api/v1/entities/entity_b", headers=self.headers()
        )
        historical_relation = self.client.get(
            "/api/v1/entities/entity_b?as_of=2026-09-22T12:00:00Z",
            headers=self.headers(),
        )
        self.assertEqual(
            current_relation.json()["data"]["relations"][0]["target"]["version_id"],
            "entity_a_v2",
        )
        self.assertEqual(
            historical_relation.json()["data"]["relations"][0]["target"]["version_id"],
            "entity_a_v1",
        )

    def test_entity_detail_etag_is_permission_aware(self):
        first = self.client.get("/api/v1/entities/entity_b", headers=self.headers())
        self.assertEqual(first.status_code, 200)
        not_modified = self.client.get(
            "/api/v1/entities/entity_b",
            headers={**self.headers(), "If-None-Match": first.headers["ETag"]},
        )
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified.content, b"")
        self.assertEqual(not_modified.headers["ETag"], first.headers["ETag"])
        weak_not_modified = self.client.get(
            "/api/v1/entities/entity_b",
            headers={**self.headers(), "If-None-Match": f'W/{first.headers["ETag"]}'},
        )
        self.assertEqual(weak_not_modified.status_code, 304)
        self.assertEqual(weak_not_modified.headers["ETag"], first.headers["ETag"])
        other = self.client.get(
            "/api/v1/entities/entity_b", headers=self.headers(self.other_key)
        )
        self.assertEqual(other.status_code, 200)
        self.assertNotEqual(other.headers["ETag"], first.headers["ETag"])

    def test_entity_detail_rejects_unsupported_history_and_bad_parameters(self):
        cases = (
            ("/api/v1/entities/entity_a?version_id=entity_a_v1&as_of=2026-09-22T12:00:00Z",
             422, "invalid_parameter"),
            ("/api/v1/entities/entity_a?as_of=2026-09-22T12:00:00",
             422, "invalid_parameter"),
            ("/api/v1/entities/entity_a?knowledge_checkpoint_id=checkpoint_1",
             422, "unsupported_history"),
            ("/api/v1/entities/entity_a?unknown=1", 422, "invalid_parameter"),
            ("/api/v1/entities/entity_a?version_id=entity_a_v1&version_id=entity_a_v1",
             422, "invalid_parameter"),
            ("/api/v1/entities/entity_a?version_id=entity_b_v1", 404, "resource_not_found"),
            ("/api/v1/entities/entity_a?as_of=2026-09-21T00:00:00Z",
             404, "resource_not_found"),
            ("/api/v1/entities/missing", 404, "resource_not_found"),
        )
        for path, status, code in cases:
            with self.subTest(path=path):
                result = self.client.get(path, headers=self.headers())
                self.assertEqual(result.status_code, status)
                self.assertEqual(result.json()["error"]["code"], code)

    def test_entity_detail_restricted_and_merged_fail_closed(self):
        with database.get_db(self.path) as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            for entity_id, status in (("entity_r", "restricted"), ("entity_m", "merged")):
                db.execute(
                    """INSERT INTO entities(id,dataset_id,type,status,created_at)
                       VALUES(?,?,'organization',?,'2026-09-23T10:00:00.000000Z')""",
                    (entity_id, dataset_id, status),
                )
                db.execute(
                    """INSERT INTO entity_versions(
                           id,entity_id,version,type,canonical_name,status,attributes_json,
                           version_sha256,available_at,created_by)
                       VALUES(?,?,1,'organization',?,?, '{}',?,
                              '2026-09-23T10:00:00.000000Z','test')""",
                    (entity_id + "_v1", entity_id, entity_id, status, "f" * 64),
                )
                db.execute(
                    "UPDATE entities SET current_version_id=? WHERE id=?",
                    (entity_id + "_v1", entity_id),
                )
        restricted = self.client.get("/api/v1/entities/entity_r", headers=self.headers())
        self.assertEqual(restricted.status_code, 403)
        self.assertEqual(restricted.json()["error"]["code"], "restricted_content")
        merged = self.client.get("/api/v1/entities/entity_m", headers=self.headers())
        self.assertEqual(merged.status_code, 503)
        self.assertEqual(merged.json()["error"]["code"], "not_ready")

    def test_runtime_openapi_declares_entity_contract(self):
        schema = app.openapi()
        listing = schema["paths"]["/api/v1/entities"]["get"]
        detail = schema["paths"]["/api/v1/entities/{id}"]["get"]
        self.assertEqual(listing["x-required-scopes"], ["read:catalog"])
        self.assertEqual(detail["x-required-scopes"], ["read:catalog"])
        self.assertEqual(
            {parameter["name"] for parameter in listing["parameters"]},
            {
                "limit", "cursor", "q", "type", "identifier_namespace",
                "identifier_value", "exchange",
            },
        )
        self.assertEqual(
            {parameter["name"] for parameter in detail["parameters"]},
            {"id", "version_id", "as_of", "knowledge_checkpoint_id", "If-None-Match"},
        )
        self.assertEqual(
            detail["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/EntityResponse"},
        )


if __name__ == "__main__":
    unittest.main()
