import hashlib
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from app import company_match, config, database
from app.ai import pipeline
from app.crawler import article_text
from app.crawler.article_text import Fetcher, decompress, due_items, fetch_article_texts
from app.crawler.runner import insert_item
from app.stories import refresh_derived
from app.worker import register_default_schedules

NOW = datetime.now(timezone.utc)
STORY = " ".join(["Nvidia said on Monday that its new data-centre chips will ship next quarter,"
                  " and that demand from cloud providers remains far above supply."] * 6)
ARTICLE = f"<html><head><title>Nvidia chips</title></head><body><article><h1>Nvidia chips ship</h1><p>{STORY}</p></article></body></html>"
CONSENT = """<html><title>Before you continue</title>
<form action="JIbuQc:fN3dRc"><input type="hidden" name="gl" value="GB"><input type="hidden" name="set_eom" value="true">
<button aria-label="Reject all">Reject all</button></form>
<form action="JIbuQc:ldDdv"><input type="hidden" name="gl" value="GB"><input type="hidden" name="set_sc" value="true">
<button aria-label="Accept all">Accept all</button></form></html>"""


class FakeWeb:
    """Google News, its consent page and a few publisher sites, with every request recorded."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.pages = {
            "https://news.example/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"),
            "https://news.example/story": httpx.Response(200, html=ARTICLE),
            "https://news.example/second": httpx.Response(200, html=ARTICLE),
            "https://news.example/private/x": httpx.Response(200, html=ARTICLE),
            "https://news.example/brief": httpx.Response(
                200, html="<html><body><article><p>Subscribe to read the full story.</p></article></body></html>"),
            "https://blog.example/robots.txt": httpx.Response(404),
            "https://blog.example/post": httpx.Response(403),
            "https://flaky.example/robots.txt": httpx.Response(404),
            "https://flaky.example/a": httpx.Response(503),
            "https://down.example/robots.txt": httpx.Response(503),
            "https://down.example/a": httpx.Response(200, html=ARTICLE),
            "https://down.example/b": httpx.Response(200, html=ARTICLE),
            # A public page that bounces the worker into the LAN, and one that moved legitimately.
            "https://news.example/bounce": httpx.Response(302, headers={"Location": "http://192.168.1.1/admin"}),
            "https://news.example/moved": httpx.Response(301, headers={"Location": "/story"}),
            "https://news.example/sneak": httpx.Response(302, headers={"Location": "/private/x"}),
            "https://news.example/huge": httpx.Response(200, html="<p>" + "x" * 5000 + "</p>"),
            # No Content-Length: only counting the streamed bytes can stop it.
            "https://news.example/endless": httpx.Response(
                200, headers={"Content-Type": "text/html"}, content=(b"<p>" + b"x" * 500 for _ in range(20))),
        }
        self.decoded = {"GOOD": "https://news.example/story", "TWO": "https://news.example/second",
                        "BRIEF": "https://news.example/brief",
                        "HIDDEN": "https://news.example/private/x"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://news.google.com/rss/articles/"):
            if "SOCS" not in request.headers.get("cookie", ""):
                return httpx.Response(302, headers={"Location": "https://consent.google.com/m?continue=x"})
            return httpx.Response(200, html='<c-wiz data-n-a-sg="SIG" data-n-a-ts="1700000000"></c-wiz>')
        if url == "https://news.example/slow":
            time.sleep(1.0)
            return httpx.Response(200, html=ARTICLE)
        if url.startswith("https://consent.google.com/m"):
            return httpx.Response(200, html=CONSENT)
        if url == "https://consent.google.com/save":
            return httpx.Response(200, headers={"Set-Cookie": "SOCS=rejected; Domain=.google.com; Path=/"})
        if url.endswith("/batchexecute"):
            request_data = json.loads(parse_qs(request.content.decode())["f.req"][0])
            article_id = json.loads(request_data[0][0][1])[2]
            frame = json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", self.decoded[article_id], 1]),
                                 None, None, None, "generic"], ["di", 10], ["af.httprm", 10, "x", 1]])
            return httpx.Response(200, text=")]}'\n\n" + frame)
        return self.pages.get(url, httpx.Response(404))

    def urls(self):
        return [str(request.url) for request in self.requests]


class ArticleTextTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, name, value in ((database, "DB_PATH", Path(folder.name) / "app.db"),
                                    (config, "ARTICLE_TEXT_ENABLED", True),
                                    (config, "ALLOW_NETWORK_TASKS", True)):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            for key, kind in (("gnews", "googlenews"), ("blog", "rss"), ("flash", "sina"), ("sec", "sec")):
                db.execute("INSERT INTO sources(key,name,channel,type) VALUES(?,?,'ai',?)", (key, key, kind))
        self.web = FakeWeb()
        self.time = 1000.0
        self.slept: list[float] = []

    def clock(self):
        return self.time

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.time += seconds

    # Split-horizon DNS could give internal names public answers; the name rule must still refuse them.
    PUBLIC = {"news.example": "93.184.216.34", "blog.example": "93.184.216.35",
              "flaky.example": "93.184.216.36", "down.example": "93.184.216.39",
              "printer": "93.184.216.40", "nas.internal": "93.184.216.41",
              "windows-server.example-tailnet.ts.net": "93.184.216.42"}
    PRIVATE = {"intranet.example": "10.0.0.5", "router.example": "192.168.1.1"}

    def resolve(self, host, port):
        if host in self.PUBLIC or host in self.PRIVATE:
            return [{**self.PUBLIC, **self.PRIVATE}[host]]
        try:
            return [str(__import__("ipaddress").ip_address(host))]
        except ValueError:
            raise OSError("unknown host") from None

    def fetcher(self, **options):
        client = httpx.Client(transport=httpx.MockTransport(self.web), follow_redirects=True)
        self.addCleanup(client.close)
        return Fetcher(client, clock=self.clock, sleep=self.sleep, resolve=self.resolve, **options)

    def item(self, source, url, title="Nvidia chips ship next quarter", summary="", age=timedelta(hours=1)):
        self.assertTrue(insert_item(source, dict(title=title, url=url, summary=summary,
                                                 published_at=NOW.isoformat())))
        with database.get_db() as db:
            item_id = db.execute("SELECT max(id) FROM items").fetchone()[0]
            db.execute("UPDATE items SET fetched_at=? WHERE id=?", ((NOW - age).isoformat(), item_id))
        return item_id

    def stored(self, item_id):
        with database.get_db() as db:
            return db.execute("SELECT * FROM article_texts WHERE item_id=?", (item_id,)).fetchone()

    def test_title_only_items_get_their_article_text(self):
        google = self.item("gnews", "https://news.google.com/rss/articles/GOOD")
        blog = self.item("blog", "https://blog.example/post")
        skipped = [self.item("flash", "https://flash.example/1"), self.item("sec", "https://sec.example/1"),
                   self.item("blog", "https://blog.example/excerpt", summary="An excerpt the feed already gave."),
                   self.item("gnews", "https://news.google.com/rss/articles/OLD", age=timedelta(days=5))]
        result = fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual(result, {"status": "ok", "candidates": 2, "ok": 1, "blocked": 1})
        row = self.stored(google)
        text = decompress(row["text_zlib"])
        self.assertIn("data-centre chips", text)
        self.assertEqual((row["status"], row["article_url"], row["chars"], row["text_sha256"]),
                         ("ok", "https://news.example/story", len(text), hashlib.sha256(text.encode()).hexdigest()))
        self.assertTrue(row["extractor"].startswith("trafilatura"))
        self.assertEqual((self.stored(blog)["status"], self.stored(blog)["http_status"]), ("blocked", 403))
        self.assertEqual([self.stored(item_id) for item_id in skipped], [None] * 4)
        with database.get_db() as db:
            self.assertEqual(due_items(db, NOW, 10), [])

    def test_google_consent_is_only_ever_rejected(self):
        self.item("gnews", "https://news.google.com/rss/articles/GOOD")
        fetch_article_texts(fetcher=self.fetcher())
        [save] = [request for request in self.web.requests if str(request.url).endswith("/save")]
        fields = parse_qs(save.content.decode())
        self.assertEqual(fields["set_eom"], ["true"])
        self.assertNotIn("set_sc", fields)

    def test_robots_txt_is_honoured(self):
        hidden = self.item("gnews", "https://news.google.com/rss/articles/HIDDEN")
        fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual((self.stored(hidden)["status"], self.stored(hidden)["error"]), ("blocked", "robots.txt"))
        self.assertNotIn("https://news.example/private/x", self.web.urls())

    def test_links_never_reach_internal_addresses(self):
        bounced = self.item("blog", "https://news.example/bounce")
        direct = [self.item("blog", url) for url in (
            "http://192.168.1.1/admin", "http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data",
            "https://intranet.example/doc", "https://news.example:8443/story", "http://printer/status",
            "https://nas.internal/files", "https://windows-server.example-tailnet.ts.net/")]
        fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual((self.stored(bounced)["status"], self.stored(bounced)["error"]),
                         ("blocked", "unsafe target: host is not on the public internet"))
        for item_id in direct:
            with self.subTest(item=item_id):
                self.assertEqual(self.stored(item_id)["status"], "blocked")
                self.assertTrue(self.stored(item_id)["error"].startswith("unsafe target"))
        self.assertEqual([self.stored(item_id)["error"] for item_id in direct[-3:]],
                         ["unsafe target: internal host name"] * 3)
        self.assertTrue(article_text._public_address("::ffff:8.8.8.8"))
        self.assertFalse(article_text._public_address("::ffff:10.0.0.1"))
        reached = {request.url.host for request in self.web.requests}
        self.assertEqual(reached, {"news.example"})  # only the public page that tried to bounce

    def test_a_redirect_is_followed_and_its_target_recorded(self):
        moved = self.item("blog", "https://news.example/moved")
        fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual((self.stored(moved)["status"], self.stored(moved)["article_url"]),
                         ("ok", "https://news.example/story"))

    def test_robots_txt_is_checked_again_after_a_redirect(self):
        sneak = self.item("blog", "https://news.example/sneak")
        fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual((self.stored(sneak)["status"], self.stored(sneak)["error"]), ("blocked", "robots.txt"))
        self.assertNotIn("https://news.example/private/x", self.web.urls())

    def test_a_name_that_rebinds_to_a_private_address_is_refused(self):
        class Stream:
            def get_extra_info(self, name):
                return ("10.0.0.7", 443) if name == "server_addr" else None
        response = httpx.Response(200, extensions={"network_stream": Stream()})
        with self.assertRaises(article_text.UnsafeTarget):
            Fetcher._check_peer(response)
        Fetcher._check_peer(httpx.Response(200))  # no stream information: nothing to compare

    def test_temporary_problems_are_retried_not_blocked(self):
        down = [self.item("blog", "https://down.example/a"), self.item("blog", "https://down.example/b")]
        unknown = self.item("blog", "https://nowhere.example/x")  # does not resolve right now
        broken = self.item("blog", "https://xn--zz.com/x")  # not a valid internationalised name
        fetch_article_texts(fetcher=self.fetcher(), now=NOW)
        self.assertEqual([(self.stored(i)["status"], self.stored(i)["error"]) for i in down],
                         [("failed", "robots.txt unreachable")] * 2)
        self.assertEqual((self.stored(unknown)["status"], self.stored(unknown)["error"]),
                         ("failed", "host does not resolve"))
        self.assertEqual(self.stored(broken)["status"], "failed")
        self.web.pages["https://down.example/robots.txt"] = httpx.Response(404)
        result = fetch_article_texts(fetcher=self.fetcher(), now=NOW + timedelta(minutes=61))
        self.assertEqual(result["candidates"], 4)
        self.assertEqual([self.stored(i)["status"] for i in down], ["ok", "ok"])

    def test_oversized_pages_are_not_downloaded_whole(self):
        huge = self.item("blog", "https://news.example/huge")
        endless = self.item("blog", "https://news.example/endless")
        with patch.object(article_text, "MAX_BYTES", 1000):
            fetch_article_texts(fetcher=self.fetcher())
        for item_id in (huge, endless):
            self.assertEqual((self.stored(item_id)["status"], self.stored(item_id)["error"]), ("not_html", "too large"))

    def test_a_slow_server_cannot_hold_the_worker(self):
        slow = self.item("blog", "https://news.example/slow")
        started = time.monotonic()
        fetch_article_texts(fetcher=self.fetcher(deadline_seconds=0.2))
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual((self.stored(slow)["status"], self.stored(slow)["error"]), ("failed", "deadline"))

    def test_one_site_is_asked_at_most_every_ten_seconds(self):
        self.item("gnews", "https://news.google.com/rss/articles/GOOD")
        self.item("gnews", "https://news.google.com/rss/articles/TWO")
        fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual(len([u for u in self.web.urls() if u.startswith("https://news.example/") and "robots" not in u]), 2)
        self.assertTrue(self.slept and all(0 < seconds <= article_text.SITE_INTERVAL_SECONDS for seconds in self.slept))

    def test_a_teaser_is_not_kept_as_article_text(self):
        brief = self.item("gnews", "https://news.google.com/rss/articles/BRIEF")
        fetch_article_texts(fetcher=self.fetcher())
        self.assertEqual((self.stored(brief)["status"], self.stored(brief)["text_zlib"]), ("no_text", None))

    def test_a_transient_failure_is_retried_once_after_an_hour(self):
        flaky = self.item("blog", "https://flaky.example/a")
        fetch_article_texts(fetcher=self.fetcher(), now=NOW)
        self.assertEqual((self.stored(flaky)["status"], self.stored(flaky)["attempts"]), ("failed", 1))
        fetch_article_texts(fetcher=self.fetcher(), now=NOW + timedelta(minutes=30))
        self.assertEqual(self.stored(flaky)["attempts"], 1)
        fetch_article_texts(fetcher=self.fetcher(), now=NOW + timedelta(minutes=61))
        self.assertEqual((self.stored(flaky)["attempts"], self.stored(flaky)["next_attempt_at"]), (2, None))
        with database.get_db() as db:
            self.assertEqual(due_items(db, NOW + timedelta(days=1), 10), [])

    def test_nothing_happens_while_switched_off(self):
        self.item("gnews", "https://news.google.com/rss/articles/GOOD")
        with patch.object(config, "ARTICLE_TEXT_ENABLED", False):
            self.assertEqual(fetch_article_texts(fetcher=self.fetcher()), {"status": "disabled"})
            register_default_schedules(NOW)
        self.assertEqual(self.web.requests, [])
        with database.get_db() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM schedules WHERE id='article-text:fetch' AND enabled=1").fetchone())
        register_default_schedules(NOW)
        with database.get_db() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM schedules WHERE id='article-text:fetch' AND enabled=1").fetchone())

    def curated_payload(self):
        sent = []

        def fake_llm(payload):
            sent.extend(payload)
            return []
        with patch.object(config, "llm_enabled", return_value=True), patch.object(pipeline, "_call_llm", fake_llm):
            pipeline.process_pending(limit=30)
        return {row["id"]: row["excerpt"] for row in sent}

    def test_curation_waits_for_the_text_and_then_reads_it(self):
        fresh = self.item("gnews", "https://news.google.com/rss/articles/GOOD", age=timedelta(minutes=5))
        self.assertEqual(self.curated_payload(), {})  # waiting for its text
        with patch.object(config, "ARTICLE_TEXT_ENABLED", False):
            self.assertEqual(self.curated_payload(), {fresh: ""})
        fetch_article_texts(fetcher=self.fetcher())
        text = decompress(self.stored(fresh)["text_zlib"])
        self.assertEqual(self.curated_payload()[fresh], text[:article_text.EXCERPT_CHARS])
        self.assertLess(article_text.EXCERPT_CHARS, len(text))

    def test_curation_stops_waiting_after_the_limit(self):
        stale = self.item("gnews", "https://news.google.com/rss/articles/GOOD",
                          age=timedelta(minutes=config.ARTICLE_TEXT_WAIT_MINUTES + 1))
        self.assertEqual(self.curated_payload(), {stale: ""})

    def test_the_story_page_shows_only_the_opening(self):
        from app.web.routes import STORY_EXCERPT_CHARS, app
        item_id = self.item("gnews", "https://news.google.com/rss/articles/GOOD")
        fetch_article_texts(fetcher=self.fetcher())
        refresh_derived()
        with database.get_db() as db:
            story = db.execute("SELECT story_id FROM story_items WHERE item_id=?", (item_id,)).fetchone()[0]
        page = TestClient(app).get(f"/story/{story}").text
        text = decompress(self.stored(item_id)["text_zlib"])
        self.assertIn("原文开头", page)
        self.assertIn(text[:STORY_EXCERPT_CHARS], page)
        self.assertNotIn(text[:STORY_EXCERPT_CHARS + 20], page)


class ArticleTextMigrationTests(unittest.TestCase):
    def test_migration_53_adds_only_the_article_text_table(self):
        import sqlite3
        from app import db_admin
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "schema52.db"
            with database.get_db(path) as db:
                db_admin.apply_migrations(db, db_admin.MIGRATIONS[:52])
            with sqlite3.connect(path) as db:
                before = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            with database.get_db(path) as db:
                self.assertEqual(db_admin.apply_migrations(db, db_admin.MIGRATIONS[:53]), (53,))
                after = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            self.assertEqual(after - before, {"article_texts", "idx_article_texts_retry"})
            db_admin.migrate_database(path)
            self.assertEqual(db_admin.verify_database(path).state, "current")


if __name__ == "__main__":
    unittest.main()
