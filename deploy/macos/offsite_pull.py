"""Pull the newest nightly bundle from the Windows host and keep a verified copy on this Mac (D25).

Standard library only, so the Mac's own /usr/bin/python3 (3.9) runs it from launchd:

    python3 offsite_pull.py --origin https://windows-server.<tailnet>.ts.net \\
        --token-file ~/.infohub/offsite.token --dest ~/Documents/InfoHub/InfohubBackups

1. Asks /api/v1/backups/latest which bundle is newest. Nothing to do if this Mac already holds
   it; if the host is off or unreachable it exits with 75 and launchd simply tries next time.
2. Streams /api/v1/backups/<name>/archive straight into a hidden staging folder (no tar file on
   disk). It accepts only folders and regular files under ``bundle/``.
3. Checks the copy on its own, without trusting the host:
   - the manifest and the database SHA-256;
   - SQLite integrity_check;
   - the SHA-256 of every evidence file the manifest lists.
4. Renames the stage into place, keeps the newest ``--keep`` bundles (default 1, about 5 GB), and
   posts a receipt so the host's self-check knows the off-site copy is fresh.

Every run writes ``status.json`` in the destination. The token file must be readable by its
owner only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import ssl
import stat
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

FORMAT = "infohub-evidence-bundle-v1"  # app/evidence_backup.py FORMAT
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.bundle$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1024 * 1024
TEMPFAIL = 75  # EX_TEMPFAIL: the host is off or unreachable; try again later


class PullError(Exception):
    """The copy cannot be trusted; nothing was published."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_token(path: Path) -> str:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PullError(f"{path} must be readable by its owner only (chmod 600)")
    raw = path.read_bytes()
    # PowerShell 5.1 writes UTF-16 with a BOM when output is redirected with '>'.
    for bom, codec in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8")):
        if raw.startswith(bom):
            raw = raw[len(bom):].decode(codec).encode("ascii", "ignore")
            break
    # `docker compose run` rebuilds first (pull_policy: build), and without a terminal the build
    # log lands on stdout next to the key, so look for the one line that is a key.
    tokens = [line.strip() for line in raw.decode("ascii", "ignore").splitlines()
              if re.fullmatch(r"ih1\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}", line.strip())]
    if len(tokens) != 1:
        raise PullError(f"{path} does not hold exactly one InfoHub API token")
    return tokens[0]


def _request(url: str, token: str, *, data: bytes | None = None, timeout: float = 30.0):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "infohub-offsite-pull/1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    return urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context())


def _safe_member(member: tarfile.TarInfo) -> PurePosixPath:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "bundle":
        raise PullError(f"unexpected path in archive: {member.name!r}")
    if not (member.isdir() or member.isfile()):
        raise PullError(f"unexpected entry type in archive: {member.name!r}")
    return PurePosixPath(*path.parts[1:]) if len(path.parts) > 1 else PurePosixPath(".")


def extract_stream(stream, stage: Path) -> int:
    """Write every member under ``stage``; returns the number of files."""
    files = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            relative = _safe_member(member)
            target = stage / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            with target.open("xb") as writer:
                shutil.copyfileobj(source, writer, CHUNK)
                writer.flush()
                os.fsync(writer.fileno())
            if target.stat().st_size != member.size:
                raise PullError(f"{relative} is truncated")
            files += 1
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for block in iter(lambda: reader.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_bundle(root: Path, expected_sha256: str) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise PullError("unsupported bundle format")
    database = root / "database.db"
    actual = _sha256(database)
    if actual != manifest.get("database_sha256") or actual != expected_sha256:
        raise PullError("database does not match its manifest")
    db = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        db.close()
    if integrity != ["ok"]:
        raise PullError("integrity_check failed: " + "; ".join(integrity[:3]))
    blobs = manifest.get("blob_sha256")
    if not isinstance(blobs, list):
        raise PullError("manifest has no blob list")
    for digest in blobs:
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise PullError("manifest lists an invalid blob hash")
        blob = root / "blobs" / "sha256" / digest[:2] / digest
        if not blob.is_file() or _sha256(blob) != digest:
            raise PullError(f"evidence file missing or corrupt: {digest}")
    return {"database_sha256": actual, "schema_version": manifest.get("schema_version"),
            "items": items, "blobs": len(blobs)}


def _bundles(dest: Path) -> list:
    return sorted(entry for entry in dest.iterdir() if entry.is_dir() and NAME.fullmatch(entry.name))


def pull(origin: str, token: str, dest: Path, keep: int = 1, timeout: float = 30.0) -> dict:
    origin = origin.rstrip("/")
    if not origin.startswith("https://"):
        raise PullError("the origin must be the host's private https:// address")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with _request(f"{origin}/api/v1/backups/latest", token, timeout=timeout) as response:
            latest = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PullError(f"the host refused: HTTP {exc.code}") from exc
    name, expected = latest.get("name"), latest.get("database_sha256")
    if not isinstance(name, str) or not NAME.fullmatch(name) or not SHA256.fullmatch(str(expected)):
        raise PullError("the host returned an unexpected answer")
    if (dest / name).is_dir():
        return {"result": "up_to_date", "name": name}
    needed = int(latest.get("bytes") or 0) + 1024 * CHUNK
    free = shutil.disk_usage(dest).free
    if free < needed:
        raise PullError(f"needs {needed // CHUNK} MB free in {dest}, {free // CHUNK} MB available")
    stage = dest / f".incoming-{name}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    try:
        with _request(f"{origin}/api/v1/backups/{name}/archive", token, timeout=max(timeout, 120)) as response:
            files = extract_stream(response, stage)
        checked = verify_bundle(stage, expected)
        os.replace(stage, dest / name)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    removed = []
    for old in _bundles(dest)[:-keep]:
        shutil.rmtree(old)
        removed.append(old.name)
    body = json.dumps({"database_sha256": expected}).encode("ascii")
    with _request(f"{origin}/api/v1/backups/{name}/receipt", token, data=body, timeout=timeout) as response:
        response.read()
    return {"result": "pulled", "name": name, "files": files, "removed": removed, **checked}


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="Keep a verified copy of the newest InfoHub bundle.")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--keep", type=int, default=1)
    args = parser.parse_args(argv)
    dest = args.dest.expanduser()
    status = {"checked_at": _now()}
    code = 0
    try:
        if args.keep < 1:
            raise PullError("--keep must be at least 1")
        status.update(pull(args.origin, read_token(args.token_file.expanduser()), dest, args.keep))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        status.update(result="unreachable", detail=str(getattr(exc, "reason", exc)))
        code = TEMPFAIL
    except (PullError, OSError, ValueError, sqlite3.Error, tarfile.TarError) as exc:
        status.update(result="failed", detail=f"{type(exc).__name__}: {exc}")
        code = 1
    print(json.dumps(status, ensure_ascii=False))
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
