from __future__ import annotations

"""Validate and atomically publish immutable analysis results."""

import hashlib
import json
from datetime import datetime
from typing import Mapping

from .analysis_runs import AnalysisRunError
from .database import get_db
from .event_relations import _existing_change, _retry_persist_should_not_run, _stable_id
from .publication import ChangeRequest, PublishedChange, PublicationResult, publish_job_result
from .curation_contracts import validate_curation_data

CONFIDENCE_KEYS={"confidence","raw_confidence","calibrated_confidence","intensity"}
RESULT_STATUSES={"valid","needs_review","insufficient_evidence","refused"}
REVIEW_STATUSES={"unreviewed","accepted","rejected","corrected"}
EVIDENCE_STATUSES={"supported","partial","insufficient","refused"}
VALIDATOR_VERSION="analysis-envelope-validator-v1"


def _json(value):
 return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)

def _sha(value): return hashlib.sha256(_json(value).encode()).hexdigest()

def _walk(value,path="$",evidence=None):
 evidence=evidence if evidence is not None else set()
 if isinstance(value,dict):
  for key,item in value.items():
   if key in CONFIDENCE_KEYS and item is not None:
    if isinstance(item,bool) or not isinstance(item,(int,float)) or not 0<=item<=1:
     raise AnalysisRunError(f"{path}.{key} must be between 0 and 1")
   if key=="evidence_id" and item is not None:
    if not isinstance(item,str): raise AnalysisRunError(f"{path}.{key} must be a string")
    evidence.add(item)
   if key in {"evidence_ids","contradicting_evidence_ids"}:
    if not isinstance(item,list) or any(not isinstance(x,str) for x in item):
     raise AnalysisRunError(f"{path}.{key} must be a string list")
    evidence.update(item)
   _walk(item,f"{path}.{key}",evidence)
 elif isinstance(value,list):
  for index,item in enumerate(value): _walk(item,f"{path}[{index}]",evidence)
 return evidence


def _validate_output(run,output:Mapping[str,object],allowed_evidence:set[str]):
 try: clean=json.loads(_json(dict(output)))
 except (TypeError,ValueError,json.JSONDecodeError) as exc: raise AnalysisRunError("analysis output is not valid JSON") from exc
 if clean.get("schema_version")!=run["output_schema_version"]: raise AnalysisRunError("analysis output schema version does not match the run")
 expected={"type":run["subject_type"],"version_id":run["subject_version_id"]}
 if clean.get("subject")!=expected: raise AnalysisRunError("analysis output subject does not match the run")
 status=clean.get("status")
 if status not in RESULT_STATUSES: raise AnalysisRunError("analysis output has an unsupported status")
 if "evidence_ids" not in clean: raise AnalysisRunError("analysis output must declare evidence_ids")
 referenced=_walk(clean)
 unknown=referenced-allowed_evidence
 if unknown: raise AnalysisRunError(f"analysis output references unknown evidence IDs: {sorted(unknown)[:5]}")
 if status in {"valid","needs_review"} and not referenced: raise AnalysisRunError("publishable analysis output requires evidence")
 if "data" not in clean: raise AnalysisRunError("analysis output must declare data")
 clean["data"]=validate_curation_data(task_type=run["task_type"],schema_version=run["output_schema_version"],status=status,data=clean["data"],allowed_evidence=allowed_evidence)
 return clean,sorted(referenced),status


