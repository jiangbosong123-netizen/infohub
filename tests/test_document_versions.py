import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.crawler import runner
from app.ingest import (
    PayloadIntegrityError,
    begin_ingest_run,
    finish_ingest_run,
    observe_candidate,
)
from app.source_time import parse_source_time


T0 = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)


class DocumentVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db_path = root / "app.db"
        self.blob_path = root / "blobs"
        for item in (
            patch.object(database, "DB_PATH", self.db_path),
            patch.object(config, "DB_PATH", self.db_path),
            patch.object(config, "BLOB_PATH", self.blob_path),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','stock','media','rss','https://example.com/feed')"""
            )
        self.source = {
            "key": "fixture", "name": "Fixture", "channel": "stock",
            "tier": "media", "type": "rss", "url": "https://example.com/feed",
            "interval_minutes": 10,
        }
        self.run_number = 0

    def _candidate(self, title="Original", summary="First", url="https://example.com/a", **values):
        published = values.pop("published", "2026-09-16T07:00:00Z")
        result = {
            "url": url,
            "title": title,
            "summary": summary,
            "published_at": published,
            "observed_at": values.pop("observed_at", T0.isoformat()),
            "source_time_values": [parse_source_time(
                published,
                field_path="entry.published",
                role="published",
                interpretation="fixture publisher timestamp",
                observed_at=T0,
            ).to_dict()],
            "source_record": {"url": url, "title": title, "summary": summary, "published": published},
            "payload_kind": "feed_entry",
        }
        result.update(values)
        return result

    def _ingest(self, candidate, *, observed_at=T0):
        self.run_number += 1
        run = begin_ingest_run(
            self.source,
            started_at=observed_at,
            trace_id=f"trace-{self.run_number}",
        )
        observation = observe_candidate(run, candidate, ordinal=0, observed_at=observed_at)
        inserted = runner.insert_item("fixture", candidate, observation=observation)
        finish_ingest_run(
            run,
            status="succeeded",
            raw_count=1,
            accepted_count=1,
            duplicate_count=0 if inserted else 1,
            rejected_count=0,
            byte_count=observation.size_bytes,
            finished_at=observed_at + timedelta(seconds=1),
        )
        return inserted, observation

    def test_same_locator_change_appends_version_and_updates_legacy_projection(self):
        first, first_observation = self._ingest(self._candidate())
        corrected_at = T0 + timedelta(minutes=5)
        second, second_observation = self._ingest(
            self._candidate(
                title="Corrected", summary="Second",
                published="2026-09-16T06:30:00Z",
                observed_at=corrected_at.isoformat(),
            ),
            observed_at=corrected_at,
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertNotEqual(first_observation.raw_record_id, second_observation.raw_record_id)
        with database.get_db() as db:
            document = db.execute("SELECT * FROM documents").fetchone()
            versions = db.execute(
                "SELECT * FROM document_versions ORDER BY version"
            ).fetchall()
            item = db.execute("SELECT title,summary,published_at FROM items").fetchone()
        self.assertEqual(len(versions), 2)
        self.assertEqual(document["first_seen_at"], "2026-09-16T08:00:00.000000Z")
        self.assertEqual(document["current_version_id"], versions[1]["id"])
        self.assertEqual(versions[1]["previous_version_id"], versions[0]["id"])
        self.assertEqual(versions[1]["correction_kind"], "content_change")
        self.assertEqual(versions[1]["published_at"], "2026-09-16T06:30:00.000000Z")
        self.assertEqual(versions[1]["published_precision"], "second")
        self.assertEqual(versions[1]["time_status"], "parsed")
        self.assertEqual(versions[1]["normalizer_version"], "document-normalizer-v1")
        self.assertEqual(versions[1]["content_origin"], "feed_excerpt")
        self.assertEqual(versions[1]["content_extent"], "excerpt")
        self.assertEqual(versions[1]["extraction_status"], "partial")
        self.assertTrue(versions[1]["time_rule_version"])
        self.assertTrue(versions[1]["tzdb_version"])
        self.assertIsNone(versions[1]["publisher_id"])
        self.assertEqual(tuple(item), ("Corrected", "Second", "2026-09-16T06:30:00.000000Z"))

    def test_repeat_fetch_only_adds_observation(self):
        candidate = self._candidate()
        self._ingest(candidate)
        self._ingest(candidate, observed_at=T0 + timedelta(minutes=10))
        with database.get_db() as db:
            counts = tuple(db.execute(
                """SELECT
                       (SELECT COUNT(*) FROM documents),
                       (SELECT COUNT(*) FROM document_versions),
                       (SELECT COUNT(*) FROM document_version_inputs),
                       (SELECT COUNT(*) FROM raw_records),
                       (SELECT COUNT(*) FROM raw_observations)"""
            ).fetchone())
            last_observed = db.execute(
                "SELECT last_observed_at FROM document_locators"
            ).fetchone()[0]
        self.assertEqual(counts, (1, 1, 1, 1, 2))
        self.assertEqual(last_observed, "2026-09-16T08:10:00.000000Z")

    def test_identical_content_at_distinct_urls_remains_distinct(self):
        self._ingest(self._candidate(url="https://example.com/a"))
        self._ingest(
            self._candidate(url="https://example.com/b"),
            observed_at=T0 + timedelta(minutes=1),
        )
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0], 2)

    def test_updated_only_time_does_not_invent_published_at(self):
        candidate = self._candidate()
        candidate["source_time_values"] = [parse_source_time(
            "2026-09-16T07:30:00Z",
            field_path="entry.updated",
            role="updated",
            interpretation="fixture update timestamp",
            observed_at=T0,
        ).to_dict()]
        candidate["source_record"]["updated"] = candidate["source_record"].pop("published")
        self._ingest(candidate)
        with database.get_db() as db:
            version = db.execute(
                """SELECT published_at,published_time_value_id,published_precision,
                          time_status FROM document_versions"""
            ).fetchone()
        self.assertIsNone(version["published_at"])
        self.assertIsNone(version["published_time_value_id"])
        self.assertEqual(version["published_precision"], "unknown")
        self.assertEqual(version["time_status"], "missing")

    def test_missing_blob_aborts_legacy_and_document_projection(self):
        candidate = self._candidate()
        run = begin_ingest_run(self.source, started_at=T0, trace_id="missing-blob")
        observation = observe_candidate(run, candidate, ordinal=0, observed_at=T0)
        (self.blob_path / observation.payload_ref).unlink()
        with self.assertRaises(PayloadIntegrityError):
            runner.insert_item("fixture", candidate, observation=observation)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_version_rows_and_inputs_are_immutable(self):
        self._ingest(self._candidate())
        with database.get_db() as db:
            version = db.execute("SELECT id FROM document_versions").fetchone()[0]
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE document_versions SET title_original='changed' WHERE id=?",
                    (version,),
                )
        with database.get_db() as db:
            self.assertEqual(
                db.execute("SELECT title_original FROM document_versions").fetchone()[0],
                "Original",
            )

    def test_current_pointer_cannot_cross_document_boundaries(self):
        self._ingest(self._candidate(url="https://example.com/a"))
        self._ingest(
            self._candidate(url="https://example.com/b"),
            observed_at=T0 + timedelta(minutes=1),
        )
        with database.get_db() as db:
            rows = db.execute(
                """SELECT document.id,document.current_version_id
                   FROM documents AS document ORDER BY document.legacy_item_id"""
            ).fetchall()
            with self.assertRaisesRegex(Exception, "must belong"):
                db.execute(
                    "UPDATE documents SET current_version_id=? WHERE id=?",
                    (rows[1]["current_version_id"], rows[0]["id"]),
                )


if __name__ == "__main__":
    unittest.main()
