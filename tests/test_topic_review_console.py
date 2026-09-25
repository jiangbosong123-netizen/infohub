import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import cli
from app import config, database
from app.topic_assignment_reviews import record_topic_assignment_review
from app.topic_review_console import create_topic_review_console
from app.topic_review_sampling import create_sample_batch, sample_queue


NOW = "2026-09-24T12:00:00.000000Z"


class TopicReviewConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "app.db"
        for mocked in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        database.init_schema()
        with database.get_db() as db:
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            self.dataset = dataset
            db.execute(
                "INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','Source','ai','rss')"
            )
            db.execute(
                "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES('t1',?,'active',?)",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO topic_versions(
                       id,topic_id,version,slug,name,group_key,description,rules_json,
                       rules_hash,version_sha256,status,available_at)
                   VALUES('tv1','t1',1,'macro','Macro','macro','','{}',?,?,'active',?)""",
                ("a" * 64, "b" * 64, NOW),
            )
            db.execute("UPDATE topic_catalog SET current_version_id='tv1' WHERE id='t1'")
            self._insert_assignment(db, 1, "https://example.test/story")
            self._insert_assignment(db, 2, "javascript:alert(1)")
            self.batch = create_sample_batch(
                db, seed="console-test", per_topic_limit=2,
                created_by="lead", now=NOW,
            )
            self.first = sample_queue(db, self.batch.batch_id, limit=1)[0]
        self.app = create_topic_review_console(
            batch_id=self.batch.batch_id,
            csrf_token="test-csrf-token",
            reviewer_id="reviewer",
            db_path=self.path,
        )
        self.client = TestClient(self.app)

    def _insert_assignment(self, db, index: int, url: str) -> None:
        db.execute(
            """INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
               VALUES(?,1,?,?,'ai',?,?)""",
            (index, url, f"Title {index}", NOW, NOW),
        )
        db.execute(
            "INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at) VALUES(?,?,?,'article',?)",
            (f"d{index}", self.dataset, index, NOW),
        )
        db.execute(
            """INSERT INTO document_versions(
                   id,document_id,version,normalizer_version,normalized_at,title_original,
                   language,text,content_sha256,version_sha256,canonical_url,source_id,
                   published_precision,time_status,time_rule_version,tzdb_version,
                   content_origin,content_extent,truncated,extraction_status,correction_kind,
                   available_at,availability_basis,point_in_time_eligible)
               VALUES(?,?,1,'v1',?,?,'en','',?,?,?,1,'unknown','legacy_unverified',
                      'legacy','unknown','legacy_unknown','none',0,'not_attempted','initial',
                      ?,'legacy_unknown',0)""",
            (f"dv{index}", f"d{index}", NOW, f"Title {index}",
             f"{index:064x}", f"{index + 100:064x}", url, NOW),
        )
        db.execute(
            "UPDATE documents SET current_version_id=? WHERE id=?",
            (f"dv{index}", f"d{index}"),
        )
        db.execute(
            """INSERT INTO document_topic_assignments(
                   id,document_version_id,topic_version_id,method,method_version,status,available_at)
               VALUES(?,?, 'tv1','fixture','fixture-v1','candidate',?)""",
            (f"a{index}", f"dv{index}", NOW),
        )

    def _decision_form(self, **overrides):
        values = {
            "csrf_token": "test-csrf-token",
            "decision": "accepted",
            "reason": "Checked the source and topic evidence.",
            "expected_previous_review_id": "",
        }
        values.update(overrides)
        return values

    def test_page_shows_one_pending_item_and_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.first.title, response.text)
        other_title = "Title 2" if self.first.title == "Title 1" else "Title 1"
        self.assertNotIn(other_title, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("javascript:alert(1)", response.text)
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_one_submission_records_one_append_only_decision_and_advances(self):
        response = self.client.post(
            f"/review/{self.first.assignment_id}",
            data=self._decision_form(),
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        with database.get_db() as db:
            rows = db.execute(
                "SELECT assignment_id,decision,reviewer_id,reason FROM topic_assignment_reviews"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                tuple(rows[0]),
                (self.first.assignment_id, "accepted", "reviewer",
                 "Checked the source and topic evidence."),
            )
        next_page = self.client.get("/")
        self.assertNotIn(self.first.title, next_page.text)
        self.assertIn("1</strong><span>已接受", next_page.text)
        self.assertNotIn("javascript:alert(1)", next_page.text)

    def test_csrf_batch_membership_and_stale_write_are_enforced(self):
        bad_csrf = self.client.post(
            f"/review/{self.first.assignment_id}",
            data=self._decision_form(csrf_token="wrong"),
        )
        self.assertEqual(bad_csrf.status_code, 403)
        outside = self.client.post(
            "/review/not-in-batch",
            data=self._decision_form(),
        )
        self.assertEqual(outside.status_code, 404)
        with database.get_db() as db:
            record_topic_assignment_review(
                db,
                assignment_id=self.first.assignment_id,
                decision="rejected",
                expected_previous_review_id=None,
                reviewer_id="other-reviewer",
                reason="Concurrent decision.",
                now=NOW,
            )
        stale = self.client.post(
            f"/review/{self.first.assignment_id}",
            data=self._decision_form(),
        )
        self.assertEqual(stale.status_code, 409)
        with database.get_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM topic_assignment_reviews").fetchone()[0], 1
            )

    def test_console_has_no_bulk_review_endpoint(self):
        response = self.client.post("/review", data=self._decision_form())
        self.assertEqual(response.status_code, 404)

    def test_cli_always_binds_loopback_and_requires_maintenance_role(self):
        with patch.object(config, "PROCESS_ROLE", "web"):
            with self.assertRaisesRegex(Exception, "requires maintenance role"):
                cli.cmd_topic_review_console(self.batch.batch_id, 8011)
        with (
            patch.object(config, "PROCESS_ROLE", "maintenance"),
            patch.object(config, "DB_PATH", self.path),
            patch("getpass.getuser", return_value="reviewer"),
            patch("secrets.token_urlsafe", return_value="generated-token"),
            patch("uvicorn.run") as run,
        ):
            cli.cmd_topic_review_console(self.batch.batch_id, 8011)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8011)

    def test_rejects_oversized_form_before_writing(self):
        response = self.client.post(
            f"/review/{self.first.assignment_id}",
            content=b"x" * 8193,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response.status_code, 413)
        with database.get_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM topic_assignment_reviews").fetchone()[0], 0
            )


if __name__ == "__main__":
    unittest.main()
