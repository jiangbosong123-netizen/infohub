import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.db_admin import verify_database
from app.legacy_topic_backfill import (
    LegacyTopicBackfillError,
    backfill_legacy_topics_batch,
)


NOW = "2026-09-23T10:00:00.000000Z"


class LegacyTopicBackfillTests(unittest.TestCase):
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

    def _seed(self, *, topics=("alpha", "beta")):
        with database.get_db() as db:
            dataset = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO sources(id,key,name,channel,type)
                   VALUES(1,'fixture','Fixture','ai','rss')"""
            )
            db.execute(
                """INSERT INTO items(
                       id,source_id,url,title,summary,channel,published_at,fetched_at
                   ) VALUES(1,1,'https://example.com/1','Title','Summary','ai',?,?)""",
                (NOW, NOW),
            )
            db.execute(
                """INSERT INTO documents(
                       id,dataset_id,legacy_item_id,kind,first_seen_at,status
                   ) VALUES('doc-1',?,1,'article',?,'active')""",
                (dataset, NOW),
            )
            db.execute(
                """INSERT INTO document_versions(
                       id,document_id,version,normalizer_version,normalized_at,
                       title_original,language,text,content_sha256,version_sha256,
                       canonical_url,source_id,published_at,published_precision,time_status,
                       time_rule_version,tzdb_version,content_origin,content_extent,truncated,
                       extraction_status,correction_kind,available_at,
                       availability_basis,point_in_time_eligible
                   ) VALUES(
                       'docv-1','doc-1',1,'fixture',?,'Title','en','Summary',?,?,
                       'https://example.com/1',1,?,'second','legacy_unverified',
                       'legacy','unknown','legacy_unknown','excerpt',0,
                       'partial','initial',?,'legacy_unknown',0
                   )""",
                (NOW, "a" * 64, "b" * 64, NOW, NOW),
            )
            db.execute("UPDATE documents SET current_version_id='docv-1' WHERE id='doc-1'")
            db.execute(
                """INSERT INTO legacy_object_mappings(
                       dataset_id,resource_type,legacy_key,target_type,target_id,
                       legacy_sha256,mapping_status,detail_json,available_at
                   ) VALUES(?,'item','1','document','doc-1',?,'mapped_unverified','{}',?)""",
                (dataset, "c" * 64, NOW),
            )
            db.execute(
                """INSERT INTO legacy_backfill_state(
                       singleton,dataset_id,cutoff_item_id,cutoff_report_id,status,
                       started_at,updated_at,finished_at
                   ) VALUES(1,?,1,0,'completed',?,?,?)""",
                (dataset, NOW, NOW, NOW),
            )
            for position, slug in enumerate(topics):
                db.execute(
                    """INSERT INTO topics(slug,name,group_key,description,rules,position)
                       VALUES(?,?, 'technology','fixture','{}',?)""",
                    (slug, slug.title(), position),
                )
                topic_id = f"topic-{slug}"
                version_id = f"topicv-{slug}"
                db.execute(
                    """INSERT INTO topic_catalog(id,dataset_id,status,created_at)
                       VALUES(?,?,'active',?)""",
                    (topic_id, dataset, NOW),
                )
                db.execute(
                    """INSERT INTO topic_versions(
                           id,topic_id,version,slug,name,group_key,description,rules_json,
                           rules_hash,version_sha256,status,available_at
                       ) VALUES(?,?,1,?,?,'technology','fixture','{}',?,?,'active',?)""",
                    (version_id, topic_id, slug, slug.title(), "d" * 64, "e" * 64, NOW),
                )
                db.execute(
                    "UPDATE topic_catalog SET current_version_id=? WHERE id=?",
                    (version_id, topic_id),
                )
                db.execute(
                    "INSERT INTO topic_slug_aliases(slug,topic_id,available_at) VALUES(?,?,?)",
                    (slug, topic_id, NOW),
                )
                db.execute(
                    "INSERT INTO item_topics(item_id,topic_slug,evidence) VALUES(1,?,?)",
                    (slug, f'["legacy-{slug}"]'),
                )

    def test_freezes_and_resumably_imports_candidate_assertions(self):
        self._seed()
        first = backfill_legacy_topics_batch(1)
        self.assertEqual(first.status, "running")
        self.assertEqual((first.snapshot_count, first.mapped_count), (2, 1))
        with database.get_db() as db:
            db.execute(
                "UPDATE item_topics SET evidence='[\"changed-later\"]' WHERE topic_slug='beta'"
            )
        second = backfill_legacy_topics_batch(1)
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.unexplained_count, 0)
        repeated = backfill_legacy_topics_batch(10)
        self.assertEqual(repeated.batch_processed, 0)
        with database.get_db() as db:
            rows = db.execute(
                """SELECT snapshot.topic_slug,snapshot.evidence_text,
                          assignment.method,assignment.method_version,
                          assignment.status,assignment.evidence_ids_json,
                          assignment.document_version_id,assignment.topic_version_id
                   FROM legacy_topic_assignment_snapshot AS snapshot
                   JOIN legacy_topic_assignment_mappings AS mapping
                     ON mapping.item_id=snapshot.item_id
                    AND mapping.topic_slug=snapshot.topic_slug
                   JOIN document_topic_assignments AS assignment
                     ON assignment.id=mapping.assignment_id
                   ORDER BY snapshot.topic_slug"""
            ).fetchall()
        self.assertEqual(rows[1][1], '["legacy-beta"]')
        self.assertEqual(
            tuple(rows[0][2:]),
            ("legacy_projection", "legacy-item-topics-v1", "candidate", "[]", "docv-1", "topicv-alpha"),
        )
        verify_database(self.path, require_current=True)

    def test_manifest_preserves_exact_legacy_evidence_hash(self):
        self._seed(topics=("alpha",))
        report = backfill_legacy_topics_batch(10)
        with database.get_db() as db:
            row = db.execute(
                """SELECT evidence_text,evidence_sha256,captured_at
                   FROM legacy_topic_assignment_snapshot"""
            ).fetchone()
        self.assertEqual(row[0], '["legacy-alpha"]')
        self.assertEqual(
            row[1], hashlib.sha256(row[0].encode("utf-8")).hexdigest()
        )
        self.assertEqual(report.source_count, 1)
        self.assertEqual(len(report.source_sha256), 64)
        self.assertEqual(len(report.manifest_sha256), 64)

    def test_unresolved_topic_fails_before_freezing_partial_manifest(self):
        self._seed(topics=("alpha",))
        with database.get_db() as db:
            db.execute(
                """INSERT INTO topics(slug,name,group_key,description,rules,position)
                   VALUES('missing','Missing','technology','fixture','{}',99)"""
            )
            db.execute(
                """INSERT INTO item_topics(item_id,topic_slug,evidence)
                   VALUES(1,'missing','[]')"""
            )
        with self.assertRaisesRegex(LegacyTopicBackfillError, "no frozen"):
            backfill_legacy_topics_batch(10)
        with database.get_db() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM legacy_topic_backfill_state").fetchone()[0], 0
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM legacy_topic_assignment_snapshot").fetchone()[0], 0
            )

    def test_frozen_snapshot_and_mapping_are_immutable(self):
        self._seed(topics=("alpha",))
        backfill_legacy_topics_batch(10)
        with database.get_db() as db:
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute(
                    "UPDATE legacy_topic_assignment_snapshot SET evidence_text='rewritten'"
                )
            with self.assertRaisesRegex(Exception, "immutable"):
                db.execute("DELETE FROM legacy_topic_assignment_mappings")
            with self.assertRaisesRegex(Exception, "already frozen"):
                db.execute(
                    """INSERT INTO legacy_topic_assignment_snapshot(
                           item_id,topic_slug,evidence_text,evidence_sha256,
                           document_version_id,topic_version_id,captured_at
                       ) VALUES(2,'late','[]',?,'docv-1','topicv-alpha',?)""",
                    ("f" * 64, NOW),
                )


if __name__ == "__main__":
    unittest.main()
