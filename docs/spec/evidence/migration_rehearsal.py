"""Reproduce PR #1 migration on disposable copies, never on source databases.

Usage: python migration_rehearsal.py /checkout/of/b18d081 /path/app.db [/path/other.db]
Only database.init_schema is called; no source registration, crawling or model calls.
Source databases are opened read-only and copied with SQLite backup API.
"""
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv[1]).resolve(strict=True)))
from app import database


def snapshot_tables(conn):
    # Exclude FTS virtual/shadow tables; verify their consistency separately.
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'items_fts%' ORDER BY name")]
    result = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        rows = [dict(r) for r in conn.execute('SELECT * FROM ' + quoted)]
        serialized = sorted(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows)
        digest = hashlib.sha256(('\n'.join(serialized)).encode()).hexdigest()
        result[name] = dict(count=len(rows), sha256=digest)
    return result


results = []
for filename in sys.argv[2:]:
    source_path = Path(filename).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='infohub-rehearsal-') as folder:
        target = Path(folder) / 'copy.db'
        source = sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)
        copied = sqlite3.connect(target)
        source.backup(copied)
        source.close()
        copied.row_factory = sqlite3.Row
        before = snapshot_tables(copied)
        copied.close()
        started = time.monotonic()
        with patch.object(database, 'DB_PATH', target):
            database.init_schema()
            database.init_schema()
        elapsed = time.monotonic() - started
        checked = sqlite3.connect(target)
        checked.row_factory = sqlite3.Row
        after = {k:v for k,v in snapshot_tables(checked).items() if k in before}
        integrity = [list(r) for r in checked.execute('PRAGMA integrity_check')]
        foreign_keys = [list(r) for r in checked.execute('PRAGMA foreign_key_check')]
        checked.execute("INSERT INTO items_fts(items_fts, rank) VALUES('integrity-check', 1)")
        migrations = [list(r) for r in checked.execute('SELECT version,name FROM schema_migrations')]
        checked.close()
        assert before == after, 'An original logical table changed'
        assert integrity == [['ok']] and not foreign_keys, 'Integrity check failed'
        results.append(dict(captured_at=datetime.now(timezone.utc).isoformat(),
            source_file=source_path.name, scope='Local file copy, not current Windows export',
            python=platform.python_version(), sqlite=sqlite3.sqlite_version,
            init_twice_seconds=round(elapsed,3), original_tables_identical=before==after,
            before=before, after=after, integrity=integrity, foreign_keys=foreign_keys,
            fts_integrity='passed', migrations=migrations))
print(json.dumps(results,ensure_ascii=False,indent=2))
