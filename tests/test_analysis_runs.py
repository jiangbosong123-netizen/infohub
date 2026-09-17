import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.analysis_runs import AnalysisInput, AnalysisRunError, prepare_analysis_run
from app.analysis_attempts import authorize_attempt, record_attempt, register_budget_policy
from app.analysis_results import publish_analysis_result
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

 def policy(self,daily=1000,per_attempt=600):
  return register_budget_policy(provider="fixture",daily_limit_microusd=daily,per_attempt_limit_microusd=per_attempt,effective_from=T0,idempotency_key=f"policy:{daily}:{per_attempt}",now=T0)

 def test_attempt_audit_is_idempotent_and_budget_reserved(self):
  job=self.job("attempt"); run=self.prepare(job); self.policy()
  auth=authorize_attempt(run_id=run.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,attempt_kind="primary",reserved_cost_microusd=400,idempotency_key="auth:one",now=T0)
  self.assertEqual(auth.decision,"allowed")
  attempt=record_attempt(authorization_id=auth.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,status="succeeded",started_at=T0,finished_at=T0,resolved_model="fixture-resolved",provider_request_id="req-1",input_tokens=10,output_tokens=5,usage_status="reported",cost_microusd=300,pricing_version="fixture-price-v1",raw_response_ref="cas://response/1",raw_response_sha256=SHA,now=T0)
  repeated=record_attempt(authorization_id=auth.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,status="succeeded",started_at=T0,finished_at=T0,resolved_model="fixture-resolved",provider_request_id="req-1",input_tokens=10,output_tokens=5,usage_status="reported",cost_microusd=300,pricing_version="fixture-price-v1",raw_response_ref="cas://response/1",raw_response_sha256=SHA,now=T0)
  self.assertEqual(attempt,repeated)
  with database.get_db() as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_attempts").fetchone()[0],1)
   with self.assertRaisesRegex(Exception,"immutable"): db.execute("UPDATE analysis_attempts SET status='failed'")

 def test_budget_blocks_before_provider_call(self):
  job=self.job("budget"); run=self.prepare(job,idempotency_key="analysis:budget"); self.policy(daily=200,per_attempt=200)
  auth=authorize_attempt(run_id=run.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,attempt_kind="primary",reserved_cost_microusd=300,idempotency_key="auth:blocked",now=T0)
  self.assertEqual(auth.decision,"blocked")
  with self.assertRaisesRegex(AnalysisRunError,"blocked attempt"):
   record_attempt(authorization_id=auth.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,status="failed",started_at=T0,finished_at=T0,now=T0)
  with database.get_db() as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_attempts").fetchone()[0],0)

 def test_repair_requires_invalid_output(self):
  job=self.job("repair"); run=self.prepare(job,idempotency_key="analysis:repair"); self.policy()
  with self.assertRaisesRegex(AnalysisRunError,"first attempt"):
   authorize_attempt(run_id=run.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,attempt_kind="repair",reserved_cost_microusd=10,idempotency_key="auth:repair-bad",now=T0)

 def completed_attempt(self,key="publish"):
  job=self.job(key); run=self.prepare(job,idempotency_key=f"analysis:{key}"); self.policy()
  auth=authorize_attempt(run_id=run.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,attempt_kind="primary",reserved_cost_microusd=100,idempotency_key=f"auth:{key}",now=T0)
  attempt=record_attempt(authorization_id=auth.id,job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,status="succeeded",started_at=T0,finished_at=T0,resolved_model="fixture-v1",usage_status="reported",input_tokens=10,output_tokens=5,cost_microusd=50,pricing_version="v1",raw_response_ref=f"cas://response/{key}",raw_response_sha256=SHA,now=T0)
  return job,run,attempt

 def test_validated_result_publishes_atomically_and_idempotently(self):
  job,run,attempt=self.completed_attempt()
  output={"schema_version":"summary/1.0","subject":{"type":"document","version_id":self.doc},"status":"valid","evidence_ids":[self.raw],"data":{"summary":"Evidence-backed summary","raw_confidence":0.8}}
  result=publish_analysis_result(job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,run_id=run.id,attempt_id=attempt.id,validated_output=output,review_status="unreviewed",evidence_status="supported",idempotency_key="result:publish",now=T0)
  repeated=publish_analysis_result(job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,run_id=run.id,attempt_id=attempt.id,validated_output=output,review_status="unreviewed",evidence_status="supported",idempotency_key="result:publish",now=T0)
  self.assertEqual(result.to_dict(),repeated.to_dict())
  with database.get_db() as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0],1)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_publication_versions").fetchone()[0],1)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0],1)

 def test_unknown_evidence_and_bad_confidence_do_not_publish(self):
  job,run,attempt=self.completed_attempt("invalid-result")
  base={"schema_version":"summary/1.0","subject":{"type":"document","version_id":self.doc},"status":"valid","evidence_ids":[self.raw],"data":{}}
  with self.assertRaisesRegex(AnalysisRunError,"unknown evidence"):
   publish_analysis_result(job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,run_id=run.id,attempt_id=attempt.id,validated_output={**base,"evidence_ids":["fake"]},review_status="unreviewed",evidence_status="supported",idempotency_key="result:fake",now=T0)
  bad={**base,"data":{"confidence":1.2}}
  with self.assertRaisesRegex(AnalysisRunError,"between 0 and 1"):
   publish_analysis_result(job_id=job.id,lease_token=job.lease_token,expected_input_version=self.doc,run_id=run.id,attempt_id=attempt.id,validated_output=bad,review_status="unreviewed",evidence_status="supported",idempotency_key="result:bad-confidence",now=T0)
  with database.get_db() as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0],0)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM change_log").fetchone()[0],0)

if __name__=="__main__": unittest.main()
