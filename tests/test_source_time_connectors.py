import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.crawler import fastnews, hkex_source, rss_source, sec_source, sina_source


class _Response:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class SourceTimeConnectorTests(unittest.TestCase):
    def test_rss_updated_only_does_not_create_publication_time(self):
        entry = SimpleNamespace(updated="Wed, 16 Sep 2026 09:00:00 GMT")
        published, values = rss_source._source_times(
            entry, rss_source.datetime(2026, 9, 16, 10, 0, tzinfo=rss_source.timezone.utc)
        )
        self.assertIsNone(published)
        self.assertEqual([value["role"] for value in values], ["published", "updated"])
        self.assertEqual(values[0]["status"], "missing")
        self.assertEqual(values[1]["utc"], "2026-09-16T09:00:00.000000Z")

    def test_sec_fields_keep_distinct_semantics_and_naive_acceptance_is_unresolved(self):
        values = sec_source._source_times({
            "acceptanceDateTime": ["2026-09-16T17:31:02"],
            "filingDate": ["2026-09-17"],
            "reportDate": ["2026-06-30"],
        }, 0)
        self.assertEqual([value["role"] for value in values], [
            "accepted", "filing_date", "report_period",
        ])
        self.assertEqual(values[0]["status"], "missing_timezone")
        self.assertIsNone(values[0]["utc"])
        self.assertEqual((values[1]["status"], values[1]["precision"]), ("valid", "date"))

    def test_hkex_uses_protocol_timezone(self):
        payload = {"result": [{
            "LONG_TEXT": "業績公告", "FILE_LINK": "/listed/co.pdf",
            "DATE_TIME": "16/09/2026 09:30",
        }]}
        company = {
            "slug": "fixture", "name": "Fixture", "name_zh": "样例",
            "code": "0001", "hkex_stock_id": "1",
        }
        with patch.object(hkex_source.http, "fetch", return_value=_Response(text=__import__("json").dumps(payload))):
            rows = hkex_source._fetch_company(company, "20260914", "20260916")
        value = rows[0]["source_time_values"][0]
        self.assertEqual(value["timezone"], "Asia/Hong_Kong")
        self.assertEqual(value["utc"], "2026-09-16T01:30:00.000000Z")

    def test_fastnews_declares_unix_seconds(self):
        payload = {"data": {"roll_data": [{
            "id": "7", "title": "足够长的测试快讯标题", "content": "测试内容",
            "ctime": 1767225600,
        }]}}
        with patch.object(fastnews.http, "fetch", return_value=_Response(payload=payload)):
            row = fastnews.fetch_cls({})[0]
        value = row["source_time_values"][0]
        self.assertEqual(value["field_path"], "data.roll_data.ctime")
        self.assertEqual(value["precision"], "second")
        self.assertEqual(value["status"], "valid")

    def test_sina_uses_shanghai_source_timezone(self):
        payload = {"result": {"data": {"feed": {"list": [{
            "id": "9", "rich_text": "这是一条长度足够的新浪财经快讯内容。",
            "create_time": "2026-09-16 09:30:00",
        }]}}}}
        with patch.object(sina_source.http, "fetch", return_value=_Response(payload=payload)):
            row = sina_source.fetch_sina({})[0]
        value = row["source_time_values"][0]
        self.assertEqual(value["timezone"], "Asia/Shanghai")
        self.assertEqual(value["utc"], "2026-09-16T01:30:00.000000Z")


if __name__ == "__main__":
    unittest.main()
