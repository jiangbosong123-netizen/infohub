import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database, db_admin
from app.api_auth import create_consumer, issue_api_key


T0 = "2026-09-27T00:00:00.000000Z"
T1 = "2026-09-27T00:01:00.000000Z"
T2 = "2026-09-28T00:00:00.000000Z"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SyncSnapshotSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "snapshot.db"
        report = db_admin.migrate_database(self.path)
        self.assertEqual(report.current_version, 41)
        now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            consumer = create_consumer(db, "snapshot-consumer", actor="test")
            self.key = issue_api_key(
                db, consumer, {"read:sync", "read:items"},
                expires_at=now + timedelta(days=2), actor="test",
            )
            state = db.execute(
                "SELECT dataset_id,current_epoch FROM dataset_state WHERE singleton=1"
            ).fetchone()
            self.dataset_id = state["dataset_id"]
            self.epoch = state["current_epoch"]
            db.execute(
                """INSERT INTO knowledge_checkpoints(
                       id,dataset_id,epoch,high_water,observed_at,clock_status)
                   VALUES('checkpoint-0',?,?,0,?,'unknown')""",
                (self.dataset_id, self.epoch, T0),
            )

    def _request(self, db, snapshot_id="snapshot-1"):
        request = {
            "resources": ["items"], "scope": "research",
        }
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        db.execute(
            """INSERT INTO sync_snapshot_requests(
                   id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                   idempotency_key,request_json,request_sha256,resources_json,
                   scopes_json,projection_scope,state,created_at,expires_at)
               VALUES(?,?,?,?,?,1,?,?,?,?,?,'research','pending',?,?)""",
            (
                snapshot_id, self.dataset_id, self.epoch, self.key.consumer_id,
                self.key.key_id, f"request-{snapshot_id}", request_json,
                digest(request_json), '["items"]', '["read:items","read:sync"]',
                T0, T2,
            ),
        )

    def _snapshot(self, db, snapshot_id="snapshot-1", *, record_count=2):
        db.execute(
            """INSERT INTO sync_snapshots(
                   id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                   projection_scope,high_water,knowledge_checkpoint_id,backup_sha256,
                   source_schema_version,manifest_json,manifest_sha256,resource_count,
                   record_count,snapshot_schema_version,created_at,ready_at,expires_at)
               VALUES(?,?,?,?,?,1,'research',0,'checkpoint-0',?,39,'{}',?,1,?,
                      'sync-snapshot-v1',?,?,?)""",
            (
                snapshot_id, self.dataset_id, self.epoch, self.key.consumer_id,
                self.key.key_id, digest("backup"), digest("{}"), record_count,
                T0, T1, T2,
            ),
        )

    def test_complete_snapshot_is_immutable_and_ready_is_derived_from_manifest(self):
        with database.get_db(self.path) as db:
            self._request(db)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """UPDATE sync_snapshot_requests
                       SET state='ready',started_at=?,finished_at=? WHERE id='snapshot-1'""",
                    (T0, T1),
                )
            db.execute(
                "UPDATE sync_snapshot_requests SET state='running',started_at=? WHERE id='snapshot-1'",
                (T0,),
            )
            db.execute(
                """INSERT INTO sync_snapshot_resources(
                       snapshot_id,resource,record_count,page_count,content_sha256)
                   VALUES('snapshot-1','items',2,1,?)""",
                (digest("records"),),
            )
            db.execute(
                """INSERT INTO sync_snapshot_pages(
                       snapshot_id,resource,page_number,first_resource_id,last_resource_id,
                       record_count,payload_ref,payload_sha256,size_bytes)
                   VALUES('snapshot-1','items',1,'item-a','item-b',2,
                          'snapshots/snapshot-1/items/1.json',?,100)""",
                (digest("page"),),
            )
            self._snapshot(db)
            db.execute(
                """UPDATE sync_snapshot_requests
                   SET state='ready',finished_at=? WHERE id='snapshot-1'""",
                (T1,),
            )
            self.assertEqual(
                db.execute(
                    "SELECT state FROM sync_snapshot_requests WHERE id='snapshot-1'"
                ).fetchone()[0],
                "ready",
            )
            for statement in (
                "UPDATE sync_snapshots SET record_count=3 WHERE id='snapshot-1'",
                "UPDATE sync_snapshot_resources SET record_count=3 WHERE snapshot_id='snapshot-1'",
                "UPDATE sync_snapshot_pages SET size_bytes=101 WHERE snapshot_id='snapshot-1'",
                "DELETE FROM sync_snapshot_requests WHERE id='snapshot-1'",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(statement)
        self.assertEqual(
            db_admin.verify_database(self.path, require_current=True).schema_version, 41
        )

    def test_incomplete_or_identity_mismatched_snapshot_fails_closed(self):
        with database.get_db(self.path) as db:
            self._request(db, "snapshot-2")
            db.execute(
                "UPDATE sync_snapshot_requests SET state='running',started_at=? WHERE id='snapshot-2'",
                (T0,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO sync_snapshot_resources(
                           snapshot_id,resource,record_count,page_count,content_sha256)
                       VALUES('snapshot-2','events',0,0,?)""",
                    (digest(""),),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self._snapshot(db, "snapshot-2", record_count=1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO sync_snapshots(
                           id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                           projection_scope,high_water,knowledge_checkpoint_id,backup_sha256,
                           source_schema_version,manifest_json,manifest_sha256,resource_count,
                           record_count,snapshot_schema_version,created_at,ready_at,expires_at)
                       VALUES('wrong',?,?,?,?,1,'research',0,'checkpoint-0',?,39,'{}',?,1,0,
                              'sync-snapshot-v1',?,?,?)""",
                    (
                        self.dataset_id, self.epoch, self.key.consumer_id, self.key.key_id,
                        digest("backup"), digest("{}"), T0, T1, T2,
                    ),
                )

    def test_idempotency_is_bound_to_key_and_authorization_version(self):
        with database.get_db(self.path) as db:
            self._request(db, "snapshot-3")
            request = db.execute(
                "SELECT * FROM sync_snapshot_requests WHERE id='snapshot-3'"
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO sync_snapshot_requests(
                           id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                           idempotency_key,request_json,request_sha256,resources_json,
                           scopes_json,projection_scope,state,created_at,expires_at)
                       VALUES('snapshot-duplicate',?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (
                        request["dataset_id"], request["dataset_epoch"], request["consumer_id"],
                        request["key_id"], request["authz_version"], request["idempotency_key"],
                        request["request_json"], request["request_sha256"],
                        request["resources_json"], request["scopes_json"],
                        request["projection_scope"], request["created_at"], request["expires_at"],
                    ),
                )

    def test_resource_and_scope_manifests_are_nonempty_unique_allowlists(self):
        with database.get_db(self.path) as db:
            for resources, scopes in (
                ('[]', '["read:sync"]'),
                ('["items","items"]', '["read:items","read:sync"]'),
                ('["unknown"]', '["read:sync"]'),
                ('["items"]', '[]'),
                ('["items"]', '["read:sync","read:sync"]'),
                ('["items"]', '["write:items"]'),
            ):
                with self.subTest(resources=resources, scopes=scopes):
                    request_json = json.dumps({"resources": json.loads(resources)})
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(
                            """INSERT INTO sync_snapshot_requests(
                                   id,dataset_id,dataset_epoch,consumer_id,key_id,authz_version,
                                   idempotency_key,request_json,request_sha256,resources_json,
                                   scopes_json,projection_scope,state,created_at,expires_at)
                               VALUES(?,?,?,?,?,1,?,?,?,?,?,'research','pending',?,?)""",
                            (
                                f"invalid-{digest(resources + scopes)[:12]}", self.dataset_id,
                                self.epoch, self.key.consumer_id, self.key.key_id,
                                f"key-{digest(resources + scopes)}", request_json,
                                digest(request_json), resources, scopes, T0, T2,
                            ),
                        )

    def test_schema_38_upgrades_through_40_without_domain_row_changes(self):
        prior = self.path.parent / "schema-38.db"
        with database.get_db(prior) as db:
            applied = db_admin.apply_migrations(db, db_admin.MIGRATIONS[:38])
            self.assertEqual(applied[-1], 38)
            db.execute(
                """INSERT INTO sources(
                       key,name,channel,tier,type,url,enabled,interval_minutes)
                   VALUES('fixture','Fixture','ai','media','rss',
                          'https://example.test/feed',1,60)"""
            )
            source_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                """INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
                   VALUES(?,'https://example.test/kept','Kept','ai',?,?)""",
                (source_id, T0, T0),
            )
        report = db_admin.migrate_database(prior)
        self.assertEqual(report.previous_version, 38)
        self.assertEqual(report.applied_versions, (39, 40, 41))
        with database.get_db(prior) as db:
            self.assertEqual(db.execute("SELECT title FROM items").fetchone()[0], "Kept")
            for table in (
                "sync_snapshot_requests", "sync_snapshots",
                "sync_snapshot_resources", "sync_snapshot_pages",
            ):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
