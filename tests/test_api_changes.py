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


class ApiChangeFeedTests(unittest.TestCase):
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
        self.now = datetime.now(timezone.utc)
        self.scopes = {
            "read:sync", "read:items", "read:events", "read:catalog",
            "read:analyses", "read:evidence", "read:signals", "read:reports",
        }
        with database.get_db() as db:
            consumer = create_consumer(db, "change-reader", actor="test", now=self.now)
            self.key = issue_api_key(
                db, consumer, self.scopes, expires_at=self.now + timedelta(days=2),
                actor="test", now=self.now,
            )
            other = create_consumer(db, "other-reader", actor="test", now=self.now)
            self.other = issue_api_key(
                db, other, self.scopes, expires_at=self.now + timedelta(days=2),
                actor="test", now=self.now,
            )
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            self.dataset_id, self.epoch = state
        self.counter = 0
        self._change("item", "item-a", "item-a-v1", {"id": "item-a", "title": "old"})
        self.client = TestClient(app)

    def headers(self, key=None):
        return {"Authorization": f"Bearer {(key or self.key).token}"}

    def _change(self, resource_type, resource_id, version_id, payload, operation="create"):
        self.counter += 1
        encoded = rfc8785.dumps(payload)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO change_log(
                       dataset_id,epoch,idempotency_key,resource_type,resource_id,
                       version_id,operation,available_at,payload_json,payload_sha256,
                       hash_algorithm)
                   VALUES(?,?,?,?,?,?,?,?,?,?, 'jcs-sha256-v1')""",
                (self.dataset_id, self.epoch, f"change:{self.counter}", resource_type,
                 resource_id, version_id, operation,
                 (self.now + timedelta(seconds=self.counter)).isoformat(),
                 encoded.decode(), hashlib.sha256(encoded).hexdigest()),
            )

    def _snapshot_cursor(self):
        created = self.client.post(
            "/api/v1/sync/snapshots",
            headers={**self.headers(), "Idempotency-Key": "change-snapshot-001"},
            json={"resources": ["items"], "scope": "research"},
        )
        self.assertEqual(created.status_code, 202, created.text)
        snapshot_id = created.json()["data"]["id"]
        self.assertEqual(process_one_job(worker_id="change-worker").state, "succeeded")
        status = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}", headers=self.headers()
        )
        self.assertEqual(status.status_code, 200, status.text)
        return status.json()["data"]["resume_cursor"]

    def test_snapshot_handoff_filters_pages_changes_and_advances_empty_scan(self):
        cursor = self._snapshot_cursor()
        self.assertTrue(cursor)
        self._change("event", "event-a", "event-a-v1", {"id": "event-a"})
        self._change("item", "item-a", "item-a-v2", {"id": "item-a", "title": "new"}, "update")
        self._change("item", "item-b", "item-b-v1", {"id": "item-b", "status": "deleted"}, "delete")
        first = self.client.get(
            f"/api/v1/changes?limit=1&cursor={cursor}", headers=self.headers()
        )
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual([row["seq"] for row in body["data"]], [3])
        self.assertTrue(body["has_more"])
        self.assertEqual(body["high_water"], 4)
        second = self.client.get(
            f"/api/v1/changes?limit=1&cursor={body['next_cursor']}", headers=self.headers()
        )
        self.assertEqual(second.status_code, 200, second.text)
        body = second.json()
        self.assertEqual((body["data"][0]["operation"], body["has_more"]), ("delete", False))
        self._change("event", "event-b", "event-b-v1", {"id": "event-b"})
        empty = self.client.get(
            f"/api/v1/changes?cursor={body['next_cursor']}", headers=self.headers()
        )
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["data"], [])
        self.assertEqual(empty.json()["high_water"], 5)
        self._change("item", "item-c", "item-c-v1", {"id": "item-c"})
        resumed = self.client.get(
            f"/api/v1/changes?cursor={empty.json()['next_cursor']}", headers=self.headers()
        )
        self.assertEqual([row["resource_id"] for row in resumed.json()["data"]], ["item-c"])

    def test_cursor_is_principal_epoch_and_integrity_bound(self):
        cursor = self._snapshot_cursor()
        wrong = self.client.get(
            f"/api/v1/changes?cursor={cursor}", headers=self.headers(self.other)
        )
        self.assertEqual((wrong.status_code, wrong.json()["error"]["code"]),
                         (400, "invalid_cursor"))
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        bad = self.client.get(f"/api/v1/changes?cursor={tampered}", headers=self.headers())
        self.assertEqual((bad.status_code, bad.json()["error"]["code"]),
                         (400, "invalid_cursor"))
        self._change("item", "item-a", "item-a-v2", {"id": "item-a"}, "update")
        with database.get_db() as db:
            db.execute(
                "UPDATE change_log SET payload_sha256=? WHERE seq=2", ("0" * 64,)
            )
        corrupt = self.client.get(
            f"/api/v1/changes?cursor={cursor}", headers=self.headers()
        )
        self.assertEqual((corrupt.status_code, corrupt.json()["error"]["code"]),
                         (503, "not_ready"))

    def test_validation_switch_and_openapi(self):
        self.assertEqual(self.client.get("/api/v1/changes", headers=self.headers()).status_code, 422)
        self.assertEqual(
            self.client.get("/api/v1/changes?cursor=x&extra=1", headers=self.headers()).status_code,
            422,
        )
        with patch.object(config, "API_SYNC_ENABLED", False):
            disabled = self.client.get("/api/v1/changes?cursor=x", headers=self.headers())
        self.assertEqual(disabled.status_code, 503)
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(
            schema["paths"]["/api/v1/changes"]["get"]["x-required-scopes"],
            ["read:sync"],
        )

    def test_epoch_change_requires_a_new_snapshot(self):
        cursor = self._snapshot_cursor()
        replacement = "epoch-replacement"
        with database.get_db() as db:
            db.execute(
                """INSERT INTO dataset_epochs(
                       dataset_id,epoch,previous_epoch,reason,owner_environment_id,
                       started_at,release_id)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.dataset_id, replacement, self.epoch, "restore rehearsal",
                 config.ENVIRONMENT_ID, self.now.isoformat(), config.APP_VERSION),
            )
            db.execute(
                """UPDATE dataset_state SET current_epoch=?,updated_at=?
                   WHERE singleton=1""",
                (replacement, self.now.isoformat()),
            )
        stale = self.client.get(
            f"/api/v1/changes?cursor={cursor}", headers=self.headers()
        )
        self.assertEqual((stale.status_code, stale.json()["error"]["code"]),
                         (409, "epoch_changed"))


if __name__ == "__main__":
    unittest.main()
