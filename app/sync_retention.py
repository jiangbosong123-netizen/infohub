from __future__ import annotations

"""Conservative cleanup of expired snapshot files; immutable ledgers remain intact."""

import hashlib
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .database import get_db
from .timeutil import format_utc


@dataclass(frozen=True)
class SyncRetentionReport:
    cutoff: str
    dry_run: bool
    candidates: int
    eligible: int
    deleted: int
    missing: int
    refused: int
    retained: int

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cleanup_expired_snapshot_files(
    *, now: datetime | None = None, dry_run: bool = True,
) -> SyncRetentionReport:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("retention time must include a timezone")
    cutoff = format_utc(current)
    with get_db() as db:
        rows = db.execute(
            """SELECT id,manifest_sha256 FROM sync_snapshots
               WHERE expires_at<=? ORDER BY expires_at,id""",
            (cutoff,),
        ).fetchall()
        retained = db.execute(
            "SELECT COUNT(*) FROM sync_snapshots WHERE expires_at>?", (cutoff,)
        ).fetchone()[0]
    storage = (Path(config.RUNTIME_PATH) / "sync-snapshots").resolve()
    eligible = deleted = missing = refused = 0
    for row in rows:
        digest = hashlib.sha256(row["id"].encode("utf-8")).hexdigest()
        root = Path(config.RUNTIME_PATH) / "sync-snapshots" / f"snapshot-{digest}"
        if not root.exists() and not root.is_symlink():
            missing += 1
            continue
        if root.is_symlink() or root.resolve().parent != storage:
            refused += 1
            continue
        manifest = root / "manifest.json"
        try:
            valid = (
                not manifest.is_symlink()
                and manifest.is_file()
                and manifest.resolve().parent == root.resolve()
                and _sha256(manifest) == row["manifest_sha256"]
            )
        except OSError:
            valid = False
        if not valid:
            refused += 1
            continue
        eligible += 1
        if not dry_run:
            try:
                shutil.rmtree(root)
            except OSError:
                refused += 1
                continue
            deleted += 1
    return SyncRetentionReport(
        cutoff=cutoff, dry_run=dry_run, candidates=len(rows), eligible=eligible, deleted=deleted,
        missing=missing, refused=refused, retained=retained,
    )
