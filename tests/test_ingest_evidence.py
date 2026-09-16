import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.crawler import runner
from app.ingest import (
    IngestEvidenceError,
    PayloadIntegrityError,
    audit_payloads,
    begin_ingest_run,
    finish_ingest_run,
    observe_candidate,
    payload_path,
    store_payload,
    verify_payload,
)
from app.source_time import parse_source_time


T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


class IngestEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database_path = self.root / "app.db"
        self.blob_path = self.root / "blobs"
        patches = (
            patch.object(database, "DB_PATH", self.database_path),
            patch.object(config, "DB_PATH", self.database_path),
            patch.object(config, "BLOB_PATH", self.blob_path),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','stock','media','fixture','https://example.com/feed')"""
            )
        self.source = {
            "key": "fixture",
            "name": "Fixture",
            "channel": "stock",
            "tier": "media",
            "type": "fixture",
            "url": "https://example.com/feed?token=source-secret&lang=en#fragment-secret",
            "interval_minutes": 10,
        }

    def _finish(self, run, **overrides):
        values = dict(
            status="succeeded", raw_count=1, accepted_count=1,
            duplicate_count=0, rejected_count=0, byte_count=100,
        )
        values.update(overrides)
        finish_ingest_run(run, **values)

    def test_same_payload_reuses_cas_but_appends_observations(self):
        candidate = {
            "url": "https://user:password@news.example/item?id=7&token=item-secret",
            "title": "unchanged",
            "summary": "evidence",
            "published_at": "2026-09-16T07:00:00Z",
            "extra": {
                "api_key": "payload-secret", "clientSecret": "camel-secret",
                "safe": "kept",
            },
            "source_time_values": [parse_source_time(
                "2026-09-16T07:00:00Z", field_path="entry.published",
                role="published", interpretation="RSS publisher timestamp",
            ).to_dict()],
        }
        first_run = begin_ingest_run(self.source, started_at=T0, trace_id="trace-one")
        first = observe_candidate(first_run, candidate, ordinal=0, observed_at=T0)
        self._finish(first_run, finished_at=T0 + timedelta(seconds=1))

        second_run = begin_ingest_run(
            self.source, started_at=T0 + timedelta(minutes=10), trace_id="trace-two"
        )
        second = observe_candidate(
            second_run, candidate, ordinal=0, observed_at=T0 + timedelta(minutes=10)
        )
        self._finish(second_run, finished_at=T0 + timedelta(minutes=10, seconds=1))

        self.assertEqual(first.raw_record_id, second.raw_record_id)
        self.assertFalse(second.new_record)
        stored = verify_payload(first.payload_ref, first.payload_sha256).read_text("utf-8")
        self.assertNotIn("source-secret", stored)
        self.assertNotIn("item-secret", stored)
        self.assertNotIn("payload-secret", stored)
        self.assertNotIn("camel-secret", stored)
        self.assertNotIn("user:password", stored)
        self.assertEqual(json.loads(stored)["extra"]["api_key"], "[redacted]")
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_config_versions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_time_values").fetchone()[0], 1)
            row = db.execute(
                "SELECT payload_kind,retention_class,external_id FROM raw_records"
            ).fetchone()
        self.assertEqual(row["payload_kind"], "generated_metadata")
        self.assertEqual(row["retention_class"], "private-metadata")
        self.assertNotIn("item-secret", row["external_id"])
        with database.get_db() as db:
            source_config = db.execute(
                "SELECT config_json FROM source_config_versions"
            ).fetchone()[0]
            run_epoch = db.execute("SELECT dataset_epoch FROM ingest_runs LIMIT 1").fetchone()[0]
            current_epoch = db.execute(
                "SELECT current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
        self.assertNotIn("source-secret", source_config)
        self.assertNotIn("fragment-secret", source_config)
        self.assertEqual(run_epoch, current_epoch)

    def test_changed_candidate_at_same_locator_creates_new_immutable_record(self):
        original = {
            "url": "https://example.com/correction",
            "title": "original title",
            "summary": "first statement",
        }
        corrected = {**original, "title": "corrected title", "summary": "corrected statement"}
        first_run = begin_ingest_run(self.source, started_at=T0)
        first = observe_candidate(first_run, original, ordinal=0, observed_at=T0)
        self._finish(first_run, finished_at=T0 + timedelta(seconds=1))
        second_run = begin_ingest_run(self.source, started_at=T0 + timedelta(minutes=1))
        second = observe_candidate(
            second_run, corrected, ordinal=0, observed_at=T0 + timedelta(minutes=1)
        )
        self._finish(second_run, finished_at=T0 + timedelta(minutes=1, seconds=1))

        self.assertNotEqual(first.raw_record_id, second.raw_record_id)
        self.assertNotEqual(first.payload_sha256, second.payload_sha256)
        with database.get_db() as db:
            rows = db.execute(
                "SELECT external_id,payload_sha256 FROM raw_records ORDER BY observed_at"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["external_id"], rows[1]["external_id"])

    def test_source_record_identity_ignores_projection_and_versions_time_rules(self):
        source_record = {
            "id": "entry-7", "published": "2026-09-16T07:00:00Z",
            "callback_url": "https://api.example/callback?token=raw-secret&lang=en",
        }
        first_time = parse_source_time(
            source_record["published"], field_path="published", role="published",
            interpretation="fixture timestamp",
        ).to_dict()
        first_run = begin_ingest_run(self.source, started_at=T0)
        first = observe_candidate(first_run, {
            "url": "https://example.com/raw-entry", "title": "first projection",
            "source_record": source_record, "payload_kind": "api_record",
            "source_time_values": [first_time],
        }, ordinal=0, observed_at=T0)
        self._finish(first_run, byte_count=first.size_bytes, finished_at=T0 + timedelta(seconds=1))

        second_time = {**first_time, "rule_version": "source-time-v2-test"}
        second_run = begin_ingest_run(self.source, started_at=T0 + timedelta(minutes=1))
        second = observe_candidate(second_run, {
            "url": "https://example.com/raw-entry", "title": "changed projection",
            "source_record": source_record, "payload_kind": "api_record",
            "source_time_values": [second_time],
        }, ordinal=0, observed_at=T0 + timedelta(minutes=1))
        self._finish(second_run, byte_count=second.size_bytes, finished_at=T0 + timedelta(minutes=1, seconds=1))

        self.assertEqual(first.raw_record_id, second.raw_record_id)
        payload = json.loads(verify_payload(first.payload_ref, first.payload_sha256).read_text("utf-8"))
        self.assertEqual(payload["source_record"]["id"], source_record["id"])
        self.assertNotIn("raw-secret", json.dumps(payload))
        self.assertNotIn("first projection", json.dumps(payload))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_time_values").fetchone()[0], 2)
            kinds = db.execute("SELECT payload_kind FROM raw_records").fetchone()[0]
        self.assertEqual(kinds, "api_record")

    def test_corrupt_or_escaping_cas_reference_is_rejected(self):
        digest, reference = store_payload(b"trusted")
        target = payload_path(digest)
        target.write_bytes(b"corrupt")
        with self.assertRaises(PayloadIntegrityError):
            store_payload(b"trusted")
        with self.assertRaises(PayloadIntegrityError):
            verify_payload("../outside", digest)
        self.assertEqual(reference, f"sha256/{digest[:2]}/{digest}")

    def test_full_payload_audit_distinguishes_corrupt_and_missing_objects(self):
        run = begin_ingest_run(self.source, started_at=T0)
        observed = observe_candidate(
            run,
            {"url": "https://example.com/audit", "title": "audit fixture"},
            ordinal=0,
            observed_at=T0,
        )
        self._finish(run, byte_count=observed.size_bytes, finished_at=T0 + timedelta(seconds=1))
        target = payload_path(observed.payload_sha256)
        target.write_bytes(b"corrupt")
        corrupt = audit_payloads()
        self.assertEqual((corrupt.records, corrupt.verified, corrupt.corrupt), (1, 0, 1))
        target.unlink()
        missing = audit_payloads()
        self.assertEqual((missing.records, missing.verified, missing.missing), (1, 0, 1))

    def test_missing_evidence_prevents_legacy_item_publication(self):
        raw = {
            "url": "https://example.com/not-published",
            "title": "must retain evidence first",
            "published_at": "2026-09-16T07:00:00Z",
        }
        with patch.dict(runner.FETCHERS, {"fixture": lambda _source: [raw]}), patch.object(
            runner, "observe_candidate", side_effect=PayloadIntegrityError("disk failure")
        ):
            inserted, ok, message = runner.run_source(self.source)
        self.assertEqual((inserted, ok), (0, False))
        self.assertIn("PayloadIntegrityError", message)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
            run = db.execute(
                "SELECT status,raw_count,accepted_count,rejected_count FROM ingest_runs"
            ).fetchone()
        self.assertEqual(tuple(run), ("failed", 1, 0, 1))

    def test_runner_preserves_connector_observation_time(self):
        raw = {
            "url": "https://example.com/observed-at-source",
            "title": "connector timestamp survives the batch",
            "published_at": "2026-09-16T07:00:00Z",
            "observed_at": "2026-09-16T08:00:00Z",
        }
        with patch.dict(runner.FETCHERS, {"fixture": lambda _source: [raw]}):
            inserted, ok, _message = runner.run_source(self.source)
        self.assertEqual((inserted, ok), (1, True))
        with database.get_db() as db:
            observed_at = db.execute("SELECT observed_at FROM raw_observations").fetchone()[0]
        self.assertEqual(observed_at, "2026-09-16T08:00:00.000000Z")

    def test_finished_run_is_immutable_and_config_changes_are_versioned(self):
        first_run = begin_ingest_run(self.source, started_at=T0)
        self._finish(first_run, finished_at=T0 + timedelta(seconds=1))
        with self.assertRaises(IngestEvidenceError):
            self._finish(first_run, finished_at=T0 + timedelta(seconds=2))
        changed = {**self.source, "interval_minutes": 20}
        second_run = begin_ingest_run(changed, started_at=T0 + timedelta(minutes=1))
        self._finish(
            second_run, status="skipped", raw_count=0, accepted_count=0,
            byte_count=0, finished_at=T0 + timedelta(minutes=1, seconds=1),
        )
        with database.get_db() as db:
            versions = db.execute(
                "SELECT version FROM source_config_versions ORDER BY version"
            ).fetchall()
        self.assertEqual([row[0] for row in versions], [1, 2])

    def test_finished_run_rejects_inconsistent_counters(self):
        run = begin_ingest_run(self.source, started_at=T0)
        with self.assertRaises(ValueError):
            finish_ingest_run(
                run, status="succeeded", raw_count=2, accepted_count=1,
                duplicate_count=0, rejected_count=0, byte_count=0,
            )

    def test_database_triggers_block_evidence_mutation_and_deletion(self):
        run = begin_ingest_run(self.source, started_at=T0)
        observed = observe_candidate(
            run, {
                "url": "https://example.com/immutable", "title": "original",
                "source_time_values": [parse_source_time(
                    "2026-09-16T07:00:00Z", field_path="entry.published",
                    role="published", interpretation="fixture timestamp",
                ).to_dict()],
            },
            ordinal=0, observed_at=T0,
        )
        self._finish(run, byte_count=observed.size_bytes, finished_at=T0 + timedelta(seconds=1))
        statements = (
            ("UPDATE raw_records SET external_id='changed' WHERE id=?", observed.raw_record_id),
            ("DELETE FROM raw_observations WHERE raw_record_id=?", observed.raw_record_id),
            ("UPDATE source_time_values SET status='invalid' WHERE raw_record_id=?", observed.raw_record_id),
            ("UPDATE source_config_versions SET version=99 WHERE id=?", run.config_version_id),
            ("DELETE FROM ingest_runs WHERE id=?", run.id),
        )
        for sql, identifier in statements:
            with self.assertRaises(sqlite3.IntegrityError), database.get_db() as db:
                db.execute(sql, (identifier,))


if __name__ == "__main__":
    unittest.main()
