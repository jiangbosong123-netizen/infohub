import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

from app import config, database, db_admin
import cli


class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_development_is_isolated_and_offline(self):
        settings = config.load_runtime_settings({}, self.root)
        expected_root = (self.root / ".runtime" / "development-local").resolve()
        self.assertEqual(settings.environment, "development")
        self.assertEqual(settings.environment_id, "development-local")
        self.assertEqual(settings.database_path, expected_root / "app.db")
        self.assertEqual(settings.blob_path, expected_root / "blobs")
        self.assertEqual(settings.backup_path, expected_root / "backups")
        self.assertFalse(settings.allow_network_tasks)
        self.assertFalse(settings.scheduler_enabled)
        self.assertFalse(settings.durable_jobs_enabled)
        self.assertFalse(settings.report_read_enabled)
        self.assertFalse(settings.report_write_enabled)
        self.assertFalse(settings.topic_statistics_enabled)
        self.assertFalse(settings.topic_read_enabled)
        self.assertFalse(settings.api_catalog_enabled)
        self.assertEqual(settings.api_key_rate_per_minute, 60)
        self.assertEqual(settings.api_consumer_concurrency, 5)
        self.assertEqual(settings.api_request_lease_seconds, 300)
        self.assertIsNone(settings.public_origin)
        self.assertEqual(settings.process_role, "web")

    def test_legacy_local_layout_requires_explicit_compatibility_flag(self):
        settings = config.load_runtime_settings(
            {"INFOHUB_LEGACY_DATA_LAYOUT": "true"}, self.root
        )
        self.assertEqual(settings.database_path, (self.root / "data" / "app.db").resolve())
        self.assertTrue(settings.legacy_data_layout)
        self.assertFalse(settings.scheduler_enabled)

    def test_curated_search_requires_curated_read_projection(self):
        with self.assertRaisesRegex(config.RuntimeConfigurationError, "requires INFOHUB_CURATION_READ_ENABLED"):
            config.load_runtime_settings({"INFOHUB_CURATION_SEARCH_ENABLED": "true"}, self.root)
        settings = config.load_runtime_settings({
            "INFOHUB_CURATION_READ_ENABLED": "true",
            "INFOHUB_CURATION_SEARCH_ENABLED": "true",
        }, self.root)
        self.assertTrue(settings.curation_search_enabled)

    def test_curated_hotspot_requires_curated_read_projection(self):
        with self.assertRaisesRegex(config.RuntimeConfigurationError, "requires INFOHUB_CURATION_READ_ENABLED"):
            config.load_runtime_settings({"INFOHUB_CURATION_HOT_ENABLED": "true"}, self.root)
        settings = config.load_runtime_settings({
            "INFOHUB_CURATION_READ_ENABLED": "true",
            "INFOHUB_CURATION_HOT_ENABLED": "true",
        }, self.root)
        self.assertTrue(settings.curation_hot_enabled)

    def test_report_read_switch_is_independent_and_defaults_off(self):
        self.assertFalse(config.load_runtime_settings({}, self.root).report_read_enabled)
        self.assertTrue(config.load_runtime_settings(
            {"INFOHUB_REPORT_READ_ENABLED": "true"}, self.root
        ).report_read_enabled)

    def test_topic_statistics_worker_switch_is_independent(self):
        settings = config.load_runtime_settings(
            {"INFOHUB_TOPIC_STATISTICS_ENABLED": "true"}, self.root
        )
        self.assertTrue(settings.topic_statistics_enabled)

    def test_topic_portal_read_switch_is_independent_and_defaults_off(self):
        self.assertFalse(config.load_runtime_settings({}, self.root).topic_read_enabled)
        settings = config.load_runtime_settings(
            {"INFOHUB_TOPIC_READ_ENABLED": "true"}, self.root
        )
        self.assertTrue(settings.topic_read_enabled)

    def test_report_write_requires_visible_read_path(self):
        with self.assertRaisesRegex(config.RuntimeConfigurationError, "requires INFOHUB_REPORT_READ_ENABLED"):
            config.load_runtime_settings({"INFOHUB_REPORT_WRITE_ENABLED": "true"}, self.root)
        settings = config.load_runtime_settings({
            "INFOHUB_REPORT_READ_ENABLED": "true",
            "INFOHUB_REPORT_WRITE_ENABLED": "true",
        }, self.root)
        self.assertTrue(settings.report_write_enabled)

    def test_api_request_limits_are_bounded_and_configurable(self):
        settings = config.load_runtime_settings({
            "INFOHUB_API_KEY_RATE_PER_MINUTE": "90",
            "INFOHUB_API_CONSUMER_CONCURRENCY": "7",
            "INFOHUB_API_REQUEST_LEASE_SECONDS": "120",
        }, self.root)
        self.assertEqual((settings.api_key_rate_per_minute,
                          settings.api_consumer_concurrency,
                          settings.api_request_lease_seconds), (90, 7, 120))
        for name, value in (
            ("INFOHUB_API_KEY_RATE_PER_MINUTE", "0"),
            ("INFOHUB_API_CONSUMER_CONCURRENCY", "101"),
            ("INFOHUB_API_REQUEST_LEASE_SECONDS", "10"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(config.RuntimeConfigurationError):
                    config.load_runtime_settings({name: value}, self.root)

    def test_production_requires_identity_paths_and_task_flags(self):
        cases = (
            {"INFOHUB_ENVIRONMENT": "production"},
            {
                "INFOHUB_ENVIRONMENT": "production",
                "INFOHUB_ENVIRONMENT_ID": "windows-production",
            },
            {
                "INFOHUB_ENVIRONMENT": "production",
                "INFOHUB_ENVIRONMENT_ID": "windows-production",
                "INFOHUB_DB_PATH": "/data/app.db",
                "INFOHUB_BLOB_PATH": "/data/blobs",
                "INFOHUB_BACKUP_PATH": "/data/backups",
            },
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(config.RuntimeConfigurationError):
                    config.load_runtime_settings(values, self.root)

    def test_valid_production_configuration_is_labeled(self):
        settings = config.load_runtime_settings(
            {
                "INFOHUB_ENVIRONMENT": "production",
                "INFOHUB_ENVIRONMENT_ID": "windows-production",
                "INFOHUB_DB_PATH": "/app/data/app.db",
                "INFOHUB_BLOB_PATH": "/app/data/blobs",
                "INFOHUB_BACKUP_PATH": "/app/data/backups",
                "INFOHUB_RUNTIME_PATH": "/app/data/runtime",
                "INFOHUB_ALLOW_NETWORK_TASKS": "false",
                "INFOHUB_ENABLE_SCHEDULER": "false",
                "INFOHUB_DURABLE_JOBS_ENABLED": "true",
                "INFOHUB_PROCESS_ROLE": "web",
                "INFOHUB_PUBLIC_ORIGIN": "https://windows-server.example-tailnet.ts.net",
            },
            self.root,
        )
        self.assertEqual(settings.environment_id, "windows-production")
        self.assertEqual(settings.process_role, "web")
        self.assertFalse(settings.allow_network_tasks)
        self.assertFalse(settings.scheduler_enabled)
        self.assertTrue(settings.durable_jobs_enabled)
        self.assertEqual(settings.public_origin,
                         "https://windows-server.example-tailnet.ts.net")

    def test_public_origin_is_private_https_tailnet_name(self):
        for value in (
            "http://windows-server.example.ts.net",
            "https://100.69.211.16",
            "https://windows-server.example.ts.net:8443",
            "https://windows-server.example.ts.net/path",
            "https://user@windows-server.example.ts.net",
        ):
            with self.subTest(value=value):
                with self.assertRaises(config.RuntimeConfigurationError):
                    config.load_runtime_settings({"INFOHUB_PUBLIC_ORIGIN": value}, self.root)
        settings = config.load_runtime_settings(
            {"INFOHUB_PUBLIC_ORIGIN": "https://WINDOWS-SERVER.EXAMPLE.TS.NET/"}, self.root)
        self.assertEqual(settings.public_origin, "https://windows-server.example.ts.net")

    def test_compose_production_environment_passes_runtime_validation(self):
        compose = yaml.safe_load((config.BASE_DIR / "compose.yaml").read_text(encoding="utf-8"))
        expected_roles = {"migrate": "maintenance", "infohub": "web", "worker": "worker"}
        self.assertEqual(set(compose["services"]), set(expected_roles))
        for name, expected_role in expected_roles.items():
            service = compose["services"][name]
            values = {key: str(value) for key, value in service["environment"].items()}
            values["INFOHUB_ENVIRONMENT_ID"] = "windows-production"
            values["INFOHUB_PUBLIC_ORIGIN"] = "https://windows-server.example-tailnet.ts.net"
            values["INFOHUB_CURATED_FEED_ENABLED"] = "true"
            values["INFOHUB_CURATION_READ_ENABLED"] = "false"
            values["INFOHUB_CURATION_SEARCH_ENABLED"] = "false"
            values["INFOHUB_CURATION_HOT_ENABLED"] = "false"
            values["INFOHUB_TOPIC_STATISTICS_ENABLED"] = "false"
            values["INFOHUB_TOPIC_READ_ENABLED"] = "false"
            values["INFOHUB_REPORT_READ_ENABLED"] = "false"
            values["INFOHUB_REPORT_WRITE_ENABLED"] = "false"
            values["INFOHUB_API_CATALOG_ENABLED"] = "false"
            settings = config.load_runtime_settings(values, self.root)
            self.assertEqual(settings.database_path, Path("/app/data/app.db"))
            self.assertEqual(settings.backup_path, Path("/app/data/backups"))
            self.assertEqual(settings.runtime_path, Path("/app/data/runtime"))
            self.assertEqual(settings.process_role, expected_role)
            self.assertTrue(settings.durable_jobs_enabled)
            self.assertIn("./data:/app/data", service["volumes"])
        web_values = {
            key: str(value)
            for key, value in compose["services"]["infohub"]["environment"].items()
        }
        worker_values = {
            key: str(value)
            for key, value in compose["services"]["worker"]["environment"].items()
        }
        web_values["INFOHUB_ENVIRONMENT_ID"] = "windows-production"
        worker_values["INFOHUB_ENVIRONMENT_ID"] = "windows-production"
        web_values["INFOHUB_PUBLIC_ORIGIN"] = "https://windows-server.example-tailnet.ts.net"
        worker_values["INFOHUB_PUBLIC_ORIGIN"] = "https://windows-server.example-tailnet.ts.net"
        web_values["INFOHUB_CURATED_FEED_ENABLED"] = "true"
        worker_values["INFOHUB_CURATED_FEED_ENABLED"] = "true"
        web_values["INFOHUB_CURATION_READ_ENABLED"] = "false"
        worker_values["INFOHUB_CURATION_READ_ENABLED"] = "false"
        web_values["INFOHUB_CURATION_SEARCH_ENABLED"] = "false"
        worker_values["INFOHUB_CURATION_SEARCH_ENABLED"] = "false"
        web_values["INFOHUB_CURATION_HOT_ENABLED"] = "false"
        worker_values["INFOHUB_CURATION_HOT_ENABLED"] = "false"
        web_values["INFOHUB_TOPIC_STATISTICS_ENABLED"] = "false"
        worker_values["INFOHUB_TOPIC_STATISTICS_ENABLED"] = "false"
        web_values["INFOHUB_TOPIC_READ_ENABLED"] = "false"
        worker_values["INFOHUB_TOPIC_READ_ENABLED"] = "false"
        web_values["INFOHUB_REPORT_READ_ENABLED"] = "false"
        worker_values["INFOHUB_REPORT_READ_ENABLED"] = "false"
        web_values["INFOHUB_REPORT_WRITE_ENABLED"] = "false"
        worker_values["INFOHUB_REPORT_WRITE_ENABLED"] = "false"
        web_values["INFOHUB_API_CATALOG_ENABLED"] = "false"
        worker_values["INFOHUB_API_CATALOG_ENABLED"] = "false"
        self.assertFalse(config.load_runtime_settings(web_values, self.root).allow_network_tasks)
        self.assertTrue(config.load_runtime_settings(worker_values, self.root).allow_network_tasks)
        self.assertIn("/api/live", " ".join(compose["services"]["infohub"]["healthcheck"]["test"]))
        self.assertEqual(compose["services"]["worker"]["command"][-1], "worker")
        self.assertEqual(compose["services"]["migrate"]["command"][-1], "prepare-release")
        self.assertEqual(
            compose["services"]["infohub"]["depends_on"]["migrate"]["condition"],
            "service_completed_successfully",
        )
        self.assertNotIn("env_file", compose["services"]["infohub"])
        self.assertNotIn("env_file", compose["services"]["migrate"])
        self.assertEqual(compose["services"]["worker"]["env_file"], [".env"])
        self.assertEqual(compose["services"]["infohub"]["ports"], ["127.0.0.1:8000:8000"])

    def test_invalid_environment_flags_and_overlapping_paths_fail(self):
        invalid_cases = (
            {"INFOHUB_ENVIRONMENT": "prod"},
            {"INFOHUB_ENVIRONMENT_ID": "Mac Production"},
            {"INFOHUB_ALLOW_NETWORK_TASKS": "sometimes"},
            {"INFOHUB_DURABLE_JOBS_ENABLED": "sometimes"},
            {"INFOHUB_PROCESS_ROLE": "combined"},
            {
                "INFOHUB_PROCESS_ROLE": "web",
                "INFOHUB_ALLOW_NETWORK_TASKS": "true",
            },
            {
                "INFOHUB_PROCESS_ROLE": "worker",
                "INFOHUB_ALLOW_NETWORK_TASKS": "true",
                "INFOHUB_ENABLE_SCHEDULER": "true",
                "INFOHUB_DURABLE_JOBS_ENABLED": "false",
            },
            {
                "INFOHUB_PROCESS_ROLE": "maintenance",
                "INFOHUB_ALLOW_NETWORK_TASKS": "true",
                "INFOHUB_ENABLE_SCHEDULER": "true",
            },
            {
                "INFOHUB_BLOB_PATH": "runtime/shared",
                "INFOHUB_BACKUP_PATH": "runtime/shared/backups",
            },
            {
                "INFOHUB_ENABLE_SCHEDULER": "true",
                "INFOHUB_ALLOW_NETWORK_TASKS": "false",
            },
            {"INFOHUB_DB_PATH": "data/app.db"},
            {
                "INFOHUB_ENVIRONMENT": "production",
                "INFOHUB_ENVIRONMENT_ID": "windows-production",
                "INFOHUB_DB_PATH": "data/app.db",
                "INFOHUB_BLOB_PATH": "/app/data/blobs",
                "INFOHUB_BACKUP_PATH": "/app/data/backups",
                "INFOHUB_ALLOW_NETWORK_TASKS": "false",
                "INFOHUB_ENABLE_SCHEDULER": "false",
            },
        )
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(config.RuntimeConfigurationError):
                    config.load_runtime_settings(values, self.root)

    def test_manual_network_commands_stop_before_importing_workers(self):
        with patch.object(config, "ALLOW_NETWORK_TASKS", False):
            with self.assertRaisesRegex(config.RuntimeConfigurationError, "crawl is disabled"):
                cli.cmd_crawl()

    def test_llm_credentials_do_not_enable_calls_in_offline_environment(self):
        with patch.multiple(
            config,
            ALLOW_NETWORK_TASKS=False,
            LLM_BASE_URL="https://example.com/v1",
            LLM_API_KEY="secret-fixture",
            LLM_MODEL="fixture",
        ):
            self.assertFalse(config.llm_enabled())

    def test_configured_database_uses_labeled_backup_directory(self):
        database_path = self.root / "production" / "app.db"
        backup_path = self.root / "production" / "verified-backups"
        with patch.object(config, "DB_PATH", database_path), patch.object(
            config, "BACKUP_PATH", backup_path
        ), patch.object(config, "ENVIRONMENT_ID", "windows-production"), patch.object(
            database, "DB_PATH", database_path
        ):
            db_admin.migrate_database(database_path)
            with database.get_db(database_path) as db:
                db.execute(
                    "INSERT INTO sources(key,name,channel,type) VALUES('fixture','Fixture','ai','rss')"
                )
            report = db_admin.backup_database()
        backup = Path(report.path)
        self.assertEqual(backup.parent, backup_path.resolve())
        self.assertIn("windows-production", backup.name)
        self.assertEqual(db_admin.verify_database(backup, require_current=True).integrity, "ok")

    def test_jobs_status_is_read_only_and_reports_rollout_gate(self):
        database_path = self.root / "status" / "app.db"
        db_admin.migrate_database(database_path)
        output = StringIO()
        with patch.object(config, "DB_PATH", database_path), patch.object(
            database, "DB_PATH", database_path
        ), redirect_stdout(output):
            cli.cmd_jobs_status()
        status = json.loads(output.getvalue())
        self.assertFalse(status["enabled"])
        self.assertEqual(status["states"]["pending"], 0)


if __name__ == "__main__":
    unittest.main()