def publish_analysis_result(*,job_id:str,lease_token:str,expected_input_version:str|None,
 run_id:str,attempt_id:str,validated_output:Mapping[str,object],review_status:str,
 evidence_status:str,idempotency_key:str,now:datetime|str|None=None)->PublicationResult:
 if review_status not in REVIEW_STATUSES: raise AnalysisRunError("unsupported review status")
 if evidence_status not in EVIDENCE_STATUSES: raise AnalysisRunError("unsupported evidence status")
 result_id=_stable_id("analysis_result",idempotency_key); publication_id=_stable_id("analysis_publication",idempotency_key)
 with get_db() as db:
  existing=_existing_change(db,publication_id)
  run=db.execute("SELECT * FROM analysis_runs WHERE id=?",(run_id,)).fetchone()
  if not run or run["job_id"]!=job_id: raise AnalysisRunError("analysis run does not belong to the job")
  attempt=db.execute("SELECT * FROM analysis_attempts WHERE id=? AND run_id=?",(attempt_id,run_id)).fetchone()
  if not attempt or attempt["status"] not in {"succeeded","refused"}: raise AnalysisRunError("only a successful or refused attempt can publish a result")
  allowed={row[0] for row in db.execute("SELECT evidence_id FROM analysis_inputs WHERE run_id=? AND evidence_id IS NOT NULL",(run_id,))}
  clean,referenced,result_status=_validate_output(run,validated_output,allowed)
  if result_status=="refused" and evidence_status!="refused": raise AnalysisRunError("refused output requires refused evidence status")
  if result_status=="insufficient_evidence" and evidence_status not in {"insufficient","partial"}: raise AnalysisRunError("insufficient output requires insufficient evidence status")
  request={"run_id":run_id,"attempt_id":attempt_id,"output":clean,"review_status":review_status,"evidence_status":evidence_status}
  request_sha=_sha({"validator_version":VALIDATOR_VERSION,"request":request})
  if existing:
   if existing.payload.get("request_sha256")!=request_sha: raise AnalysisRunError("analysis result retry inputs do not match")
   return publish_job_result(job_id=job_id,lease_token=lease_token,expected_input_version=expected_input_version,changes=(existing,),persist=_retry_persist_should_not_run,now=now)
  current=db.execute("""SELECT v.* FROM analysis_publications p JOIN analysis_publication_versions v ON v.id=p.current_publication_id
   WHERE p.subject_type=? AND p.subject_version_id=? AND p.task_type=?""",(run["subject_type"],run["subject_version_id"],run["task_type"])).fetchone()
  version=current["version"]+1 if current else 1; supersedes=current["id"] if current else None
 payload={"schema_version":"analysis-publication-v1","request_sha256":request_sha,"result_id":result_id,
  "run_id":run_id,"attempt_id":attempt_id,"subject_type":run["subject_type"],"subject_version_id":run["subject_version_id"],
  "task_type":run["task_type"],"output_schema_version":run["output_schema_version"],"result_status":result_status,
  "review_status":review_status,"evidence_status":evidence_status,"referenced_evidence_ids":referenced,"validator_version":VALIDATOR_VERSION}
 change=ChangeRequest(idempotency_key=idempotency_key,resource_type="analysis",resource_id=f"{run['subject_type']}:{run['subject_version_id']}:{run['task_type']}",version_id=publication_id,operation="update" if current else "create",payload=payload)
 def persist(db,changes:tuple[PublishedChange,...]):
  if len(changes)!=1 or changes[0].version_id!=publication_id: raise AnalysisRunError("analysis publication change mismatch")
  if db.execute("SELECT 1 FROM analysis_results WHERE run_id=?",(run_id,)).fetchone(): raise AnalysisRunError("analysis run already has a result")
  fresh=db.execute("SELECT * FROM analysis_attempts WHERE id=? AND run_id=?",(attempt_id,run_id)).fetchone()
  if not fresh or fresh["status"]!=attempt["status"]: raise AnalysisRunError("analysis attempt changed before publication")
  db.execute("""INSERT INTO analysis_results(id,run_id,attempt_id,schema_version,raw_output_ref,raw_output_sha256,
   validated_output_json,validation_report_json,result_status,created_at,available_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
   (result_id,run_id,attempt_id,run["output_schema_version"],fresh["raw_response_ref"],fresh["raw_response_sha256"],_json(clean),
    _json({"validator_version":VALIDATOR_VERSION,"status":"passed","referenced_evidence_ids":referenced}),result_status,fresh["finished_at"],changes[0].available_at))
  db.execute("""INSERT INTO analysis_publication_versions(id,subject_type,subject_version_id,task_type,result_id,version,
   review_status,evidence_status,available_at,supersedes_id,publication_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
   (publication_id,run["subject_type"],run["subject_version_id"],run["task_type"],result_id,version,review_status,evidence_status,changes[0].available_at,supersedes,changes[0].seq))
  db.execute("""INSERT INTO analysis_publications(subject_type,subject_version_id,task_type,current_publication_id)
   VALUES(?,?,?,?) ON CONFLICT(subject_type,subject_version_id,task_type) DO UPDATE SET current_publication_id=excluded.current_publication_id""",
   (run["subject_type"],run["subject_version_id"],run["task_type"],publication_id))
 return publish_job_result(job_id=job_id,lease_token=lease_token,expected_input_version=expected_input_version,changes=(change,),persist=persist,now=now)
