import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app import config, database
from app.crawler import catchup, fastnews, http, runner, sina_source

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SHANGHAI = timezone(timedelta(hours=8))


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeFeeds:
    """Three flash feeds over one id sequence: id n was published n minutes before NOW."""

    def __init__(self, newest=500, oldest=1, fail_on=None, shift=0):
        self.ids = list(range(newest, oldest - 1, -1))
        self.requests = []
        self.fail_on = fail_on
        self.shift = shift  # items published between page requests, pushing pages older

    def time(self, n):
        return NOW - timedelta(minutes=500 - n)

    def __call__(self, url, headers=None, **kwargs):
        self.requests.append(url)
        if self.fail_on is not None and len(self.requests) == self.fail_on:
            raise OSError("connection reset")
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if parts.netloc == "zhibo.sina.com.cn":
            page = int(query["page"])
            start = (page - 1) * 50 - (self.shift if page > 1 else 0)
            rows = self.ids[max(start, 0):start + 50]
            last = (len(self.ids) + 49) // 50
            return _Response({"result": {"data": {"feed": {
                "list": [{"id": n, "rich_text": f"新浪快讯 {n} 号。正文",
                          "create_time": self.time(n).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")}
                         for n in rows],
                "page_info": {"page": page, "nextPage": min(page + 1, last), "lastPage": last}}}}})
        if parts.netloc == "api-one-wscn.awtmt.com":
            cursor = int(query["cursor"]) if "cursor" in query else None
            rows = [n for n in self.ids if cursor is None or int(self.time(n).timestamp()) < cursor][:50]
            return _Response({"data": {
                "items": [{"id": n, "title": f"见闻快讯 {n}", "content_text": "正文",
                           "uri": f"/livenews/{n}", "display_time": int(self.time(n).timestamp())}
                          for n in rows],
                "next_cursor": int(self.time(rows[-1]).timestamp()) if rows else ""}})
        if parts.path == "/api/cache":
            rows = self.ids[:20]
            return _Response({"data": {"roll_data": [self.cls(n, share=False) for n in rows]}})
        if parts.path == "/v1/roll/get_roll_list":
            last_time = int(query["last_time"])
            rows = [n for n in self.ids if int(self.time(n).timestamp()) < last_time][:int(query["rn"])]
            return _Response({"data": {"roll_data": [self.cls(n, share=True) for n in rows]}})
        raise AssertionError(f"unexpected url {url}")

    def cls(self, n, *, share):
        record = {"id": n, "title": "", "content": f"财联社电报 {n} 号。正文",
                  "ctime": int(self.time(n).timestamp())}
        if share:
            record["shareurl"] = f"https://api3.cls.cn/share/article/{n}?os=web"
        return record


SOURCES = {
    "sina-7x24": (sina_source.fetch_sina, "https://finance.sina.com.cn/7x24/?id={n}"),
    "wallstreetcn-live": (fastnews.fetch_wscn_live, "https://wallstreetcn.com/livenews/{n}"),
    "cls-telegraph": (fastnews.fetch_cls, "https://www.cls.cn/detail/{n}"),
}


class FlashFeedCatchUpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "app.db"
        for item in (patch.object(database, "DB_PATH", path), patch.object(config, "DB_PATH", path),
                     patch.object(catchup, "PAGE_DELAY_SECONDS", 0)):
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        runner.upsert_sources()

    def store(self, key, ids, last_success=NOW - timedelta(minutes=400)):
        _, template = SOURCES[key]
        with database.get_db() as db:
            source = db.execute("SELECT id FROM sources WHERE key=?", (key,)).fetchone()["id"]
            for n in ids:
                db.execute("""INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
                              VALUES(?,?,?,'stock',?,?)""",
                           (source, template.format(n=n), f"stored {n}", NOW.isoformat(), NOW.isoformat()))
            db.execute("UPDATE sources SET last_success_at=? WHERE key=?",
                       (None if last_success is None else last_success.isoformat(), key))

    def fetch(self, key, feeds):
        fetcher, template = SOURCES[key]
        with patch.object(http, "fetch", feeds):
            raws = fetcher({"key": key})
        prefix = template.split("{n}")[0]
        ids = [int(r["url"][len(prefix):]) for r in raws if "_error" not in r]
        return raws, ids

    def test_a_stored_newest_page_reads_nothing_more(self):
        for key in SOURCES:
            with self.subTest(key=key):
                self.store(key, range(470, 491))
                feeds = FakeFeeds()
                raws, ids = self.fetch(key, feeds)
                self.assertEqual(len(feeds.requests), 1)
                newest = 20 if key == "cls-telegraph" else 50
                self.assertEqual(ids, list(range(500, 500 - newest, -1)))

    def test_a_gap_is_read_back_to_the_stored_items_once(self):
        for key in SOURCES:
            with self.subTest(key=key):
                self.store(key, range(1, 301))  # the newest 200 are missing
                feeds = FakeFeeds()
                raws, ids = self.fetch(key, feeds)
                self.assertEqual(ids, list(range(500, 300, -1)))
                self.assertFalse([r for r in raws if "_error" in r])
                self.assertLessEqual(len(feeds.requests), 6)

    def test_items_moving_between_pages_are_not_repeated(self):
        self.store("sina-7x24", range(1, 301))
        raws, ids = self.fetch("sina-7x24", FakeFeeds(shift=3))
        self.assertEqual(ids, list(range(500, 300, -1)))

    def test_a_source_that_never_succeeded_reads_only_its_newest_page(self):
        for key in SOURCES:
            with self.subTest(key=key):
                self.store(key, (), last_success=None)
                feeds = FakeFeeds()
                self.fetch(key, feeds)
                self.assertEqual(len(feeds.requests), 1)

    def test_reading_back_stops_at_the_last_successful_run(self):
        for key in SOURCES:
            with self.subTest(key=key):
                # Nothing stored, last success 120 minutes ago (less the 15-minute margin).
                self.store(key, (), last_success=NOW - timedelta(minutes=120))
                raws, ids = self.fetch(key, FakeFeeds())
                self.assertGreaterEqual(min(ids), 500 - 135 - 50)
                self.assertLessEqual(min(ids), 500 - 135)
                self.assertEqual(ids, sorted(ids, reverse=True))

    def test_reading_back_is_capped(self):
        self.store("sina-7x24", (), last_success=NOW - timedelta(days=30))
        feeds = FakeFeeds(newest=5000)
        raws, ids = self.fetch("sina-7x24", feeds)
        self.assertEqual(len(feeds.requests), 1 + catchup.MAX_PAGES)
        self.assertEqual(len(ids), 50 * (1 + catchup.MAX_PAGES))

    def test_a_failed_older_page_keeps_what_was_read_and_reports(self):
        self.store("sina-7x24", range(1, 301))
        raws, ids = self.fetch("sina-7x24", FakeFeeds(fail_on=3))
        self.assertEqual(ids, list(range(500, 400, -1)))
        errors = [r["_error"] for r in raws if "_error" in r]
        self.assertEqual(len(errors), 1)
        self.assertIn("补抓第 3 页失败", errors[0])

    def test_cls_keeps_one_url_per_telegraph_across_endpoints(self):
        self.store("cls-telegraph", range(1, 301))
        raws, ids = self.fetch("cls-telegraph", FakeFeeds())
        self.assertTrue(all(r["url"].startswith("https://www.cls.cn/detail/") for r in raws))
        self.assertEqual(len({r["url"] for r in raws}), len(raws))

    def test_the_runner_stores_a_closed_gap_and_records_success(self):
        self.store("wallstreetcn-live", range(1, 301))
        source = next(s for s in runner.all_sources() if s["key"] == "wallstreetcn-live")
        with patch.object(http, "fetch", FakeFeeds()):
            inserted, ok, message = runner.run_source(source)
        self.assertEqual((inserted, ok, message), (200, True, ""))
        with database.get_db() as db:
            count = db.execute("""SELECT COUNT(*) FROM items i JOIN sources s ON s.id=i.source_id
                                  WHERE s.key='wallstreetcn-live'""").fetchone()[0]
        self.assertEqual(count, 500)


if __name__ == "__main__":
    unittest.main()
