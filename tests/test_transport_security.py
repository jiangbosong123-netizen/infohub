import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.web.routes import app


class TransportSecurityTests(unittest.TestCase):
    def test_security_headers_only_apply_to_configured_public_host(self):
        with patch("app.config.PUBLIC_ORIGIN", "https://windows-server.example.ts.net"):
            local = TestClient(app).get("/api/live")
            public = TestClient(app, base_url="https://windows-server.example.ts.net").get(
                "/api/live")
        self.assertNotIn("Strict-Transport-Security", local.headers)
        self.assertEqual(public.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertEqual(public.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(public.headers["Referrer-Policy"], "same-origin")


if __name__ == "__main__":
    unittest.main()
