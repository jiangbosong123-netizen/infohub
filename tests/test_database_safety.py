import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app import db_admin
from app.jobs import claim_job, enqueue_job


class DatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "app.db"

    def _legacy_database(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.executescript(database.SCHEMA)
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','ai','rss')"""
            )
            db.execute(
                """INSERT INTO items(source_id,url,title,summary,channel,published_at,fetched_at)
                   VALUES(1,'https://example.com/one','legacy title','legacy summary','ai',
                          '2026-09-14T09:00:00+00:00','2026-09-14T09:01:00+00:00')"""
            )

    def test_new_database_is_versioned_and_idempotent(self):
        first = db_admin.migrate_database(self.path)
        second = db_admin.migrate_database(self.path)
        self.assertEqual(first.previous_state, "empty")
        self.assertEqual(first.applied_versions, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertIsNone(first.backup_path)
        self.assertEqual(second.applied_versions, ())
        self.assertEqual(second.verification.state, "current")
        self.assertEqual(second.current_version, db_admin.CURRENT_SCHEMA_VERSION)

    def test_legacy_upgrade_creates_verified_backup_and_preserves_rows(self):
        self._legacy_database()
        with patch.object(db_admin.config, "APP_VERSION", "release-test-sha"):
            report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_state, "legacy_unversioned")
        self.assertTrue(report.backup_path)
        backup = Path(report.backup_path)
        self.assertTrue(backup.exists())
        self.assertEqual(db_admin.verify_database(backup).state, "legacy_unversioned")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT title,summary FROM items").fetchone(),
                             ("legacy title", "legacy summary"))
            migrations = db.execute(
                "SELECT version,name,checksum,release_id FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(
            migrations,
            [
                (migration.version, migration.name, migration.checksum, "release-test-sha")
                for migration in db_admin.MIGRATIONS
            ],
        )
        with sqlite3.connect(self.path) as db:
            self.assertTrue(db.execute(
                "SELECT applied_at FROM schema_migrations"
            ).fetchone()[0].endswith("Z"))

    def test_failed_migration_rolls_back_schema_and_history(self):
        def fail_after_ddl(db: sqlite3.Connection) -> None:
            db.execute("CREATE TABLE should_rollback(id INTEGER PRIMARY KEY)")
            raise RuntimeError("injected failure")

        failing = db_admin.Migration(1, "failure fixture", "v1", fail_after_ddl)
        with database.get_db(self.path) as db:
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                db_admin.apply_migrations(db, (failing,))
        with sqlite3.connect(self.path) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("should_rollback", tables)
        self.assertNotIn("schema_migrations", tables)

    def test_tampered_history_is_rejected_before_change(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE schema_migrations SET checksum='wrong'")
        with self.assertRaises(db_admin.UnsupportedSchemaError):
            db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute(
                "SELECT checksum FROM schema_migrations"
            ).fetchone()[0], "wrong")

    def test_newer_unknown_version_is_rejected_before_change(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO schema_migrations(
                       version,name,checksum,applied_at,release_id
                   ) VALUES(?,'future','unknown','2026-09-15T00:00:00.000000Z','future')""",
                (db_admin.CURRENT_SCHEMA_VERSION + 1,),
            )
        with self.assertRaisesRegex(db_admin.UnsupportedSchemaError, "newer or unknown"):
            db_admin.migrate_database(self.path)
        self.assertFalse((self.root / "backups").exists())

    def test_old_migration_table_layout_is_rejected(self):
        self._legacy_database()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)""")
            db.execute("""INSERT INTO schema_migrations(version,name,applied_at)
                          VALUES(1,'unrecognized','2026-09-15T00:00:00Z')""")
        with self.assertRaisesRegex(db_admin.UnsupportedSchemaError, "unsupported layout"):
            db_admin.migrate_database(self.path)

    def test_unrecognized_nonempty_database_is_not_modified(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE unrelated(id INTEGER)")
        with self.assertRaises(db_admin.UnsupportedSchemaError):
            db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertEqual(tables, {"unrelated"})

    def test_backup_includes_wal_data_and_never_overwrites(self):
        db_admin.migrate_database(self.path)
        with database.get_db(self.path) as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('wal','WAL','ai','rss')")
        destination = self.root / "manual.db"
        report = db_admin.backup_database(self.path, destination)
        self.assertEqual(report.integrity, "ok")
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("SELECT key FROM sources").fetchone()[0], "wal")
        with self.assertRaises(FileExistsError):
            db_admin.backup_database(self.path, destination)

    def test_init_schema_uses_patched_database_path(self):
        with patch.object(database, "DB_PATH", self.path):
            database.init_schema()
        self.assertEqual(
            db_admin.verify_database(self.path, require_current=True).schema_version,
            db_admin.CURRENT_SCHEMA_VERSION,
        )

    def test_version_one_database_is_backed_up_then_extended(self):
        with database.get_db(self.path) as db:
            applied = db_admin.apply_migrations(
                db, db_admin.MIGRATIONS[:1], release_id="old-release"
            )
        self.assertEqual(applied, (1,))
        with sqlite3.connect(self.path) as db:
            self.assertNotIn(
                "jobs",
                {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )},
            )

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_state, "versioned")
        self.assertEqual(report.previous_version, 1)
        self.assertEqual(report.applied_versions, (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        self.assertEqual(db_admin.verify_database(report.backup_path).schema_version, 1)
        self.assertEqual(report.verification.schema_version, 18)

    def test_version_two_jobs_survive_publication_ledger_upgrade(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:2]), (1, 2)
            )
        with patch.object(database, "DB_PATH", self.path):
            legacy = enqueue_job(
                kind="test",
                idempotency_key="legacy-key",
                input_version="input-v1",
                payload={"value": 1},
                scheduled_for="2026-09-15T00:00:00.000000Z",
            )
            repeated_before = enqueue_job(
                kind="test",
                idempotency_key="legacy-key",
                input_version="input-v1",
                payload={"value": 1},
                scheduled_for="2026-09-15T00:00:00.000000Z",
            )
        self.assertEqual(repeated_before.id, legacy.id)

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 2)
        self.assertEqual(report.applied_versions, (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        with sqlite3.connect(self.path) as db:
            job = db.execute(
                """SELECT id,idempotency_key,logical_idempotency_key,
                          idempotency_scope,dataset_epoch FROM jobs"""
            ).fetchone()
            identity = db.execute(
                "SELECT dataset_id,current_epoch,owner_environment_id FROM dataset_state"
            ).fetchone()
        self.assertEqual(job[0], legacy.id)
        self.assertEqual(job[1], "legacy-key")
        self.assertEqual(job[2], "legacy-key")
        self.assertEqual(job[3], identity[1])
        self.assertEqual(job[4], identity[1])
        self.assertTrue(identity[0].startswith("dataset_"))
        self.assertTrue(identity[1].startswith("epoch_"))
        self.assertTrue(identity[2])
        with patch.object(database, "DB_PATH", self.path):
            repeated_after = enqueue_job(
                kind="test",
                idempotency_key="legacy-key",
                input_version="input-v1",
                payload={"value": 1},
                scheduled_for="2026-09-15T00:00:00.000000Z",
            )
            claimed = claim_job(
                worker_id="migration-test",
                now="2026-09-15T00:00:01.000000Z",
            )
        self.assertEqual(repeated_after.id, legacy.id)
        self.assertEqual(claimed.id, legacy.id)
        self.assertEqual(claimed.dataset_epoch, identity[1])

    def test_version_three_database_adds_ingest_evidence_without_rewriting_items(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:3]), (1, 2, 3)
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','stock','rss')"""
            )
            db.execute(
                """INSERT INTO items(
                       source_id,url,title,summary,channel,published_at,fetched_at
                   ) VALUES(1,'https://example.com/existing','existing','unchanged',
                            'stock','2026-09-16T00:00:00+00:00',
                            '2026-09-16T00:01:00+00:00')"""
            )
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 3)
        self.assertEqual(report.applied_versions, (4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT title,summary FROM items").fetchone(),
                ("existing", "unchanged"),
            )
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(
            {"source_config_versions", "ingest_runs", "raw_records", "raw_observations",
             "source_time_values"}
            <= tables
        )

    def test_version_four_adds_source_times_without_rewriting_raw_evidence(self):
        stamp = "2026-09-16T00:00:00.000000Z"
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:4]), (1, 2, 3, 4)
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','stock','rss')"""
            )
            dataset = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            db.execute(
                """INSERT INTO source_config_versions(
                       id,source_id,version,config_json,config_hash,available_at
                   ) VALUES('cfg',1,1,'{}','hash',?)""", (stamp,)
            )
            db.execute(
                """INSERT INTO ingest_runs(
                       id,source_id,config_version_id,dataset_id,dataset_epoch,
                       scheduled_for,started_at,finished_at,status,trace_id
                   ) VALUES('run',1,'cfg',?,?,?,?,?,'succeeded','trace')""",
                (dataset[0], dataset[1], stamp, stamp, stamp),
            )
            db.execute(
                """INSERT INTO raw_records(
                       id,first_ingest_run_id,source_id,external_id,observed_at,ingested_at,
                       final_url,selected_headers,media_type,encoding,payload_sha256,payload_ref,
                       payload_kind,truncated,size_bytes,retention_class
                   ) VALUES('raw','run',1,'entry-1',?,?,'https://example.com/1','{}',
                            'application/json','utf-8',?,?,'feed_entry',0,2,'private')""",
                (stamp, stamp, "a" * 64, "sha256/aa/" + "a" * 64),
            )
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 4)
        self.assertEqual(report.applied_versions, (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            raw = db.execute(
                "SELECT external_id,payload_sha256,payload_ref FROM raw_records"
            ).fetchone()
            columns = {row[1] for row in db.execute("PRAGMA table_info(source_time_values)")}
        self.assertEqual(raw, ("entry-1", "a" * 64, "sha256/aa/" + "a" * 64))
        self.assertIn("rule_version", columns)

    def test_version_five_adds_empty_document_tables_without_rewriting_items(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:5]),
                (1, 2, 3, 4, 5),
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','stock','rss')"""
            )
            db.execute(
                """INSERT INTO items(
                       source_id,url,title,summary,channel,published_at,fetched_at
                   ) VALUES(1,'https://example.com/existing','existing','unchanged',
                            'stock','2026-09-16T00:00:00+00:00',
                            '2026-09-16T00:01:00+00:00')"""
            )
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 5)
        self.assertEqual(report.applied_versions, (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                db.execute("SELECT title,summary FROM items").fetchone(),
                ("existing", "unchanged"),
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_version_six_adds_backfill_state_without_rewriting_documents(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:6]),
                (1, 2, 3, 4, 5, 6),
            )
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','stock','rss')"""
            )
            db.execute(
                """INSERT INTO items(
                       source_id,url,title,summary,channel,published_at,fetched_at
                   ) VALUES(1,'https://example.com/existing','existing','unchanged',
                            'stock','2026-09-16T00:00:00+00:00',
                            '2026-09-16T00:01:00+00:00')"""
            )
            db.execute(
                """INSERT INTO documents(
                       id,dataset_id,legacy_item_id,kind,first_seen_at,status
                   ) VALUES('doc',?,1,'article',
                            '2026-09-16T00:00:00.000000Z','active')""",
                (dataset_id,),
            )
            db.execute(
                """INSERT INTO document_versions(
                       id,document_id,version,normalizer_version,normalized_at,
                       title_original,language,text,content_sha256,version_sha256,
                       canonical_url,source_id,published_precision,time_status,
                       time_rule_version,tzdb_version,content_origin,content_extent,
                       truncated,extraction_status,correction_kind,available_at
                   ) VALUES('version','doc',1,'normalizer-v1',
                            '2026-09-16T00:00:00.000000Z','existing','en','unchanged',
                            ?,?,'https://example.com/existing',1,'second','parsed',
                            'source-time-v1','system','publisher_text','full',0,
                            'complete','initial','2026-09-16T00:00:00.000000Z')""",
                ("a" * 64, "b" * 64),
            )
            db.execute(
                "UPDATE documents SET current_version_id='version' WHERE id='doc'"
            )

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 6)
        self.assertEqual(report.applied_versions, (7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            version = db.execute(
                """SELECT title_original,availability_basis,point_in_time_eligible
                   FROM document_versions WHERE id='version'"""
            ).fetchone()
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertEqual(version, ("existing", "transaction_recorded", 0))
        self.assertTrue(
            {"legacy_backfill_state", "legacy_backfill_sources",
             "legacy_object_mappings", "legacy_report_identities"} <= tables
        )

    def test_version_seven_adds_empty_identity_catalog_without_rewriting_legacy(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:7]),
                (1, 2, 3, 4, 5, 6, 7),
            )
            db.execute(
                """INSERT INTO companies(slug,name,name_zh,market,aliases)
                   VALUES('fixture','Fixture','示例','PRIVATE','[]')"""
            )
            before = tuple(db.execute("SELECT * FROM companies").fetchone())

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 7)
        self.assertEqual(report.applied_versions, (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute("SELECT * FROM companies").fetchone())
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM entities),
                   (SELECT COUNT(*) FROM publishers),
                   (SELECT COUNT(*) FROM topic_catalog)"""
            ).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(counts, (0, 0, 0))

    def test_version_eight_adds_empty_sec_projection_without_rewriting_catalog(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:8]),
                (1, 2, 3, 4, 5, 6, 7, 8),
            )
            db.execute(
                """INSERT INTO companies(slug,name,name_zh,market,aliases)
                   VALUES('fixture','Fixture','示例','US','[]')"""
            )
            before = tuple(db.execute("SELECT * FROM companies").fetchone())

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 8)
        self.assertEqual(report.applied_versions, (9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute("SELECT * FROM companies").fetchone())
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM sec_security_keys),
                   (SELECT COUNT(*) FROM sec_filings),
                   (SELECT COUNT(*) FROM sec_filing_versions)"""
            ).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(counts, (0, 0, 0))

    def test_version_nine_adds_empty_event_projection_without_rewriting_stories(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:9]),
                (1, 2, 3, 4, 5, 6, 7, 8, 9),
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,type)
                   VALUES('fixture','Fixture','ai','rss')"""
            )
            db.execute(
                """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
                   VALUES(1,'https://example.com/a','A','ai',?,?)""",
                ("2026-09-17T10:00:00+00:00", "2026-09-17T10:01:00+00:00"),
            )
            db.execute(
                """INSERT INTO stories(
                       id,anchor_item_id,title,channel,url,first_at,last_at,item_count
                   ) VALUES('story-a',1,'A','ai','https://example.com/a',?,?,1)""",
                ("2026-09-17T10:00:00+00:00", "2026-09-17T10:00:00+00:00"),
            )
            before = tuple(db.execute("SELECT * FROM stories").fetchone())

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 9)
        self.assertEqual(report.applied_versions, (10, 11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute("SELECT * FROM stories").fetchone())
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM events),
                   (SELECT COUNT(*) FROM event_versions),
                   (SELECT COUNT(*) FROM legacy_story_events)"""
            ).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(counts, (0, 0, 0))

    def test_version_ten_adds_empty_event_relations_without_rewriting_events(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:10]),
                (1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
            )
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO events(
                       id,dataset_id,first_seen_at,latest_report_at,status
                   ) VALUES('event-a',?,?,?,'candidate')""",
                (dataset_id, "2026-09-17T10:00:00Z", "2026-09-17T10:00:00Z"),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,time_precision,
                       primary_entities_json,object_entities_json,facts_json,topics_json,
                       knowledge_status,version_sha256,available_at,created_by,method_version
                   ) VALUES('event-version-a','event-a',1,'event-candidate-v1','A','other',
                            'unknown','[]','[]','[]','[]','unknown',?,?,'test','test-v1')""",
                ("a" * 64, "2026-09-17T10:00:00Z"),
            )
            db.execute(
                "UPDATE events SET current_version_id='event-version-a' WHERE id='event-a'"
            )
            before = tuple(db.execute("SELECT * FROM events").fetchone())

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 10)
        self.assertEqual(report.applied_versions, (11, 12, 13, 14, 15, 16, 17, 18))
        self.assertTrue(report.backup_path)
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute("SELECT * FROM events").fetchone())
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM event_relations),
                   (SELECT COUNT(*) FROM event_merges)"""
            ).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(counts, (0, 0))

    def test_version_eleven_adds_empty_terminal_transitions_without_rewriting_events(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:11]),
                (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
            )
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO events(
                       id,dataset_id,first_seen_at,latest_report_at,status
                   ) VALUES('event-a',?,?,?,'candidate')""",
                (dataset_id, "2026-09-17T10:00:00Z", "2026-09-17T10:00:00Z"),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,time_precision,
                       primary_entities_json,object_entities_json,facts_json,topics_json,
                       knowledge_status,version_sha256,available_at,created_by,method_version
                   ) VALUES('event-version-a','event-a',1,'event-candidate-v1','A','other',
                            'unknown','[]','[]','[]','[]','unknown',?,?,'test','test-v1')""",
                ("a" * 64, "2026-09-17T10:00:00Z"),
            )
            db.execute(
                "UPDATE events SET current_version_id='event-version-a' WHERE id='event-a'"
            )
            before = tuple(db.execute("SELECT * FROM events").fetchone())

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 11)
        self.assertEqual(report.applied_versions, (12,13, 14, 15, 16, 17, 18))
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute("SELECT * FROM events").fetchone())
            counts = tuple(db.execute(
                """SELECT
                   (SELECT COUNT(*) FROM event_splits),
                   (SELECT COUNT(*) FROM event_split_replacements),
                   (SELECT COUNT(*) FROM event_split_assignments),
                   (SELECT COUNT(*) FROM event_retractions)"""
            ).fetchone())
            merge_columns = {
                row[1] for row in db.execute("PRAGMA table_info(event_merges)")
            }
        self.assertEqual(before, after)
        self.assertEqual(counts, (0, 0, 0, 0))
        self.assertIn("previous_status", merge_columns)

    def test_version_twelve_adds_empty_event_revisions_without_rewriting_events(self):
        with database.get_db(self.path) as db:
            self.assertEqual(
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:12]),
                (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12),
            )
            dataset_id = db.execute(
                "SELECT dataset_id FROM dataset_state WHERE singleton=1"
            ).fetchone()[0]
            db.execute(
                """INSERT INTO events(id,dataset_id,first_seen_at,latest_report_at,status)
                   VALUES('event-a',?,?,?,'candidate')""",
                (dataset_id, "2026-09-17T10:00:00Z", "2026-09-17T10:00:00Z"),
            )
            db.execute(
                """INSERT INTO event_versions(
                       id,event_id,version,schema_version,title,event_type,time_precision,
                       primary_entities_json,object_entities_json,facts_json,topics_json,
                       knowledge_status,version_sha256,available_at,created_by,method_version
                   ) VALUES('event-version-a','event-a',1,'event-candidate-v1','A','other',
                            'unknown','[]','[]','[]','[]','unknown',?,?,'test','test-v1')""",
                ("a" * 64, "2026-09-17T10:00:00Z"),
            )
            db.execute(
                "UPDATE events SET current_version_id='event-version-a' WHERE id='event-a'"
            )
            before = tuple(db.execute("SELECT * FROM events").fetchone())

        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.previous_version, 12)
        self.assertEqual(report.applied_versions, (13,14, 15, 16, 17, 18))
        with sqlite3.connect(self.path) as db:
            after = tuple(db.execute("SELECT * FROM events").fetchone())
            count = db.execute("SELECT COUNT(*) FROM event_revisions").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(count, 0)

    def test_current_schema_rejects_a_missing_dataset_identity(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM dataset_state")
        with self.assertRaisesRegex(
            db_admin.DatabaseVerificationError, "exactly one valid dataset identity"
        ):
            db_admin.verify_database(self.path, require_current=True)

    def test_document_time_status_reserves_honest_legacy_mapping(self):
        db_admin.migrate_database(self.path)
        with sqlite3.connect(self.path) as db:
            definition = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_versions'"
            ).fetchone()[0]
        self.assertIn("legacy_unverified", definition)


if __name__ == "__main__":
    unittest.main()
