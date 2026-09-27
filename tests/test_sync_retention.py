from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.api_auth import create_consumer, issue_api_key
from app.api_sync import SnapshotCreate, create_snapshot
from app.sync_retention import cleanup_expired_snapshot_files
from app.worker import process_one_job


class SyncRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "RUNTIME_PATH", self.root / "runtime"),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        self.now = datetime.now(timezone.utc)
        scopes = {
            "read:sync", "read:items", "read:events", "read:catalog",
            "read:analyses", "read:evidence", "read:signals", "read:reports",
        }
        with database.get_db() as db:
            consumer = create_consumer(db, "retention", actor="test", now=self.now)
            self.key = issue_api_key(
                db, consumer, scopes, expires_at=self.now + timedelta(days=3),
                actor="test", now=self.now,
            )
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            self.dataset_id, self.epoch = state
        from app.api_auth import ApiPrincipal
        self.principal = ApiPrincipal(
            self.key.key_id, self.key.consumer_id, frozenset(self.key.scopes), 1
        )

    def _build(self, suffix="001"):
        with database.get_db() as db:
            response = create_snapshot(
                db, principal=self.principal, request_id="retention",
                idempotency_key=f"retention-key-{suffix}",
                request=SnapshotCreate(resources=["items"], scope="research"), now=self.now,
            )
        result = process_one_job(worker_id=f"retention-{suffix}", now=self.now + timedelta(seconds=1))
        self.assertEqual(result.state, "succeeded")
        snapshot_id = response.data.id
        digest = hashlib.sha256(snapshot_id.encode()).hexdigest()
        return snapshot_id, Path(config.RUNTIME_PATH) / "sync-snapshots" / f"snapshot-{digest}"

    def test_dry_run_then_apply_removes_only_files_and_keeps_ledger(self):
        snapshot_id, root = self._build()
        self.assertTrue((root / "manifest.json").is_file())
        early = cleanup_expired_snapshot_files(now=self.now + timedelta(hours=12), dry_run=False)
        self.assertEqual((early.candidates, early.deleted, early.retained), (0, 0, 1))
        preview = cleanup_expired_snapshot_files(now=self.now + timedelta(days=2), dry_run=True)
        self.assertEqual((preview.candidates, preview.eligible, preview.deleted), (1, 1, 0))
        self.assertTrue(root.is_dir())
        applied = cleanup_expired_snapshot_files(now=self.now + timedelta(days=2), dry_run=False)
        self.assertEqual((applied.candidates, applied.eligible, applied.deleted), (1, 1, 1))
        self.assertFalse(root.exists())
        repeated = cleanup_expired_snapshot_files(now=self.now + timedelta(days=2), dry_run=False)
        self.assertEqual((repeated.missing, repeated.deleted), (1, 0))
        with database.get_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM sync_snapshots WHERE id=?", (snapshot_id,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT state FROM sync_snapshot_requests WHERE id=?", (snapshot_id,)
                ).fetchone()[0],
                "ready",
            )

    def test_tampered_manifest_and_symlink_are_refused(self):
        _snapshot_id, root = self._build("tamper")
        (root / "manifest.json").write_text("{}")
        report = cleanup_expired_snapshot_files(now=self.now + timedelta(days=2), dry_run=False)
        self.assertEqual((report.refused, report.deleted), (1, 0))
        self.assertTrue(root.exists())
        shutil.rmtree(root)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        root.symlink_to(outside, target_is_directory=True)
        symlink = cleanup_expired_snapshot_files(
            now=self.now + timedelta(days=2), dry_run=False
        )
        self.assertEqual((symlink.refused, symlink.deleted), (1, 0))
        self.assertEqual((outside / "keep.txt").read_text(), "keep")

    def test_manifest_symlink_is_refused_even_when_content_hash_matches(self):
        _snapshot_id, root = self._build("manifest-link")
        manifest = root / "manifest.json"
        outside = self.root / "outside-manifest.json"
        outside.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(outside)
        report = cleanup_expired_snapshot_files(
            now=self.now + timedelta(days=2), dry_run=False
        )
        self.assertEqual((report.refused, report.deleted), (1, 0))
        self.assertTrue(root.is_dir())
        self.assertTrue(outside.is_file())

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValueError):
            cleanup_expired_snapshot_files(now=datetime(2026, 1, 1), dry_run=True)


if __name__ == "__main__":
    unittest.main()
