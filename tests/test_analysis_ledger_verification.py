"""verify_database must catch every per-row analysis ledger inconsistency.

Two results are published through the public API, then exactly one row is tampered with
(immutability triggers are dropped and restored verbatim around the edit). The verifier
must name the right ledger and count exactly one bad row, so a faster implementation
cannot silently change what is detected or how it is attributed.
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.analysis_attempts import authorize_attempt, record_attempt, register_budget_policy
from app.analysis_results import publish_analysis_result
from app.analysis_runs import AnalysisInput, prepare_analysis_run
from app.crawler.runner import insert_item
from app.ingest import begin_ingest_run, observe_candidate
from app.jobs import claim_job, enqueue_job
from tests.test_analysis_runs import SHA, T0

OTHER_SHA = "c" * 64


def _publish_fixture(path: Path, blobs: Path) -> dict:
    """Two documents, each with one published summarization result."""
    with (patch.object(database, "DB_PATH", path), patch.object(config, "DB_PATH", path),
          patch.object(config, "BLOB_PATH", blobs)):
        database.init_schema()
        with database.get_db() as db:
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','ai','media','rss','https://example.com/feed')"""
            )
        source = {"key": "fixture", "name": "Fixture", "channel": "ai", "tier": "media",
                  "type": "rss", "url": "https://example.com/feed", "interval_minutes": 30}
        ingest = begin_ingest_run(source, started_at=T0.isoformat())
        for ordinal, key in enumerate("ab"):
            candidate = {
                "url": f"https://example.com/{key}", "title": key.upper(),
                "summary": f"Evidence {key}", "published_at": T0.isoformat(),
                "observed_at": T0.isoformat(),
                "source_record": {"id": key, "summary": f"Evidence {key}"},
                "payload_kind": "feed_entry",
            }
            observation = observe_candidate(
                ingest, candidate, ordinal=ordinal, observed_at=T0.isoformat()
            )
            assert insert_item("fixture", candidate, observation=observation)
        register_budget_policy(
            provider="fixture", daily_limit_microusd=1000, per_attempt_limit_microusd=600,
            effective_from=T0, idempotency_key="policy", now=T0,
        )
        # Same limits, different provider: only the provider binding can reject it.
        fixture = {"other_policy": register_budget_policy(
            provider="other", daily_limit_microusd=1000, per_attempt_limit_microusd=600,
            effective_from=T0, idempotency_key="policy:other", now=T0,
        )}
        for key in "ab":
            with database.get_db() as db:
                doc, raw = db.execute(
                    """SELECT document.current_version_id,input.raw_record_id
                       FROM documents AS document
                       JOIN items AS item ON item.id=document.legacy_item_id
                       JOIN document_version_inputs AS input
                         ON input.version_id=document.current_version_id
                       WHERE item.url=?""",
                    (f"https://example.com/{key}",),
                ).fetchone()
            enqueue_job(kind="analysis", idempotency_key=f"job:{key}", subject_id=doc,
                        input_version=doc, scheduled_for=T0)
            job = claim_job(worker_id="analysis-worker", lease_seconds=300, now=T0)
            run = prepare_analysis_run(
                job_id=job.id, lease_token=job.lease_token, expected_input_version=doc,
                subject_type="document", subject_version_id=doc, task_type="summarization",
                output_schema_version="summary/1.0",
                inputs=(AnalysisInput("primary", doc, None, raw),),
                provider="fixture", requested_model="fixture-v1",
                prompt_template_id="summary-v1", prompt_sha256=SHA,
                rendered_input_ref=f"cas://rendered/{key}", rendered_input_sha256=SHA,
                pipeline_version="pipeline-test", parameters={"temperature": 0},
                idempotency_key=f"analysis:{key}", now=T0,
            )
            authorization = authorize_attempt(
                run_id=run.id, job_id=job.id, lease_token=job.lease_token,
                expected_input_version=doc, attempt_kind="primary",
                reserved_cost_microusd=100, idempotency_key=f"auth:{key}", now=T0,
            )
            attempt = record_attempt(
                authorization_id=authorization.id, job_id=job.id,
                lease_token=job.lease_token, expected_input_version=doc,
                status="succeeded", started_at=T0, finished_at=T0,
                resolved_model="fixture-v1", usage_status="reported", input_tokens=10,
                output_tokens=5, cost_microusd=50, pricing_version="v1",
                raw_response_ref=f"cas://response/{key}", raw_response_sha256=SHA, now=T0,
            )
            output = {
                "schema_version": "summary/1.0",
                "subject": {"type": "document", "version_id": doc}, "status": "valid",
                "evidence_ids": [raw],
                "data": {"summary": f"Evidence-backed summary {key}", "raw_confidence": 0.8},
            }
            publish_analysis_result(
                job_id=job.id, lease_token=job.lease_token, expected_input_version=doc,
                run_id=run.id, attempt_id=attempt.id, validated_output=output,
                review_status="unreviewed", evidence_status="supported",
                idempotency_key=f"result:{key}", now=T0,
            )
            fixture[key] = {"doc": doc, "raw": raw, "run": run.id}
    return fixture


class AnalysisLedgerVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_dir = tempfile.TemporaryDirectory()
        root = Path(cls.template_dir.name)
        cls.template = root / "template.db"
        cls.fixture = _publish_fixture(cls.template, root / "blobs")

    @classmethod
    def tearDownClass(cls):
        cls.template_dir.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "app.db"
        with (closing(sqlite3.connect(self.template)) as source,
              closing(sqlite3.connect(self.path)) as target):
            source.backup(target)
        self.a, self.b = self.fixture["a"], self.fixture["b"]

    def tamper(self, table: str, statement: str, parameters: tuple = (), rows: int = 1) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            guards = db.execute(
                """SELECT name,sql FROM sqlite_master
                   WHERE type='trigger' AND tbl_name=?
                     AND (name LIKE '%\\_no\\_update' ESCAPE '\\'
                          OR name LIKE '%\\_no\\_delete' ESCAPE '\\')""",
                (table,),
            ).fetchall()
            for name, _ in guards:
                db.execute(f'DROP TRIGGER "{name}"')
            self.assertEqual(db.execute(statement, parameters).rowcount, rows)
            for _, sql in guards:
                db.execute(sql)

    def assert_ledgers(self, runs: int, attempts: int, results: int) -> None:
        with self.assertRaises(db_admin.DatabaseVerificationError) as raised:
            db_admin.verify_database(self.path, require_current=True)
        message = str(raised.exception)
        self.assertTrue(message.startswith("event projections are invalid: "), message)
        self.assertTrue(message.endswith(
            f", analysis_runs={runs}, analysis_attempts={attempts}"
            f", analysis_results={results}"
        ), message)

    def test_untouched_ledger_verifies(self):
        report = db_admin.verify_database(self.path, require_current=True)
        self.assertEqual(report.state, "current")

    # analysis_runs: manifest, parameters, subject and inputs.
    def test_manifest_hash_mismatch(self):
        self.tamper("analysis_runs", "UPDATE analysis_runs SET input_manifest_sha256=? WHERE id=?",
                    (OTHER_SHA, self.a["run"]))
        self.assert_ledgers(1, 0, 0)

    def test_parameters_differ_from_manifest(self):
        self.tamper("analysis_runs", "UPDATE analysis_runs SET parameters_json=? WHERE id=?",
                    ('{"temperature":1}', self.a["run"]))
        self.assert_ledgers(1, 0, 0)

    def test_unknown_subject_version(self):
        self.tamper("analysis_runs", "UPDATE analysis_runs SET subject_version_id=? WHERE id=?",
                    ("missing-version", self.a["run"]))
        self.assert_ledgers(1, 0, 2)

    def test_runs_with_null_ids_are_counted_one_by_one(self):
        # SQLite lets a TEXT primary key hold NULL; such runs match nothing and each is invalid.
        self.tamper("analysis_runs", "UPDATE analysis_runs SET id=NULL", rows=2)
        self.assert_ledgers(2, 2, 4)

    def test_run_without_inputs(self):
        self.tamper("analysis_inputs", "DELETE FROM analysis_inputs WHERE run_id=?",
                    (self.a["run"],))
        self.assert_ledgers(1, 0, 0)

    def test_input_ordinal_gap(self):
        self.tamper("analysis_inputs", "UPDATE analysis_inputs SET ordinal=1 WHERE run_id=?",
                    (self.a["run"],))
        self.assert_ledgers(1, 0, 0)

    def test_input_evidence_not_in_document_version(self):
        self.tamper("analysis_inputs", "UPDATE analysis_inputs SET evidence_id=? WHERE run_id=?",
                    (self.b["raw"], self.a["run"]))
        self.assert_ledgers(1, 0, 0)

    def test_inputs_do_not_include_subject(self):
        self.tamper(
            "analysis_inputs",
            "UPDATE analysis_inputs SET document_version_id=?,evidence_id=? WHERE run_id=?",
            (self.b["doc"], self.b["raw"], self.a["run"]),
        )
        self.assert_ledgers(1, 0, 0)

    # analysis_attempts: authorization, budget policy and attempt identity.
    def test_authorization_for_another_provider_than_the_run(self):
        # The policy follows the authorization, so only the run binding disagrees.
        self.tamper(
            "analysis_attempt_authorizations",
            """UPDATE analysis_attempt_authorizations SET provider='other',budget_policy_id=?
               WHERE run_id=?""",
            (self.fixture["other_policy"], self.a["run"]),
        )
        self.assert_ledgers(0, 1, 0)

    def test_authorization_under_another_providers_policy(self):
        self.tamper(
            "analysis_attempt_authorizations",
            "UPDATE analysis_attempt_authorizations SET budget_policy_id=? WHERE run_id=?",
            (self.fixture["other_policy"], self.a["run"]),
        )
        self.assert_ledgers(0, 1, 0)

    def test_allowed_over_per_attempt_limit(self):
        self.tamper(
            "analysis_attempt_authorizations",
            "UPDATE analysis_attempt_authorizations SET reserved_cost_microusd=601 WHERE run_id=?",
            (self.a["run"],),
        )
        self.assert_ledgers(0, 1, 0)

    def test_attempt_recorded_for_blocked_authorization(self):
        self.tamper(
            "analysis_attempt_authorizations",
            "UPDATE analysis_attempt_authorizations SET decision='blocked' WHERE run_id=?",
            (self.a["run"],),
        )
        self.assert_ledgers(0, 1, 0)

    def test_attempt_identity_differs_from_authorization(self):
        self.tamper("analysis_attempts",
                    "UPDATE analysis_attempts SET attempt_kind='retry' WHERE run_id=?",
                    (self.a["run"],))
        self.assert_ledgers(0, 1, 0)

    # analysis_results: result, attempt and output envelope.
    def test_result_from_failed_attempt(self):
        self.tamper("analysis_attempts",
                    "UPDATE analysis_attempts SET status='failed' WHERE run_id=?",
                    (self.a["run"],))
        self.assert_ledgers(0, 0, 1)

    def test_result_raw_output_hash_mismatch(self):
        self.tamper("analysis_results",
                    "UPDATE analysis_results SET raw_output_sha256=? WHERE run_id=?",
                    (OTHER_SHA, self.a["run"]))
        self.assert_ledgers(0, 0, 1)

    def test_result_created_at_differs_from_attempt(self):
        self.tamper("analysis_results",
                    "UPDATE analysis_results SET created_at=? WHERE run_id=?",
                    ("2026-09-17T16:00:01+00:00", self.a["run"]))
        self.assert_ledgers(0, 0, 1)

    def test_result_report_not_passed(self):
        self.tamper("analysis_results",
                    "UPDATE analysis_results SET validation_report_json=? WHERE run_id=?",
                    ('{"status":"failed"}', self.a["run"]))
        self.assert_ledgers(0, 0, 1)

    def test_result_status_differs_from_output(self):
        self.tamper("analysis_results",
                    "UPDATE analysis_results SET result_status='needs_review' WHERE run_id=?",
                    (self.a["run"],))
        self.assert_ledgers(0, 0, 1)

    def test_result_output_for_another_subject(self):
        with closing(sqlite3.connect(self.path)) as db:
            output = json.loads(db.execute(
                "SELECT validated_output_json FROM analysis_results WHERE run_id=?",
                (self.a["run"],),
            ).fetchone()[0])
        output["subject"]["version_id"] = self.b["doc"]
        self.tamper("analysis_results",
                    "UPDATE analysis_results SET validated_output_json=? WHERE run_id=?",
                    (json.dumps(output), self.a["run"]))
        self.assert_ledgers(0, 0, 1)

    # Current publication pointers, publication versions and their change_log rows.
    def test_pointer_task_differs_from_publication(self):
        self.tamper(
            "analysis_publications",
            "UPDATE analysis_publications SET task_type='importance' WHERE subject_version_id=?",
            (self.a["doc"],),
        )
        self.assert_ledgers(0, 0, 1)

    def test_publication_available_at_differs_from_result(self):
        self.tamper(
            "analysis_publication_versions",
            "UPDATE analysis_publication_versions SET available_at=? WHERE subject_version_id=?",
            ("2026-09-17T16:00:01+00:00", self.a["doc"]),
        )
        self.assert_ledgers(0, 0, 1)

    def test_change_payload_names_another_result(self):
        with closing(sqlite3.connect(self.path)) as db:
            seq, payload = db.execute(
                """SELECT change.seq,change.payload_json
                   FROM analysis_publication_versions AS publication
                   JOIN change_log AS change ON change.seq=publication.publication_seq
                   WHERE publication.subject_version_id=?""",
                (self.a["doc"],),
            ).fetchone()
        payload = json.loads(payload)
        payload["result_id"] = "another-result"
        self.tamper("change_log", "UPDATE change_log SET payload_json=? WHERE seq=?",
                    (json.dumps(payload), seq))
        self.assert_ledgers(0, 0, 1)

    def test_change_row_is_not_an_analysis_change(self):
        self.tamper(
            "change_log",
            """UPDATE change_log SET resource_type='report'
               WHERE seq=(SELECT publication_seq FROM analysis_publication_versions
                          WHERE subject_version_id=?)""",
            (self.a["doc"],),
        )
        self.assert_ledgers(0, 0, 1)


if __name__ == "__main__":
    unittest.main()
