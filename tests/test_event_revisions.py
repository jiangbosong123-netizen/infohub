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
from app.event_relations import EventRelationError
from app.event_revisions import (
    EventRevision, RevisionEvidence, publish_event_revision,
)
from app.event_transitions import RetractionEvidence, publish_event_retraction
from app.ingest import begin_ingest_run, observe_candidate
from app.jobs import claim_job, enqueue_job, get_job

UTC = timezone.utc
T0 = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)


class EventRevisionTests(unittest.TestCase):
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
            item.start(); self.addCleanup(item.stop)
        database.init_schema()
        with database.get_db() as db:
            db.execute("""INSERT INTO sources(key,name,channel,tier,type,url)
                        VALUES('fixture','Fixture','ai','media','rss','https://example.com/feed')""")
        self.number = 0
        self.doc1, self.raw1 = self.ingest("first")
        self.doc2, self.raw2 = self.ingest("second")

    def ingest(self, key):
        source = {"key":"fixture","name":"Fixture","channel":"ai","tier":"media",
                  "type":"rss","url":"https://example.com/feed","interval_minutes":30}
        candidate = {"url":f"https://example.com/{key}","title":key,"summary":key,
                     "published_at":T0.isoformat(),"observed_at":T0.isoformat(),
                     "source_record":{"id":key},"payload_kind":"feed_entry"}
        run = begin_ingest_run(source, started_at=T0.isoformat())
        observation = observe_candidate(run, candidate, ordinal=0, observed_at=T0.isoformat())
        from app.crawler.runner import insert_item
        self.assertTrue(insert_item("fixture", candidate, observation=observation))
        with database.get_db() as db:
            row = db.execute("""SELECT d.current_version_id,i.raw_record_id
                              FROM documents d JOIN document_versions v ON v.id=d.current_version_id
                              JOIN document_version_inputs i ON i.version_id=v.id
                              WHERE v.canonical_url=?""", (candidate["url"],)).fetchone()
        return row[0], row[1]

    def initial_revision(self):
        return EventRevision(
            schema_version="event-candidate-v1", title="Company reports revenue",
            event_type="earnings", event_time_start="2026-09-17T00:00:00Z",
            event_time_end=None, time_precision="day",
            primary_entities=("entity_company",), object_entities=(),
            facts=({"fact_id":"revenue","metric":"revenue","value":100,"unit":"USD"},),
            topics=("topic_earnings",), knowledge_status="reported",
        )

    def event(self):
        self.number += 1
        event_id, version_id = f"event_{uuid4().hex}", f"event_version_{uuid4().hex}"
        revision = self.initial_revision(); semantic = {
            **revision.__dict__, "primary_entities":list(revision.primary_entities),
            "object_entities":[], "facts":list(revision.facts), "topics":list(revision.topics),
            "created_by":"test", "method_version":"test-v1",
        }
        digest = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",",":")).encode()).hexdigest()
        with database.get_db() as db:
            dataset_id = db.execute("SELECT dataset_id FROM dataset_state WHERE singleton=1").fetchone()[0]
            db.execute("""INSERT INTO events(id,dataset_id,first_seen_at,latest_report_at,status)
                        VALUES(?,?,?,?, 'candidate')""", (event_id,dataset_id,T0,T0))
            db.execute("""INSERT INTO event_versions(
                        id,event_id,version,schema_version,title,event_type,event_time_start,
                        time_precision,primary_entities_json,object_entities_json,facts_json,
                        topics_json,knowledge_status,version_sha256,available_at,created_by,method_version)
                        VALUES(?,?,1,?,?,?,?,?,'[\"entity_company\"]','[]',?,'[\"topic_earnings\"]',
                               'reported',?,?, 'test','test-v1')""",
                       (version_id,event_id,revision.schema_version,revision.title,revision.event_type,
                        revision.event_time_start,revision.time_precision,
                        json.dumps(list(revision.facts), separators=(",",":")),digest,T0))
            db.execute("UPDATE events SET current_version_id=? WHERE id=?", (version_id,event_id))
            db.execute("""INSERT INTO event_evidence(
                        id,event_version_id,document_version_id,evidence_id,fact_id,role,available_at)
                        VALUES(?,?,?,?,?,'supports',?)""",
                       (f"evidence_{uuid4().hex}",version_id,self.doc1,self.raw1,"revenue",T0))
        return event_id, version_id

    def job(self, key):
        job = enqueue_job(kind="event-maintenance", idempotency_key=f"job:{key}",
                          subject_id=key, input_version="events-v1", scheduled_for=T0)
        claimed = claim_job(worker_id=f"worker:{key}", lease_seconds=300,
                            now=T0 + timedelta(seconds=self.number))
        self.assertEqual(claimed.id, job.id)
        return claimed

    def test_fact_revision_appends_complete_auditable_version(self):
        event_id, old_version = self.event()
        revised = EventRevision(**{
            **self.initial_revision().__dict__,
            "facts":({"fact_id":"revenue","metric":"revenue","value":110,"unit":"USD"},),
            "knowledge_status":"confirmed_by_primary",
        })
        evidence = (RevisionEvidence(self.doc2,self.raw2,"supports","revenue"),)
        job = self.job("fact-revision")
        result = publish_event_revision(
            job_id=job.id,lease_token=job.lease_token,expected_input_version="events-v1",
            event_id=event_id,expected_event_version_id=old_version,revision=revised,
            evidence=evidence,revision_kind="correction",reason="Audited filing corrects revenue.",
            idempotency_key="event-revision:revenue",now=T0+timedelta(seconds=20))
        repeated = publish_event_revision(
            job_id=job.id,lease_token=job.lease_token,expected_input_version="events-v1",
            event_id=event_id,expected_event_version_id=old_version,revision=revised,
            evidence=evidence,revision_kind="correction",reason="Audited filing corrects revenue.",
            idempotency_key="event-revision:revenue",now=T0+timedelta(seconds=21))
        self.assertEqual(result.to_dict(), repeated.to_dict())
        new_version = result.changes[0].version_id
        with self.assertRaisesRegex(EventRelationError, "retry inputs"):
            publish_event_revision(
                job_id=job.id,lease_token=job.lease_token,expected_input_version="events-v1",
                event_id=event_id,expected_event_version_id=old_version,revision=revised,
                evidence=evidence,revision_kind="correction",reason="Different reason.",
                idempotency_key="event-revision:revenue",now=T0+timedelta(seconds=22))
        with database.get_db() as db:
            event = db.execute("SELECT * FROM events WHERE id=?",(event_id,)).fetchone()
            versions = db.execute("SELECT * FROM event_versions WHERE event_id=? ORDER BY version",(event_id,)).fetchall()
            audit = db.execute("SELECT * FROM event_revisions").fetchone()
            new_evidence = db.execute("SELECT * FROM event_evidence WHERE event_version_id=?",(new_version,)).fetchall()
        self.assertEqual(len(versions),2)
        self.assertEqual(versions[0]["facts_json"], '[{"fact_id":"revenue","metric":"revenue","value":100,"unit":"USD"}]')
        self.assertEqual(versions[1]["previous_version_id"],old_version)
        self.assertEqual(json.loads(versions[1]["facts_json"])[0]["value"],110)
        self.assertEqual(event["current_version_id"],new_version)
        self.assertEqual(event["last_fact_change_at"],result.changes[0].available_at)
        self.assertEqual(json.loads(audit["changed_fields_json"]),["facts","knowledge_status"])
        self.assertEqual(audit["publication_seq"],result.changes[0].seq)
        self.assertEqual([(r["evidence_id"],r["fact_id"]) for r in new_evidence],[(self.raw2,"revenue")])
        self.assertEqual(get_job(job.id).state,"succeeded")
        self.assertEqual(verify_database(self.path,require_current=True).integrity,"ok")

    def test_knowledge_only_revision_does_not_claim_fact_change(self):
        event_id, old_version = self.event()
        revised = EventRevision(**{**self.initial_revision().__dict__,"knowledge_status":"corroborated"})
        job = self.job("knowledge")
        publish_event_revision(
            job_id=job.id,lease_token=job.lease_token,expected_input_version="events-v1",
            event_id=event_id,expected_event_version_id=old_version,revision=revised,
            evidence=(RevisionEvidence(self.doc2,self.raw2,"supports","revenue"),),
            revision_kind="knowledge_update",reason="Independent source corroborates the fact.",
            idempotency_key="event-revision:knowledge",now=T0+timedelta(seconds=30))
        with database.get_db() as db:
            event = db.execute("SELECT last_fact_change_at FROM events WHERE id=?",(event_id,)).fetchone()
            fields = json.loads(db.execute("SELECT changed_fields_json FROM event_revisions").fetchone()[0])
        self.assertIsNone(event[0]); self.assertEqual(fields,["knowledge_status"])
        self.assertEqual(verify_database(self.path,require_current=True).integrity,"ok")

    def test_noop_bad_fact_evidence_and_terminal_revision_publish_nothing(self):
        event_id, old_version = self.event(); job = self.job("invalid")
        with self.assertRaisesRegex(EventRelationError,"does not change"):
            publish_event_revision(
                job_id=job.id,lease_token=job.lease_token,expected_input_version="events-v1",
                event_id=event_id,expected_event_version_id=old_version,
                revision=self.initial_revision(),evidence=(RevisionEvidence(self.doc2,self.raw2),),
                revision_kind="correction",reason="No change.",idempotency_key="revision:noop",now=T0)
        revised = EventRevision(**{**self.initial_revision().__dict__,"title":"Corrected title"})
        with self.assertRaisesRegex(EventRelationError,"unknown fact"):
            publish_event_revision(
                job_id=job.id,lease_token=job.lease_token,expected_input_version="events-v1",
                event_id=event_id,expected_event_version_id=old_version,revision=revised,
                evidence=(RevisionEvidence(self.doc2,self.raw2,"supports","missing"),),
                revision_kind="correction",reason="Bad evidence.",idempotency_key="revision:bad",now=T0)
        retract_job = self.job("retract")
        publish_event_retraction(
            job_id=retract_job.id,lease_token=retract_job.lease_token,expected_input_version="events-v1",
            event_id=event_id,expected_event_version_id=old_version,
            evidence=(RetractionEvidence(self.doc2,self.raw2),),reason="Withdrawn.",
            idempotency_key="retract:terminal",now=T0+timedelta(seconds=40))
        terminal_version = get_current_version(self.path,event_id)
        later_job = self.job("terminal-revision")
        with self.assertRaisesRegex(EventRelationError,"terminal"):
            publish_event_revision(
                job_id=later_job.id,lease_token=later_job.lease_token,expected_input_version="events-v1",
                event_id=event_id,expected_event_version_id=terminal_version,revision=revised,
                evidence=(RevisionEvidence(self.doc2,self.raw2),),revision_kind="correction",
                reason="Too late.",idempotency_key="revision:terminal",now=T0+timedelta(seconds=41))
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0],0)


def get_current_version(path,event_id):
    with sqlite3_connect(path) as db:
        return db.execute("SELECT current_version_id FROM events WHERE id=?",(event_id,)).fetchone()[0]


def sqlite3_connect(path):
    import sqlite3
    return sqlite3.connect(path)


if __name__ == "__main__": unittest.main()
