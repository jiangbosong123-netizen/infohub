import importlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import config, database, evidence_backup, offsite_backup, self_check
from app.api_auth import create_consumer, issue_api_key
from app.crawler import runner
from app.ingest import begin_ingest_run, observe_candidate
from app.scheduled_backup import run_nightly_backup
from app.worker import register_default_schedules
from app.web import routes

MACOS = str(Path(__file__).resolve().parents[1] / "deploy" / "macos")


def load_puller():
    if MACOS not in sys.path:
        sys.path.insert(0, MACOS)
    return importlib.import_module("offsite_pull")


class _Response(io.BytesIO):
    """What urllib.request.urlopen returns, enough for offsite_pull."""


class OffsiteBackupTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for target, name, value in (
            (database, "DB_PATH", self.root / "app.db"), (config, "DB_PATH", self.root / "app.db"),
            (config, "BLOB_PATH", self.root / "blobs"), (config, "BACKUP_PATH", self.root / "backups"),
            (config, "RUNTIME_PATH", self.root / "runtime"), (config, "BACKUP_RESERVE_MB", 1),
            (config, "API_BACKUPS_ENABLED", True),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        database.init_schema()
        source = {"key": "wire", "name": "Wire", "channel": "ai", "tier": "info", "type": "rss",
                  "url": "https://example.com/feed"}
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier,url) VALUES('wire','Wire','ai','rss','info',?)",
                       (source["url"],))
        ingest = begin_ingest_run(source)
        for number in range(3):  # each observation keeps its raw payload as an evidence blob
            raw = dict(url=f"https://example.com/{number}", title=f"OpenAI story {number}", companies=["openai"])
            observe_candidate(ingest, raw, ordinal=number)
            self.assertTrue(runner.insert_item("wire", raw))
        self.bundle = Path(run_nightly_backup()["path"])
        self.token = self.issue({"read:backups"})
        self.client = TestClient(routes.app)
        self.puller = load_puller()

    def issue(self, scopes):
        with database.get_db() as db:
            consumer = create_consumer(db, f"mac-{len(scopes)}-{sorted(scopes)[0]}", actor="test")
            return issue_api_key(db, consumer, scopes, actor="test",
                                 expires_at=datetime.now(timezone.utc) + timedelta(days=30)).token

    def get(self, path, token=None):
        return self.client.get(path, headers={"Authorization": f"Bearer {token or self.token}"})

    def route(self, url, token, *, data=None, timeout=30.0):
        """offsite_pull's HTTP calls, answered by the app in process."""
        path = url.split("ts.net", 1)[1]
        headers = {"Authorization": f"Bearer {token}"}
        if data is None:
            response = self.client.get(path, headers=headers)
        else:
            response = self.client.post(path, content=data,
                                        headers={**headers, "Content-Type": "application/json"})
        if response.status_code >= 400:
            raise self.puller.urllib.error.HTTPError(url, response.status_code, "refused", {}, None)
        return _Response(response.content)

    def tree(self, root):
        return sorted((path.relative_to(root).as_posix(), path.read_bytes() if path.is_file() else None)
                      for path in root.rglob("*"))

    def test_the_mac_pulls_verifies_and_reports_the_newest_bundle(self):
        latest = self.get("/api/v1/backups/latest").json()
        self.assertEqual(latest["name"], self.bundle.name)
        self.assertEqual(latest["files"], 2 + 3)  # database, manifest and three evidence blobs
        self.assertEqual(latest["files"], sum(1 for path in self.bundle.rglob("*") if path.is_file()))
        destination = self.root / "mac"
        with patch.object(self.puller, "_request", side_effect=self.route):
            result = self.puller.pull("https://windows-server.example.ts.net", self.token, destination)
            self.assertEqual(result["result"], "pulled")
            self.assertEqual((result["items"], result["blobs"]), (3, 3))
            self.assertEqual(self.tree(destination / self.bundle.name), self.tree(self.bundle))
            self.assertEqual(evidence_backup.verify_backup_bundle(destination / self.bundle.name)["status"], "ok")
            self.assertEqual(self.puller.pull("https://windows-server.example.ts.net", self.token, destination),
                             {"result": "up_to_date", "name": self.bundle.name})
        receipt = offsite_backup.read_receipt()
        self.assertEqual((receipt["name"], receipt["database_sha256"]), (self.bundle.name, latest["database_sha256"]))

    def test_only_a_newer_bundle_replaces_the_mac_copy(self):
        destination = self.root / "mac"
        with patch.object(self.puller, "_request", side_effect=self.route):
            self.puller.pull("https://windows-server.example.ts.net", self.token, destination)
            newer = Path(run_nightly_backup()["path"])
            result = self.puller.pull("https://windows-server.example.ts.net", self.token, destination)
        self.assertEqual(result["removed"], [self.bundle.name])
        self.assertEqual([entry.name for entry in destination.iterdir() if entry.is_dir()], [newer.name])

    def test_the_api_needs_the_flag_the_scope_and_a_published_name(self):
        self.assertEqual(self.client.get("/api/v1/backups/latest").status_code, 401)
        self.assertEqual(self.get("/api/v1/backups/latest", self.issue({"read:items"})).status_code, 403)
        for name in ("missing.bundle", "..", self.bundle.name + "x"):
            with self.subTest(name=name):
                self.assertEqual(self.get(f"/api/v1/backups/{name}/archive").status_code, 404)
        with patch.object(config, "API_BACKUPS_ENABLED", False):
            self.assertEqual(self.get("/api/v1/backups/latest").status_code, 503)
        wrong = self.client.post(f"/api/v1/backups/{self.bundle.name}/receipt",
                                 headers={"Authorization": f"Bearer {self.token}"},
                                 json={"database_sha256": "0" * 64})
        self.assertEqual(wrong.status_code, 409)
        self.assertIsNone(offsite_backup.read_receipt())

    def test_a_damaged_or_hostile_archive_is_refused(self):
        archive = self.get(f"/api/v1/backups/{self.bundle.name}/archive").content
        stage = self.root / "stage"
        stage.mkdir()
        self.puller.extract_stream(io.BytesIO(archive), stage)
        expected = json.loads((self.bundle / "manifest.json").read_text())["database_sha256"]
        self.assertEqual(self.puller.verify_bundle(stage, expected)["items"], 3)
        blob = next((stage / "blobs").rglob("*/*/*"))
        blob.write_bytes(b"tampered")
        with self.assertRaisesRegex(self.puller.PullError, "evidence file"):
            self.puller.verify_bundle(stage, expected)
        for name, kind in (("../escape", tarfile.REGTYPE), ("bundle/link", tarfile.SYMTYPE), ("/etc/x", tarfile.REGTYPE)):
            with self.subTest(name=name):
                hostile = io.BytesIO()
                with tarfile.open(fileobj=hostile, mode="w") as writer:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    writer.addfile(info, io.BytesIO(b""))
                hostile.seek(0)
                target = self.root / f"hostile-{abs(hash(name))}"
                target.mkdir()
                with self.assertRaises(self.puller.PullError):
                    self.puller.extract_stream(hostile, target)

    def test_the_token_file_must_be_private(self):
        path = self.root / "offsite.token"
        path.write_bytes(b"\xff\xfe" + f"{self.token}\r\n".encode("utf-16-le"))  # PowerShell '>' output
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(self.puller.PullError, "chmod 600"):
            self.puller.read_token(path)
        os.chmod(path, 0o600)
        self.assertEqual(self.puller.read_token(path), self.token)

    def test_the_self_check_warns_when_the_mac_copy_is_a_week_old(self):
        now = datetime.now(timezone.utc)
        register_default_schedules()
        with database.get_db() as db:
            self.assertEqual(self_check._offsite(db, now), [])  # the backup schedule is not a week old
            db.execute("UPDATE schedules SET created_at=? WHERE id='maintenance:backup'",
                       ((now - timedelta(days=8)).isoformat(),))
            self.assertEqual([p.code for p in self_check._offsite(db, now)], ["offsite_stale"])
            sha = json.loads((self.bundle / "manifest.json").read_text())["database_sha256"]
            offsite_backup.record_receipt(self.bundle.name, sha, "consumer", now=now - timedelta(days=2))
            self.assertEqual(self_check._offsite(db, now), [])
            offsite_backup.record_receipt(self.bundle.name, sha, "consumer", now=now - timedelta(days=8))
            [problem] = self_check._offsite(db, now)
            self.assertIn("8 天", problem.detail)
            with patch.object(config, "API_BACKUPS_ENABLED", False):
                self.assertEqual(self_check._offsite(db, now), [])

    def test_a_new_offsite_key_replaces_the_previous_one(self):
        with database.get_db() as db:
            first = offsite_backup.issue_offsite_key(db, actor="test")
        self.assertEqual(self.get("/api/v1/backups/latest", first).status_code, 200)
        self.assertEqual(self.get("/api/v1/items", first).status_code, 403)  # backups only
        with database.get_db() as db:
            second = offsite_backup.issue_offsite_key(db, actor="test")
        self.assertEqual(self.get("/api/v1/backups/latest", first).status_code, 401)
        self.assertEqual(self.get("/api/v1/backups/latest", second).status_code, 200)

    def test_the_mac_script_reads_the_format_the_host_writes(self):
        self.assertEqual(self.puller.FORMAT, evidence_backup.FORMAT)


if __name__ == "__main__":
    unittest.main()
