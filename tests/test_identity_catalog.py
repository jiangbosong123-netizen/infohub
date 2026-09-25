import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.catalog import sync_identity_catalog, sync_topic_catalog
from app.crawler import runner
from app.db_admin import DatabaseVerificationError, verify_database
from app.topics import sync_topics
from cli import _load_companies


class IdentityCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "app.db"
        for item in (
            patch.object(database, "DB_PATH", self.path),
            patch.object(config, "DB_PATH", self.path),
        ):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()

    def _load_configured_catalog(self):
        _load_companies()
        runner.upsert_sources()
        with database.get_db() as db:
            sync_topics(db)
            return sync_identity_catalog(db)

    def test_watchlist_is_shadowed_without_promoting_people_or_market_guesses(self):
        report = self._load_configured_catalog()
        self.assertEqual(report.companies_seen, 23)
        with database.get_db() as db:
            us_entities = db.execute(
                """SELECT COUNT(*) FROM companies AS company
                   JOIN legacy_company_entities AS mapping ON mapping.company_id=company.id
                   JOIN entities AS entity ON entity.id=mapping.entity_id
                   WHERE company.market='US' AND entity.type='organization'
                     AND entity.current_version_id IS NOT NULL"""
            ).fetchone()[0]
            elon = db.execute(
                """SELECT alias.match_mode,alias.ambiguity
                   FROM entity_aliases AS alias
                   JOIN legacy_company_entities AS mapping ON mapping.entity_id=alias.entity_id
                   JOIN companies AS company ON company.id=mapping.company_id
                   WHERE company.slug='tesla' AND alias.alias_key='elon musk'"""
            ).fetchone()
            formal_tickers = db.execute(
                "SELECT COUNT(*) FROM entity_identifiers WHERE namespace='exchange_ticker'"
            ).fetchone()[0]
            legacy_tickers = db.execute(
                "SELECT COUNT(*) FROM entity_identifiers WHERE namespace='legacy_ticker'"
            ).fetchone()[0]
            listings = db.execute("SELECT COUNT(*) FROM security_listings").fetchone()[0]
        self.assertEqual(us_entities, 13)
        self.assertEqual(tuple(elon), ("candidate_only", "unreviewed"))
        self.assertEqual(formal_tickers, 0)
        self.assertEqual(legacy_tickers, 13)
        self.assertEqual(listings, 0)

    def test_company_identity_is_stable_and_name_changes_append_a_version(self):
        with database.get_db() as db:
            db.execute(
                """INSERT INTO companies(slug,name,name_zh,market,aliases)
                   VALUES('fixture','Fixture Inc','示例公司','PRIVATE',?)""",
                (json.dumps(["Founder Name"]),),
            )
            sync_identity_catalog(db)
            first = db.execute(
                "SELECT entity_id FROM legacy_company_entities WHERE company_id=1"
            ).fetchone()[0]
            db.execute("UPDATE companies SET name='Fixture Holdings' WHERE id=1")
            sync_identity_catalog(db)
            second = db.execute(
                "SELECT entity_id FROM legacy_company_entities WHERE company_id=1"
            ).fetchone()[0]
            versions = db.execute(
                """SELECT canonical_name FROM entity_versions
                   WHERE entity_id=? ORDER BY version""",
                (first,),
            ).fetchall()
            before_repeat = db.execute(
                "SELECT COUNT(*) FROM entity_versions WHERE entity_id=?", (first,)
            ).fetchone()[0]
            sync_identity_catalog(db)
            after_repeat = db.execute(
                "SELECT COUNT(*) FROM entity_versions WHERE entity_id=?", (first,)
            ).fetchone()[0]
            version_id = db.execute(
                "SELECT id FROM entity_versions WHERE entity_id=? ORDER BY version LIMIT 1",
                (first,),
            ).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                db.execute(
                    "UPDATE entity_versions SET canonical_name='rewritten' WHERE id=?",
                    (version_id,),
                )
        self.assertEqual(first, second)
        self.assertEqual([row[0] for row in versions], ["Fixture Inc", "Fixture Holdings"])
        self.assertEqual(before_repeat, after_repeat)

    def test_topic_rename_keeps_old_slug_on_the_same_stable_identity(self):
        old = {
            "slug": "old-name", "name": "Old", "group_key": "technology",
            "description": "old", "rules": {"keywords": ["old"]}, "status": "active",
        }
        new = {
            "slug": "new-name", "name": "New", "group_key": "technology",
            "description": "new", "rules": {"keywords": ["new"]}, "status": "active",
        }
        with database.get_db() as db:
            sync_topic_catalog(db, [old])
            old_id = db.execute(
                "SELECT topic_id FROM topic_slug_aliases WHERE slug='old-name'"
            ).fetchone()[0]
            sync_topic_catalog(
                db, [new], previous_slugs={"new-name": ["old-name"]}
            )
            aliases = db.execute(
                """SELECT slug,topic_id FROM topic_slug_aliases
                   WHERE slug IN ('old-name','new-name') ORDER BY slug"""
            ).fetchall()
            versions = db.execute(
                "SELECT slug FROM topic_versions WHERE topic_id=? ORDER BY version",
                (old_id,),
            ).fetchall()
        self.assertEqual({row[1] for row in aliases}, {old_id})
        self.assertEqual([row[0] for row in versions], ["old-name", "new-name"])

    def test_publishers_are_stable_and_separate_from_crawl_sources(self):
        first = self._load_configured_catalog()
        with database.get_db() as db:
            cnbc = db.execute(
                "SELECT publisher_id FROM publisher_legacy_keys WHERE legacy_key='cnbc'"
            ).fetchone()[0]
            publisher_count = db.execute("SELECT COUNT(*) FROM publishers").fetchone()[0]
            source_count = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            second = sync_identity_catalog(db)
            same = db.execute(
                "SELECT publisher_id FROM publisher_legacy_keys WHERE legacy_key='cnbc'"
            ).fetchone()[0]
        self.assertEqual(first.publishers_seen, publisher_count)
        self.assertNotEqual(publisher_count, source_count)
        self.assertEqual(cnbc, same)
        self.assertEqual(second.publisher_versions_created, 0)

    def test_removed_source_is_disabled_and_readded_source_is_enabled(self):
        one = dict(
            key="one", name="One", channel="ai", tier="info", type="rss",
            url="https://example.com/one", interval_minutes=30,
        )
        two = dict(
            key="two", name="Two", channel="stock", tier="media", type="rss",
            url="https://example.com/two", interval_minutes=60,
        )
        with patch.object(runner, "all_sources", return_value=[one]):
            runner.upsert_sources()
        with patch.object(runner, "all_sources", return_value=[two]):
            runner.upsert_sources()
        with database.get_db() as db:
            self.assertEqual(
                db.execute("SELECT enabled FROM sources WHERE key='one'").fetchone()[0], 0
            )
        with patch.object(runner, "all_sources", return_value=[one, two]):
            runner.upsert_sources()
        with database.get_db() as db:
            states = dict(db.execute("SELECT key,enabled FROM sources"))
        self.assertEqual(states, {"one": 1, "two": 1})

    def test_database_verification_rejects_a_stale_catalog_projection(self):
        self._load_configured_catalog()
        with database.get_db() as db:
            db.execute(
                """UPDATE entities SET status='inactive'
                   WHERE id=(SELECT id FROM entities LIMIT 1)"""
            )
        with self.assertRaisesRegex(
            DatabaseVerificationError, "catalog current projections disagree"
        ):
            verify_database(self.path, require_current=True)


if __name__ == "__main__":
    unittest.main()
