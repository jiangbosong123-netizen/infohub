import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.catalog import sync_identity_catalog
from app.crawler import runner, sec_source
from app.ingest import begin_ingest_run, observe_candidate


class SecIdentityTests(unittest.TestCase):
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
                """INSERT INTO companies(slug,name,name_zh,ticker,market,cik,aliases)
                   VALUES('alphabet','Alphabet Inc.','Alphabet','GOOGL','US','0001652044','[]')"""
            )
            db.execute(
                """INSERT INTO sources(key,name,channel,tier,type,url)
                   VALUES('sec','SEC','stock','official','sec','https://data.sec.gov')"""
            )
            sync_identity_catalog(db)
        self.source = {
            "key": "sec", "name": "SEC", "channel": "stock", "tier": "official",
            "type": "sec", "url": "https://data.sec.gov", "interval_minutes": 30,
        }
        self.associations = [
            {"cik": "0001652044", "name": "Alphabet Inc.", "ticker": "GOOGL", "exchange": "NASDAQ"},
            {"cik": "0001652044", "name": "Alphabet Inc.", "ticker": "GOOG", "exchange": "NASDAQ"},
        ]

    def _candidate(self, *, accession, form, document, filing_date, report_date):
        filing = {
            "accessionNumber": accession,
            "form": form,
            "primaryDocument": document,
            "filingDate": filing_date,
            "reportDate": report_date,
            "items": "",
        }
        issuer = {
            "cik": "0001652044", "name": "Alphabet Inc.", "formerNames": [],
            "tickers": ["GOOGL", "GOOG"], "exchanges": ["Nasdaq", "Nasdaq"],
            "tickerExchangeAssociations": self.associations,
        }
        return {
            "url": f"https://www.sec.gov/Archives/{accession}/{document}",
            "title": f"Alphabet SEC {form}", "summary": f"Alphabet filed {form}",
            "published_at": None, "event_type": "earnings", "official": 1,
            "companies": ["alphabet"],
            "extra": {
                "form": form, "cik": "0001652044", "accession": accession,
                "primary_document": document, "filing_date": filing_date,
                "report_date": report_date, "items": "",
                "sec_associations": self.associations,
            },
            "source_time_values": sec_source._source_times({
                "acceptanceDateTime": [f"{filing_date.replace('-', '')}120000"],
                "filingDate": [filing_date], "reportDate": [report_date],
            }, 0),
            "observed_at": "2026-09-17T12:30:00+00:00",
            "source_record": {"filing": filing, "issuer": issuer},
            "payload_kind": "api_record",
        }

    def _ingest(self, candidate):
        run = begin_ingest_run(self.source, started_at="2026-09-17T12:30:00+00:00")
        observation = observe_candidate(
            run, candidate, ordinal=0, observed_at=candidate["observed_at"]
        )
        return runner.insert_item("sec", candidate, observation=observation)

    def test_multi_class_securities_are_stable_and_not_claimed_as_adrs(self):
        self.assertTrue(self._ingest(self._candidate(
            accession="0001652044-26-000001", form="10-K", document="a10k.htm",
            filing_date="2026-02-01", report_date="2025-12-31",
        )))
        self.assertFalse(self._ingest(self._candidate(
            accession="0001652044-26-000001", form="10-K", document="a10k.htm",
            filing_date="2026-02-01", report_date="2025-12-31",
        )))
        with database.get_db() as db:
            keys = db.execute(
                "SELECT ticker,exchange FROM sec_security_keys ORDER BY ticker"
            ).fetchall()
            listings = db.execute(
                """SELECT listing_type,verification_status
                   FROM security_listings ORDER BY ticker"""
            ).fetchall()
            versions = db.execute("SELECT COUNT(*) FROM sec_filing_versions").fetchone()[0]
        self.assertEqual([tuple(row) for row in keys], [
            ("GOOG", "NASDAQ"), ("GOOGL", "NASDAQ"),
        ])
        self.assertEqual([tuple(row) for row in listings], [
            ("unknown", "candidate"), ("unknown", "candidate"),
        ])
        self.assertEqual(versions, 1)

        self.associations = [
            {**self.associations[0], "name": "Alphabet Holdings"},
            self.associations[1],
        ]
        self.assertFalse(self._ingest(self._candidate(
            accession="0001652044-26-000001", form="10-K", document="a10k.htm",
            filing_date="2026-02-01", report_date="2025-12-31",
        )))
        with database.get_db() as db:
            version_counts = dict(db.execute(
                """SELECT key.ticker,COUNT(version.id)
                   FROM sec_security_keys AS key
                   JOIN entity_versions AS version
                     ON version.entity_id=key.security_entity_id
                   GROUP BY key.ticker"""
            ))
        self.assertEqual(version_counts, {"GOOG": 1, "GOOGL": 2})

    def test_amendment_links_after_base_filing_arrives_without_rewriting_history(self):
        self._ingest(self._candidate(
            accession="0001652044-26-000002", form="20-F/A", document="a20fa.htm",
            filing_date="2026-03-02", report_date="2025-12-31",
        ))
        with database.get_db() as db:
            first = db.execute(
                "SELECT amendment_status FROM sec_filing_versions WHERE form='20-F/A'"
            ).fetchone()[0]
        self.assertEqual(first, "unresolved")

        self._ingest(self._candidate(
            accession="0001652044-26-000001", form="20-F", document="a20f.htm",
            filing_date="2026-03-01", report_date="2025-12-31",
        ))
        with database.get_db() as db:
            amendment = db.execute(
                """SELECT version,amendment_status,amends_filing_id
                   FROM sec_filing_versions WHERE form='20-F/A' ORDER BY version"""
            ).fetchall()
            base = db.execute(
                "SELECT id FROM sec_filings WHERE accession_number='0001652044-26-000001'"
            ).fetchone()[0]
        self.assertEqual([row["amendment_status"] for row in amendment], ["unresolved", "linked"])
        self.assertEqual(amendment[-1]["amends_filing_id"], base)

    def test_projection_rejects_normalized_metadata_not_present_in_raw_evidence(self):
        candidate = self._candidate(
            accession="0001652044-26-000003", form="6-K", document="a6k.htm",
            filing_date="2026-03-03", report_date="2026-03-03",
        )
        candidate["extra"] = {**candidate["extra"], "form": "20-F"}
        with self.assertRaisesRegex(Exception, "disagrees with raw evidence"):
            self._ingest(candidate)


class SecConnectorSemanticsTests(unittest.TestCase):
    def test_exchange_association_parser_preserves_multiple_classes(self):
        rows = sec_source._parse_associations({
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
                [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
            ],
        })
        self.assertEqual([row["ticker"] for row in rows], ["GOOGL", "GOOG"])
        self.assertEqual({row["cik"] for row in rows}, {"0001652044"})

    def test_foreign_issuer_forms_and_amendments_are_explicit(self):
        self.assertEqual(sec_source._classify("6-K", ""), ("外国发行人临时报告", "other"))
        self.assertEqual(sec_source._classify("20-F", ""), ("外国发行人年报", "earnings"))
        self.assertEqual(sec_source._classify("20-F/A", ""), ("外国发行人年报修订", "earnings"))


if __name__ == "__main__":
    unittest.main()
