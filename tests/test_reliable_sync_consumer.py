from __future__ import annotations

import copy
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
from examples.reliable_sync_consumer import (
    ConsumerProtocolError,
    ReliableSyncConsumer,
    SnapshotRequired,
)


class ReliableSyncConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server_path = self.root / "server.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.server_path),
            patch.object(config, "DB_PATH", self.server_path),
            patch.object(config, "RUNTIME_PATH", self.root / "runtime"),
            patch("app.web.routes.get_db", lambda: database.get_db(self.server_path)),
            patch("app.web.v1_auth.get_db", lambda: database.get_db(self.server_path)),
            patch.object(config, "API_SYNC_ENABLED", True),
            patch.object(config, "DURABLE_JOBS_ENABLED", True),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        self.now = datetime.now(timezone.utc)
        scopes = {"read:sync", "read:items"}
        with database.get_db() as db:
            consumer = create_consumer(db, "reference-consumer", actor="test", now=self.now)
            self.key = issue_api_key(
                db, consumer, scopes, expires_at=self.now + timedelta(days=2),
                actor="test", now=self.now,
            )
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            self.dataset_id, self.epoch = state
        self.counter = 0
        self._change("item", "item-a", "item-a-v1", {"id": "item-a", "title": "A"})
        self._change("item", "item-b", "item-b-v1", {"id": "item-b", "title": "B"})
        self.client = TestClient(app)
        self.consumer = ReliableSyncConsumer(self.root / "consumer.db")

    def headers(self):
        return {"Authorization": f"Bearer {self.key.token}"}

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
                (self.dataset_id, self.epoch, f"consumer:{self.counter}", resource_type,
                 resource_id, version_id, operation,
                 (self.now + timedelta(seconds=self.counter)).isoformat(),
                 encoded.decode(), hashlib.sha256(encoded).hexdigest()),
            )

    def _ready_snapshot(self, idempotency="consumer-snapshot-001"):
        created = self.client.post(
            "/api/v1/sync/snapshots",
            headers={**self.headers(), "Idempotency-Key": idempotency},
            json={"resources": ["items"], "scope": "research"},
        )
        self.assertEqual(created.status_code, 202, created.text)
        snapshot_id = created.json()["data"]["id"]
        self.assertEqual(process_one_job(worker_id="consumer-worker").state, "succeeded")
        status = self.client.get(
            f"/api/v1/sync/snapshots/{snapshot_id}", headers=self.headers()
        )
        self.assertEqual(status.status_code, 200, status.text)
        pages = []
        cursor = None
        while True:
            params = {"resource": "items", "limit": 1}
            if cursor:
                params["cursor"] = cursor
            response = self.client.get(
                f"/api/v1/sync/snapshots/{snapshot_id}/pages",
                params=params, headers=self.headers(),
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            pages.append(body)
            cursor = body["pagination"]["next_cursor"]
            if cursor is None:
                break
        return status.json(), {"items": pages}

    def _changes(self, cursor, limit=1):
        response = self.client.get(
            "/api/v1/changes", params={"cursor": cursor, "limit": limit},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_snapshot_then_changes_are_atomic_and_tombstones_are_retained(self):
        status, pages = self._ready_snapshot()
        state = self.consumer.import_snapshot(status, pages, received_at=self.now)
        self.assertEqual((state.high_water, state.resources), (2, ("items",)))
        objects = self.consumer.objects()
        self.assertEqual([row["resource_id"] for row in objects], ["item-a", "item-b"])
        self.assertTrue(all(row["operation"] == "snapshot" for row in objects))

        self._change("event", "event-hidden", "event-hidden-v1", {"id": "event-hidden"})
        self._change(
            "item", "item-a", "item-a-v2", {"id": "item-a", "title": "A2"}, "update"
        )
        self._change(
            "item", "item-b", "item-b-v2", {"id": "item-b", "status": "deleted"},
            "delete",
        )
        first = self._changes(state.resume_cursor)
        self.assertTrue(first["has_more"])
        state = self.consumer.apply_changes(first, received_at=self.now + timedelta(minutes=1))
        self.assertEqual(state.high_water, 4)
        second = self._changes(state.resume_cursor)
        self.assertFalse(second["has_more"])
        state = self.consumer.apply_changes(second, received_at=self.now + timedelta(minutes=2))
        self.assertEqual(state.high_water, 5)
        objects = {row["resource_id"]: row for row in self.consumer.objects()}
        self.assertEqual(objects["item-a"]["payload"]["title"], "A2")
        self.assertEqual(objects["item-b"]["operation"], "delete")
        self.assertEqual(objects["item-b"]["payload"]["status"], "deleted")
        self.assertNotIn("event-hidden", objects)

        self._change("event", "event-hidden-2", "event-hidden-2-v1", {"id": "event-hidden-2"})
        empty = self._changes(state.resume_cursor)
        self.assertEqual(empty["data"], [])
        state = self.consumer.apply_changes(empty, received_at=self.now + timedelta(minutes=3))
        self.assertEqual(state.high_water, 6)

        for resource_id, operation in (
            ("item-withdrawn", "withdraw"),
            ("item-merged", "merge"),
            ("item-split", "split"),
        ):
            self._change(
                "item", resource_id, f"{resource_id}-v1",
                {"id": resource_id, "status": operation}, operation,
            )
        terminal = self._changes(state.resume_cursor, limit=100)
        state = self.consumer.apply_changes(terminal)
        self.assertEqual(state.high_water, 9)
        operations = {
            row["resource_id"]: row["operation"] for row in self.consumer.objects()
        }
        self.assertEqual(operations["item-withdrawn"], "withdraw")
        self.assertEqual(operations["item-merged"], "merge")
        self.assertEqual(operations["item-split"], "split")

    def test_bad_change_rolls_back_object_and_cursor_together(self):
        status, pages = self._ready_snapshot()
        state = self.consumer.import_snapshot(status, pages)
        self._change("item", "item-c", "item-c-v1", {"id": "item-c"})
        self._change("item", "item-d", "item-d-v1", {"id": "item-d"})
        valid = self._changes(state.resume_cursor, limit=100)
        corrupt = copy.deepcopy(valid)
        corrupt["data"][1]["payload_sha256"] = "0" * 64
        with self.assertRaisesRegex(ConsumerProtocolError, "hash"):
            self.consumer.apply_changes(corrupt)
        unchanged = self.consumer.state()
        self.assertEqual(unchanged.high_water, 2)
        ids = {row["resource_id"] for row in self.consumer.objects()}
        self.assertNotIn("item-c", ids)
        self.assertNotIn("item-d", ids)
        applied = self.consumer.apply_changes(valid)
        self.assertEqual(applied.high_water, 4)
        ids = {row["resource_id"] for row in self.consumer.objects()}
        self.assertIn("item-c", ids)
        self.assertIn("item-d", ids)

    def test_snapshot_page_tampering_and_epoch_change_require_safe_recovery(self):
        status, pages = self._ready_snapshot()
        tampered = copy.deepcopy(pages)
        tampered["items"][0]["data"][0]["payload"]["title"] = "tampered"
        with self.assertRaisesRegex(ConsumerProtocolError, "page hash"):
            self.consumer.import_snapshot(status, tampered)
        self.assertIsNone(self.consumer.state())

        state = self.consumer.import_snapshot(status, pages)
        response = self._changes(state.resume_cursor)
        response["dataset_epoch"] = "restored-epoch"
        with self.assertRaisesRegex(SnapshotRequired, "epoch changed"):
            self.consumer.apply_changes(response)
        self.assertEqual(self.consumer.state().dataset_epoch, self.epoch)


if __name__ == "__main__":
    unittest.main()
