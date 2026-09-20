import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.evidence_backup import (
    EvidenceBackupError, create_backup_bundle, restore_backup_bundle, verify_backup_bundle,
)
from app.ingest import begin_ingest_run, observe_candidate, verify_payload
from app.report_generation import prepare_report_generation, record_report_response
from app.report_inputs import freeze_calendar_daily
from app.report_query import published_calendar_report
from app.report_versions import publish_structured_report


class EvidenceBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db_path = self.root / "app.db"
        self.blob_path = self.root / "blobs"
        for target, name, value in (
            (database, "DB_PATH", self.db_path),
            (config, "DB_PATH", self.db_path),
            (config, "BLOB_PATH", self.blob_path),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(id,key,name,channel,tier,type,url)
                          VALUES(1,'test','Test','ai','media','rss','https://example.test/feed')""")
            db.execute("""INSERT INTO items(id,source_id,url,title,channel,score,tmt,published_at,fetched_at)
                          VALUES(1,1,'https://example.test/one','Evidence','ai',80,1,
                                 '2026-09-18T12:00:00Z','2026-09-18T12:01:00Z')""")
        self.raw = observe_candidate(
            begin_ingest_run({"key": "test", "name": "Test", "channel": "ai",
                              "tier": "media", "type": "rss", "url": "https://example.test/feed"}),
            {"url": "https://example.test/one", "title": "Evidence"}, ordinal=0,
        )
        snapshot = freeze_calendar_daily("2026-09-18")["snapshot_id"]
        self.snapshot = snapshot
        run = prepare_report_generation(
            snapshot_id=snapshot, provider="local", requested_model="test-model",
            prompt_template_id="daily-v1", prompt_template_text="Template {{ items }}",
            rendered_prompt="Prompt for backup", parameters={"temperature": 0},
            now="2026-09-19T08:00:00Z",
        )
        record_report_response(
            run_id=run["run_id"], response=json.dumps({
                "schema_version": "infohub.report-draft/1.0", "date": "2026-09-18",
                "sections": [{"channel": "ai", "claims": [
                    {"text": "There is one article", "input_ordinals": [0]},
                ]}],
            }), resolved_model="test-model", started_at="2026-09-19T08:00:00Z",
            finished_at="2026-09-19T08:01:00Z",
        )

    def test_bundle_contains_exact_referenced_objects_and_verifies(self):
        target = self.root / "backups" / "one.bundle"
        result = create_backup_bundle(target)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unique_blobs"], 3)
        self.assertEqual(result["evidence"]["raw_records"]["verified"], 1)
        self.assertEqual(result["evidence"]["report_prompts"]["verified"], 1)
        self.assertEqual(result["evidence"]["report_responses"]["verified"], 1)
        self.assertEqual(len(list((target / "blobs").rglob("[0-9a-f]" * 64))), 3)
        self.assertEqual(verify_backup_bundle(target)["database_sha256"], result["database_sha256"])
        self.assertFalse(list(target.parent.glob(".*.tmp")))
        with self.assertRaises(FileExistsError):
            create_backup_bundle(target)

    def test_missing_source_blocks_publication_and_cleans_stage(self):
        (self.blob_path / self.raw.payload_ref).unlink()
        target = self.root / "backups" / "broken.bundle"
        with self.assertRaises(EvidenceBackupError):
            create_backup_bundle(target)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob(".*.tmp")))

    def test_copied_blob_or_manifest_tampering_fails_verification(self):
        target = self.root / "backups" / "tampered.bundle"
        create_backup_bundle(target)
        copied = target / "blobs" / self.raw.payload_ref
        copied.write_bytes(b"tampered")
        with self.assertRaises(EvidenceBackupError):
            verify_backup_bundle(target)
        copied.write_bytes((self.blob_path / self.raw.payload_ref).read_bytes())
        manifest_path = target / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["blob_sha256"] = []
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(EvidenceBackupError):
            verify_backup_bundle(target)

    def test_restore_into_new_directory_keeps_live_data_and_evidence_readable(self):
        publish_structured_report(self.snapshot)
        bundle = self.root / "backups" / "restorable.bundle"
        create_backup_bundle(bundle)
        live_bytes = self.db_path.read_bytes()
        restored = self.root / "recovered" / "data"
        result = restore_backup_bundle(bundle, restored)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["unique_blobs"], 3)
        self.assertEqual(self.db_path.read_bytes(), live_bytes)
        self.assertEqual(verify_payload(self.raw.payload_ref, self.raw.payload_sha256,
                                        restored / "blobs").read_bytes(),
                         verify_payload(self.raw.payload_ref, self.raw.payload_sha256,
                                        self.blob_path).read_bytes())
        with sqlite3.connect(f"file:{restored / 'database.db'}?mode=ro", uri=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_generation_runs").fetchone()[0], 1)
        with patch.object(database, "DB_PATH", restored / "database.db"), patch.object(
            config, "DB_PATH", restored / "database.db"
        ):
            with database.get_db() as restored_db:
                report = published_calendar_report(restored_db, "2026-09-18")
            self.assertEqual(report["mode"], "structured_fallback")
            self.assertEqual(report["citation_count"], 1)
        with self.assertRaises(FileExistsError):
            restore_backup_bundle(bundle, restored)

    def test_tampered_bundle_cannot_create_restore_destination(self):
        bundle = self.root / "backups" / "damaged.bundle"
        create_backup_bundle(bundle)
        (bundle / "blobs" / self.raw.payload_ref).unlink()
        restored = self.root / "recovered" / "damaged"
        with self.assertRaises(EvidenceBackupError):
            restore_backup_bundle(bundle, restored)
        self.assertFalse(restored.exists())


if __name__ == "__main__":
    unittest.main()
