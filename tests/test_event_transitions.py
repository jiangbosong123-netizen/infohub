import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import config, database
from app.db_admin import verify_database
from app.event_relations import EventRelationError, publish_event_merge, resolve_event
from app.event_transitions import (
    RetractionEvidence,
    SplitAssignment,
    event_status_at_sequence,
    publish_event_retraction,
    publish_event_split,
)
from app.ingest import begin_ingest_run, observe_candidate
from app.jobs import claim_job, enqueue_job, get_job


UTC = timezone.utc
T0 = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)


class EventTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app.db"
        self.blobs = Path(self.temp.name) / "blobs"
        for item in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
            patch.object(config, "BLOB_PATH", self.blobs),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','ai','media','rss','https://example.com/feed')"""
            )
        self.number = 0
        self.first_document_version, self.first_evidence = self.ingest("first")
        self.second_document_version, self.second_evidence = self.ingest("second")

    def ingest(self, key: str) -> tuple[str, str]:
        source = {
            "key": "fixture", "name": "Fixture", "channel": "ai", "tier": "media",
            "type": "rss", "url": "https://example.com/feed", "interval_minutes": 30,
        }
        candidate = {
            "url": f"https://example.com/{key}", "title": f"Evidence {key}",
            "summary": f"Version-pinned source evidence {key}.",
            "published_at": T0.isoformat(), "observed_at": T0.isoformat(),
            "source_record": {"id": key, "title": f"Evidence {key}"},
            "payload_kind": "feed_entry",
        }
        run = begin_ingest_run(source, started_at=T0.isoformat())
        observation = observe_candidate(run, candidate, ordinal=0, observed_at=T0.isoformat())
        from app.crawler.runner import insert_item

        self.assertTrue(insert_item("fixture", candidate, observation=observation))
        with database.get_db() as db:
            row = db.execute(
                """SELECT document.current_version_id,input.raw_record_id
                   FROM documents AS document
                   JOIN document_versions AS version
                     ON version.id=document.current_version_id
                   JOIN document_version_inputs AS input
                     ON input.version_id=document.current_version_id
                   WHERE version.canonical_url=?""",
                (candidate["url"],),
            ).fetchone()
        return row["current_version_id"], row["raw_record_id"]

    def event(
        self, title: str, evidence: tuple[tuple[str, str], ...] = ()
    ) -> tuple[str, str]:
        self.number += 1
        event_id = f"event_{self.number}_{uuid4().hex}"
        version_id = f"event_version_{self.number}_{uuid4().hex}"
        now = T0
        payload = {"title": title, "event_type": "other", "facts": []}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with database.get_db() as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO events(id,dataset_id,first_seen_at,latest_report_at,status)
                   VALUES(?,?,?,?, 'candidate')""",
                (event_id, dataset_id, now, now),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,time_precision,
                       primary_entities_json,object_entities_json,facts_json,topics_json,
                       knowledge_status,version_sha256,available_at,created_by,method_version
                   ) VALUES(?,?,1,'event-candidate-v1',?,'other','unknown','[]','[]',
                            '[]','[]','unknown',?,?, 'test','test-v1')""",
                (version_id, event_id, title, digest, now),
            )
            db.execute(
                "UPDATE events SET current_version_id=? WHERE id=?", (version_id, event_id)
            )
            for ordinal, (document_version_id, evidence_id) in enumerate(evidence):
                db.execute(
                    """INSERT INTO event_evidence(
                           id,event_version_id,document_version_id,evidence_id,fact_id,role,
                           available_at
                       ) VALUES(?,?,?,?,NULL,'supports',?)""",
                    (
                        f"event_evidence_{self.number}_{ordinal}_{uuid4().hex}", version_id,
                        document_version_id, evidence_id, now,
                    ),
                )
        return event_id, version_id

    def job(self, key: str):
        job = enqueue_job(
            kind="event-maintenance", idempotency_key=f"job:{key}", subject_id=key,
            input_version="events-v1", scheduled_for=T0,
        )
        claimed = claim_job(
            worker_id=f"worker:{key}", lease_seconds=300,
            now=T0 + timedelta(seconds=self.number),
        )
        self.assertEqual(claimed.id, job.id)
        return claimed

    def test_split_is_complete_append_only_and_idempotent(self):
        source_evidence = (
            (self.first_document_version, self.first_evidence),
            (self.second_document_version, self.second_evidence),
        )
        original_id, original_version = self.event("Combined report", source_evidence)
        first_id, first_version = self.event("First event")
        second_id, second_version = self.event("Second event")
        assignments = (
            SplitAssignment(
                self.first_document_version, self.first_evidence, first_id, first_version
            ),
            SplitAssignment(
                self.second_document_version, self.second_evidence, second_id, second_version
            ),
        )
        job = self.job("split")
        result = publish_event_split(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="events-v1", original_event_id=original_id,
            expected_original_version_id=original_version,
            replacements=((first_id, first_version), (second_id, second_version)),
            assignments=assignments, reason="The source evidence describes two events.",
            idempotency_key="event-split:combined", now=T0 + timedelta(seconds=20),
        )
        repeated = publish_event_split(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="events-v1", original_event_id=original_id,
            expected_original_version_id=original_version,
            replacements=((second_id, second_version), (first_id, first_version)),
            assignments=reversed(assignments), reason="The source evidence describes two events.",
            idempotency_key="event-split:combined", now=T0 + timedelta(seconds=21),
        )
        self.assertEqual(result.to_dict(), repeated.to_dict())
        with self.assertRaisesRegex(EventRelationError, "retry inputs"):
            publish_event_split(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version="events-v1", original_event_id=original_id,
                expected_original_version_id=original_version,
                replacements=((first_id, first_version), (second_id, second_version)),
                assignments=assignments, reason="A different explanation.",
                idempotency_key="event-split:combined",
                now=T0 + timedelta(seconds=22),
            )

        sequence = result.changes[0].seq
        with database.get_db() as db:
            event = db.execute(
                "SELECT status,current_version_id FROM events WHERE id=?", (original_id,)
            ).fetchone()
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "event_splits", "event_split_replacements",
                    "event_split_assignments", "change_log",
                )
            }
            before = event_status_at_sequence(db, original_id, sequence - 1)
            after = event_status_at_sequence(db, original_id, sequence)
            resolution = resolve_event(db, original_id)
        self.assertEqual(dict(event), {"status": "split", "current_version_id": original_version})
        self.assertEqual(counts, {
            "event_splits": 1, "event_split_replacements": 2,
            "event_split_assignments": 2, "change_log": 1,
        })
        self.assertEqual((before, after), ("candidate", "split"))
        self.assertEqual(resolution.canonical_id, original_id)
        self.assertEqual(get_job(job.id).state, "succeeded")
        self.assertEqual(verify_database(self.path, require_current=True).integrity, "ok")

    def test_split_rejects_partial_or_undeclared_evidence_without_writes(self):
        evidence = (
            (self.first_document_version, self.first_evidence),
            (self.second_document_version, self.second_evidence),
        )
        original_id, original_version = self.event("Combined", evidence)
        first_id, first_version = self.event("First")
        second_id, second_version = self.event("Second")
        third_id, third_version = self.event("Undeclared")
        job = self.job("invalid-split")
        with self.assertRaisesRegex(EventRelationError, "every original evidence"):
            publish_event_split(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version="events-v1", original_event_id=original_id,
                expected_original_version_id=original_version,
                replacements=((first_id, first_version), (second_id, second_version)),
                assignments=(SplitAssignment(
                    self.first_document_version, self.first_evidence, first_id, first_version
                ),),
                reason="Incomplete review.", idempotency_key="event-split:partial",
                now=T0 + timedelta(seconds=30),
            )
        with self.assertRaisesRegex(EventRelationError, "undeclared replacement"):
            publish_event_split(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version="events-v1", original_event_id=original_id,
                expected_original_version_id=original_version,
                replacements=((first_id, first_version), (second_id, second_version)),
                assignments=(
                    SplitAssignment(
                        self.first_document_version, self.first_evidence, first_id, first_version
                    ),
                    SplitAssignment(
                        self.second_document_version, self.second_evidence,
                        third_id, third_version,
                    ),
                ),
                reason="Invalid review.", idempotency_key="event-split:undeclared",
                now=T0 + timedelta(seconds=31),
            )
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_splits").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM events WHERE id=?", (original_id,)).fetchone()[0],
                "candidate",
            )
        self.assertEqual(get_job(job.id).state, "running")

    def test_retraction_appends_version_and_keeps_old_version_immutable(self):
        event_id, old_version = self.event(
            "Incorrect report", ((self.first_document_version, self.first_evidence),)
        )
        with database.get_db() as db:
            old_row = dict(db.execute(
                "SELECT * FROM event_versions WHERE id=?", (old_version,)
            ).fetchone())
        job = self.job("retraction")
        evidence = (RetractionEvidence(
            self.second_document_version, self.second_evidence, "contradicts"
        ),)
        result = publish_event_retraction(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="events-v1", event_id=event_id,
            expected_event_version_id=old_version, evidence=evidence,
            reason="The issuer explicitly withdrew the report.",
            idempotency_key="event-retraction:incorrect",
            now=T0 + timedelta(seconds=40),
        )
        repeated = publish_event_retraction(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="events-v1", event_id=event_id,
            expected_event_version_id=old_version, evidence=evidence,
            reason="The issuer explicitly withdrew the report.",
            idempotency_key="event-retraction:incorrect",
            now=T0 + timedelta(seconds=41),
        )
        self.assertEqual(result.to_dict(), repeated.to_dict())
        with self.assertRaisesRegex(EventRelationError, "retry inputs"):
            publish_event_retraction(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version="events-v1", event_id=event_id,
                expected_event_version_id=old_version, evidence=evidence,
                reason="A different explanation.",
                idempotency_key="event-retraction:incorrect",
                now=T0 + timedelta(seconds=42),
            )
        new_version = result.changes[0].version_id
        sequence = result.changes[0].seq
        with database.get_db() as db:
            event = db.execute(
                """SELECT status,current_version_id,last_fact_change_at
                   FROM events WHERE id=?""", (event_id,)
            ).fetchone()
            old_after = dict(db.execute(
                "SELECT * FROM event_versions WHERE id=?", (old_version,)
            ).fetchone())
            current = db.execute(
                """SELECT version,previous_version_id,knowledge_status,created_by,
                          method_version,available_at
                   FROM event_versions WHERE id=?""", (new_version,)
            ).fetchone()
            retraction = db.execute(
                "SELECT retracted_event_version_id,publication_seq FROM event_retractions"
            ).fetchone()
            roles = [row[0] for row in db.execute(
                "SELECT role FROM event_evidence WHERE event_version_id=?", (new_version,)
            )]
            before = event_status_at_sequence(db, event_id, sequence - 1)
            after = event_status_at_sequence(db, event_id, sequence)
        self.assertEqual(old_after, old_row)
        self.assertEqual(event["status"], "retracted")
        self.assertEqual(event["current_version_id"], new_version)
        self.assertEqual(event["last_fact_change_at"], current["available_at"])
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["previous_version_id"], old_version)
        self.assertEqual(current["knowledge_status"], "retracted")
        self.assertEqual(current["created_by"], "event_retraction")
        self.assertEqual(current["method_version"], "event-terminal-transitions-v1")
        self.assertEqual(retraction["retracted_event_version_id"], new_version)
        self.assertEqual(retraction["publication_seq"], sequence)
        self.assertEqual(roles, ["contradicts"])
        self.assertEqual((before, after), ("candidate", "retracted"))
        self.assertEqual(verify_database(self.path, require_current=True).integrity, "ok")

    def test_terminal_conflicts_and_bad_retraction_evidence_publish_nothing(self):
        absorbed_id, absorbed_version = self.event("Duplicate")
        survivor_id, survivor_version = self.event("Canonical")
        merge_job = self.job("merge")
        publish_event_merge(
            job_id=merge_job.id, lease_token=merge_job.lease_token,
            expected_input_version="events-v1", absorbed_event_id=absorbed_id,
            survivor_event_id=survivor_id, expected_absorbed_version_id=absorbed_version,
            expected_survivor_version_id=survivor_version,
            evidence_ids=(self.first_evidence,), reason="Reviewed duplicate.",
            idempotency_key="event-merge:terminal-test",
            now=T0 + timedelta(seconds=50),
        )
        retract_job = self.job("invalid-retraction")
        with self.assertRaisesRegex(EventRelationError, "canonical"):
            publish_event_retraction(
                job_id=retract_job.id, lease_token=retract_job.lease_token,
                expected_input_version="events-v1", event_id=absorbed_id,
                expected_event_version_id=absorbed_version,
                evidence=(RetractionEvidence(
                    self.second_document_version, self.second_evidence
                ),),
                reason="Cannot retract a merged alias.",
                idempotency_key="event-retraction:merged",
                now=T0 + timedelta(seconds=51),
            )
        with self.assertRaisesRegex(EventRelationError, "does not belong"):
            publish_event_retraction(
                job_id=retract_job.id, lease_token=retract_job.lease_token,
                expected_input_version="events-v1", event_id=survivor_id,
                expected_event_version_id=survivor_version,
                evidence=(RetractionEvidence(
                    self.first_document_version, self.second_evidence
                ),),
                reason="Mismatched evidence pair.",
                idempotency_key="event-retraction:mismatched",
                now=T0 + timedelta(seconds=52),
            )
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_retractions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
