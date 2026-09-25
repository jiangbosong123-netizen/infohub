from __future__ import annotations

"""Offline portal read checks against a disposable copy of a restored bundle."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from . import config
from .evidence_backup import EvidenceBackupError, verify_backup_bundle


def _local_page_checks() -> dict:
    from fastapi.testclient import TestClient

    from . import database
    from .web import routes

    with database.get_db() as db:
        legacy = db.execute("SELECT date FROM daily_reports ORDER BY date DESC LIMIT 1").fetchone()
        versioned = db.execute(
            """SELECT s.report_date FROM report_publications p
               JOIN report_versions v ON v.id=p.current_version_id
               JOIN report_input_snapshots s ON s.id=v.input_snapshot_id
               ORDER BY s.report_date DESC LIMIT 1"""
        ).fetchone()
        topic = db.execute("SELECT slug FROM topics WHERE enabled=1 ORDER BY position LIMIT 1").fetchone()
        story = db.execute("SELECT id FROM stories WHERE redirect_to IS NULL ORDER BY id LIMIT 1").fetchone()
        sample = db.execute(
            "SELECT title FROM items WHERE COALESCE(tmt,1)!=0 AND length(trim(title))>0 ORDER BY id LIMIT 1"
        ).fetchone()

    pages = [("home", "/", None), ("topics", "/topics", None),
             ("search", "/search", {"q": sample[0][:32] if sample else "InfoHubSmoke"}),
             ("daily_list", "/daily", None), ("live", "/api/live", None)]
    if legacy:
        pages.append(("daily_legacy", f"/daily/{quote(legacy[0], safe='')}", None))
    if versioned and routes.REPORT_READ_ENABLED:
        pages.append(("daily_versioned", f"/daily/{quote(versioned[0], safe='')}", None))
    if topic:
        pages.append(("topic_detail", f"/topics/{quote(topic[0], safe='')}", None))
    if story:
        pages.append(("story_detail", f"/story/{quote(story[0], safe='')}", None))

    results = []
    with TestClient(routes.app, raise_server_exceptions=False) as client:
        for name, path, params in pages:
            response = client.get(path, params=params, follow_redirects=False)
            passed = response.status_code == 200
            if name == "live":
                passed = passed and response.json().get("status") == "live"
            elif name == "daily_versioned":
                passed = (passed and "新版日报校验失败" not in response.text
                          and "版本 " in response.text)
            results.append({"name": name, "status": "ok" if passed else "failed",
                            "http_status": response.status_code})
    return {"status": "ok" if all(r["status"] == "ok" for r in results) else "failed",
            "checks": results}


def smoke_restored_bundle(bundle: Path | str) -> dict:
    """Verify and render portal routes without writing into the restored bundle."""
    root = Path(bundle).expanduser().resolve(strict=True)
    before = verify_backup_bundle(root)
    with tempfile.TemporaryDirectory(prefix="infohub-portal-smoke-") as temp:
        disposable = Path(temp) / "app.db"
        shutil.copyfile(root / "database.db", disposable)
        environment = os.environ.copy()
        environment.update({
            "INFOHUB_DB_PATH": str(disposable),
            "INFOHUB_BLOB_PATH": str(root / "blobs"),
            "INFOHUB_PROCESS_ROLE": "web",
            "INFOHUB_ALLOW_NETWORK_TASKS": "false",
            "INFOHUB_ENABLE_SCHEDULER": "false",
            "OPENAI_API_KEY": "",
        })
        try:
            child = subprocess.run(
                [sys.executable, "-m", "app.portal_smoke", "--child"],
                cwd=config.BASE_DIR, env=environment, text=True, capture_output=True,
                timeout=90, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceBackupError("portal smoke timed out") from exc
    if child.returncode not in (0, 1):
        raise EvidenceBackupError(
            f"portal smoke process failed (exit {child.returncode})"
        )
    try:
        result = json.loads(child.stdout)
    except (ValueError, TypeError) as exc:
        raise EvidenceBackupError("portal smoke did not return a valid result") from exc
    after = verify_backup_bundle(root)
    if before["database_sha256"] != after["database_sha256"]:
        raise EvidenceBackupError("portal smoke changed its source bundle")
    return {**result, "database_sha256": after["database_sha256"],
            "bundle_unchanged": True}


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit(2)
    try:
        outcome = _local_page_checks()
    except Exception as exc:
        outcome = {"status": "failed", "checks": [], "error_type": type(exc).__name__}
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome["status"] == "ok" else 1)
