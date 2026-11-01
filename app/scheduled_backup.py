from __future__ import annotations

"""Nightly evidence bundles with a free-space guard and count-based retention."""

import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config, database
from .db_admin import PRE_MIGRATION_DIRECTORY
from .disk_space import lowest_free
from .evidence_backup import create_backup_bundle


NIGHTLY_DIRECTORY = "nightly"
# Bundles this job publishes; manual bundles and pre-migration copies live elsewhere.
_BUNDLE = re.compile(r"^[^.][^/]*\.(\d{8}T\d{6}\.\d{6}Z)\.bundle$")
_PRE_MIGRATION = re.compile(r"^[^.][^/]*\.(\d{8}T\d{6}\.\d{6}Z)\.db$")
# Stage directories create_backup_bundle leaves behind only when the process is killed.
_STAGE = re.compile(r"^\.[^/]+\.bundle\.[0-9a-f]{32}\.tmp$")
STALE_STAGE_SECONDS = 6 * 3600
MB = 1024 * 1024


class BackupSpaceError(RuntimeError):
    """The backup disk cannot hold another bundle plus the configured reserve."""


def nightly_root() -> Path:
    return config.BACKUP_PATH / NIGHTLY_DIRECTORY


def _tree_bytes(path: Path, shared: Path | None = None) -> int:
    """Bytes of the files under ``path``, leaving out those also under ``shared`` at the same place."""
    total = 0
    for directory, _, files in os.walk(path):
        relative = os.path.relpath(directory, path)
        for name in files:
            if shared is not None and (shared / relative / name).is_file():
                continue
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except FileNotFoundError:
                pass
    return total


def _links_work(bundle: Path, root: Path) -> bool:
    """Whether the backup folder can hard-link files of ``bundle`` (a Windows share may not)."""
    probe = root / f".link-probe.{uuid4().hex}.tmp"
    try:
        os.link(bundle / "manifest.json", probe)
    except OSError:
        return False
    probe.unlink()
    return True


def _database_bytes(path: Path) -> int:
    return sum(candidate.stat().st_size for candidate in (path, Path(f"{path}-wal"))
               if candidate.exists())


def _stamped(folder: Path, pattern: re.Pattern, directories: bool) -> list[Path]:
    """Entries named ``<stem>.<environment>.<UTC stamp>.<suffix>``, oldest first."""
    if not folder.is_dir():
        return []
    found = [(match.group(1), entry) for entry in folder.iterdir()
             if entry.is_dir() == directories and (match := pattern.match(entry.name))]
    return [entry for _, entry in sorted(found)]


def nightly_bundles(root: Path | None = None) -> list[Path]:
    return _stamped(root or nightly_root(), _BUNDLE, directories=True)


def pre_migration_copies() -> list[Path]:
    return _stamped(config.BACKUP_PATH / PRE_MIGRATION_DIRECTORY, _PRE_MIGRATION, directories=False)


def _remove_stale_stages(root: Path, now: float) -> list[str]:
    removed = []
    for entry in root.iterdir():
        if (entry.is_dir() and _STAGE.match(entry.name)
                and now - entry.stat().st_mtime > STALE_STAGE_SECONDS):
            shutil.rmtree(entry)
            removed.append(entry.name)
    return removed


def run_nightly_backup() -> dict:
    """Publish one verified bundle, then keep only the newest ``BACKUP_KEEP`` nightly bundles
    and the newest ``BACKUP_KEEP_PRE_MIGRATION`` pre-migration copies.

    Nothing is deleted before the new bundle has verified, so a failed night keeps every
    earlier copy; manual backups elsewhere in the backup folder are never touched. Evidence files
    the newest bundle already holds are hard-linked from it, so a night writes the database copy
    and only the files collected since. Free space must cover what will be written plus the
    reserve before anything is written.
    """
    source = Path(database.DB_PATH).expanduser().resolve(strict=True)
    root = nightly_root()
    root.mkdir(parents=True, exist_ok=True)
    stale = _remove_stale_stages(root, time.time())
    bundles = nightly_bundles(root)
    previous = bundles[-1] if bundles and _links_work(bundles[-1], root) else None
    shared = previous / "blobs" if previous is not None else None
    needed = (_database_bytes(source) + _tree_bytes(Path(config.BLOB_PATH), shared)
              + config.BACKUP_RESERVE_MB * MB)
    free, _ = lowest_free((root,))
    if free < needed:
        raise BackupSpaceError(
            f"backup needs {needed // MB} MB free including the {config.BACKUP_RESERVE_MB} MB "
            f"reserve, but {free // MB} MB is available"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    bundle = create_backup_bundle(root / f"{source.stem}.{config.ENVIRONMENT_ID}.{stamp}.bundle",
                                  link_from=previous)
    published = Path(bundle["path"])
    older = [entry for entry in nightly_bundles(root) if entry.name != published.name]
    removed = older[:max(0, len(older) - (config.BACKUP_KEEP - 1))]
    for entry in removed:
        shutil.rmtree(entry)
    copies = pre_migration_copies()
    expired = copies[:max(0, len(copies) - config.BACKUP_KEEP_PRE_MIGRATION)]
    for entry in expired:
        # Opening a copy read-only can leave SQLite sidecar files next to it.
        for sidecar in ("-wal", "-shm", "-journal"):
            Path(f"{entry}{sidecar}").unlink(missing_ok=True)
        entry.unlink()
    return {
        "path": str(published), "database_sha256": bundle["database_sha256"],
        "schema_version": bundle["schema_version"], "unique_blobs": bundle["unique_blobs"],
        "blobs_linked": bundle["blobs_linked"],
        "bundle_mb": round(_tree_bytes(published) / MB, 1),
        "kept": len(older) - len(removed) + 1, "removed": [entry.name for entry in removed],
        "pre_migration_removed": [entry.name for entry in expired],
        "stale_stages_removed": stale,
        "free_mb_after": lowest_free((root,))[0] // MB,
    }
