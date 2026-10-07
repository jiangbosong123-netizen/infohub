import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from app import config, database
from app.crawler import http, runner

FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>NVIDIA one</title><link>https://example.test/1</link><pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>NVIDIA two</title><link>https://example.test/2</link><pubDate>Tue, 06 Oct 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


class FakeServer:
    """Answers each GET from a queue of (status, headers, body) and records what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, headers=None, timeout=None, follow_redirects=None):
        self.requests.append((url, dict(headers or {})))
        status, extra, body = self.responses.pop(0) if self.responses else (200, {}, FEED)
        return httpx.Response(status, headers=extra, content=body, request=httpx.Request("GET", url))


class PauseTests(unittest.TestCase):
    def setUp(self):
        http._pauses.clear()
        self.addCleanup(http._pauses.clear)
        sleep = patch.object(http.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_retry_after_accepts_seconds_and_dates(self):
        now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        seconds = httpx.Response(429, headers={"Retry-After": "120"})
        date = httpx.Response(429, headers={"Retry-After": format_datetime(now + timedelta(hours=1), usegmt=True)})
        self.assertEqual(http.retry_after_seconds(seconds, now), 120)
        self.assertEqual(http.retry_after_seconds(date, now), 3600)
        self.assertIsNone(http.retry_after_seconds(httpx.Response(429), now))
        self.assertIsNone(http.retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"}), now))

    def test_a_long_retry_after_pauses_the_whole_host(self):
        server = FakeServer((429, {"Retry-After": "3600"}, b""))
        with patch.object(http.httpx, "get", server):
            with self.assertRaises(httpx.HTTPStatusError):
                http.fetch("https://news.google.com/rss/search?q=a")
            with self.assertRaisesRegex(http.HostPaused, "news.google.com"):
                http.fetch("https://news.google.com/rss/search?q=b")
            self.assertEqual(http.fetch("https://other.test/feed").status_code, 200)
        self.assertEqual([u for u, _ in server.requests],
                         ["https://news.google.com/rss/search?q=a", "https://other.test/feed"])
        self.sleep.assert_not_called()
        self.assertIsNotNone(http.host_paused_until("https://news.google.com/x"))

    def test_without_a_long_wait_the_old_retries_remain(self):
        cases = (
            ((429, {}, b""), 30),                        # rate limited, no Retry-After: 30 s, once
            ((429, {"Retry-After": "5"}, b""), 5),
            ((503, {}, b""), 2),                         # unavailable: the generic 2 s retry
        )
        for first, waited in cases:
            with self.subTest(first=first[:2]):
                self.sleep.reset_mock()
                server = FakeServer(first)
                with patch.object(http.httpx, "get", server):
                    self.assertEqual(http.fetch("https://feed.test/rss").status_code, 200)
                self.assertEqual(len(server.requests), 2)
                self.sleep.assert_called_once_with(waited)
                self.assertIsNone(http.host_paused_until("https://feed.test/rss"))

    def test_not_modified_is_returned_only_when_asked(self):
        with patch.object(http.httpx, "get", FakeServer((304, {}, b""))):
            self.assertEqual(http.fetch("https://feed.test/rss", allow_not_modified=True).status_code, 304)
        with patch.object(http.httpx, "get", FakeServer((304, {}, b""), (304, {}, b""))):
            with self.assertRaises(httpx.HTTPStatusError):
                http.fetch("https://feed.test/rss")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, name, value in ((database, "DB_PATH", root / "app.db"), (config, "DB_PATH", root / "app.db"),
                                    (config, "BLOB_PATH", root / "blobs")):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        runner.upsert_sources()
        http._pauses.clear()
        self.addCleanup(http._pauses.clear)
        self.source = next(s for s in runner.all_sources() if s["key"] == "techcrunch")

    def source_row(self):
        with database.get_db() as db:
            return dict(db.execute("SELECT * FROM sources WHERE key='techcrunch'").fetchone())

    def test_feed_validators_are_sent_back_and_a_304_reads_nothing(self):
        server = FakeServer((200, {"ETag": '"v1"', "Last-Modified": "Tue, 06 Oct 2026 11:00:00 GMT"}, FEED),
                            (304, {}, b""))
        with patch.object(http.httpx, "get", server):
            first = runner.run_source(self.source)
            second = runner.run_source(self.source)
        self.assertEqual((first, second), ((2, True, ""), (0, True, "")))
        self.assertNotIn("If-None-Match", server.requests[0][1])
        self.assertEqual(server.requests[1][1]["If-None-Match"], '"v1"')
        self.assertEqual(server.requests[1][1]["If-Modified-Since"], "Tue, 06 Oct 2026 11:00:00 GMT")
        self.assertEqual(json.loads(self.source_row()["state"])["etag"], '"v1"')

    def test_validators_are_not_kept_when_storing_failed(self):
        server = FakeServer((200, {"ETag": '"v1"'}, FEED))
        with patch.object(http.httpx, "get", server), \
                patch.object(runner, "insert_item", side_effect=RuntimeError("disk full")):
            inserted, ok, _ = runner.run_source(self.source)
        self.assertFalse(ok)
        self.assertNotIn("etag", json.loads(self.source_row()["state"] or "{}"))

    def test_a_paused_host_is_skipped_without_counting_a_failure(self):
        http._pause_host(self.source["url"], 3600)
        server = FakeServer()
        with patch.object(http.httpx, "get", server):
            inserted, ok, message = runner.run_source(self.source)
            with database.get_db() as db:
                db.execute("UPDATE sources SET last_run_at=NULL WHERE key='techcrunch'")
            with patch.object(runner, "all_sources", return_value=[self.source]):
                due = runner.run_due_sources()
        row = self.source_row()
        self.assertEqual((inserted, ok, row["fail_count"]), (0, False, 0))
        self.assertIn("要求暂停至", row["last_error"])
        self.assertEqual((due["ran"], server.requests), (0, []))
        with database.get_db() as db:
            status = db.execute("""SELECT status,error_code FROM ingest_runs
                                   WHERE source_id=(SELECT id FROM sources WHERE key='techcrunch')""").fetchall()
        self.assertEqual([tuple(r) for r in status], [("skipped", "host_paused")])


if __name__ == "__main__":
    unittest.main()
