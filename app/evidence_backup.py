from __future__ import annotations

"""Consistent SQLite plus referenced-CAS backup bundles."""

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config, database
from .db_admin import (
    VerificationReport, backup_database, same_bytes_as_verified, verify_database,
)
from .ingest import PayloadIntegrityError, audit_evidence_payloads, verify_payload


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
    return _verify_bundle(bundle)[0]


def _verify_bundle(
    bundle: Path | str, verified: VerificationReport | None = None,
) -> tuple[dict, VerificationReport]:
    """Verify a bundle; ``verified`` is a full verification of byte-identical database bytes.

    Creating and restoring a bundle copy or rename a database file that already passed full
    verification; for those, matching its SHA-256 replaces another full run. Blobs are always
    re-hashed, and a bundle verified on its own (``db-bundle-verify``) gets every check.
    """
    root = Path(bundle).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise EvidenceBackupError("backup bundle is not a directory")
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise EvidenceBackupError("unsupported backup bundle format")
        database_file = root / "database.db"
        report = (verify_database(database_file, require_current=True) if verified is None
                  else same_bytes_as_verified(database_file, verified))
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
    }, report


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
        # backup_database fully verifies the snapshot it publishes; reuse that report.
        report = backup_database(source, stage / "database.db", require_current=True)
        database_file = stage / "database.db"
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
        _verify_bundle(stage, report)
        _sync_directory(stage)
        if target.exists():
            raise FileExistsError(f"backup destination already exists: {target}")
        os.replace(stage, target)
        _sync_directory(target.parent)
        return _verify_bundle(target, report)[0]
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def restore_backup_bundle(bundle: Path | str, destination: Path | str) -> dict:
    """Restore a verified bundle to a new isolated directory without switching live data."""
    source = Path(bundle).expanduser().resolve(strict=True)
    verified, report = _verify_bundle(source)
    target = Path(destination).expanduser().resolve()
    try:
        target.relative_to(source)
    except ValueError:
        pass
    else:
        raise EvidenceBackupError("restore destination cannot be inside its bundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"restore destination already exists: {target}")
    stage = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    stage.mkdir()
    try:
        for name in ("database.db", "manifest.json"):
            with (source / name).open("rb") as reader, (stage / name).open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
        for digest in _referenced_hashes(stage / "database.db"):
            relative = Path("sha256") / digest[:2] / digest
            source_file = verify_payload(relative.as_posix(), digest, source / "blobs")
            restored = stage / "blobs" / relative
            restored.parent.mkdir(parents=True, exist_ok=True)
            with source_file.open("rb") as reader, restored.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            verify_payload(relative.as_posix(), digest, stage / "blobs")
        staged, _ = _verify_bundle(stage, report)
        if staged["database_sha256"] != verified["database_sha256"]:
            raise EvidenceBackupError("restored database differs from backup source")
        _sync_directory(stage)
        if target.exists():
            raise FileExistsError(f"restore destination already exists: {target}")
        os.replace(stage, target)
        _sync_directory(target.parent)
        result, _ = _verify_bundle(target, report)
        return {
            **result,
            "database_path": str(target / "database.db"),
            "blob_path": str(target / "blobs"),
        }
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _identity(path: Path) -> tuple[str, str, str, int]:
    """(dataset_id, epoch, owner environment, change high water) of a database, read-only."""
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        dataset_id, epoch, owner = db.execute(
            "SELECT dataset_id,current_epoch,owner_environment_id FROM dataset_state WHERE singleton=1"
        ).fetchone()
        high_water = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM change_log WHERE dataset_id=? AND epoch=?",
            (dataset_id, epoch),
        ).fetchone()[0]
    return dataset_id, epoch, owner, high_water


