from __future__ import annotations

"""Append-only model-attempt authorization, budget and usage audit."""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .analysis_runs import AnalysisRunError, _clean, _time
from .database import get_db
from .event_relations import _stable_id
from .jobs import _current_lease, _validate_input_version


@dataclass(frozen=True)
class AttemptAuthorization:
    id: str
    run_id: str
    attempt_number: int
    decision: str
    reason: str
    reserved_cost_microusd: int


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    run_id: str
    attempt_number: int
    status: str
    cost_microusd: int | None


def register_budget_policy(
    *, provider: str, daily_limit_microusd: int, per_attempt_limit_microusd: int,
    effective_from: datetime | str, idempotency_key: str,
    supersedes_policy_id: str | None = None, now: datetime | str | None = None,
) -> str:
    provider = _clean(provider, "provider")
    if min(daily_limit_microusd, per_attempt_limit_microusd) < 0:
        raise ValueError("budget limits cannot be negative")
    current, effective = _time(now), _time(effective_from)
    policy_id = _stable_id("analysis_budget_policy", idempotency_key)
    with get_db() as db:
        existing = db.execute("SELECT * FROM analysis_budget_policies WHERE id=?",(policy_id,)).fetchone()
        expected=(provider,daily_limit_microusd,per_attempt_limit_microusd,effective,supersedes_policy_id)
        if existing:
            actual=(existing["provider"],existing["daily_limit_microusd"],existing["per_attempt_limit_microusd"],existing["effective_from"],existing["supersedes_policy_id"])
            if actual != expected: raise AnalysisRunError("budget policy retry inputs do not match")
            return policy_id
        if supersedes_policy_id:
            old=db.execute("SELECT provider FROM analysis_budget_policies WHERE id=?",(supersedes_policy_id,)).fetchone()
            if not old or old["provider"] != provider:
                raise AnalysisRunError("superseded budget policy is missing or belongs to another provider")
        db.execute("""INSERT INTO analysis_budget_policies(
          id,provider,daily_limit_microusd,per_attempt_limit_microusd,effective_from,created_at,supersedes_policy_id)
          VALUES(?,?,?,?,?,?,?)""",(policy_id,provider,daily_limit_microusd,per_attempt_limit_microusd,effective,current,supersedes_policy_id))
    return policy_id


