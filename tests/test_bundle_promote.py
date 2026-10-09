import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.db_admin import verify_database
from app.evidence_backup import EvidenceBackupError, create_backup_bundle, promote_backup_bundle
from app.ingest import audit_evidence_payloads, begin_ingest_run, observe_candidate
from app.jobs import claim_job, enqueue_job
from app.publication import ChangeRequest, get_dataset_identity, publish_job_result
from app.runtime_health import write_worker_heartbeat
from app.timeutil import utc_now

SOURCE = {"key": "test", "name": "Test", "channel": "ai", "tier": "media", "type": "rss",
          "url": "https://example.test/feed"}


class BundlePromoteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live = self.root / "data" / "app.db"
        self.live.parent.mkdir()
        for target, name, value in (
            (database, "DB_PATH", self.live), (config, "DB_PATH", self.live),
            (config, "BLOB_PATH", self.root / "data" / "blobs"),
            (config, "BACKUP_PATH", self.root / "data" / "backups"),
            (config, "RUNTIME_PATH", self.root / "data" / "runtime"),
            (config, "PROCESS_ROLE", "maintenance"),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type,url)
                          VALUES(1,'test','Test','ai','media','rss','https://example.test/feed')""")
        self.run_ = begin_ingest_run(SOURCE)
        for n in range(2):
            observe_candidate(self.run_, {"url": f"https://example.test/{n}", "title": f"Evidence {n}"}, ordinal=n)
        self.bundle = Path(create_backup_bundle(self.root / "backups" / "night.bundle")["path"])
        self.jobs = 0

    def urls(self, path=None):
        with closing(sqlite3.connect(path or self.live)) as db:
            return sorted(row[0] for row in db.execute("SELECT external_id FROM raw_records"))

    def source_name(self, path=None):
        with closing(sqlite3.connect(path or self.live)) as db:
            return db.execute("SELECT name FROM sources WHERE id=1").fetchone()[0]

    def publish_change(self):
        self.jobs += 1
        enqueue_job(kind="publish-fixture", idempotency_key=f"job:{self.jobs}", input_version="v1")
        job = claim_job(worker_id="test", lease_seconds=300)
        publish_job_result(
            job_id=job.id, lease_token=job.lease_token, expected_input_version="v1",
            changes=(ChangeRequest(f"change:{self.jobs}", "item", f"item-{self.jobs}", "v1", "create",
                                   {"id": self.jobs}),),
            persist=lambda db, changes: None,
        )

    def blobs(self):
        return sorted((self.root / "data" / "blobs").rglob("[0-9a-f]" * 64))

    def test_the_backup_replaces_newer_data_and_the_epoch_moves_on(self):
        before = get_dataset_identity()
        observe_candidate(self.run_, {"url": "https://example.test/later", "title": "After backup"}, ordinal=2)
        self.publish_change()
        backed_up = self.urls(self.bundle / "database.db")
        self.assertEqual(len(self.urls()), len(backed_up) + 1)
        result = promote_backup_bundle(self.bundle)
        self.assertEqual(self.urls(), backed_up)
        replaced = Path(result["replaced"])
        self.assertEqual(replaced.parent, self.live.parent.resolve() / "replaced")
        self.assertEqual(len(self.urls(replaced / "app.db")), len(backed_up) + 1)
        self.assertTrue(result["epoch"]["rotated"])
        self.assertEqual((result["change_high_water"], result["previous_change_high_water"]), (0, 1))
        after = get_dataset_identity()
        self.assertEqual((after.dataset_id, after.epoch), (before.dataset_id, result["epoch"]["current"]))
        self.assertNotEqual(after.epoch, before.epoch)
        self.assertEqual(verify_database(self.live, require_current=True).state, "current")
        self.assertTrue(audit_evidence_payloads(self.root / "data" / "blobs", self.live).healthy)

    def test_restoring_the_state_consumers_saw_keeps_the_epoch(self):
        before = get_dataset_identity()
        result = promote_backup_bundle(self.bundle)
        self.assertFalse(result["epoch"]["rotated"])
        self.assertEqual(get_dataset_identity().epoch, before.epoch)

    def test_missing_and_damaged_evidence_files_are_put_back(self):
        missing, damaged = self.blobs()[:2]
        missing.unlink()
        damaged.write_bytes(b"bit rot")
        result = promote_backup_bundle(self.bundle)
        self.assertEqual((result["blobs_added"], result["blobs_repaired"]), (1, 1))
        self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), damaged.name)
        self.assertTrue(audit_evidence_payloads(self.root / "data" / "blobs", self.live).healthy)

    def assert_refused(self, message):
        digest = hashlib.sha256(self.live.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EvidenceBackupError, message):
            promote_backup_bundle(self.bundle)
        self.assertEqual(hashlib.sha256(self.live.read_bytes()).hexdigest(), digest)
        self.assertFalse((self.live.parent / "replaced").exists())

    def test_refuses_while_the_worker_runs(self):
        write_worker_heartbeat(worker_id="w", started_at=utc_now(), state="running")
        self.assert_refused("worker is running")
        write_worker_heartbeat(worker_id="w", started_at=utc_now(), state="stopped")
        promote_backup_bundle(self.bundle)

    def test_refuses_while_another_process_has_the_database_open(self):
        holder = subprocess.Popen([sys.executable, "-c", f"""
