import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, database
from app.ai import pipeline
from app.redaction import redact

KEY = "sk-live-0123456789abcdef"
PING = "https://hc-ping.example/9f1c-secret-uuid"


class RedactionTests(unittest.TestCase):
    def test_known_secrets_and_credential_shapes_are_removed(self):
        with patch.object(config, "LLM_API_KEY", KEY), patch.object(config, "EXTERNAL_HEARTBEAT_URL", PING):
            text = redact(f"401 for {KEY} at {PING}; Authorization: Bearer abc.def token=xyz api_key=k1")
        for secret in (KEY, PING, "abc.def", "xyz", "k1"):
            self.assertNotIn(secret, text)

    def test_model_errors_are_logged_without_the_key(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(database, "DB_PATH", Path(folder) / "app.db"), \
                patch.object(config, "llm_enabled", return_value=True), \
                patch.object(config, "LLM_API_KEY", KEY), \
                patch.object(pipeline, "_call_llm", side_effect=RuntimeError(f"invalid key {KEY}")):
            database.init_schema()
            with database.get_db() as db:
                db.execute("INSERT INTO sources(id,key,name,channel,type) VALUES(1,'s','S','ai','rss')")
                db.execute("""INSERT INTO items(id,source_id,url,title,channel,published_at,fetched_at)
                              VALUES(1,1,'https://e.test/1','t','ai','2026-10-12T00:00:00+00:00',
                                     '2026-10-12T00:00:00+00:00')""")
            with self.assertLogs("app.ai.pipeline", "WARNING") as logs:
                pipeline.process_pending(limit=5)
        self.assertIn("invalid key", "\n".join(logs.output))
        self.assertNotIn(KEY, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
