from __future__ import annotations

"""Consistent SQLite plus referenced-CAS backup bundles."""

import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config, database
from .db_admin import backup_database, verify_database
from .ingest import audit_evidence_payloads, payload_path, verify_payload


FORMAT = "infohub-evidence-bundle-v1"


class EvidenceBackupError(RuntimeError):
    """A backup bundle cannot be trusted or published."""


def _referenced_hashes(path: Path) -> list[str]:
    query = """
        SELECT payload_sha256 AS digest FROM raw_records
        UNION SELECT rendered_input_sha256 FROM analysis_runs
        UNION SELECT raw_response_sha256 FROM analysis_attempts
              WHERE raw_response_sha256 IS NOT NULL
        UNION SELECT raw_output_sha256 FROM analysis_results
              WHERE raw_output_sha256 IS NOT NULL
        UNION SELECT rendered_prompt_sha256 FROM report_generation_runs
        UNION SELECT raw_response_sha256 FROM report_generation_attempts
              WHERE raw_response_sha256 IS NOT NULL
        ORDER BY digest
    """
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        return [row[0] for row in db.execute(query)]


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Windows filesystems do not all support directory fsync.
        pass


def verify_backup_bundle(bundle: Path | str) -> dict:
    root = Path(bundle).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise EvidenceBackupError("backup bundle is not a directory")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise EvidenceBackupError("unsupported backup bundle format")
        database_file = root / "database.db"
        report = verify_database(database_file, require_current=True)
        if (manifest.get("database_sha256") != report.file_sha256
                or manifest.get("schema_version") != report.schema_version
                or manifest.get("dataset_id") != report.dataset_id
                or manifest.get("dataset_epoch") != report.dataset_epoch
                or manifest.get("change_high_water") != report.change_high_water):
            raise EvidenceBackupError("backup database does not match its manifest")
        hashes = _referenced_hashes(database_file)
        if manifest.get("blob_sha256") != hashes:
            raise EvidenceBackupError("backup blob manifest does not match database references")
        evidence = audit_evidence_payloads(root / "blobs", database_file)
        if not evidence.healthy:
            raise EvidenceBackupError("backup contains missing or corrupt evidence blobs")
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as exc:
        raise EvidenceBackupError("backup bundle verification failed") from exc
    return {
        "status": "ok", "path": str(root), "database_sha256": report.file_sha256,
        "schema_version": report.schema_version, "dataset_id": report.dataset_id,
        "dataset_epoch": report.dataset_epoch,
        "change_high_water": report.change_high_water,
        "unique_blobs": len(hashes), "evidence": evidence.to_dict(),
    }


def create_backup_bundle(destination: Path | str | None = None) -> dict:
    """Snapshot the current DB, copy exactly its referenced blobs, then publish atomically."""
    source = Path(database.DB_PATH).expanduser().resolve(strict=True)
    blob_root = Path(config.BLOB_PATH).expanduser().resolve()
    verify_database(source, require_current=True)
    if destination is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = config.BACKUP_PATH / f"{source.stem}.{config.ENVIRONMENT_ID}.{stamp}.bundle"
    else:
        target = Path(destination)
    target = target.expanduser().resolve()
    try:
        target.relative_to(blob_root)
    except ValueError:
        pass
    else:
        raise EvidenceBackupError("backup destination cannot be inside the blob store")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"backup destination already exists: {target}")
    stage = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    stage.mkdir()
    try:
        backup_database(source, stage / "database.db")
        database_file = stage / "database.db"
        report = verify_database(database_file, require_current=True)
        source_audit = audit_evidence_payloads(blob_root, database_file)
        if not source_audit.healthy:
            raise EvidenceBackupError("source has missing or corrupt referenced blobs")
        hashes = _referenced_hashes(database_file)
        for digest in hashes:
            relative = Path("sha256") / digest[:2] / digest
            source_file = verify_payload(relative.as_posix(), digest, blob_root)
            copied = stage / "blobs" / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            with source_file.open("rb") as reader, copied.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            verify_payload(relative.as_posix(), digest, stage / "blobs")
        manifest = {
            "format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
            "database_sha256": report.file_sha256,
            "schema_version": report.schema_version, "dataset_id": report.dataset_id,
            "dataset_epoch": report.dataset_epoch,
            "change_high_water": report.change_high_water,
            "blob_sha256": hashes,
        }
        with (stage / "manifest.json").open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verify_backup_bundle(stage)
        _sync_directory(stage)
        if target.exists():
            raise FileExistsError(f"backup destination already exists: {target}")
        os.replace(stage, target)
        _sync_directory(target.parent)
        return verify_backup_bundle(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