import sqlite3, sys
c = sqlite3.connect({str(self.live)!r}); c.execute('SELECT 1 FROM sources').fetchall()
print('open', flush=True); sys.stdin.read()
"""], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "open")
        self.assert_refused("open elsewhere")

    def test_refuses_another_environment_or_dataset(self):
        with patch.object(config, "ENVIRONMENT_ID", "another-host"):
            with self.assertRaisesRegex(EvidenceBackupError, "belongs to environment"):
                promote_backup_bundle(self.bundle)
        other = self.root / "other" / "app.db"
        other.parent.mkdir()
        with patch.object(database, "DB_PATH", other), patch.object(config, "DB_PATH", other):
            database.init_schema()
            foreign = create_backup_bundle(self.root / "backups" / "other.bundle")["path"]
        self.bundle = Path(foreign)
        self.assert_refused("different dataset")

    def test_only_the_maintenance_role_promotes(self):
        with patch.object(config, "PROCESS_ROLE", "web"), self.assertRaises(config.RuntimeConfigurationError):
            promote_backup_bundle(self.bundle)

    def test_a_damaged_live_database_is_replaced(self):
        self.live.write_bytes(b"not a database at all" * 100)
        result = promote_backup_bundle(self.bundle)
        self.assertTrue(result["epoch"]["rotated"])
        self.assertIsNone(result["previous_change_high_water"])
        self.assertEqual(verify_database(self.live, require_current=True).state, "current")
        self.assertEqual((Path(result["replaced"]) / "app.db").read_bytes()[:21], b"not a database at all")

    def test_writes_still_in_the_old_wal_stay_with_the_old_database(self):
        # A process that dies without closing leaves committed rows only in the WAL file.
        subprocess.run([sys.executable, "-c", f"""
import os, sqlite3
c = sqlite3.connect({str(self.live)!r}); c.execute('PRAGMA wal_autocheckpoint=0')
c.execute("UPDATE sources SET name='only in wal' WHERE id=1"); c.commit(); os._exit(0)
"""], check=True)
        self.assertTrue(Path(f"{self.live}-wal").stat().st_size)
        result = promote_backup_bundle(self.bundle)
        self.assertEqual(self.source_name(), "Test")
        self.assertEqual(self.source_name(Path(result["replaced"]) / "app.db"), "only in wal")
        self.assertFalse(Path(f"{self.live}-wal").exists() and Path(f"{self.live}-wal").stat().st_size)


class ConnectionCleanupTests(unittest.TestCase):
    def test_admin_operations_close_their_connections(self):
        from app import db_admin
        from app.evidence_backup import _in_use
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            with closing(sqlite3.connect(path)) as db:
                db.executescript(database.SCHEMA)
            db_admin.migrate_database(path)  # verifies, backs up, migrates
            self.assertFalse(_in_use(path))
            db_admin.backup_database(path, Path(folder) / "copy.db")
            db_admin.verify_database(path)
            self.assertFalse(_in_use(path))
            self.assertFalse(_in_use(Path(folder) / "copy.db"))


if __name__ == "__main__":
    unittest.main()
