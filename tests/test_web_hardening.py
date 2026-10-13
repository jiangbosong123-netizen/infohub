import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config
from app.ai.daily import _digest_fallback, render_markdown
from app.web import v1_auth
from app.web.routes import MAX_PAGE, app

PUBLIC = "https://windows-server.example-tailnet.ts.net"


class DailyReportEscapingTests(unittest.TestCase):
    def test_feed_text_cannot_add_images_or_links(self):
        item = dict(channel="ai", title="OpenAI 发布新模型 ![](https://evil.example/t.png)",
                    url="https://news.example/a", source_name="Wire [重新登录](https://phish.example)",
                    summary="详情见 [重新登录](https://phish.example) <script>x</script>",
                    published_at="2026-10-12T08:00:00+00:00", event_type=None, score=80)
        cluster = dict(channel="ai", title="Click ![](https://evil.example/c.png)", url="javascript:alert(1)",
                       source_count=2, heat=0.5)
        cluster["url"] = "javascript:void0"  # no parentheses: only the scheme rule refuses it
        markdown = _digest_fallback("2026-10-12", {"items": [item], "clusters": [cluster]})
        self.assertNotIn("(javascript:", markdown)  # not linked at all, before the sanitizer runs
        page = render_markdown(markdown)
        self.assertNotIn("<img", page)
        self.assertNotIn("phish.example\"", page)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("<script>", page)
        self.assertIn('href="https://news.example/a"', page)

    def test_model_written_reports_lose_images_but_keep_links(self):
        page = render_markdown("![x](https://evil.example/i.png) [来源](https://news.example/a)")
        self.assertNotIn("<img", page)
        self.assertIn('href="https://news.example/a"', page)


class PortalTransportTests(unittest.TestCase):
    def client(self, host):
        return TestClient(app, base_url=f"http://{host}")

    def test_only_the_public_name_and_the_machine_itself_are_answered(self):
        with patch.object(config, "PUBLIC_ORIGIN", PUBLIC):
            self.assertEqual(self.client("attacker.example").get("/api/live").status_code, 400)
            for host in ("windows-server.example-tailnet.ts.net", "127.0.0.1:8000", "localhost:8000"):
                with self.subTest(host=host):
                    self.assertEqual(self.client(host).get("/api/live").status_code, 200)
        # No public origin yet (development): every host is answered.
        self.assertEqual(self.client("testserver").get("/api/live").status_code, 200)

    def test_every_response_refuses_framing_and_sniffing(self):
        response = self.client("testserver").get("/api/live")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("Strict-Transport-Security", response.headers)
        with patch.object(config, "PUBLIC_ORIGIN", PUBLIC):
            secure = self.client("windows-server.example-tailnet.ts.net").get("/api/live")
        self.assertEqual(secure.headers["Strict-Transport-Security"], "max-age=31536000")

    def test_interactive_docs_are_off_but_the_schema_stays(self):
        client = self.client("testserver")
        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/redoc").status_code, 404)
        self.assertEqual(client.get("/openapi.json").status_code, 200)

    def test_deep_pages_are_refused(self):
        client = self.client("testserver")
        for path in ("/", "/topics/ai", "/story/x"):
            with self.subTest(path=path):
                self.assertEqual(client.get(f"{path}?page={MAX_PAGE + 1}").status_code, 422)
                self.assertEqual(client.get(f"{path}?page=100000000000000000000").status_code, 422)

    def test_a_malformed_api_token_never_opens_the_database(self):
        with patch.object(v1_auth, "get_db", side_effect=AssertionError("database opened")):
            response = self.client("testserver").get("/api/v1/items", headers={"Authorization": "Bearer junk"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
