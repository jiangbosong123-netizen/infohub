import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app import db_admin, evidence_backup, ingest
from app.evidence_backup import create_backup_bundle, restore_backup_bundle, verify_backup_bundle
from tests import test_evidence_backup as backup_fixtures


class BackupVerificationReuseTests(unittest.TestCase):
    """Bundles verify each distinct database once and still refuse changed bytes."""

    def setUp(self):
        self.fixture = backup_fixtures.EvidenceBackupTests(
            "test_bundle_contains_exact_referenced_objects_and_verifies")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def full_verifications(self, stack: ExitStack) -> list:
        calls = []
        real = db_admin.verify_database

        def counted(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)
        stack.enter_context(patch.object(db_admin, "verify_database", counted))
        stack.enter_context(patch.object(evidence_backup, "verify_database", counted))
        return calls

    def test_bundle_verifies_the_source_and_the_snapshot_once_each(self):
        target = self.root / "backups" / "once.bundle"
        with ExitStack() as stack:
            calls = self.full_verifications(stack)
            result = create_backup_bundle(target)
        self.assertEqual(len(calls), 2)
        self.assertEqual(verify_backup_bundle(target)["database_sha256"], result["database_sha256"])

    def test_snapshot_bytes_changed_after_verification_are_not_published(self):
        target = self.root / "backups" / "changed.bundle"
        real_audit = evidence_backup.audit_evidence_payloads

        def audit_then_change(root, database_file):
            report = real_audit(root, database_file)
            with open(database_file, "ab") as handle:
                handle.write(b"\0")
            return report
        with patch.object(evidence_backup, "audit_evidence_payloads", audit_then_change):
            with self.assertRaises((db_admin.DatabaseVerificationError,
                                    evidence_backup.EvidenceBackupError)):
                create_backup_bundle(target)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob(".*.tmp")))

    def test_restore_fully_verifies_only_the_source_bundle(self):
        bundle = self.root / "backups" / "restore.bundle"
        create_backup_bundle(bundle)
        with ExitStack() as stack:
            calls = self.full_verifications(stack)
            restored = restore_backup_bundle(bundle, self.root / "restored")
        self.assertEqual(len(calls), 1)
        self.assertEqual(verify_backup_bundle(self.root / "restored")["database_sha256"],
                         restored["database_sha256"])

    def test_backup_database_verifies_once_and_rejects_changed_publication(self):
        with ExitStack() as stack:
            calls = self.full_verifications(stack)
            report = db_admin.backup_database(destination=self.root / "one.db")
        self.assertEqual(len(calls), 1)
        self.assertEqual(report.path, str((self.root / "one.db").resolve()))
        self.assertEqual(report.file_sha256, db_admin._sha256(self.root / "one.db"))
        real_replace = os.replace

        def replace_then_change(source, destination):
            real_replace(source, destination)
            with open(destination, "ab") as handle:
                handle.write(b"\0")
        with patch.object(db_admin.os, "replace", replace_then_change):
            with self.assertRaisesRegex(db_admin.DatabaseVerificationError, "differs from the verified"):
                db_admin.backup_database(destination=self.root / "two.db")


class MigrationCheckCacheTests(unittest.TestCase):
    def test_migration_check_restores_the_connections_page_cache(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(Path(folder) / "app.db")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA cache_size=-1234")
            self.assertTrue(db_admin.apply_migrations(db))
            self.assertEqual(db.execute("PRAGMA cache_size").fetchone()[0], -1234)
            db.close()


class AuditDigestCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backup_fixtures.EvidenceBackupTests(
            "test_bundle_contains_exact_referenced_objects_and_verifies")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_each_object_is_hashed_once_and_every_row_still_counts(self):
        raw = self.fixture.raw
        good = {"payload_ref": raw.payload_ref, "payload_sha256": raw.payload_sha256}
        missing = {"payload_ref": f"sha256/{'0' * 2}/{'0' * 64}", "payload_sha256": "0" * 64}
        rows = [good] * 5 + [missing] * 3
        hashed = []
        real_hash = ingest._hash_file

        def counted(path):
            hashed.append(path)
            return real_hash(path)
        with patch.object(ingest, "_hash_file", counted):
            uncached = ingest._audit_references(rows)
            uncached_calls = len(hashed)
            hashed.clear()
            cached = ingest._audit_references(rows, digests={})
        self.assertEqual(cached, uncached)
        self.assertEqual((cached.records, cached.verified, cached.missing), (8, 5, 3))
        self.assertEqual((uncached_calls, len(hashed)), (5, 1))


if __name__ == "__main__":
    unittest.main()
