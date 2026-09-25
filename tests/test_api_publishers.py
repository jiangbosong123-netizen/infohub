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


def digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ApiPublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "publishers.db"
        db_admin.migrate_database(self.path)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "publisher-test", actor="test")
            self.key = issue_api_key(
                db, consumer, {"read:catalog"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            wrong = create_consumer(db, "publisher-wrong", actor="test")
            self.wrong_key = issue_api_key(
                db, wrong, {"read:items"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            self._organization(db, dataset)
            self._publisher(db, dataset, "publisher-a", "Alpha News", "active")
            self._publisher(
                db, dataset, "publisher-b", "Beta Wire", "active", "org-beta"
            )
            self._publisher(db, dataset, "publisher-c", "Former Press", "inactive")
            self._name(db, "publisher-a", "Alpha News")
            self._name(db, "publisher-a", "Alpha")
            self._name(db, "publisher-a", "Old Alpha", status="deprecated")
            self._domain(db, "publisher-a", "alpha.example", "verified", "evidence-a")
            self._domain(db, "publisher-a", "legacy.example", "legacy_unverified")
            self._domain(db, "publisher-a", "candidate.example", "candidate")
        patches = (
            patch("app.web.routes.get_db", lambda: database.get_db(self.path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path)),
            patch.object(config, "API_CATALOG_ENABLED", True),
        )
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app)

    def _organization(self, db, dataset):
        db.execute(
            """INSERT INTO entities(id,dataset_id,type,status,created_at)
               VALUES('org-beta',?,'organization','active',?)""",
            (dataset, NOW),
        )
        payload = {
            "type": "organization", "canonical_name": "Beta Group",
            "status": "active", "attributes": {},
        }
        db.execute(
            """INSERT INTO entity_versions(
                   id,entity_id,version,type,canonical_name,status,attributes_json,
                   version_sha256,available_at,created_by)
               VALUES('org-beta-v1','org-beta',1,'organization','Beta Group','active',
                      '{}',?,?, 'test')""",
            (digest(payload), NOW),
        )
        db.execute(
            "UPDATE entities SET current_version_id='org-beta-v1' WHERE id='org-beta'"
        )

    def _publisher(self, db, dataset, publisher_id, name, status, entity_id=None):
        db.execute(
            """INSERT INTO publishers(
                   id,dataset_id,organization_entity_id,status,created_at)
               VALUES(?,?,?,?,?)""",
            (publisher_id, dataset, entity_id, status, NOW),
        )
        version_id = f"{publisher_id}-v1"
        db.execute(
            """INSERT INTO publisher_versions(
                   id,publisher_id,version,name,status,version_sha256,available_at)
               VALUES(?,?,1,?,?,?,?)""",
            (version_id, publisher_id, name, status,
             digest({"name": name, "status": status}), NOW),
        )
        db.execute(
            "UPDATE publishers SET current_version_id=? WHERE id=?",
            (version_id, publisher_id),
        )

    def _name(self, db, publisher_id, name, status="active"):
        assertion = digest({"name": name, "language": "und", "status": status})
        db.execute(
            """INSERT INTO publisher_names(
                   id,publisher_id,name,name_key,status,assertion_sha256,available_at)
               VALUES(?,?,?,?,?,?,?)""",
            (f"name-{assertion[:12]}", publisher_id, name, name.casefold(), status,
             assertion, NOW),
        )

    def _replace_version(self, db, publisher_id, name, status, version_hash=None):
        version_id = f"{publisher_id}-v2"
        db.execute(
            """INSERT INTO publisher_versions(
                   id,publisher_id,version,previous_version_id,name,status,
                   version_sha256,available_at)
               VALUES(?,?,2,?,?,?,?,?)""",
            (version_id, publisher_id, f"{publisher_id}-v1", name, status,
             version_hash or digest({"name": name, "status": status}), NOW),
        )
        db.execute(
            "UPDATE publishers SET current_version_id=?,status=? WHERE id=?",
            (version_id, status, publisher_id),
        )

    def _domain(self, db, publisher_id, domain, status, evidence_id=None):
        assertion = digest({
            "domain": domain, "valid_from": None, "valid_to": None,
            "evidence_id": evidence_id, "verification_status": status,
        })
        db.execute(
            """INSERT INTO publisher_domains(
                   id,publisher_id,domain,evidence_id,verification_status,
                   assertion_sha256,available_at)
               VALUES(?,?,?,?,?,?,?)""",
            (f"domain-{assertion[:12]}", publisher_id, domain, evidence_id, status,
             assertion, NOW),
        )

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def test_list_is_typed_paginated_and_verified_only(self):
        first = self.client.get("/api/v1/publishers?limit=1", headers=self.headers())
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["request_id"], first.headers["X-Request-ID"])
        self.assertEqual(body["schema_version"], "1.0.0")
        self.assertIsNone(body["knowledge_cutoff"])
        publisher = body["data"][0]
        self.assertEqual(publisher["id"], "publisher-a")
        self.assertEqual(publisher["aliases"], ["Alpha"])
        self.assertEqual(
            publisher["domains"],
            [{"domain": "alpha.example", "valid_from": None, "valid_to": None,
              "evidence_id": "evidence-a"}],
        )
        serialized = json.dumps(publisher)
        self.assertNotIn("legacy.example", serialized)
        self.assertNotIn("candidate.example", serialized)
        second = self.client.get(
            "/api/v1/publishers",
            params={"limit": 1, "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["data"][0]["organization_entity_id"], "org-beta")

    def test_query_cursor_binding_and_retired_mapping(self):
        filtered = self.client.get(
            "/api/v1/publishers?q=Alpha", headers=self.headers()
        )
        self.assertEqual([row["id"] for row in filtered.json()["data"]], ["publisher-a"])
        cursor = self.client.get(
            "/api/v1/publishers?limit=1", headers=self.headers()
        ).json()["pagination"]["next_cursor"]
        mismatch = self.client.get(
            "/api/v1/publishers",
            params={"limit": 1, "q": "Alpha", "cursor": cursor},
            headers=self.headers(),
        )
        self.assertEqual(mismatch.status_code, 400)
        retired = self.client.get(
            "/api/v1/publishers/publisher-c", headers=self.headers()
        )
        self.assertEqual(retired.json()["data"]["status"], "retired")

    def test_detail_etag_scope_and_strict_parameters(self):
        detail = self.client.get(
            "/api/v1/publishers/publisher-a", headers=self.headers()
        )
        self.assertEqual(detail.status_code, 200)
        etag = detail.headers["etag"]
        cached = self.client.get(
            "/api/v1/publishers/publisher-a",
            headers={**self.headers(), "If-None-Match": f"W/{etag}"},
        )
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.headers["etag"], etag)
        self.assertEqual(
            self.client.get("/api/v1/publishers/missing", headers=self.headers()).status_code,
            404,
        )
        denied = self.client.get(
            "/api/v1/publishers", headers=self.headers(self.wrong_key)
        )
        self.assertEqual(denied.status_code, 403)
        for path in (
            "/api/v1/publishers?unknown=1",
            "/api/v1/publishers?limit=01",
            "/api/v1/publishers?limit=1&limit=2",
            "/api/v1/publishers?q=" + "x" * 201,
            "/api/v1/publishers/publisher-a?version=v1",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "invalid_parameter")

    def test_list_fails_closed_for_unsupported_or_invalid_identity(self):
        with database.get_db(self.path) as db:
            self._replace_version(db, "publisher-c", "Former Press", "merged")
        response = self.client.get("/api/v1/publishers", headers=self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "not_ready")
        detail = self.client.get("/api/v1/publishers/publisher-c", headers=self.headers())
        self.assertEqual(detail.status_code, 503)

    def test_openapi_declares_catalog_scope_and_parameters(self):
        schema = self.client.get("/openapi.json").json()
        listing = schema["paths"]["/api/v1/publishers"]["get"]
        detail = schema["paths"]["/api/v1/publishers/{id}"]["get"]
        self.assertEqual(listing["x-required-scopes"], ["read:catalog"])
        self.assertEqual(detail["x-required-scopes"], ["read:catalog"])
        self.assertEqual(
            {parameter["name"] for parameter in listing["parameters"]},
            {"limit", "cursor", "q"},
        )

    def test_restricted_detail_and_broken_hash_fail_closed(self):
        with database.get_db(self.path) as db:
            self._replace_version(db, "publisher-b", "Beta Wire", "restricted")
        restricted = self.client.get(
            "/api/v1/publishers/publisher-b", headers=self.headers()
        )
        self.assertEqual(restricted.status_code, 403)
        with database.get_db(self.path) as db:
            self._replace_version(db, "publisher-a", "Alpha News", "active", "0" * 64)
        invalid = self.client.get(
            "/api/v1/publishers/publisher-a", headers=self.headers()
        )
        self.assertEqual(invalid.status_code, 503)

    def test_invalid_verified_domain_assertion_fails_whole_list(self):
        with database.get_db(self.path) as db:
            db.execute(
                """INSERT INTO publisher_domains(
                       id,publisher_id,domain,verification_status,
                       assertion_sha256,available_at)
                   VALUES('bad-domain','publisher-c','bad.example','verified',?,?)""",
                ("0" * 64, NOW),
            )
        response = self.client.get("/api/v1/publishers", headers=self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "not_ready")

    def test_dangling_or_non_organization_link_fails_closed(self):
        with database.get_db(self.path) as db:
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO entities(id,dataset_id,type,status,created_at)
                   VALUES('person-x',?,'person','active',?)""",
                (dataset, NOW),
            )
            payload = {
                "type": "person", "canonical_name": "Person X",
                "status": "active", "attributes": {},
            }
            db.execute(
                """INSERT INTO entity_versions(
                       id,entity_id,version,type,canonical_name,status,attributes_json,
                       version_sha256,available_at,created_by)
                   VALUES('person-x-v1','person-x',1,'person','Person X','active',
                          '{}',?,?, 'test')""",
                (digest(payload), NOW),
            )
            db.execute(
                "UPDATE entities SET current_version_id='person-x-v1' WHERE id='person-x'"
            )
            db.execute(
                "UPDATE publishers SET organization_entity_id='person-x' WHERE id='publisher-b'"
            )
        response = self.client.get("/api/v1/publishers", headers=self.headers())
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
