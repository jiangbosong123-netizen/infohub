import unittest
from unittest.mock import patch

import httpx

from app import config, external_heartbeat, worker

URL = "https://hc-ping.example/0b6c8e1e-secret-token"


class ExternalHeartbeatTests(unittest.TestCase):
    def test_the_url_must_be_https(self):
        self.assertEqual(config._external_heartbeat_url("  "), "")
        self.assertEqual(config._external_heartbeat_url(URL), URL)
        for bad in ("http://hc-ping.example/x", "hc-ping.example/x", "https:///x"):
            with self.subTest(bad=bad), self.assertRaises(config.RuntimeConfigurationError):
                config._external_heartbeat_url(bad)

    def test_nothing_is_sent_without_a_url(self):
        with patch.object(config, "EXTERNAL_HEARTBEAT_URL", ""), \
                patch.object(external_heartbeat.httpx, "get") as get:
            self.assertFalse(external_heartbeat.ping())
        get.assert_not_called()

    def test_a_failed_ping_is_logged_without_the_credential(self):
        request = httpx.Request("GET", URL)
        with patch.object(config, "EXTERNAL_HEARTBEAT_URL", URL), \
                patch.object(external_heartbeat.httpx, "get",
                             return_value=httpx.Response(503, request=request)), \
                self.assertLogs("app.external_heartbeat", "WARNING") as logs:
            self.assertFalse(external_heartbeat.ping())
        self.assertIn("hc-ping.example", logs.output[0])
        self.assertNotIn("secret-token", logs.output[0])
        with patch.object(config, "EXTERNAL_HEARTBEAT_URL", URL), \
                patch.object(external_heartbeat.httpx, "get", side_effect=httpx.ConnectError("offline")):
            self.assertFalse(external_heartbeat.ping())

    def test_only_a_finished_crawl_cycle_pings(self):
        with patch.object(config, "EXTERNAL_HEARTBEAT_URL", URL), \
                patch.object(external_heartbeat.httpx, "get",
                             return_value=httpx.Response(200, request=httpx.Request("GET", URL))) as get, \
                patch("app.crawler.runner.run_due_sources", return_value={"ran": 3, "results": []}):
            self.assertEqual(worker._crawl(), {"ran": 3, "results": []})
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], URL)
        with patch.object(config, "EXTERNAL_HEARTBEAT_URL", URL), \
                patch.object(external_heartbeat.httpx, "get") as get, \
                patch("app.crawler.runner.run_due_sources", side_effect=RuntimeError("database locked")):
            with self.assertRaises(RuntimeError):
                worker._crawl()
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
