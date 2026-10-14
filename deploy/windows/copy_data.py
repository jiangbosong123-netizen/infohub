"""Copy InfoHub's data folder into an empty Docker named volume and prove the copy (stdlib only).

Run once by move-to-volume.ps1 in a python:3.12-slim container, with every InfoHub container
stopped, the old folder mounted read-only at /from and the new, empty volume at /to:

    python /copy_data.py /from /to

Every file is streamed, fsynced and hashed on the way, then read back from /to and hashed again.
The run fails unless both trees hold exactly the same directories and files with the same
SHA-256, no source file changed meanwhile, the database had no pending WAL (it was closed
cleanly) and integrity_check on the copy says ok. Prints one JSON report; exits 0 only when the
copy is verified. It never writes to /from and makes no network calls.
"""

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

CHUNK = 1024 * 1024
DATABASE = "app.db"


class CopyError(Exception):
    """The copy cannot be trusted; the message says why."""


def _tree(root: Path) -> tuple[list[str], dict[str, Path]]:
    """Relative directory names and regular files under ``root``; anything else is refused."""
    directories, files = [], {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise CopyError(f"symbolic link not supported: {relative}")
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files[relative] = path
        else:
            raise CopyError(f"not a regular file: {relative}")
    return directories, files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as reader:
        for block in iter(lambda: reader.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_size, status.st_mtime_ns


def copy_tree(source: Path, target: Path) -> dict:
    if not (source / DATABASE).is_file():
        raise CopyError(f"{DATABASE} not found in the source")
    if any(target.iterdir()):
        raise CopyError("the target is not empty")
    wal = source / (DATABASE + "-wal")
    if wal.is_file() and wal.stat().st_size:
        raise CopyError(f"{wal.name} is not empty: the database was not closed cleanly")
    directories, files = _tree(source)
    before = {relative: _stamp(path) for relative, path in files.items()}
    for relative in directories:
        (target / relative).mkdir(parents=True, exist_ok=True)
    written = {}
    for relative, path in files.items():
        destination = target / relative
        digest = hashlib.sha256()
        with open(path, "rb") as reader, open(destination, "xb") as writer:
            for block in iter(lambda: reader.read(CHUNK), b""):
                digest.update(block)
                writer.write(block)
            writer.flush()
            os.fsync(writer.fileno())
        os.utime(destination, ns=(path.stat().st_atime_ns, before[relative][1]))
        written[relative] = digest.hexdigest()
    return {"directories": directories, "files": written, "before": before}


def verify_copy(source: Path, target: Path, copied: dict) -> list[str]:
    problems = []
    source_directories, source_files = _tree(source)
    target_directories, target_files = _tree(target)
    for name, mine, theirs in (("directory", source_directories, target_directories),
                               ("file", list(source_files), list(target_files))):
        problems += [f"{name} missing from the copy: {path}" for path in sorted(set(mine) - set(theirs))]
        problems += [f"{name} only in the copy: {path}" for path in sorted(set(theirs) - set(mine))]
    for relative in sorted(set(source_files) & set(target_files)):
        if relative not in copied["files"]:
            problems.append(f"file appeared in the source during the copy: {relative}")
        elif _stamp(source_files[relative]) != copied["before"][relative]:
            problems.append(f"source file changed during the copy: {relative}")
        elif _sha256(target_files[relative]) != copied["files"][relative]:
            problems.append(f"copy differs from the source: {relative}")
    return problems


def database_summary(path: Path) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        return {
            "integrity": "ok" if integrity == ["ok"] else "; ".join(integrity[:5]),
            # 0 for a legacy database that has not been upgraded yet.
            "schema_version": db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone() else 0,
            "items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        }
    finally:
        db.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: copy_data.py SOURCE TARGET", file=sys.stderr)
        return 2
    source, target = Path(argv[0]), Path(argv[1])
    report: dict = {"source": str(source), "target": str(target)}
    try:
        copied = copy_tree(source, target)
        report["files"] = len(copied["files"])
        report["bytes"] = sum(size for size, _ in copied["before"].values())
        report["database_sha256"] = copied["files"][DATABASE]
        problems = verify_copy(source, target, copied)
        report["database"] = database_summary(target / DATABASE)
        if report["database"]["integrity"] != "ok":
            problems.append("integrity_check failed on the copy")
    except (CopyError, OSError, sqlite3.Error) as exc:
        problems = [f"{type(exc).__name__}: {exc}"]
    report["problems"] = problems
    report["verdict"] = "copy verified" if not problems else "COPY FAILED"
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
