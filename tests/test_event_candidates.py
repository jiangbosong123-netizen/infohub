import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.catalog import sync_identity_catalog
from app.crawler import runner
from app.event_candidates import EventProjectionError, project_legacy_stories
from app.event_projection_audit import audit_event_projection
from app.ingest import begin_ingest_run, observe_candidate
from app.stories import refresh_derived


class EventCandidateProjectionTests(unittest.TestCase):
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
                """INSERT INTO companies(slug,name,name_zh,market,aliases)
                   VALUES('openai','OpenAI','OpenAI','PRIVATE','[]')"""
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('fixture','Fixture','ai','media','rss','https://example.com/feed')"""
            )
            sync_identity_catalog(db)
        self.source = {
            "key": "fixture", "name": "Fixture", "channel": "ai", "tier": "media",
            "type": "rss", "url": "https://example.com/feed", "interval_minutes": 30,
        }
        self.number = 0

    def _ingest(self, title: str, *, summary: str = "", event_type: str = "") -> int:
        self.number += 1
        observed = datetime(2026, 9, 17, 10, self.number, tzinfo=timezone.utc).isoformat()
        candidate = {
            "url": f"https://example.com/{self.number}", "title": title,
            "summary": summary, "published_at": observed, "event_type": event_type,
            "companies": ["openai"], "observed_at": observed,
            "source_record": {
                "id": self.number, "title": title, "summary": summary,
                "published_at": observed,
            },
            "payload_kind": "feed_entry",
        }
        run = begin_ingest_run(self.source, started_at=observed)
        observation = observe_candidate(run, candidate, ordinal=0, observed_at=observed)
        self.assertTrue(runner.insert_item("fixture", candidate, observation=observation))
        with database.get_db() as db:
            return db.execute("SELECT MAX(id) FROM items").fetchone()[0]

    def _prepare(self):
        refresh_derived()
        with database.get_db() as db:
            sync_identity_catalog(db)

    def test_read_only_event_audit_distinguishes_missing_and_invalid_projection(self):
        self._ingest("OpenAI releases a coding platform for developers")
        self._prepare()
        before = audit_event_projection(self.path)
        self.assertEqual(before["status"], "incomplete")
        self.assertIn("story_mapping_incomplete", before["blocked_reasons"])
        with database.get_db() as db:
            project_legacy_stories(db)
        after = audit_event_projection(self.path)
        self.assertEqual(after["status"], "ok")
        self.assertEqual(after["imported_candidate_links"], 1)
        self.assertEqual(after["violations"]["invalid_candidate_events"], 0)
        with database.get_db() as db:
            db.execute("UPDATE events SET status='active'")
        altered = audit_event_projection(self.path)
        self.assertEqual(altered["status"], "invalid")
        self.assertEqual(altered["violations"]["invalid_candidate_events"], 1)

    def test_legacy_cluster_becomes_unknown_candidate_with_version_pinned_evidence(self):
        self._ingest("OpenAI releases a coding platform for developers")
        self._ingest("OpenAI releases a coding platform for developers today")
        self._prepare()
        with database.get_db() as db:
            first = project_legacy_stories(db)
            second = project_legacy_stories(db)
            event = db.execute(
                """SELECT event.status,version.knowledge_status,version.facts_json,
                          version.time_precision,event.last_fact_change_at
                   FROM events AS event
                   JOIN event_versions AS version ON version.id=event.current_version_id"""
            ).fetchone()
            roles = [row[0] for row in db.execute(
                "SELECT role FROM document_event_links ORDER BY document_version_id"
            )]
            evidence = db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0]
            decisions = db.execute(
                "SELECT DISTINCT decision,review_status FROM match_decisions"
            ).fetchall()
        self.assertEqual(first.events_created, 1)
        self.assertEqual(first.links_created, 2)
        self.assertEqual(second.event_versions_created, 0)
        self.assertEqual(second.links_created, 0)
        self.assertEqual(tuple(event), ("candidate", "unknown", "[]", "unknown", None))
        self.assertEqual(roles, ["candidate", "candidate"])
        self.assertEqual(evidence, 2)
        self.assertEqual([tuple(row) for row in decisions], [("candidate_link", "pending")])

    def test_redirected_legacy_story_ids_resolve_to_one_event(self):
        one = self._ingest("OpenAI releases a coding platform for developers")
        two = self._ingest("OpenAI 发布全新开发者编程平台")
        refresh_derived()
        with database.get_db() as db:
            db.execute(
                "UPDATE items SET title_zh='OpenAI 发布全新开发者编程平台' WHERE id=?",
                (one,),
            )
        refresh_derived()
        with database.get_db() as db:
            sync_identity_catalog(db)
            project_legacy_stories(db)
            mappings = db.execute(
                "SELECT story_id,event_id,canonical_story_id FROM legacy_story_events"
            ).fetchall()
            active = db.execute(
                "SELECT COUNT(*) FROM stories WHERE redirect_to IS NULL"
            ).fetchone()[0]
        self.assertGreaterEqual(len(mappings), 2)
        self.assertEqual(len({row["event_id"] for row in mappings}), active)
        redirected = [row for row in mappings if row["story_id"] != row["canonical_story_id"]]
        self.assertTrue(redirected)

    def test_semantic_projection_change_appends_version_and_superseding_links(self):
        self._ingest("OpenAI publishes quarterly results", event_type="earnings")
        self._prepare()
        with database.get_db() as db:
            project_legacy_stories(db)
            story_id = db.execute("SELECT id FROM stories").fetchone()[0]
            db.execute("UPDATE stories SET title='OpenAI corrected quarterly results' WHERE id=?", (story_id,))
            report = project_legacy_stories(db)
            versions = db.execute(
                "SELECT version,title,knowledge_status FROM event_versions ORDER BY version"
            ).fetchall()
            links = db.execute(
                "SELECT supersedes_link_id FROM document_event_links ORDER BY available_at,id"
            ).fetchall()
        self.assertEqual(report.event_versions_created, 1)
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertEqual([row["knowledge_status"] for row in versions], ["unknown", "unknown"])
        self.assertEqual(len(links), 2)
        self.assertIsNotNone(links[-1]["supersedes_link_id"])

    def test_projection_rejects_story_items_without_document_evidence(self):
        with database.get_db() as db:
            db.execute(
                """INSERT INTO items(
                       source_id,url,title,channel,published_at,fetched_at,companies
                   ) VALUES(1,'https://example.com/legacy','Legacy','ai',?,?, '[]')""",
                ("2026-09-17T10:00:00+00:00", "2026-09-17T10:01:00+00:00"),
            )
            db.execute(
                """INSERT INTO stories(
                       id,anchor_item_id,title,channel,url,first_at,last_at,item_count
                   ) VALUES('legacy',1,'Legacy','ai','https://example.com/legacy',?,?,1)""",
                ("2026-09-17T10:00:00+00:00", "2026-09-17T10:00:00+00:00"),
            )
            db.execute(
                """INSERT INTO story_items(item_id,story_id,match_reason,match_score)
                   VALUES(1,'legacy','titles-v3',1)"""
            )
        with self.assertRaisesRegex(EventProjectionError, "without document evidence"):
            with database.get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                project_legacy_stories(db)
        with database.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
