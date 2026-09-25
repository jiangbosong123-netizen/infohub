from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import rfc8785

from app import config, database, db_admin
from app.jobs import JobEnvironmentError, LeaseLostError, claim_job, enqueue_job, get_job
from app.publication import (
    ChangeRequest,
    DatasetEnvironmentError,
    EpochConflictError,
    EpochRotationBlockedError,
    PublicationConflictError,
    create_knowledge_checkpoint,
    get_dataset_identity,
    publish_job_result,
    record_clock_check,
    rotate_dataset_epoch,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class PublicationLedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "publication.db"
        patcher = patch.object(database, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """CREATE TABLE publication_fixture(
                       id INTEGER PRIMARY KEY,
                       resource_type TEXT NOT NULL,
                       resource_id TEXT NOT NULL,
                       version_id TEXT NOT NULL,
                       publication_seq INTEGER NOT NULL UNIQUE,
                       payload_json TEXT NOT NULL
                   )"""
            )
        self.number = 0

    def job(self, *, logical_key: str | None = None, lease_seconds: int = 300):
        self.number += 1
        key = logical_key or f"job:{self.number}"
        job = enqueue_job(
            kind="publish-fixture",
            idempotency_key=key,
            subject_id=f"fixture:{self.number}",
            input_version="input-v1",
            scheduled_for=T0,
        )
        claimed = claim_job(
            worker_id=f"worker:{self.number}", lease_seconds=lease_seconds, now=T0
        )
        self.assertEqual(claimed.id, job.id)
        return claimed

    def change(
        self,
        key: str = "change:one",
        resource_id: str = "item-one",
        version_id: str = "item-one-v1",
        payload: dict | None = None,
    ) -> ChangeRequest:
        return ChangeRequest(
            idempotency_key=key,
            resource_type="item",
            resource_id=resource_id,
            version_id=version_id,
            operation="create",
            payload=payload or {"id": resource_id, "score": 1.0},
        )

    @staticmethod
    def persist_fixture(db, changes) -> None:
        for change in changes:
            db.execute(
                """INSERT INTO publication_fixture(
                       resource_type,resource_id,version_id,publication_seq,payload_json
                   ) VALUES(?,?,?,?,?)""",
                (
                    change.resource_type,
                    change.resource_id,
                    change.version_id,
                    change.seq,
                    rfc8785.dumps(change.payload).decode("utf-8"),
                ),
            )

    def test_dataset_identity_is_stable_and_bound_to_environment(self):
        first = get_dataset_identity()
        database.init_schema()
        second = get_dataset_identity()
        self.assertEqual(second, first)
        self.assertTrue(first.dataset_id.startswith("dataset_"))
        self.assertTrue(first.epoch.startswith("epoch_"))
        self.assertEqual(first.owner_environment_id, config.ENVIRONMENT_ID)
        self.assertEqual(first.high_water, 0)

        with patch.object(config, "ENVIRONMENT_ID", "different-environment"):
            with self.assertRaises(DatasetEnvironmentError):
                get_dataset_identity()
            with self.assertRaises(JobEnvironmentError):
                enqueue_job(kind="test", idempotency_key="wrong-environment")

    def test_content_change_and_job_success_commit_together(self):
        job = self.job()
        request = self.change(payload={"score": 1.0, "id": "item-one"})
        result = publish_job_result(
            job_id=job.id,
            lease_token=job.lease_token,
            expected_input_version="input-v1",
            changes=(request,),
            persist=self.persist_fixture,
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(result.job.state, "succeeded")
        self.assertEqual(result.dataset.high_water, result.changes[0].seq)
        canonical = rfc8785.dumps({"score": 1.0, "id": "item-one"})
        self.assertEqual(result.changes[0].payload_sha256, hashlib.sha256(canonical).hexdigest())
        self.assertEqual(result.changes[0].hash_algorithm, "jcs-sha256-v1")
        with sqlite3.connect(self.path) as db:
            content = db.execute(
                "SELECT publication_seq,payload_json FROM publication_fixture"
            ).fetchone()
            stored = db.execute(
                "SELECT seq,payload_json,payload_sha256 FROM change_log"
            ).fetchone()
        self.assertEqual(content[0], stored[0])
        self.assertEqual(content[1], canonical.decode("utf-8"))
        self.assertEqual(stored[1], canonical.decode("utf-8"))
        self.assertEqual(stored[2], hashlib.sha256(canonical).hexdigest())

    def test_callback_failure_rolls_back_content_change_and_completion(self):
        job = self.job()

        def fail_after_write(db, changes):
            self.persist_fixture(db, changes)
            raise RuntimeError("injected publication failure")

        with self.assertRaisesRegex(RuntimeError, "injected publication failure"):
            publish_job_result(
                job_id=job.id,
                lease_token=job.lease_token,
                expected_input_version="input-v1",
                changes=(self.change(),),
                persist=fail_after_write,
                now=T0 + timedelta(seconds=1),
            )
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publication_fixture").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)
            attempt = db.execute("SELECT status FROM job_attempts").fetchone()[0]
        self.assertEqual(get_job(job.id).state, "running")
        self.assertEqual(attempt, "running")

        result = publish_job_result(
            job_id=job.id,
            lease_token=job.lease_token,
            expected_input_version="input-v1",
            changes=(self.change(),),
            persist=self.persist_fixture,
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual(result.job.state, "succeeded")

    def test_expired_worker_cannot_publish_any_row(self):
        job = self.job(lease_seconds=10)
        with self.assertRaises(LeaseLostError):
            publish_job_result(
                job_id=job.id,
                lease_token=job.lease_token,
                expected_input_version="input-v1",
                changes=(self.change(),),
                persist=self.persist_fixture,
                now=T0 + timedelta(seconds=10),
            )
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publication_fixture").fetchone()[0], 0)

    def test_success_retry_is_idempotent_and_does_not_run_callback_twice(self):
        job = self.job()
        calls = []

        def persist(db, changes):
            calls.append(tuple(change.seq for change in changes))
            self.persist_fixture(db, changes)

        request = self.change()
        first = publish_job_result(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="input-v1", changes=(request,), persist=persist, now=T0,
        )
        second = publish_job_result(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="input-v1", changes=(request,), persist=persist,
            now=T0 + timedelta(seconds=2),
        )
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(calls), 1)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM publication_fixture").fetchone()[0], 1)

        changed = self.change(payload={"id": "item-one", "score": 2})
        with self.assertRaises(PublicationConflictError):
            publish_job_result(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version="input-v1", changes=(changed,), persist=persist,
                now=T0 + timedelta(seconds=3),
            )

    def test_empty_result_completes_without_inventing_a_change(self):
        job = self.job()
        before = get_dataset_identity()
        result = publish_job_result(
            job_id=job.id,
            lease_token=job.lease_token,
            expected_input_version="input-v1",
            changes=(),
            now=T0,
        )
        self.assertEqual(result.changes, ())
        self.assertEqual(result.dataset.high_water, before.high_water)
        self.assertEqual(result.job.state, "succeeded")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)

        repeated = publish_job_result(
            job_id=job.id,
            lease_token=job.lease_token,
            expected_input_version="input-v1",
            changes=(),
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(repeated.to_dict(), result.to_dict())
        with self.assertRaises(ValueError):
            publish_job_result(
                job_id=job.id,
                lease_token=job.lease_token,
                expected_input_version="input-v1",
                changes=(),
                persist=lambda db, changes: None,
                now=T0,
            )

    def test_multiple_changes_are_ordered_and_conflict_rolls_back_the_batch(self):
        first_job = self.job()
        first_request = self.change(key="existing")
        first = publish_job_result(
            job_id=first_job.id, lease_token=first_job.lease_token,
            expected_input_version="input-v1", changes=(first_request,),
            persist=self.persist_fixture, now=T0,
        )

        second_job = self.job()
        fresh = self.change(
            key="fresh", resource_id="item-two", version_id="item-two-v1"
        )
        with self.assertRaises(PublicationConflictError):
            publish_job_result(
                job_id=second_job.id, lease_token=second_job.lease_token,
                expected_input_version="input-v1", changes=(fresh, first_request),
                persist=self.persist_fixture, now=T0,
            )
        self.assertEqual(get_job(second_job.id).state, "running")
        with sqlite3.connect(self.path) as db:
            keys = [row[0] for row in db.execute(
                "SELECT idempotency_key FROM change_log ORDER BY seq"
            )]
        self.assertEqual(keys, ["existing"])

        third_job = self.job()
        third_changes = (
            self.change(key="batch-a", resource_id="a", version_id="a-v1"),
            self.change(key="batch-b", resource_id="b", version_id="b-v1"),
        )
        batch = publish_job_result(
            job_id=third_job.id, lease_token=third_job.lease_token,
            expected_input_version="input-v1", changes=third_changes,
            persist=self.persist_fixture, now=T0,
        )
        self.assertEqual(
            [change.seq for change in batch.changes],
            [first.changes[0].seq + 1, first.changes[0].seq + 2],
        )

    def test_uncommitted_publication_is_not_visible(self):
        job = self.job()
        callback_started = threading.Event()
        release_callback = threading.Event()
        result_holder = []
        errors = []

        def persist(db, changes):
            self.persist_fixture(db, changes)
            callback_started.set()
            if not release_callback.wait(timeout=5):
                raise TimeoutError("test callback was not released")

        def publish():
            try:
                result_holder.append(publish_job_result(
                    job_id=job.id, lease_token=job.lease_token,
                    expected_input_version="input-v1", changes=(self.change(),),
                    persist=persist, now=T0,
                ))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=publish)
        thread.start()
        self.assertTrue(callback_started.wait(timeout=5))
        reader = sqlite3.connect(self.path)
        try:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM publication_fixture").fetchone()[0], 0)
            self.assertEqual(reader.execute(
                "SELECT state FROM jobs WHERE id=?", (job.id,)
            ).fetchone()[0], "running")
        finally:
            reader.close()
            release_callback.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result_holder[0].job.state, "succeeded")

    def test_epoch_rotation_fences_old_work_and_scopes_idempotency(self):
        old_job = self.job(logical_key="repeatable-job")
        old_result = publish_job_result(
            job_id=old_job.id, lease_token=old_job.lease_token,
            expected_input_version="input-v1", changes=(self.change(key="repeatable-change"),),
            persist=self.persist_fixture, now=T0,
        )
        old_identity = old_result.dataset
        new_identity = rotate_dataset_epoch(
            expected_epoch=old_identity.epoch,
            reason="isolated restore rehearsal",
            now=T0 + timedelta(seconds=1),
        )
        self.assertEqual(new_identity.dataset_id, old_identity.dataset_id)
        self.assertNotEqual(new_identity.epoch, old_identity.epoch)
        self.assertEqual(new_identity.high_water, 0)
        with self.assertRaises(EpochConflictError):
            rotate_dataset_epoch(
                expected_epoch=old_identity.epoch, reason="stale operator request", now=T0
            )

        new_job = self.job(logical_key="repeatable-job")
        self.assertNotEqual(new_job.id, old_job.id)
        self.assertEqual(new_job.idempotency_key, old_job.idempotency_key)
        self.assertEqual(new_job.dataset_epoch, new_identity.epoch)
        new_result = publish_job_result(
            job_id=new_job.id, lease_token=new_job.lease_token,
            expected_input_version="input-v1", changes=(self.change(key="repeatable-change"),),
            persist=self.persist_fixture, now=T0 + timedelta(seconds=2),
        )
        self.assertGreater(new_result.changes[0].seq, old_result.changes[0].seq)

    def test_epoch_rotation_cancels_unstarted_old_epoch_work(self):
        pending = enqueue_job(
            kind="publish-fixture",
            idempotency_key="pending-before-rotation",
            scheduled_for=T0,
        )
        old_identity = get_dataset_identity()
        new_identity = rotate_dataset_epoch(
            expected_epoch=old_identity.epoch,
            reason="discard queued work after discontinuous restore",
            now=T0,
        )
        self.assertEqual(get_job(pending.id).state, "cancelled")
        self.assertNotEqual(new_identity.epoch, old_identity.epoch)
        self.assertIsNone(claim_job(worker_id="new-epoch-worker", now=T0))

    def test_live_lease_blocks_epoch_rotation(self):
        job = self.job()
        identity = get_dataset_identity()
        with self.assertRaises(EpochRotationBlockedError):
            rotate_dataset_epoch(
                expected_epoch=identity.epoch,
                reason="must not race a worker",
                now=T0 + timedelta(seconds=1),
            )
        self.assertEqual(get_job(job.id).state, "running")
        self.assertEqual(get_dataset_identity().epoch, identity.epoch)

    def test_checkpoints_are_conservative_and_do_not_create_changes(self):
        verified = record_clock_check(
            measured_at=T0,
            recorded_at=T0 + timedelta(seconds=1),
            source="Windows Time Service",
            offset_ms=25.5,
            detail={"command": "fixture"},
        )
        self.assertEqual(verified.status, "verified")
        checkpoint = create_knowledge_checkpoint(
            clock_check_id=verified.id,
            observed_at=T0 + timedelta(seconds=2),
        )
        self.assertEqual(checkpoint.clock_status, "verified")
        self.assertEqual(checkpoint.high_water, 0)

        unknown = create_knowledge_checkpoint(observed_at=T0 + timedelta(seconds=3))
        self.assertEqual(unknown.clock_status, "unknown")
        stale = create_knowledge_checkpoint(
            clock_check_id=verified.id,
            observed_at=T0 + timedelta(seconds=301),
        )
        self.assertEqual(stale.clock_status, "suspect")
        bad_offset = record_clock_check(
            measured_at=T0,
            recorded_at=T0,
            source="fixture",
            offset_ms=1_500,
        )
        self.assertEqual(bad_offset.status, "suspect")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_checkpoints").fetchone()[0], 3)

        with patch.object(config, "ENVIRONMENT_ID", "different-environment"):
            with self.assertRaises(DatasetEnvironmentError):
                record_clock_check(
                    measured_at=T0,
                    recorded_at=T0,
                    source="fixture",
                    offset_ms=0,
                )

    def test_backup_preserves_dataset_identity_and_high_water(self):
        job = self.job()
        result = publish_job_result(
            job_id=job.id,
            lease_token=job.lease_token,
            expected_input_version="input-v1",
            changes=(self.change(),),
            persist=self.persist_fixture,
            now=T0,
        )
        destination = self.path.parent / "publication-backup.db"
        report = db_admin.backup_database(self.path, destination)
        verified = db_admin.verify_database(destination, require_current=True)
        self.assertEqual(report.dataset_id, result.dataset.dataset_id)
        self.assertEqual(report.dataset_epoch, result.dataset.epoch)
        self.assertEqual(report.change_high_water, result.dataset.high_water)
        self.assertEqual(verified.to_dict(), report.to_dict())

    def test_jcs_rejects_non_i_json_numbers_before_writing(self):
        job = self.job()
        with self.assertRaisesRegex(ValueError, "not valid I-JSON"):
            publish_job_result(
                job_id=job.id,
                lease_token=job.lease_token,
                expected_input_version="input-v1",
                changes=(self.change(payload={"invalid": float("nan")}),),
                persist=self.persist_fixture,
                now=T0,
            )
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
