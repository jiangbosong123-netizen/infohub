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
from app.event_relations import (
    EventRelationError,
    publish_event_merge,
    publish_event_relation,
    resolve_event,
)
from app.ingest import begin_ingest_run, observe_candidate
from app.jobs import claim_job, enqueue_job, get_job
from app.timeutil import utc_now


UTC = timezone.utc
T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class EventRelationTests(unittest.TestCase):
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
        source = {
            "key": "fixture", "name": "Fixture", "channel": "ai", "tier": "media",
            "type": "rss", "url": "https://example.com/feed", "interval_minutes": 30,
        }
        candidate = {
            "url": "https://example.com/evidence", "title": "Source evidence",
            "summary": "Evidence for a reviewed event relationship.",
            "published_at": T0.isoformat(), "observed_at": T0.isoformat(),
            "source_record": {"id": "evidence", "title": "Source evidence"},
            "payload_kind": "feed_entry",
        }
        run = begin_ingest_run(source, started_at=T0.isoformat())
        observation = observe_candidate(run, candidate, ordinal=0, observed_at=T0.isoformat())
        from app.crawler.runner import insert_item

        self.assertTrue(insert_item("fixture", candidate, observation=observation))
        with database.get_db() as db:
            self.evidence_id = db.execute("SELECT id FROM raw_records").fetchone()[0]
        self.number = 0

    def event(self, title: str) -> tuple[str, str]:
        self.number += 1
        event_id = f"event_{self.number}_{uuid4().hex}"
        version_id = f"event_version_{self.number}_{uuid4().hex}"
        now = utc_now()
        payload = {"title": title, "event_type": "other", "facts": []}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with database.get_db() as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO events(
                       id,dataset_id,first_seen_at,latest_report_at,status
                   ) VALUES(?,?,?,?, 'candidate')""",
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
        return event_id, version_id

    def job(self, key: str):
        job = enqueue_job(
            kind="event-maintenance", idempotency_key=f"job:{key}",
            subject_id=key, input_version="events-v1", scheduled_for=T0,
        )
        claimed = claim_job(
            worker_id=f"worker:{key}", lease_seconds=300,
            now=T0 + timedelta(seconds=self.number),
        )
        self.assertEqual(claimed.id, job.id)
        return claimed

    def test_relation_supersession_is_append_only_and_observable(self):
        source_id, source_version = self.event("Company announces a transaction")
        target_id, target_version = self.event("Company denies the transaction")
        first_job = self.job("relation-one")
        first = publish_event_relation(
            job_id=first_job.id, lease_token=first_job.lease_token,
            expected_input_version="events-v1", from_event_id=target_id,
            to_event_id=source_id, expected_from_version_id=target_version,
            expected_to_version_id=source_version, relation="denies",
            evidence_ids=(self.evidence_id,), reason="The source explicitly denies it.",
            idempotency_key="event-relation:denial", now=T0 + timedelta(seconds=20),
        )
        repeated = publish_event_relation(
            job_id=first_job.id, lease_token=first_job.lease_token,
            expected_input_version="events-v1", from_event_id=target_id,
            to_event_id=source_id, expected_from_version_id=target_version,
            expected_to_version_id=source_version, relation="denies",
            evidence_ids=(self.evidence_id,), reason="The source explicitly denies it.",
            idempotency_key="event-relation:denial", now=T0 + timedelta(seconds=21),
        )
        self.assertEqual(first.to_dict(), repeated.to_dict())
        with self.assertRaisesRegex(EventRelationError, "retry inputs"):
            publish_event_relation(
                job_id=first_job.id, lease_token=first_job.lease_token,
                expected_input_version="events-v1", from_event_id=target_id,
                to_event_id=source_id, expected_from_version_id=target_version,
                expected_to_version_id=source_version, relation="denies",
                evidence_ids=(self.evidence_id,), reason="A different reason.",
                idempotency_key="event-relation:denial",
                now=T0 + timedelta(seconds=22),
            )
        first_relation_id = first.changes[0].version_id

        second_job = self.job("relation-two")
        publish_event_relation(
            job_id=second_job.id, lease_token=second_job.lease_token,
            expected_input_version="events-v1", from_event_id=target_id,
            to_event_id=source_id, expected_from_version_id=target_version,
            expected_to_version_id=source_version, relation="corrects",
            evidence_ids=(self.evidence_id,), reason="A later report corrects the denial.",
            idempotency_key="event-relation:correction",
            supersedes_relation_id=first_relation_id,
            now=T0 + timedelta(seconds=30),
        )
        with database.get_db() as db:
            rows = db.execute(
                """SELECT relation,supersedes_relation_id,publication_seq
                   FROM event_relations ORDER BY publication_seq"""
            ).fetchall()
            current = db.execute(
                """SELECT relation FROM event_relations AS relation
                   WHERE NOT EXISTS(
                       SELECT 1 FROM event_relations AS newer
                       WHERE newer.supersedes_relation_id=relation.id
                   )"""
            ).fetchone()[0]
            changes = db.execute(
                "SELECT operation,version_id FROM change_log ORDER BY seq"
            ).fetchall()
        self.assertEqual([row["relation"] for row in rows], ["denies", "corrects"])
        self.assertIsNone(rows[0]["supersedes_relation_id"])
        self.assertEqual(rows[1]["supersedes_relation_id"], first_relation_id)
        self.assertEqual(current, "corrects")
        self.assertEqual([row["operation"] for row in changes], ["update", "update"])
        self.assertEqual(verify_database(self.path, require_current=True).integrity, "ok")

    def test_merge_chain_resolves_old_ids_and_rejects_cycles(self):
        event_a, version_a = self.event("A")
        event_b, version_b = self.event("B")
        event_c, version_c = self.event("C")
        first_job = self.job("merge-a-b")
        first = publish_event_merge(
            job_id=first_job.id, lease_token=first_job.lease_token,
            expected_input_version="events-v1", absorbed_event_id=event_a,
            survivor_event_id=event_b, expected_absorbed_version_id=version_a,
            expected_survivor_version_id=version_b, evidence_ids=(self.evidence_id,),
            reason="Reviewed duplicate event.", idempotency_key="event-merge:a-b",
            now=T0 + timedelta(seconds=40),
        )
        repeated = publish_event_merge(
            job_id=first_job.id, lease_token=first_job.lease_token,
            expected_input_version="events-v1", absorbed_event_id=event_a,
            survivor_event_id=event_b, expected_absorbed_version_id=version_a,
            expected_survivor_version_id=version_b, evidence_ids=(self.evidence_id,),
            reason="Reviewed duplicate event.", idempotency_key="event-merge:a-b",
            now=T0 + timedelta(seconds=41),
        )
        self.assertEqual(first.to_dict(), repeated.to_dict())

        second_job = self.job("merge-b-c")
        publish_event_merge(
            job_id=second_job.id, lease_token=second_job.lease_token,
            expected_input_version="events-v1", absorbed_event_id=event_b,
            survivor_event_id=event_c, expected_absorbed_version_id=version_b,
            expected_survivor_version_id=version_c, evidence_ids=(self.evidence_id,),
            reason="Second reviewed duplicate event.", idempotency_key="event-merge:b-c",
            now=T0 + timedelta(seconds=50),
        )
        with database.get_db() as db:
            resolution = resolve_event(db, event_a)
            statuses = dict(db.execute("SELECT id,status FROM events"))
            version_count = db.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0]
        self.assertEqual(resolution.chain, (event_a, event_b, event_c))
        self.assertEqual(resolution.canonical_id, event_c)
        self.assertEqual(statuses[event_a], "merged")
        self.assertEqual(statuses[event_b], "merged")
        self.assertEqual(statuses[event_c], "candidate")
        self.assertEqual(version_count, 3)

        cycle_job = self.job("merge-c-a")
        with self.assertRaisesRegex(EventRelationError, "cycle"):
            publish_event_merge(
                job_id=cycle_job.id, lease_token=cycle_job.lease_token,
                expected_input_version="events-v1", absorbed_event_id=event_c,
                survivor_event_id=event_a, expected_absorbed_version_id=version_c,
                expected_survivor_version_id=version_c,
                evidence_ids=(self.evidence_id,), reason="Invalid cycle.",
                idempotency_key="event-merge:c-a", now=T0 + timedelta(seconds=60),
            )
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_merges").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 2)
        self.assertEqual(get_job(cycle_job.id).state, "running")
        self.assertEqual(verify_database(self.path, require_current=True).integrity, "ok")

    def test_missing_evidence_and_stale_versions_publish_nothing(self):
        event_a, version_a = self.event("A")
        event_b, version_b = self.event("B")
        missing_job = self.job("merge-missing-evidence")
        with self.assertRaisesRegex(EventRelationError, "evidence"):
            publish_event_merge(
                job_id=missing_job.id, lease_token=missing_job.lease_token,
                expected_input_version="events-v1", absorbed_event_id=event_a,
                survivor_event_id=event_b, expected_absorbed_version_id=version_a,
                expected_survivor_version_id=version_b, evidence_ids=("missing",),
                reason="Cannot prove this merge.", idempotency_key="event-merge:missing",
                now=T0 + timedelta(seconds=70),
            )
        stale_job = self.job("relation-stale")
        with self.assertRaisesRegex(EventRelationError, "changed"):
            publish_event_relation(
                job_id=stale_job.id, lease_token=stale_job.lease_token,
                expected_input_version="events-v1", from_event_id=event_a,
                to_event_id=event_b, expected_from_version_id="stale-version",
                expected_to_version_id=version_b, relation="related_to",
                evidence_ids=(self.evidence_id,), reason="Stale decision.",
                idempotency_key="event-relation:stale",
                now=T0 + timedelta(seconds=80),
            )
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_merges").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_relations").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
