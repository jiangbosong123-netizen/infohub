import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import database, db_admin
from app.api_auth import create_consumer, issue_api_key, revoke_api_key
from app.web.v1_auth import required_v1_scope, v1_auth_guard


class V1AuthGatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "auth.db"
        db_admin.migrate_database(self.path)
        self.now = datetime.now(timezone.utc)
        with database.get_db(self.path) as db:
            self.consumer = create_consumer(db, "gateway-test", actor="test")
            self.items_key = issue_api_key(
                db, self.consumer, {"read:items"},
                expires_at=self.now + timedelta(days=1), actor="test",
            )
            self.reports_key = issue_api_key(
                db, self.consumer, {"read:reports"},
                expires_at=self.now + timedelta(days=1), actor="test",
            )
        app = FastAPI()
        app.middleware("http")(v1_auth_guard)

        @app.get("/api/v1/items")
        def items(request: Request):
            return {"consumer_id": request.state.api_principal.consumer_id,
                    "key_id": request.state.api_principal.key_id}

        @app.get("/api/v1/items/fail")
        def failed_item():
            raise RuntimeError("synthetic route failure")

        @app.get("/api/health")
        def health():
            return {"status": "ok"}

        self.client = TestClient(app)
        patcher = patch("app.web.v1_auth.get_db", lambda: database.get_db(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_key_reaches_route_without_exposing_secret(self):
        response = self.client.get(
            "/api/v1/items", headers={"Authorization": f"Bearer {self.items_key.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"consumer_id": self.consumer,
                                           "key_id": self.items_key.key_id})
        self.assertIn("X-Request-ID", response.headers)
        self.assertNotIn(self.items_key.token, response.text)

    def test_missing_malformed_duplicate_and_revoked_keys_fail_closed(self):
        for headers in ({}, {"Authorization": self.items_key.token},
                        {"Authorization": "Bearer invalid"}):
            response = self.client.get("/api/v1/items", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "invalid_token")
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        duplicate = self.client.get("/api/v1/items", headers=[
            ("Authorization", f"Bearer {self.items_key.token}"),
            ("authorization", f"Bearer {self.items_key.token}"),
        ])
        self.assertEqual(duplicate.status_code, 401)
        with database.get_db(self.path) as db:
            revoke_api_key(db, self.items_key.key_id, actor="test")
        self.assertEqual(self.client.get(
            "/api/v1/items", headers={"Authorization": f"Bearer {self.items_key.token}"}
        ).status_code, 401)

    def test_scope_failure_is_structured_and_does_not_echo_token(self):
        response = self.client.get(
            "/api/v1/items", headers={"Authorization": f"Bearer {self.reports_key.token}"})
        self.assertEqual(response.status_code, 403)
        error = response.json()["error"]
        self.assertEqual(error["code"], "insufficient_scope")
        self.assertEqual(error["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(error["details"], {})
        self.assertNotIn(self.reports_key.token, response.text)

    def test_authentication_store_failure_is_retryable_without_leaking_details(self):
        with patch("app.web.v1_auth.get_db", side_effect=sqlite3.OperationalError("secret db path")):
            response = self.client.get(
                "/api/v1/items", headers={"Authorization": f"Bearer {self.items_key.token}"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "temporarily_unavailable")
        self.assertTrue(response.json()["error"]["retryable"])
        self.assertNotIn("secret db path", response.text)
        self.assertNotIn(self.items_key.token, response.text)

    def test_gateway_returns_rate_limit_with_retry_after(self):
        fixed = datetime(2026, 9, 22, 12, 0, 10, tzinfo=timezone.utc)
        headers = {"Authorization": f"Bearer {self.items_key.token}"}
        with patch("app.web.v1_auth.config.API_KEY_RATE_PER_MINUTE", 2), \
                patch("app.api_rate_limit._utc", return_value=fixed):
            self.assertEqual(self.client.get("/api/v1/items", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/v1/items", headers=headers).status_code, 200)
            denied = self.client.get("/api/v1/items", headers=headers)
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(denied.json()["error"]["code"], "rate_limited")
        self.assertEqual(denied.headers["Retry-After"], "50")
        self.assertNotIn(self.items_key.token, denied.text)

    def test_request_lease_is_released_if_route_raises(self):
        with self.assertRaises(RuntimeError):
            self.client.get("/api/v1/items/fail", headers={
                "Authorization": f"Bearer {self.items_key.token}"})
        with database.get_db(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_request_leases").fetchone()[0], 0)

    def test_unknown_v1_paths_and_methods_are_denied_and_legacy_health_is_unchanged(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/v1/admin").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/items").status_code, 404)
        self.assertEqual(self.client.get(
            "/api/v1/events", headers={"Authorization": f"Bearer {self.items_key.token}"}
        ).status_code, 403)
        # Known contract paths are still absent until their own reviewed PR.
        self.assertEqual(self.client.get(
            "/api/v1/reports", headers={"Authorization": f"Bearer {self.reports_key.token}"}
        ).status_code, 404)

    def test_policy_covers_target_contract_and_rejects_unlisted_shapes(self):
        cases = {
            ("GET", "/api/v1/items/a/versions"): "read:items",
            ("GET", "/api/v1/items/a/evidence"): "read:evidence",
            ("GET", "/api/v1/events/a/evidence"): "read:evidence",
            ("GET", "/api/v1/entities"): "read:catalog",
            ("GET", "/api/v1/sources/a"): "read:catalog",
            ("GET", "/api/v1/signals/a/inputs"): "read:signals",
            ("GET", "/api/v1/reports/a"): "read:reports",
            ("POST", "/api/v1/sync/snapshots"): "read:sync",
            ("GET", "/api/v1/sync/snapshots/a/pages"): "read:sync",
            ("GET", "/api/v1/changes"): "read:sync",
            ("GET", "/api/v1/items/a/unlisted"): None,
            ("POST", "/api/v1/events"): None,
        }
        for (method, path), expected in cases.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(required_v1_scope(method, path), expected)

    def test_real_portal_app_has_the_gateway_installed(self):
        from app.web.routes import app as portal_app

        response = TestClient(portal_app).get("/api/v1/items")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_token")


if __name__ == "__main__":
    unittest.main()
