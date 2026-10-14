"""Free space where InfoHub writes, including the host's own disk when the data is in a volume.

A Docker volume on Windows lives in Docker's virtual disk: inside the container it reports that
disk's size (up to about 1 TB), not what is left on C:. Compose therefore also mounts a folder of
the host at INFOHUB_HOST_DISK_PATH, and every free-space check takes the fuller of the two.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import config

HOST_DISK = "Windows 主机磁盘"


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def lowest_free(paths) -> tuple[int, str]:
    """Free bytes on the fullest filesystem holding any of ``paths`` or the host disk, and a label
    for it (Chinese, for the alert mail)."""
    found: dict[object, tuple[int, str]] = {}
    for path in paths:
        existing = _existing(Path(path))
        found[os.stat(existing).st_dev] = (shutil.disk_usage(existing).free, f"{existing} 所在磁盘")
    host = config.HOST_DISK_PATH
    if host is not None and Path(host).is_dir():
        found[HOST_DISK] = (shutil.disk_usage(host).free, HOST_DISK)
    return min(found.values())
