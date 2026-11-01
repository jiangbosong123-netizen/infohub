import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin, scheduled_backup
from app.evidence_backup import verify_backup_bundle
from app.ingest import begin_ingest_run, observe_candidate
from app.jobs import enqueue_job
from app.scheduled_backup import (
    BackupSpaceError, nightly_bundles, pre_migration_copies, run_nightly_backup,
)
from app.worker import process_one_job

Usage = namedtuple("Usage", "total used free")


class ScheduledBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backups = self.root / "backups"
        for target, name, value in (
            (database, "DB_PATH", self.root / "app.db"), (config, "DB_PATH", self.root / "app.db"),
            (config, "BLOB_PATH", self.root / "blobs"), (config, "BACKUP_PATH", self.backups),
            (config, "RUNTIME_PATH", self.root / "runtime"), (config, "BACKUP_KEEP", 3),
            (config, "BACKUP_RESERVE_MB", 1), (config, "BACKUP_KEEP_PRE_MIGRATION", 2),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def names(self):
        return [entry.name for entry in nightly_bundles()]

    def test_keeps_the_newest_bundles_and_nothing_else_is_touched(self):
        manual = self.backups / "app.manual.20261001T000000.000000Z.bundle"
        pre_migration = self.backups / "app.production.20261001T000000.000000Z.db"
        notes = self.backups / "nightly" / "notes"
        for folder in (manual, notes):
            folder.mkdir(parents=True)
        pre_migration.write_bytes(b"copy")
        results = [run_nightly_backup() for _ in range(4)]
        published = [Path(result["path"]).name for result in results]
        self.assertEqual(self.names(), published[1:])
        self.assertEqual(results[3]["removed"], published[:1])
        self.assertEqual([result["kept"] for result in results], [1, 2, 3, 3])
        self.assertEqual(verify_backup_bundle(results[3]["path"])["status"], "ok")
        self.assertTrue(manual.is_dir() and notes.is_dir() and pre_migration.is_file())

    def test_a_failed_night_deletes_nothing(self):
        for _ in range(3):
            run_nightly_backup()
        before = self.names()
        with patch.object(scheduled_backup, "create_backup_bundle", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                run_nightly_backup()
        self.assertEqual(self.names(), before)

    def test_refuses_before_writing_when_space_is_short(self):
        needed = (self.root / "app.db").stat().st_size + config.BACKUP_RESERVE_MB * scheduled_backup.MB
        with patch.object(shutil, "disk_usage", return_value=Usage(0, 0, needed - 1)):
            with self.assertRaisesRegex(BackupSpaceError, "MB is available"):
                run_nightly_backup()
        self.assertEqual(list((self.backups / "nightly").iterdir()), [])
        with patch.object(shutil, "disk_usage", return_value=Usage(0, 0, needed)):
            self.assertEqual(len([run_nightly_backup()]), 1)

    def test_the_host_disk_behind_a_docker_volume_must_have_the_space(self):
        host = self.root / "host-disk"
        host.mkdir()
        needed = (self.root / "app.db").stat().st_size + config.BACKUP_RESERVE_MB * scheduled_backup.MB

        def usage(path):
            return Usage(0, 0, needed - 1 if Path(path) == host else 10 * needed)

        with patch.object(config, "HOST_DISK_PATH", host), patch.object(shutil, "disk_usage", side_effect=usage):
            with self.assertRaisesRegex(BackupSpaceError, "MB is available"):
                run_nightly_backup()
        self.assertEqual(list((self.backups / "nightly").iterdir()), [])

    def test_the_blob_store_counts_towards_the_space_needed(self):
        blob = config.BLOB_PATH / "sha256" / "ab" / ("ab" * 32)
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"x" * 4096)
        needed = (self.root / "app.db").stat().st_size + config.BACKUP_RESERVE_MB * scheduled_backup.MB
        with patch.object(shutil, "disk_usage", return_value=Usage(0, 0, needed + 4095)):
            with self.assertRaises(BackupSpaceError):
                run_nightly_backup()

    def test_stages_left_by_a_killed_process_are_removed_once_stale(self):
        nightly = self.backups / "nightly"
        stale = nightly / f".app.production.20261001T000000.000000Z.bundle.{'a' * 32}.tmp"
        fresh = nightly / f".app.production.20261009T000000.000000Z.bundle.{'b' * 32}.tmp"
        for folder in (stale, fresh):
            folder.mkdir(parents=True)
        old = time.time() - scheduled_backup.STALE_STAGE_SECONDS - 60
        os.utime(stale, (old, old))
        result = run_nightly_backup()
        self.assertEqual(result["stale_stages_removed"], [stale.name])
        self.assertTrue(fresh.is_dir())

    def test_upgrades_copy_the_live_database_into_their_own_folder(self):
        legacy = self.root / "legacy.db"
        with sqlite3.connect(legacy) as db:
            db.executescript(database.SCHEMA)
        with patch.object(config, "DB_PATH", legacy):
            report = db_admin.migrate_database(legacy)
        self.assertEqual(Path(report.backup_path).parent.name, "pre-migration")
        self.assertEqual([entry.name for entry in pre_migration_copies()], [Path(report.backup_path).name])
        # Copies of any other database stay beside it, as before.
        other = self.root / "other" / "app.db"
        other.parent.mkdir()
        with sqlite3.connect(other) as db:
            db.executescript(database.SCHEMA)
        self.assertEqual(Path(db_admin.migrate_database(other).backup_path).parent, other.parent.resolve() / "backups")

    def test_keeps_the_newest_pre_migration_copies(self):
        folder = self.backups / "pre-migration"
        folder.mkdir(parents=True)
        names = [f"app.production.202610{day:02d}T000000.000000Z.db" for day in (3, 1, 2)]
        for name in names:
            (folder / name).write_bytes(b"copy")
        (folder / f"{names[1]}-shm").write_bytes(b"sidecar")
        (folder / "notes.txt").write_bytes(b"kept")
        oddity = folder / "app.production.20260901T000000.000000Z.db"  # a folder, not a copy
        oddity.mkdir()
        result = run_nightly_backup()
        self.assertEqual(result["pre_migration_removed"], [names[1]])
        self.assertEqual(sorted(entry.name for entry in folder.iterdir()),
                         sorted([names[0], names[2], "notes.txt", oddity.name]))

    def collect(self, *titles):
        with database.get_db() as db:
            db.execute("""INSERT OR IGNORE INTO sources(id,key,name,channel,tier,type,url)
                          VALUES(1,'test','Test','ai','media','rss','https://example.test/feed')""")
        run = begin_ingest_run({"key": "test", "name": "Test", "channel": "ai", "tier": "media",
                                "type": "rss", "url": "https://example.test/feed"})
        for ordinal, title in enumerate(titles):
            observe_candidate(run, {"url": f"https://example.test/{title}", "title": title}, ordinal=ordinal)

    @staticmethod
    def blobs(bundle):
        return {path.name: path for path in (Path(bundle) / "blobs").rglob("[0-9a-f]" * 64)}

    def test_later_nights_link_the_evidence_files_they_share(self):
        self.collect("one", "two")
        first = run_nightly_backup()
        self.assertEqual(first["blobs_linked"], 0)
        self.collect("three")
        second = run_nightly_backup()
        self.assertEqual((second["unique_blobs"], second["blobs_linked"]), (3, 2))
        old, new = self.blobs(first["path"]), self.blobs(second["path"])
        self.assertEqual(sorted(old), sorted(name for name in new if name in old))
        for name, path in old.items():
            self.assertTrue(os.path.samefile(path, new[name]))
            # Never shared with the live store: a bundle stays a copy of it.
            self.assertFalse(os.path.samefile(path, config.BLOB_PATH / "sha256" / name[:2] / name))
        self.assertEqual(verify_backup_bundle(second["path"])["status"], "ok")
        # Removing the oldest bundle leaves the newer ones whole.
        for _ in range(3):
            run_nightly_backup()
        self.assertNotIn(Path(first["path"]).name, self.names())
        self.assertEqual(verify_backup_bundle(self.backups / "nightly" / self.names()[0])["status"], "ok")

    def test_a_damaged_earlier_copy_is_copied_again_from_the_live_store(self):
        self.collect("one", "two")
        first = run_nightly_backup()
        name, damaged = sorted(self.blobs(first["path"]).items())[0]
        damaged.unlink()
        damaged.write_bytes(b"not the evidence")
        second = run_nightly_backup()
        self.assertEqual(second["blobs_linked"], 1)
        self.assertFalse(os.path.samefile(damaged, self.blobs(second["path"])[name]))
        self.assertEqual(verify_backup_bundle(second["path"])["status"], "ok")

    def test_files_the_newest_bundle_holds_need_no_space(self):
        self.collect("one")
        run_nightly_backup()
        blobs = sum(path.stat().st_size for path in config.BLOB_PATH.rglob("*") if path.is_file())
        self.assertGreater(blobs, 0)
        needed = (scheduled_backup._database_bytes(self.root / "app.db")
                  + config.BACKUP_RESERVE_MB * scheduled_backup.MB)
        with patch.object(shutil, "disk_usage", return_value=Usage(0, 0, needed)):
            self.assertEqual(run_nightly_backup()["blobs_linked"], 1)
        # Where hard links do not work, every file is copied and counted again.
        with patch.object(scheduled_backup.os, "link", side_effect=OSError("no links")):
            with patch.object(shutil, "disk_usage", return_value=Usage(0, 0, needed + blobs - 1)):
                with self.assertRaises(BackupSpaceError):
                    run_nightly_backup()
            self.assertEqual(run_nightly_backup()["blobs_linked"], 0)

    def test_the_worker_runs_the_backup_job(self):
        enqueue_job(kind="backup", idempotency_key="backup-test")
        job = process_one_job(worker_id="backup-worker")
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(Path(json.loads(job.result_ref)["path"]).name, self.names()[0])


if __name__ == "__main__":
    unittest.main()
