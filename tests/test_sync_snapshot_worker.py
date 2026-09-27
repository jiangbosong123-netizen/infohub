from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import rfc8785

from app import config, database
from app.api_auth import create_consumer, issue_api_key, revoke_api_key
from app.jobs import claim_job, enqueue_job
from app.sync_snapshots import SyncSnapshotError, build_sync_snapshot
from app.worker import process_one_job


UTC = timezone.utc
T0 = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
ALL_SCOPES = {
    "read:sync", "read:items", "read:events", "read:catalog", "read:analyses",
    "read:evidence", "read:signals", "read:reports",
}


class SyncSnapshotWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "app.db"
        for item in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "RUNTIME_PATH", self.root / "runtime"),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            self.consumer_id = create_consumer(db, "sync-worker", actor="test", now=T0)
            self.key = issue_api_key(
                db, self.consumer_id, ALL_SCOPES,
                expires_at=T0 + timedelta(days=3), actor="test", now=T0,
            )
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            self.dataset_id = state["dataset_id"]
            self.epoch = state["current_epoch"]
        self.counter = 0

    def _change(self, resource_type: str, resource_id: str, version_id: str,
                payload: dict, operation: str = "create") -> None:
        self.counter += 1
        encoded = rfc8785.dumps(payload)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO change_log(
                       dataset_id,epoch,idempotency_key,resource_type,resource_id,
                       version_id,operation,available_at,payload_json,payload_sha256,
                       hash_algorithm
                   ) VALUES(?,?,?,?,?,?,?,?,?,?, 'jcs-sha256-v1')""",
                (self.dataset_id, self.epoch, f"fixture:{self.counter}", resource_type,
                 resource_id, version_id, operation,
                 (T0 + timedelta(seconds=self.counter)).isoformat(),
                 encoded.decode("utf-8"), hashlib.sha256(encoded).hexdigest()),
            )

    def _request(self, resources=("items",), projection_scope="research", max_attempts=1,
                 claim=True):
        snapshot_id = f"snapshot-{self.counter + 1}"
        job = enqueue_job(
            kind="sync-snapshot", idempotency_key=f"sync:{snapshot_id}",
            subject_id=snapshot_id, input_version=self.epoch,
            payload={"snapshot_id": snapshot_id}, scheduled_for=T0,
            max_attempts=max_attempts,
        )
        request = {"resources": list(resources), "scope": projection_scope}
        request_bytes = rfc8785.dumps(request)
        with database.get_db() as db:
            db.execute(
                """INSERT INTO sync_snapshot_requests(
                       id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                       idempotency_key,request_json,request_sha256,resources_json,
                       scopes_json,projection_scope,state,job_id,created_at,expires_at)
                   VALUES(?,?,?,?,?,1,?,?,?,?,?,?,'pending',?,?,?)""",
                (snapshot_id, self.dataset_id, self.epoch, self.consumer_id,
                 self.key.key_id, f"request:{snapshot_id}",
                 request_bytes.decode("utf-8"), hashlib.sha256(request_bytes).hexdigest(),
                 json.dumps(list(resources)), json.dumps(sorted(ALL_SCOPES)), projection_scope,
                 job.id, T0.isoformat(), (T0 + timedelta(days=1)).isoformat()),
            )
        if claim:
            claimed = claim_job(worker_id="sync-test", kinds=("sync-snapshot",), now=T0)
            self.assertEqual(claimed.id, job.id)
            job = claimed
        return snapshot_id, job

    def test_completed_backup_fixes_high_water_and_writes_jcs_pages(self):
        self._change("item", "item-b", "item-b-v1", {"id": "item-b", "score": 1.0})
        self._change("item", "item-a", "item-a-v1", {"id": "item-a", "title": "old"})
        self._change("item", "item-a", "item-a-v2", {"id": "item-a", "title": "new"}, "update")
        snapshot_id, job = self._request(("reports", "items"))

        def concurrent_write():
            self._change("item", "item-c", "item-c-v1", {"id": "item-c"})

        result = build_sync_snapshot(
            snapshot_id, job_id=job.id, lease_token=job.lease_token,
            page_size=1, now=(T0 + timedelta(minutes=1)).isoformat(),
            after_backup=concurrent_write,
        )
        self.assertEqual((result.high_water, result.record_count, result.resource_count), (3, 2, 2))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT MAX(seq) FROM change_log").fetchone()[0], 4)
            request = db.execute(
                "SELECT state FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
            ).fetchone()
            self.assertEqual(request["state"], "ready")
            resources = db.execute(
                """SELECT * FROM sync_snapshot_resources
                   WHERE snapshot_id=? ORDER BY resource""", (snapshot_id,)
            ).fetchall()
            self.assertEqual(
                [(row["resource"], row["record_count"], row["page_count"])
                 for row in resources],
                [("items", 2, 2), ("reports", 0, 0)],
            )
            pages = db.execute(
                """SELECT * FROM sync_snapshot_pages
                   WHERE snapshot_id=? ORDER BY resource,page_number""", (snapshot_id,)
            ).fetchall()
        records = []
        resource_stream = hashlib.sha256()
        for page in pages:
            path = Path(config.RUNTIME_PATH) / page["payload_ref"]
            self.assertTrue(path.is_file())
            self.assertNotIn("staging", page["payload_ref"])
            payload = path.read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), page["payload_sha256"])
            page_records = json.loads(payload)
            records.extend(page_records)
            for record in page_records:
                resource_stream.update(rfc8785.dumps(record))
                resource_stream.update(b"\n")
        self.assertEqual([row["resource_id"] for row in records], ["item-a", "item-b"])
        self.assertEqual(records[0]["version_id"], "item-a-v2")
        root = _snapshot_root(config.RUNTIME_PATH, snapshot_id)
        self.assertTrue((root / "source.db").is_file())
        manifest_bytes = (root / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), result.manifest_sha256)
        self.assertEqual(manifest["knowledge_cutoff"]["high_water"], 3)
        self.assertEqual(manifest["record_count"], 2)
        item_manifest = next(row for row in manifest["resources"] if row["resource"] == "items")
        self.assertEqual(item_manifest["content_sha256"], resource_stream.hexdigest())

    def test_ready_build_is_idempotent_for_the_same_job(self):
        self._change("item", "item-a", "item-a-v1", {"id": "item-a"})
        snapshot_id, job = self._request()
        first = build_sync_snapshot(
            snapshot_id, job_id=job.id, lease_token=job.lease_token,
            now=(T0 + timedelta(minutes=1)).isoformat(),
        )
        second = build_sync_snapshot(
            snapshot_id, job_id=job.id, lease_token=job.lease_token,
            now=(T0 + timedelta(minutes=2)).isoformat(),
        )
        self.assertEqual(second, first)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_snapshots").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_checkpoints").fetchone()[0], 1)

    def test_authorization_change_after_backup_fails_without_partial_manifest(self):
        self._change("item", "item-a", "item-a-v1", {"id": "item-a"})
        snapshot_id, job = self._request()

        def revoke():
            with database.get_db() as db:
                revoke_api_key(db, self.key.key_id, actor="test", now=T0 + timedelta(seconds=2))

        with self.assertRaisesRegex(SyncSnapshotError, "authorization"):
            build_sync_snapshot(
                snapshot_id, job_id=job.id, lease_token=job.lease_token,
                now=(T0 + timedelta(minutes=1)).isoformat(), after_backup=revoke,
            )
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_snapshots").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_snapshot_resources").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_checkpoints").fetchone()[0], 0)
        self.assertFalse(_snapshot_root(config.RUNTIME_PATH, snapshot_id).exists())

    def test_worker_marks_terminal_selected_projection_failure(self):
        snapshot_id, _job = self._request(projection_scope="selected", claim=False)
        result = process_one_job(worker_id="regular-worker", now=T0 + timedelta(minutes=1))
        self.assertEqual(result.state, "dead_letter")
        with database.get_db() as db:
            request = db.execute(
                "SELECT state,error_code FROM sync_snapshot_requests WHERE id=?",
                (snapshot_id,),
            ).fetchone()
        self.assertEqual(request["state"], "failed")
        self.assertEqual(request["error_code"], "handler_syncsnapshoterror")

    def test_regular_worker_completes_snapshot_job_and_references_result(self):
        self._change("item", "item-a", "item-a-v1", {"id": "item-a"})
        snapshot_id, _job = self._request(claim=False)
        result = process_one_job(worker_id="regular-worker")
        self.assertEqual(result.state, "succeeded")
        self.assertIn(snapshot_id, result.result_ref)
        with database.get_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT state FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
                ).fetchone()[0],
                "ready",
            )


def _snapshot_root(runtime_path: Path, snapshot_id: str) -> Path:
    digest = hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()
    return Path(runtime_path) / "sync-snapshots" / f"snapshot-{digest}"