def authorize_attempt(
    *, run_id: str, job_id: str, lease_token: str, expected_input_version: str | None,
    attempt_kind: str, reserved_cost_microusd: int, idempotency_key: str,
    now: datetime | str | None = None,
) -> AttemptAuthorization:
    if attempt_kind not in {"primary","retry","repair"}: raise AnalysisRunError("unsupported attempt kind")
    if reserved_cost_microusd < 0: raise ValueError("reserved cost cannot be negative")
    current=_time(now); budget_day=current[:10]
    authorization_id=_stable_id("analysis_attempt_authorization",idempotency_key)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing=db.execute("SELECT * FROM analysis_attempt_authorizations WHERE id=?",(authorization_id,)).fetchone()
        if existing:
            expected=(run_id,attempt_kind,reserved_cost_microusd)
            actual=(existing["run_id"],existing["attempt_kind"],existing["reserved_cost_microusd"])
            if actual != expected: raise AnalysisRunError("attempt authorization retry inputs do not match")
            return AttemptAuthorization(existing["id"],existing["run_id"],existing["attempt_number"],existing["decision"],existing["reason"],existing["reserved_cost_microusd"])
        job=_current_lease(db,job_id,lease_token,current); _validate_input_version(db,job,expected_input_version)
        run=db.execute("SELECT * FROM analysis_runs WHERE id=? AND job_id=?",(run_id,job_id)).fetchone()
        if not run: raise AnalysisRunError("analysis run does not belong to the leased job")
        prior=db.execute("""SELECT a.attempt_number,a.attempt_kind,t.status
          FROM analysis_attempt_authorizations a LEFT JOIN analysis_attempts t ON t.authorization_id=a.id
          WHERE a.run_id=? AND a.decision='allowed' ORDER BY a.attempt_number""",(run_id,)).fetchall()
        if any(row["status"] is None for row in prior): raise AnalysisRunError("previous allowed attempt has not been recorded")
        attempt_number=len(prior)+1
        if attempt_number>4: raise AnalysisRunError("analysis attempt limit reached")
        nonrepair=sum(row["attempt_kind"]!="repair" for row in prior)
        repairs=sum(row["attempt_kind"]=="repair" for row in prior)
        if attempt_number==1 and attempt_kind!="primary": raise AnalysisRunError("first attempt must be primary")
        if attempt_number>1 and attempt_kind=="primary": raise AnalysisRunError("primary attempt already exists")
        if attempt_kind=="retry" and nonrepair>=3: raise AnalysisRunError("automatic retry limit reached")
        if attempt_kind=="repair" and (repairs or not prior or prior[-1]["status"]!="invalid_output"):
            raise AnalysisRunError("repair requires one immediately preceding invalid output")
        policy=db.execute("""SELECT p.* FROM analysis_budget_policies p
          WHERE p.provider=? AND p.effective_from<=? AND NOT EXISTS(
            SELECT 1 FROM analysis_budget_policies n WHERE n.supersedes_policy_id=p.id)
          ORDER BY p.effective_from DESC,p.id DESC LIMIT 1""",(run["provider"],current)).fetchone()
        if not policy: raise AnalysisRunError("no active budget policy for analysis provider")
        reserved=db.execute("""SELECT COALESCE(SUM(reserved_cost_microusd),0)
          FROM analysis_attempt_authorizations WHERE provider=? AND budget_day=? AND decision='allowed'""",(run["provider"],budget_day)).fetchone()[0]
        allowed=(reserved_cost_microusd<=policy["per_attempt_limit_microusd"] and reserved+reserved_cost_microusd<=policy["daily_limit_microusd"])
        decision="allowed" if allowed else "blocked"
        reason="within_budget" if allowed else "budget_limit_exceeded"
        db.execute("""INSERT INTO analysis_attempt_authorizations(
          id,run_id,attempt_number,attempt_kind,provider,budget_policy_id,budget_day,
          reserved_cost_microusd,decision,reason,authorized_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (authorization_id,run_id,attempt_number,attempt_kind,run["provider"],policy["id"],budget_day,reserved_cost_microusd,decision,reason,current))
    return AttemptAuthorization(authorization_id,run_id,attempt_number,decision,reason,reserved_cost_microusd)


def record_attempt(
    *, authorization_id: str, job_id: str, lease_token: str,
    expected_input_version: str | None, status: str, started_at: datetime | str,
    finished_at: datetime | str, resolved_model: str | None = None,
    provider_request_id: str | None = None, input_tokens: int | None = None,
    output_tokens: int | None = None, usage_status: str = "unknown",
    cost_microusd: int | None = None, pricing_version: str | None = None,
    raw_response_ref: str | None = None, raw_response_sha256: str | None = None,
    error_type: str | None = None, error_detail: str | None = None,
    now: datetime | str | None = None,
) -> AttemptRecord:
    if status not in {"succeeded","failed","refused","invalid_output"}: raise AnalysisRunError("unsupported attempt status")
    if usage_status not in {"reported","estimated","unknown"}: raise AnalysisRunError("unsupported usage status")
    for value,name in ((input_tokens,"input_tokens"),(output_tokens,"output_tokens"),(cost_microusd,"cost_microusd")):
        if value is not None and value<0: raise ValueError(f"{name} cannot be negative")
    if usage_status=="unknown" and any(v is not None for v in (input_tokens,output_tokens,cost_microusd)):
        raise AnalysisRunError("unknown usage cannot contain token or cost values")
    if raw_response_sha256 is not None and (len(raw_response_sha256)!=64 or any(c not in "0123456789abcdef" for c in raw_response_sha256)):
        raise AnalysisRunError("raw response hash must be a lowercase SHA-256")
    current,start,finish=_time(now),_time(started_at),_time(finished_at)
    if finish<start: raise AnalysisRunError("attempt finished before it started")
    attempt_id=f"analysis_attempt_{hashlib.sha256(authorization_id.encode()).hexdigest()[:32]}"
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing=db.execute("SELECT * FROM analysis_attempts WHERE id=?",(attempt_id,)).fetchone()
        values=(status,resolved_model,provider_request_id,start,finish,input_tokens,output_tokens,usage_status,cost_microusd,pricing_version,raw_response_ref,raw_response_sha256,error_type,error_detail)
        if existing:
            actual=tuple(existing[k] for k in ("status","resolved_model","provider_request_id","started_at","finished_at","input_tokens","output_tokens","usage_status","cost_microusd","pricing_version","raw_response_ref","raw_response_sha256","error_type","error_detail"))
            if actual!=values: raise AnalysisRunError("attempt record retry inputs do not match")
            return AttemptRecord(existing["id"],existing["run_id"],existing["attempt_number"],existing["status"],existing["cost_microusd"])
        job=_current_lease(db,job_id,lease_token,current); _validate_input_version(db,job,expected_input_version)
        auth=db.execute("""SELECT a.*,r.job_id FROM analysis_attempt_authorizations a
          JOIN analysis_runs r ON r.id=a.run_id WHERE a.id=?""",(authorization_id,)).fetchone()
        if not auth or auth["job_id"]!=job_id: raise AnalysisRunError("attempt authorization does not belong to the leased job")
        if auth["decision"]!="allowed": raise AnalysisRunError("blocked attempt cannot be recorded as a provider call")
        db.execute("""INSERT INTO analysis_attempts(
          id,authorization_id,run_id,attempt_number,attempt_kind,resolved_model,provider_request_id,
          started_at,finished_at,status,input_tokens,output_tokens,usage_status,cost_microusd,
          pricing_version,raw_response_ref,raw_response_sha256,error_type,error_detail,recorded_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (attempt_id,authorization_id,auth["run_id"],auth["attempt_number"],auth["attempt_kind"],resolved_model,provider_request_id,start,finish,status,input_tokens,output_tokens,usage_status,cost_microusd,pricing_version,raw_response_ref,raw_response_sha256,error_type,error_detail,current))
    return AttemptRecord(attempt_id,auth["run_id"],auth["attempt_number"],status,cost_microusd)