def _worker_running() -> bool:
    from .runtime_health import worker_heartbeat_path
    from .timeutil import parse_utc
    try:
        value = json.loads(worker_heartbeat_path().read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - parse_utc(value["heartbeat_at"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
    return value.get("state") == "running" and age.total_seconds() <= config.WORKER_HEARTBEAT_TTL_SECONDS


def _in_use(path: Path) -> bool:
    """True while another connection has the database open (it then holds a shared lock)."""
    connection = sqlite3.connect(path, timeout=0.5, isolation_level=None)
    try:
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return True
        return False  # a damaged file is what a restore replaces
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def _copy_verified(source: Path, target: Path, digest: str | None = None) -> None:
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if digest is not None:
            hasher = hashlib.sha256()
            with temporary.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
            if hasher.hexdigest() != digest:
                raise PayloadIntegrityError("copied evidence file does not match its hash")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def promote_backup_bundle(bundle: Path | str) -> dict:
    """Make a verified bundle the live database. Web and worker must be stopped.

    The bundle's evidence files are added to the live blob store, which only ever grows. The
    current database and its WAL files move to ``<database folder>/replaced/<UTC stamp>/``, and
    when the restore could rewind history that API consumers already saw, the dataset starts a
    new epoch so their cursors re-synchronise instead of silently skipping changes.
    """
    if config.PROCESS_ROLE != "maintenance":
        raise config.RuntimeConfigurationError("db-bundle-promote requires INFOHUB_PROCESS_ROLE=maintenance")
    source = Path(bundle).expanduser().resolve(strict=True)
    verified, report = _verify_bundle(source)
    restored = _identity(source / "database.db")
    if restored[2] != config.ENVIRONMENT_ID:
        raise EvidenceBackupError(
            f"bundle belongs to environment {restored[2]!r}, not {config.ENVIRONMENT_ID!r}")
    live = Path(database.DB_PATH).expanduser().resolve()
    blob_root = Path(config.BLOB_PATH).expanduser().resolve()
    if _worker_running():
        raise EvidenceBackupError("the worker is running; stop web and worker first")
    previous = None
    if live.exists():
        if _in_use(live):
            raise EvidenceBackupError("the live database is open elsewhere; stop web and worker first")
        try:
            previous = _identity(live)
        except (sqlite3.Error, TypeError):
            previous = None  # unreadable: assume consumers saw more than the bundle holds
        if previous and previous[0] != restored[0]:
            raise EvidenceBackupError("bundle holds a different dataset than the live database")
    needed = sum(entry.stat().st_size for entry in source.rglob("*") if entry.is_file())
    free = shutil.disk_usage(live.parent if live.parent.exists() else Path.cwd()).free
    if free < needed:
        raise EvidenceBackupError(f"promotion needs {needed} bytes free, {free} available")

    added = repaired = 0
    for digest in _referenced_hashes(source / "database.db"):
        relative = Path("sha256") / digest[:2] / digest
        target = blob_root / relative
        if target.exists():
            try:
                verify_payload(relative.as_posix(), digest, blob_root)
                continue
            except PayloadIntegrityError:
                repaired += 1
        else:
            added += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_verified(source / "blobs" / relative, target, digest)
        _sync_directory(target.parent)

    live.parent.mkdir(parents=True, exist_ok=True)
    stage = live.parent / f".{live.name}.{uuid4().hex}.promote.tmp"
    replaced = None
    try:
        _copy_verified(source / "database.db", stage)
        same_bytes_as_verified(stage, report)
        if live.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            replaced = live.parent / "replaced" / stamp
            replaced.mkdir(parents=True)
            # The main file first, then its WAL files: a WAL left beside the new database would
            # be replayed into it.
            for suffix in ("", "-wal", "-shm", "-journal"):
                current = Path(f"{live}{suffix}")
                if current.exists():
                    os.replace(current, replaced / current.name)
            _sync_directory(replaced)
        os.replace(stage, live)
        _sync_directory(live.parent)
    finally:
        stage.unlink(missing_ok=True)

    continuous = previous is not None and previous[1] == restored[1] and previous[3] == restored[3]
    epoch = restored[1]
    if not continuous:
        from .publication import rotate_dataset_epoch
        epoch = rotate_dataset_epoch(
            expected_epoch=restored[1], reason=f"promote backup {source.name}").epoch
    return {
        "status": "ok", "database_path": str(live), "bundle": str(source),
        "database_sha256": verified["database_sha256"], "dataset_id": restored[0],
        "replaced": str(replaced) if replaced else None,
        "blobs_added": added, "blobs_repaired": repaired,
        "epoch": {"restored": restored[1], "current": epoch, "rotated": not continuous},
        "change_high_water": restored[3],
        "previous_change_high_water": previous[3] if previous else None,
    }
