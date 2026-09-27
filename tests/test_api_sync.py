from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import rfc8785
from fastapi.testclient import TestClient

from app import config, database
from app.api_auth import create_consumer, issue_api_key
from app.worker import process_one_job
from app.web.routes import app


class ApiSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "RUNTIME_PATH", self.root / "runtime"),
            patch("app.web.routes.get_db", lambda: database.get_db(self.path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path)),
            patch.object(config, "API_SYNC_ENABLED", True),
            patch.object(config, "DURABLE_JOBS_ENABLED", True),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        now = datetime.now(timezone.utc)
        self.all_scopes = {
            "read:sync", "read:items", "read:events", "read:catalog",
            "read:analyses", "read:evidence", "read:signals", "read:reports",
        }
        with database.get_db() as db:
            consumer = create_consumer(db, "sync-api", actor="test", now=now)
            self.key = issue_api_key(
                db, consumer, self.all_scopes, expires_at=now + timedelta(days=2),
                actor="test", now=now,
            )
            other = create_consumer(db, "other", actor="test", now=now)
            self.other_key = issue_api_key(
                db, other, self.all_scopes, expires_at=now + timedelta(days=2),
                actor="test", now=now,
            )
            limited = create_consumer(db, "limited", actor="test", now=now)
            self.limited_key = issue_api_key(
                db, limited, {"read:sync"}, expires_at=now + timedelta(days=2),
                actor="test", now=now,
            )
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            self.dataset_id, self.epoch = state
            for ordinal, item_id in enumerate(("item-b", "item-a"), 1):
                payload = {"id": item_id, "title": item_id}
                encoded = rfc8785.dumps(payload)
                db.execute(
                    """INSERT INTO change_log(
                           dataset_id,epoch,idempotency_key,resource_type,resource_id,
                           version_id,operation,available_at,payload_json,payload_sha256,
                           hash_algorithm)
                       VALUES(?,?,?,?,?,?,'create',?,?,?,'jcs-sha256-v1')""",
                    (self.dataset_id, self.epoch, f"fixture:{ordinal}", "item", item_id,
                     f"{item_id}-v1", now.isoformat(), encoded.decode(),
                     hashlib.sha256(encoded).hexdigest()),
                )
        self.client = TestClient(app)

    def headers(self, key=None, idempotency="snapshot-key-0001"):
        return {
            "Authorization": f"Bearer {(key or self.key).token}",
            "Idempotency-Key": idempotency,
        }

    def create(self, **kwargs):
        return self.client.post(
            "/api/v1/sync/snapshots", headers=self.headers(**kwargs),
            json={"resources": ["items"], "scope": "research"},
        )

    def test_create_is_atomic_idempotent_and_scope_bound(self):
        created = self.create()
        self.assertEqual(created.status_code, 202, created.text)
        snapshot_id = created.json()["data"]["id"]
        self.assertEqual(created.json()["data"]["status"], "pending")
        repeated = self.create()
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(repeated.json()["data"]["id"], snapshot_id)
        conflict = self.client.post(
            "/api/v1/sync/snapshots", headers=self.headers(),
            json={"resources": ["events"], "scope": "research"},
        )
        self.assertEqual((conflict.status_code, conflict.json()["error"]["code"]),
                         (409, "idempotency_conflict"))
        limited = self.create(key=self.limited_key, idempotency="limited-key-001")
        self.assertEqual((limited.status_code, limited.json()["error"]["code"]),
                         (403, "insufficient_scope"))
        with database.get_db() as db:
            row = db.execute(
                "SELECT job_id,state FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
            ).fetchone()
            self.assertIsNotNone(row["job_id"])
            self.assertEqual(row["state"], "pending")

    def test_worker_result_pages_are_verified_paginated_and_private(self):
        created = self.create()
        snapshot_id = created.json()["data"]["id"]
        job = process_one_job(worker_id="api-sync-worker")
        self.assertEqual(job.state, "succeeded")
        status = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}", headers=self.headers()
        )
        self.assertEqual(status.status_code, 200, status.text)
        data = status.json()["data"]
        self.assertEqual((data["status"], data["high_water"]), ("ready", 2))
        self.assertEqual(data["manifest"][0]["count"], 2)
        first = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}/pages?resource=items&limit=1",
            headers=self.headers(),
        )
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["data"][0]["resource_id"], "item-a")
        self.assertNotIn("payload_ref", first.text)
        cursor = body["pagination"]["next_cursor"]
        second = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}/pages?resource=items&limit=1&cursor={cursor}",
            headers=self.headers(),
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["data"][0]["resource_id"], "item-b")
        self.assertIsNone(second.json()["pagination"]["next_cursor"])
        hidden = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}", headers=self.headers(self.other_key)
        )
        self.assertEqual(hidden.status_code, 404)
        with database.get_db() as db:
            payload_ref = db.execute(
                """SELECT payload_ref FROM sync_snapshot_pages
                   WHERE snapshot_id=? AND resource='items' ORDER BY page_number LIMIT 1""",
                (snapshot_id,),
            ).fetchone()[0]
        (Path(config.RUNTIME_PATH) / payload_ref).write_bytes(b"[]")
        corrupt = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}/pages?resource=items&limit=1",
            headers=self.headers(),
        )
        self.assertEqual((corrupt.status_code, corrupt.json()["error"]["code"]),
                         (503, "not_ready"))

    def test_fail_closed_validation_and_feature_switch(self):
        self.assertEqual(self.create(key=self.key).status_code, 202)
        second = self.create(idempotency="second-key-0002")
        self.assertEqual((second.status_code, second.json()["error"]["code"]),
                         (429, "snapshot_limit_reached"))
        selected = self.client.post(
            "/api/v1/sync/snapshots", headers=self.headers(idempotency="selected-key-01"),
            json={"resources": ["items"], "scope": "selected"},
        )
        self.assertEqual(selected.status_code, 503)
        invalid = self.client.post(
            "/api/v1/sync/snapshots?extra=1", headers=self.headers(idempotency="invalid-key-001"),
            json={"resources": ["items"], "scope": "research"},
        )
        self.assertEqual(invalid.status_code, 422)
        with patch.object(config, "API_SYNC_ENABLED", False):
            disabled = self.client.post(
                "/api/v1/sync/snapshots", headers=self.headers(idempotency="disabled-key-01"),
                json={"resources": ["items"], "scope": "research"},
            )
        self.assertEqual(disabled.status_code, 503)

    def test_openapi_declares_sync_scope(self):
        schema = self.client.get("/openapi.json").json()
        for path, method in (
            ("/api/v1/sync/snapshots", "post"),
            ("/api/v1/sync/snapshots/{id}", "get"),
            ("/api/v1/sync/snapshots/{id}/pages", "get"),
        ):
            self.assertEqual(schema["paths"][path][method]["x-required-scopes"], ["read:sync"])


if __name__ == "__main__":
    unittest.main()
