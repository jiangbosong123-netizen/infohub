import json
import unittest

from app import database, db_admin
from app.analysis_attempts import authorize_attempt, record_attempt, register_budget_policy
from app.analysis_contracts import validate_analysis_data
from app.analysis_results import _validate_output, publish_analysis_result
from app.analysis_runs import AnalysisInput, AnalysisRunError, prepare_analysis_run
from app.impact_contracts import IMPACT_SCHEMA_VERSION
from app.jobs import claim_job, enqueue_job
from tests import test_analysis_runs as analysis_run_fixtures


SHA = analysis_run_fixtures.SHA
T0 = analysis_run_fixtures.T0


def impact_data(**changes):
    assessment = {
        "target": {"entity_id": "organization:example", "type": "organization"},
        "aspect": "operating_cost",
        "horizon": {"bucket": "quarter", "min_days": 8, "max_days": 90},
        "direction": "positive",
        "intensity": 0.4,
        "evidence_ids": ["raw-1"],
        "contradicting_evidence_ids": [],
        "mechanism": "If the announced plan is implemented, operating costs may fall.",
        "assumptions": ["Execution occurs on schedule."],
        "raw_confidence": 0.72,
        "calibrated_confidence": None,
        "calibration_version": None,
        "uncertainty_reason": "The plan has not yet produced realized results.",
    }
    assessment.update(changes)
    return {"assessments": [assessment]}


class ImpactContractUnitTests(unittest.TestCase):
    def test_review_only_contract_is_strict_and_evidence_grounded(self):
        clean = validate_analysis_data(
            task_type="impact", schema_version=IMPACT_SCHEMA_VERSION,
            status="needs_review", data=impact_data(), allowed_evidence={"raw-1"},
        )
        assessment = clean["assessments"][0]
        self.assertEqual(assessment["aspect"], "operating_cost")
        self.assertEqual(assessment["horizon"], {
            "bucket": "quarter", "min_days": 8, "max_days": 90,
        })
        self.assertEqual(assessment["raw_confidence"], 0.72)

    def test_valid_and_calibrated_claims_remain_closed_before_admission(self):
        with self.assertRaisesRegex(AnalysisRunError, "quality admission"):
            validate_analysis_data(
                task_type="impact", schema_version=IMPACT_SCHEMA_VERSION,
                status="valid", data=impact_data(), allowed_evidence={"raw-1"},
            )
        with self.assertRaisesRegex(AnalysisRunError, "unavailable before admission"):
            validate_analysis_data(
                task_type="impact", schema_version=IMPACT_SCHEMA_VERSION,
                status="needs_review",
                data=impact_data(
                    calibrated_confidence=0.7, calibration_version="impact-calibration-v1",
                ),
                allowed_evidence={"raw-1"},
            )

    def test_horizon_unknown_and_evidence_rules_fail_closed(self):
        cases = (
            (impact_data(horizon={"bucket": "quarter", "min_days": 0, "max_days": 90}),
             "boundaries"),
            (impact_data(direction="unknown", intensity=0.4), "null intensity"),
            (impact_data(direction="unknown", intensity=None, uncertainty_reason=None),
             "uncertainty_reason"),
            (impact_data(evidence_ids=["raw-1", "raw-1"]), "duplicate"),
            (impact_data(contradicting_evidence_ids=["raw-1"]), "disjoint"),
            (impact_data(evidence_ids=["missing"]), "unknown evidence"),
        )
        for data, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                AnalysisRunError, message
            ):
                validate_analysis_data(
                    task_type="impact", schema_version=IMPACT_SCHEMA_VERSION,
                    status="needs_review", data=data, allowed_evidence={"raw-1"},
                )

    def test_nonpublishable_shape_and_schema_ownership_are_enforced(self):
        clean = validate_analysis_data(
            task_type="impact", schema_version=IMPACT_SCHEMA_VERSION,
            status="insufficient_evidence", data={"reason_code": "no_direct_support"},
            allowed_evidence=set(),
        )
        self.assertEqual(clean, {"reason_code": "no_direct_support"})
        with self.assertRaisesRegex(AnalysisRunError, "does not match"):
            validate_analysis_data(
                task_type="tone", schema_version=IMPACT_SCHEMA_VERSION,
                status="needs_review", data=impact_data(), allowed_evidence={"raw-1"},
            )

    def test_analysis_envelope_accepts_review_only_impact(self):
        run = {
            "output_schema_version": IMPACT_SCHEMA_VERSION,
            "subject_type": "event", "subject_version_id": "event-version-1",
            "task_type": "impact",
        }
        output = {
            "schema_version": IMPACT_SCHEMA_VERSION,
            "subject": {"type": "event", "version_id": "event-version-1"},
            "status": "needs_review", "evidence_ids": ["raw-1"],
            "data": impact_data(),
        }
        clean, evidence, status = _validate_output(run, output, {"raw-1"})
        self.assertEqual((evidence, status), (["raw-1"], "needs_review"))
        self.assertEqual(clean["data"]["assessments"][0]["direction"], "positive")
        for mutation, message in (
            (lambda item: item.update({"unexpected": True}), "unknown fields"),
            (lambda item: item.update({"evidence_ids": []}), "exactly list"),
            (lambda item: item.update({"evidence_ids": ["raw-1", "raw-1"]}), "duplicate"),
        ):
            invalid = json.loads(json.dumps(output))
            mutation(invalid)
            with self.subTest(message=message), self.assertRaisesRegex(
                AnalysisRunError, message
            ):
                _validate_output(run, invalid, {"raw-1"})


class ImpactPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = analysis_run_fixtures.AnalysisRunTests(
            "test_manifest_is_version_pinned_immutable_and_idempotent"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.path = fixture.path
        with database.get_db() as db:
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO entities(id,dataset_id,type,status,created_at) VALUES(?,?,?,?,?)",
                ("organization:example", dataset_id, "organization", "active", T0.isoformat()),
            )
            db.execute(
                """INSERT INTO entity_versions(
                       id,entity_id,version,previous_version_id,type,canonical_name,status,
                       attributes_json,version_sha256,available_at,created_by)
                   VALUES(?,?,1,NULL,?,?,?,?,?,?,?)""",
                (
                    "organization:example:v1", "organization:example", "organization",
                    "Example", "active", "{}", "c" * 64, T0.isoformat(), "test",
                ),
            )
            db.execute(
                "UPDATE entities SET current_version_id=? WHERE id=?",
                ("organization:example:v1", "organization:example"),
            )
            db.execute(
                """INSERT INTO events(
                       id,dataset_id,first_seen_at,latest_report_at,status)
                   VALUES(?,?,?,?,?)""",
                ("event-impact", dataset_id, T0.isoformat(), T0.isoformat(), "candidate"),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,previous_version_id,schema_version,title,event_type,
                       event_time_start,event_time_end,time_precision,primary_entities_json,
                       object_entities_json,facts_json,topics_json,knowledge_status,
                       version_sha256,available_at,created_by,method_version)
                   VALUES(?,?,1,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "event-impact-v1", "event-impact", "event-candidate-v1", "Cost plan",
                    "earnings", None, None, "unknown", '["organization:example"]', "[]",
                    "[]", "[]", "reported", "b" * 64, T0.isoformat(), "test", "test-v1",
                ),
            )
            db.execute(
                "UPDATE events SET current_version_id=? WHERE id=?",
                ("event-impact-v1", "event-impact"),
            )
            db.execute(
                """INSERT INTO event_evidence(
                       id,event_version_id,document_version_id,evidence_id,fact_id,role,available_at)
                   VALUES(?,?,?,?,NULL,'supports',?)""",
                ("impact-evidence", "event-impact-v1", fixture.doc, fixture.raw, T0.isoformat()),
            )

    def completed_attempt(self):
        job = enqueue_job(
            kind="analysis", idempotency_key="job:impact", subject_id="event-impact",
            input_version="event-impact-v1", scheduled_for=T0,
        )
        job = claim_job(worker_id="impact-worker", lease_seconds=300, now=T0)
        run = prepare_analysis_run(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="event-impact-v1", subject_type="event",
            subject_version_id="event-impact-v1", task_type="impact",
            output_schema_version=IMPACT_SCHEMA_VERSION,
            inputs=(
                AnalysisInput("primary", None, "event-impact-v1", None),
                AnalysisInput("supporting", self.fixture.doc, None, self.fixture.raw),
            ),
            provider="fixture", requested_model="fixture-impact-v1",
            prompt_template_id="impact-v1", prompt_sha256=SHA,
            rendered_input_ref="cas://rendered/impact", rendered_input_sha256=SHA,
            pipeline_version="impact-contracts-v1", parameters={"temperature": 0},
            idempotency_key="analysis:impact", now=T0,
        )
        register_budget_policy(
            provider="fixture", daily_limit_microusd=1000,
            per_attempt_limit_microusd=600, effective_from=T0,
            idempotency_key="policy:impact", now=T0,
        )
        authorization = authorize_attempt(
            run_id=run.id, job_id=job.id, lease_token=job.lease_token,
            expected_input_version="event-impact-v1", attempt_kind="primary",
            reserved_cost_microusd=100, idempotency_key="auth:impact", now=T0,
        )
        attempt = record_attempt(
            authorization_id=authorization.id, job_id=job.id,
            lease_token=job.lease_token, expected_input_version="event-impact-v1",
            status="succeeded", started_at=T0, finished_at=T0,
            resolved_model="fixture-impact-v1", usage_status="reported",
            input_tokens=10, output_tokens=5, cost_microusd=50,
            pricing_version="fixture-v1", raw_response_ref="cas://response/impact",
            raw_response_sha256=SHA, now=T0,
        )
        return job, run, attempt

    def output(self):
        return {
            "schema_version": IMPACT_SCHEMA_VERSION,
            "subject": {"type": "event", "version_id": "event-impact-v1"},
            "status": "needs_review", "evidence_ids": [self.fixture.raw],
            "data": impact_data(evidence_ids=[self.fixture.raw]),
        }

    def test_event_bound_impact_publishes_with_entity_and_evidence_validation(self):
        job, run, attempt = self.completed_attempt()
        publish_analysis_result(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="event-impact-v1", run_id=run.id,
            attempt_id=attempt.id, validated_output=self.output(),
            review_status="unreviewed", evidence_status="partial",
            idempotency_key="result:impact", now=T0,
        )
        with database.get_db() as db:
            row = db.execute(
                "SELECT validation_report_json,result_status FROM analysis_results"
            ).fetchone()
        report = json.loads(row["validation_report_json"])
        self.assertEqual(row["result_status"], "needs_review")
        self.assertEqual(report["task_validation"]["status"], "passed")
        self.assertEqual(
            report["task_validation"]["supporting_evidence_ids"], [self.fixture.raw]
        )
        db_admin.verify_database(self.path, require_current=True)

    def test_impact_run_and_publication_reject_wrong_subject_schema_and_event_evidence(self):
        job = enqueue_job(
            kind="analysis", idempotency_key="job:impact-bad-subject",
            subject_id=self.fixture.doc, input_version=self.fixture.doc, scheduled_for=T0,
        )
        job = claim_job(worker_id="impact-worker", lease_seconds=300, now=T0)
        with self.assertRaisesRegex(AnalysisRunError, "event version subject"):
            prepare_analysis_run(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version=self.fixture.doc, subject_type="document",
                subject_version_id=self.fixture.doc, task_type="impact",
                output_schema_version=IMPACT_SCHEMA_VERSION,
                inputs=(AnalysisInput("primary", self.fixture.doc, None, self.fixture.raw),),
                provider="fixture", requested_model="fixture-impact-v1",
                prompt_template_id="impact-v1", prompt_sha256=SHA,
                rendered_input_ref="cas://rendered/impact", rendered_input_sha256=SHA,
                pipeline_version="impact-contracts-v1", parameters={},
                idempotency_key="analysis:impact-bad-subject", now=T0,
            )
        job, run, attempt = self.completed_attempt()
        with database.get_db() as db:
            db.execute("DROP TRIGGER event_evidence_no_delete")
            db.execute("DELETE FROM event_evidence")
            db.execute(
                """CREATE TRIGGER event_evidence_no_delete
                   BEFORE DELETE ON event_evidence
                   BEGIN SELECT RAISE(ABORT,'event evidence is immutable'); END"""
            )
        with self.assertRaisesRegex(AnalysisRunError, "not direct event evidence"):
            publish_analysis_result(
                job_id=job.id, lease_token=job.lease_token,
                expected_input_version="event-impact-v1", run_id=run.id,
                attempt_id=attempt.id, validated_output=self.output(),
                review_status="unreviewed", evidence_status="partial",
                idempotency_key="result:impact-unlinked", now=T0,
            )

    def test_database_verifier_recomputes_the_impact_contract(self):
        job, run, attempt = self.completed_attempt()
        publish_analysis_result(
            job_id=job.id, lease_token=job.lease_token,
            expected_input_version="event-impact-v1", run_id=run.id,
            attempt_id=attempt.id, validated_output=self.output(),
            review_status="unreviewed", evidence_status="partial",
            idempotency_key="result:impact-verifier", now=T0,
        )
        with database.get_db() as db:
            row = db.execute(
                "SELECT id,validated_output_json FROM analysis_results"
            ).fetchone()
            output = json.loads(row["validated_output_json"])
            output["data"]["assessments"][0]["aspect"] = "market_price"
            db.execute("DROP TRIGGER analysis_results_no_update")
            db.execute(
                "UPDATE analysis_results SET validated_output_json=? WHERE id=?",
                (json.dumps(output, sort_keys=True, separators=(",", ":")), row["id"]),
            )
            db.execute(
                """CREATE TRIGGER analysis_results_no_update
                   BEFORE UPDATE ON analysis_results
                   BEGIN SELECT RAISE(ABORT,'analysis results are immutable'); END"""
            )
        with self.assertRaisesRegex(
            db_admin.DatabaseVerificationError, "analysis_results=1",
        ):
            db_admin.verify_database(self.path, require_current=True)


if __name__ == "__main__":
    unittest.main()
