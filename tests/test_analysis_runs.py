import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.analysis_runs import AnalysisInput, AnalysisRunError, prepare_analysis_run
from app.ingest import begin_ingest_run, observe_candidate
from app.jobs import claim_job, enqueue_job

T0=datetime(2026,9,17,16,0,tzinfo=timezone.utc)
SHA="a"*64

class AnalysisRunTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
  self.path=Path(self.temp.name)/"app.db"; self.blobs=Path(self.temp.name)/"blobs"
  for item in (patch.object(database,"DB_PATH",self.path),patch.object(config,"DB_PATH",self.path),patch.object(config,"BLOB_PATH",self.blobs)):
   item.start(); self.addCleanup(item.stop)
  database.init_schema()
  with database.get_db() as db:
   db.execute("INSERT INTO sources(key,name,channel,tier,type,url) VALUES('fixture','Fixture','ai','media','rss','https://example.com/feed')")
  source={"key":"fixture","name":"Fixture","channel":"ai","tier":"media","type":"rss","url":"https://example.com/feed","interval_minutes":30}
  candidate={"url":"https://example.com/a","title":"A","summary":"Evidence","published_at":T0.isoformat(),"observed_at":T0.isoformat(),"source_record":{"id":"a"},"payload_kind":"feed_entry"}
  run=begin_ingest_run(source,started_at=T0.isoformat()); observation=observe_candidate(run,candidate,ordinal=0,observed_at=T0.isoformat())
  from app.crawler.runner import insert_item
  self.assertTrue(insert_item("fixture",candidate,observation=observation))
  with database.get_db() as db:
   row=db.execute("SELECT d.current_version_id,i.raw_record_id FROM documents d JOIN document_version_inputs i ON i.version_id=d.current_version_id").fetchone()
  self.doc,self.raw=row[0],row[1]

 def job(self,key):
  job=enqueue_job(kind="analysis",idempotency_key=f"job:{key}",subject_id=self.doc,input_version=self.doc,scheduled_for=T0)
  return claim_job(worker_id="analysis-worker",lease_seconds=300,now=T0)

 def prepare(self,job,**changes):
  values=dict(job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,
   subject_type="document",subject_version_id=self.doc,task_type="summarization",
   output_schema_version="summary/1.0",inputs=(AnalysisInput("primary",self.doc,None,self.raw),),
   provider="fixture",requested_model="fixture-v1",prompt_template_id="summary-v1",
   prompt_sha256=SHA,rendered_input_ref="cas://rendered/a",rendered_input_sha256=SHA,
   pipeline_version="pipeline-test",parameters={"temperature":0},idempotency_key="analysis:a",now=T0)
  values.update(changes); return prepare_analysis_run(**values)

 def test_manifest_is_version_pinned_immutable_and_idempotent(self):
  job=self.job("valid"); first=self.prepare(job); repeated=self.prepare(job)
  self.assertEqual(first,repeated)
  with self.assertRaisesRegex(AnalysisRunError,"retry inputs"):
   self.prepare(job,requested_model="fixture-v2")
  with database.get_db() as db:
   run=db.execute("SELECT * FROM analysis_runs").fetchone(); inputs=db.execute("SELECT * FROM analysis_inputs").fetchall()
   with self.assertRaisesRegex(Exception,"immutable"):
    db.execute("UPDATE analysis_runs SET provider='changed'")
  self.assertEqual(run["input_manifest_sha256"],first.input_manifest_sha256)
  self.assertEqual(len(inputs),1); self.assertEqual(inputs[0]["evidence_id"],self.raw)

 def test_bad_evidence_and_missing_subject_publish_nothing(self):
  job=self.job("invalid")
  with self.assertRaisesRegex(AnalysisRunError,"does not belong"):
   self.prepare(job,inputs=(AnalysisInput("primary",self.doc,None,"missing"),),idempotency_key="analysis:bad")
  with self.assertRaisesRegex(AnalysisRunError,"subject version"):
   self.prepare(job,subject_version_id="missing",idempotency_key="analysis:missing")
  with database.get_db() as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0],0)

 def test_missing_event_subject_is_rejected(self):
  job=self.job("closure")
  with self.assertRaisesRegex(AnalysisRunError,"subject version does not exist"):
   self.prepare(job,inputs=(AnalysisInput("primary",self.doc,None,self.raw),),subject_type="event",subject_version_id="missing-event",idempotency_key="analysis:closure")

if __name__=="__main__": unittest.main()
