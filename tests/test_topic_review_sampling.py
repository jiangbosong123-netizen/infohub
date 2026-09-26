import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database, db_admin
from app.topic_assignment_reviews import record_topic_assignment_review
from app.topic_review_export import (
    TopicReviewExportError,
    build_topic_review_export,
    export_topic_review_sample,
    verify_topic_review_export,
)
from app.topic_review_sampling import (
    create_sample_batch,
    sample_queue,
    sample_report,
)


NOW = "2026-09-24T09:00:00.000000Z"
LATER = "2026-09-24T09:01:00.000000Z"


class TopicReviewSamplingTests(unittest.TestCase):
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
            db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
            for index, topic_id in enumerate(("t1", "t2"), 1):
                topic_version_id = f"tv{index}"
                db.execute(
                    "INSERT INTO topic_catalog(id,dataset_id,status,created_at) VALUES(?,?,'active',?)",
                    (topic_id, dataset, NOW),
                )
                db.execute(
                    """INSERT INTO topic_versions(
                           id,topic_id,version,slug,name,group_key,description,rules_json,
                           rules_hash,version_sha256,status,available_at)
                       VALUES(?,?,1,?,?,?,'','{}',?,?,'active',?)""",
                    (topic_version_id, topic_id, f"topic-{index}", f"Topic {index}",
                     "technology", chr(96 + index) * 64, chr(98 + index) * 64, NOW),
                )
                db.execute(
                    "UPDATE topic_catalog SET current_version_id=? WHERE id=?",
                    (topic_version_id, topic_id),
                )
            for index, topic_version_id in enumerate(("tv1", "tv1", "tv1", "tv2", "tv2", "tv2"), 1):
                self._insert_assignment(db, index, topic_version_id)
            record_topic_assignment_review(
                db, assignment_id="a6", decision="accepted",
                expected_previous_review_id=None, reviewer_id="fixture",
                reason="Exclude one already decided assignment.", now=NOW,
            )

    def _insert_assignment(self, db, index: int, topic_version_id: str) -> None:
        item_id = index
        document_id, version_id, assignment_id = f"d{index}", f"dv{index}", f"a{index}"
        db.execute(
            """INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
               VALUES(?,1,?,?, 'ai',?,?)""",
            (item_id, f"https://example.test/{index}", f"Title {index}", NOW, NOW),
        )
        db.execute(
            "INSERT INTO documents(id,dataset_id,legacy_item_id,kind,first_seen_at) VALUES(?,?,?,'article',?)",
            (document_id, self.dataset, item_id, NOW),
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
            (version_id, document_id, NOW, f"Title {index}",
             f"{index:064x}", f"{index + 100:064x}",
             f"https://example.test/{index}", NOW),
        )
        db.execute(
            "UPDATE documents SET current_version_id=? WHERE id=?", (version_id, document_id)
        )
        db.execute(
            """INSERT INTO document_topic_assignments(
                   id,document_version_id,topic_version_id,method,method_version,status,available_at)
               VALUES(?,?,?,'fixture','fixture-v1','candidate',?)""",
            (assignment_id, version_id, topic_version_id, NOW),
        )

    def test_batch_is_deterministic_frozen_and_reports_later_decisions(self):
        with database.get_db() as db:
            db.execute("BEGIN IMMEDIATE")
            first = create_sample_batch(
                db, seed="evaluation-v1", per_topic_limit=2,
                created_by="review-lead", now=NOW,
            )
            repeated = create_sample_batch(
                db, seed="evaluation-v1", per_topic_limit=2,
                created_by="another-user", now=NOW,
            )
            self.assertEqual(first.batch_id, repeated.batch_id)
            self.assertEqual((first.candidate_count, first.topic_count, first.member_count), (5, 2, 4))
            initial = sample_queue(db, first.batch_id, limit=10)
            self.assertEqual(len(initial), 4)
            self.assertEqual({item.topic_id for item in initial}, {"t1", "t2"})
            selected_assignment = initial[0].assignment_id
            record_topic_assignment_review(
                db, assignment_id=selected_assignment, decision="accepted",
                expected_previous_review_id=None, reviewer_id="human",
                reason="Source evidence checked.", now=LATER,
            )
            self._insert_assignment(db, 7, "tv1")
            report = sample_report(db, first.batch_id)
            self.assertEqual((report.accepted, report.rejected, report.pending), (1, 0, 3))
            self.assertEqual((report.decided_bps, report.acceptance_bps), (2500, 10000))
            self.assertNotIn("a7", {item.assignment_id for item in sample_queue(
                db, first.batch_id, limit=10, pending_only=False
            )})
            self.assertEqual(len(sample_queue(db, first.batch_id, limit=10)), 3)
        db_admin.verify_database(self.path, require_current=True)

    def test_batches_members_and_review_order_are_immutable(self):
        with database.get_db() as db:
            batch = create_sample_batch(
                db, seed="immutability", per_topic_limit=1,
                created_by="review-lead", now=NOW,
            )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE topic_review_sampling_batches SET seed='rewrite' WHERE id=?",
                    (batch.batch_id,),
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "DELETE FROM topic_review_sampling_members WHERE batch_id=?",
                    (batch.batch_id,),
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("UPDATE topic_assignment_review_order SET sequence=99")

    def test_point_in_time_export_is_deterministic_and_contains_review_provenance(self):
        first_path = self.path.parent / "review-one.json"
        second_path = self.path.parent / "review-two.json"
        with database.get_db() as db:
            batch = create_sample_batch(
                db, seed="export", per_topic_limit=1,
                created_by="review-lead", now=NOW,
            )
            item = sample_queue(db, batch.batch_id, limit=1)[0]
            before = build_topic_review_export(
                db, batch.batch_id,
                review_cutoff_sequence=batch.review_cutoff_sequence,
            )
            self.assertIsNone(before["payload"]["members"][0]["review"])
            review = record_topic_assignment_review(
                db, assignment_id=item.assignment_id, decision="accepted",
                expected_previous_review_id=None, reviewer_id="human",
                reason="Evidence and source checked.", now=LATER,
            )
            cutoff = db.execute(
                "SELECT sequence FROM topic_assignment_review_order WHERE review_id=?",
                (review.current_review_id,),
            ).fetchone()[0]
            current = build_topic_review_export(
                db, batch.batch_id, review_cutoff_sequence=cutoff,
            )
            exported = current["payload"]["members"][0]["review"]
            self.assertEqual(exported["id"], review.current_review_id)
            self.assertEqual(exported["review_sequence"], cutoff)
            self.assertEqual(exported["reviewer_id"], "human")
            first = export_topic_review_sample(
                db, batch.batch_id, first_path,
                review_cutoff_sequence=cutoff, exported_at=NOW,
            )
            second = export_topic_review_sample(
                db, batch.batch_id, second_path,
                review_cutoff_sequence=cutoff, exported_at=LATER,
            )
        self.assertEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(verify_topic_review_export(first_path), first)
        if os.name != "nt":
            self.assertEqual(first_path.stat().st_mode & 0o777, 0o600)
        with database.get_db() as db:
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                export_topic_review_sample(db, batch.batch_id, first_path)

    def test_export_rejects_future_cutoff_and_tampering(self):
        export_path = self.path.parent / "review.json"
        with database.get_db() as db:
            batch = create_sample_batch(
                db, seed="tamper", per_topic_limit=1,
                created_by="review-lead", now=NOW,
            )
            current = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM topic_assignment_review_order"
            ).fetchone()[0]
            with self.assertRaisesRegex(TopicReviewExportError, "beyond"):
                build_topic_review_export(
                    db, batch.batch_id, review_cutoff_sequence=current + 1
                )
            export_topic_review_sample(db, batch.batch_id, export_path)
        document = json.loads(export_path.read_text(encoding="utf-8"))
        document["payload"]["members"][0]["title"] = "tampered"
        export_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(TopicReviewExportError, "digest"):
            verify_topic_review_export(export_path)

    def test_migration_31_preserves_schema_30_review_state(self):
        predecessor = self.path.parent / "predecessor.db"
        with database.get_db(predecessor) as db:
            db_admin.apply_migrations(db, db_admin.MIGRATIONS[:30])
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('x','X','ai','rss')")
        report = db_admin.migrate_database(predecessor)
        self.assertEqual(report.applied_versions, (31, 32, 33, 34, 35, 36))
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 30)
        with database.get_db(predecessor) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "x")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM topic_review_sampling_batches"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
