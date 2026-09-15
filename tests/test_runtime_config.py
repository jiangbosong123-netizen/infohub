import tempfile
import unittest
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
        self.assertEqual(settings.process_role, "web")

    def test_legacy_local_layout_requires_explicit_compatibility_flag(self):
        settings = config.load_runtime_settings(
            {"INFOHUB_LEGACY_DATA_LAYOUT": "true"}, self.root
        )
        self.assertEqual(settings.database_path, (self.root / "data" / "app.db").resolve())
        self.assertTrue(settings.legacy_data_layout)
        self.assertFalse(settings.scheduler_enabled)

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
                "INFOHUB_ALLOW_NETWORK_TASKS": "true",
                "INFOHUB_ENABLE_SCHEDULER": "true",
            },
            self.root,
        )
        self.assertEqual(settings.environment_id, "windows-production")
        self.assertEqual(settings.process_role, "combined")
        self.assertTrue(settings.allow_network_tasks)
        self.assertTrue(settings.scheduler_enabled)

    def test_compose_production_environment_passes_runtime_validation(self):
        compose = yaml.safe_load((config.BASE_DIR / "compose.yaml").read_text(encoding="utf-8"))
        service = compose["services"]["infohub"]
        values = {key: str(value) for key, value in service["environment"].items()}
        values["INFOHUB_ENVIRONMENT_ID"] = "windows-production"
        settings = config.load_runtime_settings(values, self.root)
        self.assertEqual(settings.database_path, Path("/app/data/app.db"))
        self.assertEqual(settings.backup_path, Path("/app/data/backups"))
        self.assertEqual(settings.process_role, "combined")
        self.assertIn("./data:/app/data", service["volumes"])

    def test_invalid_environment_flags_and_overlapping_paths_fail(self):
        invalid_cases = (
            {"INFOHUB_ENVIRONMENT": "prod"},
            {"INFOHUB_ENVIRONMENT_ID": "Mac Production"},
            {"INFOHUB_ALLOW_NETWORK_TASKS": "sometimes"},
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


if __name__ == "__main__":
    unittest.main()
