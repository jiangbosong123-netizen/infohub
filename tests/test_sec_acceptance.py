import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app import company_match, config, database
from app.catalog import sync_identity_catalog
from app.crawler import runner, sec_source
from app.sec_acceptance import correct_acceptance_times

CIK = "0001652044"
# Newest first, as EDGAR lists them. The API value of the first is Eastern time marked as UTC and
# that of the second is correct, as both were seen in 2026; the headers are what counts.
FILINGS = [
    dict(accessionNumber="0001652044-26-000003", form="8-K", items="2.02,9.01", filingDate="2026-10-08",
         reportDate="2026-10-08", acceptanceDateTime="2026-10-08T16:05:12.000Z", primaryDocument="a8k.htm",
         header="20261008160512", utc="2026-10-08T20:05:12+00:00"),
    dict(accessionNumber="0001652044-26-000002", form="10-Q", items="", filingDate="2026-07-28",
         reportDate="2026-06-30", acceptanceDateTime="2026-07-28T20:30:00.000Z", primaryDocument="a10q.htm",
         header="20260728163000", utc="2026-07-28T20:30:00+00:00"),
    dict(accessionNumber="0001652044-26-000001", form="4", items="", filingDate="2026-03-02",
         reportDate="2026-02-27", acceptanceDateTime="2026-03-03T03:00:00.000Z", primaryDocument="f4.xml",
         header="20260302180000", utc="2026-03-02T23:00:00+00:00"),  # Eastern standard time
]


def url_of(filing):
    return (f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/"
            f"{filing['accessionNumber'].replace('-', '')}/{filing['primaryDocument']}")


def header_page(accession, accepted):
    return (f"<HTML><HEAD><TITLE>SEC EDGAR Submission {accession}</TITLE>\n<!--\n"
            f"<SEC-HEADER>{accession}.hdr.sgml : 20261008\n<ACCEPTANCE-DATETIME>{accepted}\n"
            f"<ACCESSION-NUMBER>{accession}\n<TYPE>8-K\n-->\n</HEAD><BODY><PRE>\n"
            f"&lt;ACCEPTANCE-DATETIME&gt;{accepted}\n</PRE></BODY></HTML>")


class FakeSec:
    """data.sec.gov and the EDGAR archive, with per-accession header failures."""

    def __init__(self, filings=FILINGS, failures=None):
        self.filings = filings
        self.failures = failures or {}
        self.header_reads = []

    def fetch(self, url, headers=None, **_):
        if url.startswith("https://data.sec.gov/submissions/"):
            keys = ("accessionNumber", "form", "items", "filingDate", "reportDate", "acceptanceDateTime",
                    "primaryDocument")
            payload = {"cik": CIK.lstrip("0"), "name": "Alphabet Inc.", "tickers": ["GOOGL"],
                       "exchanges": ["Nasdaq"], "formerNames": [],
                       "filings": {"recent": {key: [f[key] for f in self.filings] for key in keys}}}
            return SimpleNamespace(text=json.dumps(payload))
        self.header_reads.append(url)
        filing = next(f for f in self.filings if f["accessionNumber"] in url)
        failure = self.failures.get(filing["accessionNumber"])
        if isinstance(failure, int):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("status", request=request, response=httpx.Response(failure, request=request))
        if failure == "garbled":  # a page in a format the reader does not know
            return SimpleNamespace(text="<html>Filing index moved</html>")
        if failure is not None:
            raise failure
        return SimpleNamespace(text=header_page(filing["accessionNumber"], filing["header"]))


class SecAcceptanceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        for target, name, value in ((database, "DB_PATH", root / "app.db"), (config, "DB_PATH", root / "app.db"),
                                    (config, "BLOB_PATH", root / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        runner.upsert_sources()
        with database.get_db() as db:
            db.execute("""INSERT INTO companies(slug,name,name_zh,ticker,market,cik,aliases)
                          VALUES('alphabet','Alphabet Inc.','Alphabet','GOOGL','US',?,'[]')""", (CIK,))
            sync_identity_catalog(db)
        self.source = next(s for s in runner.all_sources() if s["type"] == "sec")
        for target, name, value in ((sec_source, "resolve_missing_ciks", lambda companies: {}),
                                    (sec_source.time, "sleep", lambda seconds: None)):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)

    def run_sec(self, fake):
        with patch.object(sec_source.http, "fetch", fake.fetch), patch("app.stories.refresh_derived", lambda: None):
            return runner.run_source(self.source)

    def items(self):
        with database.get_db() as db:
            return {row["url"]: (row["published_at"], json.loads(row["extra"]))
                    for row in db.execute("SELECT url,published_at,extra FROM items")}

    def count(self, table):
        with database.get_db() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_filings_take_the_acceptance_time_their_header_states(self):
        fake = FakeSec()
        self.run_sec(fake)
        items = self.items()
        for filing in FILINGS:
            published_at, extra = items[url_of(filing)]
            self.assertEqual(published_at, filing["utc"])  # an old filing seen today still sorts as old
            self.assertEqual(extra["_legacy_time_basis"], "source_accepted")
            self.assertEqual(extra["sec_acceptance"]["acceptance_datetime"], filing["header"])
        with database.get_db() as db:
            accepted = {row[0] for row in db.execute("SELECT accepted_at FROM sec_filing_versions")}
            roles = {(row[0], row[1]) for row in db.execute("SELECT field_path,role FROM source_time_values")}
        self.assertEqual(accepted, {"2026-10-08T20:05:12.000000Z", "2026-07-28T20:30:00.000000Z",
                                    "2026-03-02T23:00:00.000000Z"})
        self.assertIn(("filings.recent.acceptanceDateTime", "other"), roles)
        self.assertIn(("header.acceptance_datetime", "accepted"), roles)

        # Seen again: no header is read twice and the stored record is unchanged, header included.
        before = (self.count("raw_records"), self.count("sec_filing_versions"))
        self.run_sec(fake)
        self.assertEqual(len(fake.header_reads), 3)
        self.assertEqual((self.count("raw_records"), self.count("sec_filing_versions")), before)

    def test_new_filings_beyond_the_budget_wait_for_a_later_run(self):
        fake = FakeSec()
        with patch.object(sec_source, "HEADER_READS_PER_RUN", 2):
            self.run_sec(fake)
            self.assertEqual(set(self.items()), {url_of(FILINGS[0]), url_of(FILINGS[1])})
            self.run_sec(fake)
        self.assertEqual(len(fake.header_reads), 3)
        self.assertEqual(self.items()[url_of(FILINGS[2])][0], FILINGS[2]["utc"])

    def test_a_failed_read_waits_and_a_missing_header_falls_back_to_when_seen(self):
        fake = FakeSec(failures={FILINGS[0]["accessionNumber"]: 404,
                                 FILINGS[1]["accessionNumber"]: httpx.ConnectError("offline")})
        new, ok, note = self.run_sec(fake)
        self.assertEqual((new, ok), (1, False))  # shown as a partial failure, without back-off
        self.assertIn("SEC 文件头读取失败", note)
        published_at, extra = self.items()[url_of(FILINGS[0])]
        self.assertEqual(extra["_legacy_time_basis"], "connector_observed")
        self.assertNotIn("sec_acceptance", extra)

        fake.failures = {}
        self.run_sec(fake)
        self.assertEqual(len(self.items()), 3)
        self.assertEqual(self.items()[url_of(FILINGS[2])][0], FILINGS[2]["utc"])
        # The filing without a header was stored; it is not read again on every run.
        self.assertEqual(fake.header_reads.count(fake.header_reads[0]), 1)

    def test_an_http_error_on_one_header_defers_only_that_filing(self):
        fake = FakeSec(failures={FILINGS[0]["accessionNumber"]: 500})
        new, ok, note = self.run_sec(fake)
        self.assertEqual((new, ok), (2, False))
        self.assertIn("1 份留到下一轮", note)
        items = self.items()
        self.assertNotIn(url_of(FILINGS[0]), items)
        self.assertEqual(items[url_of(FILINGS[2])][0], FILINGS[2]["utc"])  # older ones were not held back

    def test_an_unknown_header_format_is_stored_at_first_sight_and_reported(self):
        fake = FakeSec(failures={FILINGS[1]["accessionNumber"]: "garbled"})
        new, ok, note = self.run_sec(fake)
        self.assertEqual((new, ok), (3, False))
        self.assertIn("格式无法识别", note)
        self.assertEqual(self.items()[url_of(FILINGS[1])][1]["_legacy_time_basis"], "connector_observed")

    def test_a_header_for_another_filing_is_not_used(self):
        page = SimpleNamespace(text=header_page("0001652044-26-000009", "20261008160512"))
        with patch.object(sec_source.http, "fetch", return_value=page), \
                self.assertRaises(sec_source.HeaderFormatError):
            sec_source.read_acceptance(CIK, "0001652044-26-000003")
        request = httpx.Request("GET", "https://www.sec.gov/x")
        server_error = httpx.HTTPStatusError("status", request=request,
                                             response=httpx.Response(500, request=request))
        with patch.object(sec_source.http, "fetch", side_effect=server_error), self.assertRaises(httpx.HTTPStatusError):
            sec_source.read_acceptance(CIK, "0001652044-26-000003")

    def test_legacy_items_are_corrected_from_their_headers(self):
        stored = {FILINGS[0]["accessionNumber"]: "2026-10-08T16:05:12+00:00",  # 4 h early
                  FILINGS[1]["accessionNumber"]: "2026-07-28T20:30:00+00:00"}  # right
        with database.get_db() as db:
            source_id = db.execute("SELECT id FROM sources WHERE type='sec'").fetchone()[0]
            for filing in FILINGS[:2]:
                db.execute("""INSERT INTO items(source_id,url,title,channel,published_at,fetched_at,extra)
                              VALUES(?,?,?,'stock',?,?,?)""",
                           (source_id, url_of(filing), filing["form"], stored[filing["accessionNumber"]],
                            "2026-10-08T20:10:00+00:00", json.dumps({"form": filing["form"], "cik": CIK})))
        fake = FakeSec()
        with patch.object(sec_source.http, "fetch", fake.fetch):
            preview = correct_acceptance_times(apply=False, pause=0)
            self.assertEqual(self.items()[url_of(FILINGS[0])][0], stored[FILINGS[0]["accessionNumber"]])
            report = correct_acceptance_times(apply=True, pause=0)
            again = correct_acceptance_times(apply=True, pause=0)
        for result in (preview, report):
            self.assertEqual((result["checked"], result["matches"], result["differs"], result["remaining"]),
                             (2, 1, 1, 0))
            self.assertEqual(result["stored_minus_header"], {"-4 h": 1})
        self.assertEqual(again["checked"], 0)
        items = self.items()
        self.assertEqual(items[url_of(FILINGS[0])][0], FILINGS[0]["utc"])
        self.assertEqual(items[url_of(FILINGS[0])][1]["_legacy_time_basis"], "source_accepted")

        # The collector reuses the header read for a stored filing instead of reading it again.
        reads = len(fake.header_reads)
        with patch.object(sec_source.http, "fetch", fake.fetch):
            rows = sec_source.fetch_sec(self.source)
        self.assertEqual(len(fake.header_reads), reads + 1)  # only the filing not stored yet
        carried = next(row for row in rows if row["url"] == url_of(FILINGS[0]))
        self.assertEqual(carried["source_record"]["header"]["acceptance_datetime"], FILINGS[0]["header"])

    def test_the_legacy_command_counts_an_unknown_header_format_and_goes_on(self):
        with database.get_db() as db:
            source_id = db.execute("SELECT id FROM sources WHERE type='sec'").fetchone()[0]
            for filing in FILINGS[:2]:
                db.execute("""INSERT INTO items(source_id,url,title,channel,published_at,fetched_at,extra)
                              VALUES(?,?,?,'stock',?,?,'{}')""",
                           (source_id, url_of(filing), filing["form"], filing["utc"], filing["utc"]))
        fake = FakeSec(failures={FILINGS[0]["accessionNumber"]: "garbled"})
        with patch.object(sec_source.http, "fetch", fake.fetch):
            report = correct_acceptance_times(apply=True, pause=0)
        self.assertEqual((report["checked"], report["no_header"], report["matches"], report["stopped"]),
                         (2, 1, 1, None))

    def test_a_stop_leaves_the_rest_for_the_next_run(self):
        with database.get_db() as db:
            source_id = db.execute("SELECT id FROM sources WHERE type='sec'").fetchone()[0]
            for filing in FILINGS:
                db.execute("""INSERT INTO items(source_id,url,title,channel,published_at,fetched_at,extra)
                              VALUES(?,?,?,'stock',?,?,'{}')""",
                           (source_id, url_of(filing), filing["form"], filing["utc"], filing["utc"]))
        fake = FakeSec(failures={FILINGS[1]["accessionNumber"]: httpx.ConnectError("offline")})
        with patch.object(sec_source.http, "fetch", fake.fetch):
            report = correct_acceptance_times(apply=True, pause=0)
        self.assertEqual((report["checked"], report["remaining"]), (1, 2))
        self.assertIn("offline", report["stopped"])


class SourceTimeTests(unittest.TestCase):
    def test_the_header_is_eastern_time_and_the_api_value_is_kept_as_other(self):
        recent = {"acceptanceDateTime": ["2026-10-08T16:05:12.000Z"], "filingDate": ["2026-10-08"],
                  "reportDate": ["2026-10-08"]}
        values = sec_source._source_times(recent, 0, {"acceptance_datetime": "20261008160512",
                                                      "read_at": "2026-10-08T20:06:00+00:00"})
        self.assertEqual([value["role"] for value in values], ["other", "filing_date", "report_period", "accepted"])
        self.assertEqual((values[3]["utc"], values[3]["status"], values[3]["timezone"]),
                         ("2026-10-08T20:05:12.000000Z", "valid", "America/New_York"))
        winter = sec_source._source_times(recent, 0, {"acceptance_datetime": "20261208160512",
                                                      "read_at": "2026-12-08T21:06:00+00:00"})
        self.assertEqual(winter[3]["utc"], "2026-12-08T21:05:12.000000Z")
        self.assertNotIn("accepted", [value["role"] for value in sec_source._source_times(recent, 0)])


if __name__ == "__main__":
    unittest.main()
