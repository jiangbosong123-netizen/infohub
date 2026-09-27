"""Read-only audit: copies a specified SQLite database into memory, prints aggregates.

Usage: python profile.py /absolute/path/to/database.db
Does not import the application, load .env, call external services, or change source data.
Counts describe one snapshot, not a population-level NLP quality evaluation.
"""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1]).resolve(strict=True)
source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
db = sqlite3.connect(':memory:')
source.backup(db)
source.close()
db.row_factory = sqlite3.Row

queries = {
    'items': '''SELECT COUNT(*) total, SUM(raw_summary IS NULL) raw_missing,
        SUM(raw_summary='') raw_empty, SUM(length(raw_summary)>0) raw_nonempty,
        SUM(score IS NULL) pending_score, SUM(score=-1) skipped_score,
        SUM(tmt=0) hidden, SUM(tmt IS NULL) pending_tmt,
        SUM(title_zh='-') failed_title, MIN(fetched_at) first_fetched_at,
        MAX(fetched_at) last_fetched_at, MIN(published_at) first_published_at,
        MAX(published_at) last_published_at FROM items''',
    'channels': '''SELECT channel,COUNT(*) total,SUM(tmt=0) hidden,
        SUM(raw_summary IS NULL) raw_missing,SUM(raw_summary='') raw_empty,
        SUM(score IS NULL) pending_score FROM items GROUP BY channel''',
    'stories': '''SELECT COUNT(*) total,SUM(redirect_to IS NOT NULL) redirects,
        SUM(redirect_to IS NULL AND item_count>0) active,
        SUM(redirect_to IS NULL AND item_count=1) singletons,
        SUM(redirect_to IS NULL AND source_count>=2) multipublisher FROM stories''',
    'counts': '''SELECT (SELECT COUNT(*) FROM sources WHERE enabled=1) enabled_sources,
        (SELECT COUNT(*) FROM companies) companies,
        (SELECT COUNT(*) FROM topics WHERE enabled=1) topics,
        (SELECT COUNT(*) FROM derived_dirty) dirty,
        (SELECT COUNT(*) FROM daily_reports) reports,
        (SELECT COUNT(*) FROM item_discoveries) discoveries''',
    'integrity': 'PRAGMA integrity_check',
    'foreign_keys': 'PRAGMA foreign_key_check',
    'visibility_membership': '''SELECT
        SUM(tmt=0 AND EXISTS(SELECT 1 FROM story_items s WHERE s.item_id=i.id)) hidden_in_story,
        SUM(COALESCE(tmt,1)!=0 AND NOT EXISTS(SELECT 1 FROM story_items s WHERE s.item_id=i.id)) visible_unindexed,
        SUM(NOT EXISTS(SELECT 1 FROM item_discoveries d WHERE d.item_id=i.id)) no_discovery
        FROM items i''',
    'raw_by_fetch_day': '''SELECT substr(fetched_at,1,10) day,COUNT(*) total,
        SUM(raw_summary IS NULL) raw_missing,SUM(raw_summary='') raw_empty
        FROM items GROUP BY day''',
    'hidden_macro_keyword_candidates': '''SELECT COUNT(*) candidates FROM items WHERE tmt=0
        AND (title LIKE '%美联储%' OR title LIKE '%通胀%' OR title LIKE '%利率%'
             OR title LIKE '%非农%' OR title LIKE '%CPI%')''',
    'per_source': '''SELECT s.key,COUNT(i.id) total,SUM(i.raw_summary IS NULL) raw_missing,
        SUM(i.raw_summary='') raw_empty FROM sources s JOIN items i ON i.source_id=s.id
        GROUP BY s.id ORDER BY total DESC''',
}
results = {key: [dict(row) for row in db.execute(sql)] for key, sql in queries.items()}
company_ids = {row['id']: row['slug'] for row in db.execute('SELECT id,slug FROM companies')}
links = {}
for row in db.execute('SELECT item_id,company_id FROM item_companies'):
    links.setdefault(row['item_id'], set()).add(company_ids.get(row['company_id']))
mismatches = 0
invalid_json = 0
invalid_times = 0
naive_times = 0
future_at_capture = 0
now = datetime.now(timezone.utc)
rows_digest = hashlib.sha256()
for row in db.execute('SELECT * FROM items ORDER BY id'):
    rows_digest.update(json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode())
    rows_digest.update(b'\n')
    try:
        companies = json.loads(row['companies'])
        if set(companies) != links.get(row['id'], set()):
            mismatches += 1
        json.loads(row['extra'])
    except (ValueError, TypeError):
        invalid_json += 1
    for key in ('published_at', 'fetched_at'):
        try:
            dt = datetime.fromisoformat(row[key].replace('Z', '+00:00'))
            if dt.tzinfo is None:
                naive_times += 1
            elif dt > now:
                future_at_capture += 1
        except (ValueError, TypeError):
            invalid_times += 1
results['consistency'] = dict(company_link_mismatches=mismatches, invalid_json_rows=invalid_json,
    invalid_time_fields=invalid_times, naive_time_fields=naive_times,
    future_time_fields_at_capture=future_at_capture)
schema = [dict(row) for row in db.execute(
    "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
print(json.dumps(dict(captured_at=now.isoformat(), source_file=path.name,
    scope='Local database snapshot; NOT a fresh Windows production database export',
    sqlite_version=sqlite3.sqlite_version, item_rows_sha256=rows_digest.hexdigest(),
    results=results, queries=queries, schema=schema), ensure_ascii=False, indent=2))
db.close()
