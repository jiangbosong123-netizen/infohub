from __future__ import annotations

"""Off-site copies of the nightly bundle (D25): the owner's Mac pulls the newest one over the
private HTTPS API and reports back once it has verified its copy.

Only bundles the nightly job published (verified, immutable) are served, as a plain tar stream
with every path under ``bundle/``. The Mac re-checks every hash itself, so the stream needs no
length or checksum of its own. The receipt is a small JSON file in the runtime folder, which the
self-check reads to warn when the off-site copy is getting old.
"""

import json
import os
import re
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from . import config
from .scheduled_backup import nightly_bundles
from .timeutil import format_utc, parse_utc

RECEIPT_FILE = "offsite-receipt.json"
OFFSITE_MAX_AGE = timedelta(days=7)
CHUNK = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OffsiteError(ValueError):
    """A request names no published bundle, or a receipt does not match it."""


def _manifest(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not _SHA256.fullmatch(str(manifest.get("database_sha256"))):
        raise OffsiteError("bundle manifest is unreadable")
    return manifest


def bundle_path(name: str) -> Path:
    """A published nightly bundle by its exact name; anything else is refused."""
    for bundle in nightly_bundles():
        if bundle.name == name:
            return bundle
    raise OffsiteError("no such nightly bundle")


def latest_bundle() -> dict | None:
    bundles = nightly_bundles()
    if not bundles:
        return None
    bundle = bundles[-1]
    manifest = _manifest(bundle)
    sizes = [path.stat().st_size for path in bundle.rglob("*") if path.is_file()]
    return {
        "name": bundle.name, "created_at": manifest.get("created_at"),
        "database_sha256": manifest["database_sha256"],
        "schema_version": manifest.get("schema_version"),
        "files": len(sizes), "bytes": sum(sizes),
    }


def tar_stream(bundle: Path) -> Iterator[bytes]:
    """The bundle as an uncompressed tar, every entry under ``bundle/``, read in 1 MiB chunks."""
    entries = [bundle, *sorted(bundle.rglob("*"))]
    for path in entries:
        relative = path.relative_to(bundle).as_posix()
        info = tarfile.TarInfo("bundle" if relative == "." else f"bundle/{relative}")
        status = path.stat()
        info.mtime = int(status.st_mtime)
        if path.is_dir():
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            yield info.tobuf(format=tarfile.PAX_FORMAT)
            continue
        info.size, info.mode = status.st_size, 0o644
        yield info.tobuf(format=tarfile.PAX_FORMAT)
        sent = 0
        with path.open("rb") as reader:
            while sent < info.size:
                block = reader.read(min(CHUNK, info.size - sent))
                if not block:
                    raise OSError(f"{relative} shrank while it was being sent")
                sent += len(block)
                yield block
        if info.size % tarfile.BLOCKSIZE:
            yield b"\0" * (tarfile.BLOCKSIZE - info.size % tarfile.BLOCKSIZE)
    yield b"\0" * (2 * tarfile.BLOCKSIZE)


def _receipt_path() -> Path:
    return Path(config.RUNTIME_PATH) / RECEIPT_FILE


def record_receipt(name: str, database_sha256: str, consumer_id: str,
                   now: datetime | None = None) -> dict:
    """Remember that the Mac holds a verified copy of ``name``."""
    manifest = _manifest(bundle_path(name))
    if database_sha256 != manifest["database_sha256"]:
        raise OffsiteError("receipt does not match the bundle")
    receipt = {"name": name, "database_sha256": database_sha256, "consumer_id": consumer_id,
               "received_at": format_utc(now or datetime.now(timezone.utc))}
    target = _receipt_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with stage.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(stage, target)
    return receipt


OFFSITE_CONSUMER = "mac-offsite"
OFFSITE_KEY_DAYS = 365


def issue_offsite_key(db, *, actor: str, now: datetime | None = None) -> str:
    """One key that may only read backups, for the consumer ``mac-offsite``; older keys of that
    consumer are revoked, so only the Mac's newest copy works. Returns the token (shown once)."""
    from .api_auth import create_consumer, issue_api_key, revoke_api_key
    current = now or datetime.now(timezone.utc)
    row = db.execute("SELECT id,status FROM api_consumers WHERE name=?", (OFFSITE_CONSUMER,)).fetchone()
    if row and row[1] != "active":
        raise OffsiteError(f"API consumer {OFFSITE_CONSUMER} was revoked; issue a key with api-admin instead")
    consumer = row[0] if row else create_consumer(db, OFFSITE_CONSUMER, actor=actor, now=current)
    for (key_id,) in db.execute("SELECT key_id FROM api_keys WHERE consumer_id=? AND revoked_at IS NULL",
                                (consumer,)).fetchall():
        revoke_api_key(db, key_id, actor=actor, now=current)
    return issue_api_key(db, consumer, {"read:backups"}, actor=actor, now=current,
                         expires_at=current + timedelta(days=OFFSITE_KEY_DAYS)).token


def read_receipt() -> dict | None:
    try:
        value = json.loads(_receipt_path().read_text(encoding="utf-8"))
        parse_utc(value["received_at"])
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None
