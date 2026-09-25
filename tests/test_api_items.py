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


T0 = "2026-09-25T12:00:00.000000Z"
T1 = "2026-09-25T12:01:00.000000Z"
T2 = "2026-09-25T12:02:00.000000Z"


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def object_sha(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return sha(encoded)


class ApiItemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "items.db"
        db_admin.migrate_database(self.path)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "item-test", actor="test")
            self.key = issue_api_key(
                db, consumer, {"read:items"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            wrong = create_consumer(db, "item-wrong", actor="test")
            self.wrong_key = issue_api_key(
                db, wrong, {"read:catalog"},
                expires_at=now + timedelta(days=1), actor="test",
            )
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            epoch = db.execute(
                "SELECT current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO sources(
                       id,key,name,channel,tier,type,url,enabled,interval_minutes)
                   VALUES(1,'wire-feed','Wire Feed','ai','media','rss',
                          'https://feed.example/rss',1,30)"""
            )
            self._published_time_evidence(db, dataset, epoch)
            self._publisher(db, dataset)
            self._document(
                db, dataset, 1, "doc-a", "article", "active", T2,
                "Alpha launch", "Alpha full text", "en",
                "https://user:password@alpha.example/story?token=secret&view=full#part",
                publisher_id="publisher-a", published_at=T1,
                published_time_value_id="time-a", raw_record_id="raw-a",
                time_status="parsed", point_in_time=True,
                content_origin="publisher_text", content_extent="full",
                extraction_status="complete",
            )
            self._document(
                db, dataset, 2, "doc-b", "flash", "active", T2,
                "Beta update", "Beta excerpt", "zh", "https://beta.example/update",
            )
            self._document(
                db, dataset, 3, "doc-c", "filing", "active", T1,
                "Gamma filing", "Generated metadata", "en", "urn:infohub:legacy-item:3",
                content_origin="generated_metadata", content_extent="excerpt",
            )
            self._document(
                db, dataset, 4, "doc-w", "article", "withdrawn", T0,
                "Withdrawn", "Withdrawn text", "en", "https://old.example/withdrawn",
            )
            self._document(
                db, dataset, 5, "doc-r", "article", "restricted", T0,
                "Restricted", "Restricted text", "en", "https://private.example/item",
            )
            self._document(
                db, dataset, 6, "doc-d", "article", "duplicate_alias", T0,
                "Duplicate", "Duplicate text", "en", "https://dup.example/item",
            )
        patches = (
            patch("app.web.routes.get_db", lambda: database.get_db(self.path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path)),
            patch.object(config, "API_ITEMS_ENABLED", True),
        )
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.client = TestClient(app)

    def _published_time_evidence(self, db, dataset, epoch):
        config_json = "{}"
        db.execute(
            """INSERT INTO source_config_versions(
                   id,source_id,version,config_json,config_hash,available_at)
               VALUES('cfg-1',1,1,?,?,?)""",
            (config_json, sha(config_json), T0),
        )
        db.execute(
            """INSERT INTO ingest_runs(
                   id,source_id,config_version_id,dataset_id,dataset_epoch,
                   scheduled_for,started_at,finished_at,status,trace_id)
               VALUES('run-1',1,'cfg-1',?,?,?,?,?,'succeeded','trace-item-test')""",
            (dataset, epoch, T0, T0, T0),
        )
        payload_sha = sha("raw-alpha")
        db.execute(
            """INSERT INTO raw_records(
                   id,first_ingest_run_id,source_id,external_id,observed_at,
                   ingested_at,media_type,payload_sha256,payload_ref,payload_kind,
                   size_bytes,retention_class)
               VALUES('raw-a','run-1',1,'alpha',?,?,'application/json',?,?,'api_record',
                      0,'private-metadata')""",
            (T0, T0, payload_sha, f"sha256/{payload_sha}"),
        )
        db.execute(
            """INSERT INTO source_time_values(
                   id,raw_record_id,ordinal,field_path,raw_value,role,utc,precision,
                   interpretation,status,rule_version,tzdb_version)
               VALUES('time-a','raw-a',0,'published_at',?,'published',?,'second',
                      'exact','valid','time-test-v1','test')""",
            (T1, T1),
        )

    def _publisher(self, db, dataset):
        db.execute(
            """INSERT INTO publishers(id,dataset_id,status,created_at)
               VALUES('publisher-a',?,'active',?)""",
            (dataset, T0),
        )
        digest = object_sha({"name": "Alpha News", "status": "active"})
        db.execute(
            """INSERT INTO publisher_versions(
                   id,publisher_id,version,name,status,version_sha256,available_at)
               VALUES('publisher-a-v1','publisher-a',1,'Alpha News','active',?,?)""",
            (digest, T0),
        )
        db.execute(
            "UPDATE publishers SET current_version_id='publisher-a-v1' WHERE id='publisher-a'"
        )

    def _document(
        self, db, dataset, legacy_id, document_id, kind, status, first_seen,
        title, text, language, url, *, publisher_id=None, published_at=None,
        published_time_value_id=None, raw_record_id=None,
        time_status="legacy_unverified", point_in_time=False,
        content_origin="legacy_unknown", content_extent="excerpt",
        extraction_status="partial", version_hash=None,
    ):
        db.execute(
            """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
               VALUES(1,?,?, 'ai',?,?)""",
            (f"https://legacy.example/{legacy_id}", title, T0, T0),
        )
        self.assertEqual(db.execute("SELECT last_insert_rowid()").fetchone()[0], legacy_id)
        db.execute(
            """INSERT INTO documents(
                   id,dataset_id,legacy_item_id,kind,first_seen_at,status)
               VALUES(?,?,?,?,?,?)""",
            (document_id, dataset, legacy_id, kind, first_seen, status),
        )
        version_id = f"{document_id}-v1"
        precision = "second" if published_at else "unknown"
        value = {
            "normalizer_version": "normalizer-test-v1", "title_original": title,
            "language": language, "text": text, "canonical_url": url,
            "source_id": 1, "publisher_id": publisher_id,
            "published_at": published_at, "published_precision": precision,
            "time_status": time_status, "time_rule_version": "time-test-v1",
            "tzdb_version": "test", "content_origin": content_origin,
            "content_extent": content_extent, "truncated": 0,
            "extraction_status": extraction_status,
        }
        db.execute(
            """INSERT INTO document_versions(
                   id,document_id,version,normalizer_version,normalized_at,
                   title_original,language,text,content_sha256,version_sha256,
                   canonical_url,source_id,publisher_id,published_at,published_precision,
                   published_time_value_id,time_status,time_rule_version,tzdb_version,
                   content_origin,content_extent,
                   truncated,extraction_status,correction_kind,available_at,
                   availability_basis,point_in_time_eligible)
               VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'initial',?,?,?)""",
            (
                version_id, document_id, "normalizer-test-v1", T0, title, language,
                text, sha(text), version_hash or object_sha(value), url, 1,
                publisher_id, published_at,
                precision, published_time_value_id, time_status, "time-test-v1", "test",
                content_origin,
                content_extent, 0, extraction_status, T0,
                "transaction_recorded" if point_in_time else "legacy_unknown",
                int(point_in_time),
            ),
        )
        if raw_record_id:
            db.execute(
                """INSERT INTO document_version_inputs(version_id,raw_record_id,role)
                   VALUES(?,?,'primary')""",
                (version_id, raw_record_id),
            )
        db.execute(
            "UPDATE documents SET current_version_id=? WHERE id=?",
            (version_id, document_id),
        )

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def _append_version(self, db, document_id, version, previous_id, title, *, current=True):
        version_id = f"{document_id}-v{version}"
        text = f"{title} body"
        value = {
            "normalizer_version": "normalizer-test-v1", "title_original": title,
            "language": "en", "text": text,
            "canonical_url": f"https://alpha.example/story?v={version}",
            "source_id": 1, "publisher_id": None, "published_at": None,
            "published_precision": "unknown", "time_status": "missing",
            "time_rule_version": "time-test-v1", "tzdb_version": "test",
            "content_origin": "feed_excerpt", "content_extent": "excerpt",
            "truncated": 0, "extraction_status": "partial",
        }
        db.execute(
            """INSERT INTO document_versions(
                   id,document_id,version,previous_version_id,normalizer_version,
                   normalized_at,title_original,language,text,content_sha256,
                   version_sha256,canonical_url,source_id,published_precision,
                   time_status,time_rule_version,tzdb_version,content_origin,
                   content_extent,truncated,extraction_status,correction_kind,
                   available_at,availability_basis,point_in_time_eligible)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'content_change',
                      ?,'transaction_recorded',0)""",
            (
                version_id, document_id, version, previous_id, "normalizer-test-v1",
                T2, title, "en", text, sha(text), object_sha(value),
                f"https://alpha.example/story?v={version}", 1, "unknown", "missing",
                "time-test-v1", "test", "feed_excerpt", "excerpt", 0, "partial", T2,
            ),
        )
        if current:
            db.execute(
                "UPDATE documents SET current_version_id=? WHERE id=?",
                (version_id, document_id),
            )
        return version_id

    def test_list_orders_by_seen_time_then_id_and_sanitizes_url(self):
        first = self.client.get("/api/v1/items?limit=1", headers=self.headers())
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["request_id"], first.headers["X-Request-ID"])
        self.assertEqual(body["pagination"]["order"], "first_seen_at_desc_id_asc")
        self.assertEqual(body["data"][0]["id"], "doc-a")
        version = body["data"][0]["current_version"]
        self.assertNotIn("text", version)
        self.assertEqual(version["text_length"], len("Alpha full text"))
        self.assertEqual(version["source_id"], "wire-feed")
        self.assertEqual(version["publisher_id"], "publisher-a")
        self.assertEqual(version["canonical_url"], "https://alpha.example/story?view=full")
        self.assertTrue(version["time"]["point_in_time_eligible"])
        self.assertEqual(version["time"]["source_time_value_id"], "time-a")
        detail = self.client.get("/api/v1/items/doc-a", headers=self.headers())
        self.assertEqual(detail.json()["data"]["current_version"]["text"], "Alpha full text")
        second = self.client.get(
            "/api/v1/items",
            params={"limit": 1, "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["data"][0]["id"], "doc-b")

    def test_filters_and_cursor_are_bound(self):
        response = self.client.get(
            "/api/v1/items?q=Gamma&kind=filing&language=en&source_id=wire-feed",
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.json()["data"]], ["doc-c"])
        self.assertIsNone(response.json()["data"][0]["current_version"]["canonical_url"])
        publisher = self.client.get(
            "/api/v1/items?publisher_id=publisher-a", headers=self.headers()
        )
        self.assertEqual([row["id"] for row in publisher.json()["data"]], ["doc-a"])
        cursor = self.client.get(
            "/api/v1/items?limit=1", headers=self.headers()
        ).json()["pagination"]["next_cursor"]
        mismatch = self.client.get(
            "/api/v1/items", params={"limit": 1, "kind": "flash", "cursor": cursor},
            headers=self.headers(),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["error"]["code"], "filter_mismatch")

    def test_detail_etag_status_and_scope_boundaries(self):
        detail = self.client.get("/api/v1/items/doc-a", headers=self.headers())
        self.assertEqual(detail.status_code, 200)
        etag = detail.headers["etag"]
        cached = self.client.get(
            "/api/v1/items/doc-a",
            headers={**self.headers(), "If-None-Match": etag},
        )
        self.assertEqual(cached.status_code, 304)
        withdrawn = self.client.get("/api/v1/items/doc-w", headers=self.headers())
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(withdrawn.json()["data"]["status"], "withdrawn")
        self.assertEqual(
            self.client.get("/api/v1/items/doc-r", headers=self.headers()).status_code, 403
        )
        self.assertEqual(
            self.client.get("/api/v1/items/doc-d", headers=self.headers()).status_code, 503
        )
        self.assertEqual(
            self.client.get("/api/v1/items/missing", headers=self.headers()).status_code, 404
        )
        denied = self.client.get("/api/v1/items", headers=self.headers(self.wrong_key))
        self.assertEqual(denied.status_code, 403)

    def test_parameters_and_feature_flag_are_strict(self):
        for path in (
            "/api/v1/items?unknown=1", "/api/v1/items?limit=01",
            "/api/v1/items?limit=1&limit=2", "/api/v1/items?kind=news",
            "/api/v1/items?language=", "/api/v1/items?source_id=",
            "/api/v1/items?q=" + "x" * 201, "/api/v1/items/doc-a?version=v1",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "invalid_parameter")
        with patch.object(config, "API_ITEMS_ENABLED", False):
            disabled = self.client.get("/api/v1/items", headers=self.headers())
        self.assertEqual(disabled.status_code, 503)
        self.assertEqual(disabled.json()["error"]["code"], "not_ready")

    def test_missing_current_or_bad_hash_fails_closed(self):
        with database.get_db(self.path) as db:
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
                   VALUES(1,'https://legacy.example/7','Missing','ai',?,?)""",
                (T0, T0),
            )
            db.execute(
                """INSERT INTO documents(
                       id,dataset_id,legacy_item_id,kind,first_seen_at,status)
                   VALUES('doc-missing',?,7,'article',?,'active')""",
                (dataset, T0),
            )
        unavailable = self.client.get("/api/v1/items", headers=self.headers())
        self.assertEqual(unavailable.status_code, 503)
        with database.get_db(self.path) as db:
            db.execute("UPDATE documents SET status='withdrawn' WHERE id='doc-missing'")
            # Immutable rows can be born corrupt; readers must detect that state.
            self._document(
                db, dataset, 8, "doc-bad", "article", "active", T0,
                "Bad hash", "Bad text", "en", "https://bad.example/item",
                version_hash="0" * 64,
            )
        invalid = self.client.get("/api/v1/items?q=Bad", headers=self.headers())
        self.assertEqual(invalid.status_code, 503)
        self.assertEqual(invalid.json()["error"]["code"], "not_ready")

    def test_published_time_without_raw_evidence_fails_closed(self):
        with database.get_db(self.path) as db:
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            self._document(
                db, dataset, 7, "doc-time-gap", "article", "active", T0,
                "Missing time evidence", "Body", "en", "https://time.example/item",
                published_at=T1, time_status="parsed",
            )
        response = self.client.get(
            "/api/v1/items?q=Missing%20time%20evidence", headers=self.headers()
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "not_ready")

    def test_version_history_is_bounded_ordered_and_cursor_bound(self):
        with database.get_db(self.path) as db:
            second = self._append_version(db, "doc-a", 2, "doc-a-v1", "Alpha revised")
            third = self._append_version(db, "doc-a", 3, second, "Alpha corrected")
        first = self.client.get(
            "/api/v1/items/doc-a/versions?limit=1", headers=self.headers()
        )
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["data"]["current_version_id"], third)
        self.assertEqual(body["pagination"]["order"], "version_desc")
        entry = body["data"]["versions"][0]
        self.assertTrue(entry["is_current"])
        self.assertEqual(entry["previous_version_id"], second)
        self.assertEqual(entry["version"]["version"], 3)
        self.assertNotIn("text", entry["version"])
        second_page = self.client.get(
            "/api/v1/items/doc-a/versions",
            params={"limit": 1, "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(
            second_page.json()["data"]["versions"][0]["version"]["version"], 2
        )
        mismatch = self.client.get(
            "/api/v1/items/doc-b/versions",
            params={"limit": 1, "cursor": body["pagination"]["next_cursor"]},
            headers=self.headers(),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["error"]["code"], "filter_mismatch")

    def test_version_history_boundaries_and_current_pointer_fail_closed(self):
        self.assertEqual(
            self.client.get("/api/v1/items/missing/versions", headers=self.headers()).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/v1/items/doc-r/versions", headers=self.headers()).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/v1/items/doc-d/versions", headers=self.headers()).status_code,
            503,
        )
        for path in (
            "/api/v1/items/doc-a/versions?unknown=1",
            "/api/v1/items/doc-a/versions?limit=01",
            "/api/v1/items/doc-a/versions?limit=1&limit=2",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 422)
        with database.get_db(self.path) as db:
            self._append_version(
                db, "doc-a", 2, "doc-a-v1", "Unpublished current", current=False
            )
        stale = self.client.get("/api/v1/items/doc-a/versions", headers=self.headers())
        self.assertEqual(stale.status_code, 503)
        self.assertEqual(stale.json()["error"]["code"], "not_ready")

    def test_openapi_declares_item_contract(self):
        schema = self.client.get("/openapi.json").json()
        listing = schema["paths"]["/api/v1/items"]["get"]
        detail = schema["paths"]["/api/v1/items/{id}"]["get"]
        versions = schema["paths"]["/api/v1/items/{id}/versions"]["get"]
        self.assertEqual(listing["x-required-scopes"], ["read:items"])
        self.assertEqual(detail["x-required-scopes"], ["read:items"])
        self.assertEqual(versions["x-required-scopes"], ["read:items"])
        self.assertEqual(
            {parameter["name"] for parameter in listing["parameters"]},
            {"limit", "cursor", "q", "kind", "language", "source_id", "publisher_id"},
        )
        self.assertEqual(
            {parameter["name"] for parameter in versions["parameters"]},
            {"id", "limit", "cursor"},
        )


if __name__ == "__main__":
    unittest.main()
