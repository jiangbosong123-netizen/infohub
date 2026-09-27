"""Read-only US coverage and timestamp review; no application startup or network.

Usage: python us_time_review.py /path/to/checkout /path/to/database.db
Copies the DB to memory; extracts the existing SEC/RSS timestamp functions via AST
to reproduce their behavior in this disposable process without loading .env.
"""
import ast
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

root=Path(sys.argv[1]).resolve(strict=True)
path=Path(sys.argv[2]).resolve(strict=True)
source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
db=sqlite3.connect(':memory:')
source.backup(db)
source.close()
db.row_factory=sqlite3.Row
queries={
    'companies_by_market':'SELECT market,COUNT(*) count FROM companies GROUP BY market',
    'us_company_items':'''SELECT c.slug,c.ticker,c.name_zh,COUNT(DISTINCT ic.item_id) linked_items
        FROM companies c LEFT JOIN item_companies ic ON ic.company_id=c.id
        WHERE c.market='US' GROUP BY c.id ORDER BY c.slug''',
    'us_linked_unique_items':'''SELECT COUNT(DISTINCT ic.item_id) count FROM item_companies ic
        JOIN companies c ON c.id=ic.company_id WHERE c.market='US' ''',
    'us_enabled_company_sources':'''SELECT s.key,s.company_slug,s.interval_minutes
        FROM sources s JOIN companies c ON c.slug=s.company_slug
        WHERE c.market='US' AND s.enabled=1 ORDER BY s.key''',
    'sec_forms':'''SELECT json_extract(i.extra,'$.form') form,COUNT(*) count
        FROM items i JOIN sources s ON s.id=i.source_id WHERE s.key='sec-edgar'
        GROUP BY json_extract(i.extra,'$.form') ORDER BY count DESC''',
    'stock_sources':'''SELECT s.key,s.enabled,COUNT(i.id) stored_items FROM sources s
        LEFT JOIN items i ON i.source_id=s.id WHERE s.channel='stock'
        GROUP BY s.id ORDER BY s.key''',
}
results={k:[dict(r) for r in db.execute(sql)] for k,sql in queries.items()}
db.close()

def isolated_function(relative,name,globals_):
    tree=ast.parse((root/relative).read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    module=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]))
    exec(compile(module,str(root/relative),'exec'),globals_)
    return globals_[name]

parse_sec=isolated_function('app/crawler/sec_source.py','_parse_ts',{'datetime':datetime,'timezone':timezone})
original_tz=os.environ.get('TZ')
tz_results={}
try:
    for name in ['UTC','Asia/Shanghai','America/New_York']:
        os.environ['TZ']=name
        time.tzset()
        tz_results[name]={raw:parse_sec(raw) for raw in ['2026-09-14T16:05:00','2026-09-14T16:05:00Z']}
finally:
    if original_tz is None: os.environ.pop('TZ',None)
    else: os.environ['TZ']=original_tz
    time.tzset()
parse_rss=isolated_function('app/crawler/rss_source.py','_to_iso',
    {'datetime':datetime,'timezone':timezone,'parsedate_to_datetime':parsedate_to_datetime})
rss_updated_only=parse_rss(SimpleNamespace(updated_parsed=(2026,9,14,16,5,0,0,0,0)))
calendar_examples={}
for raw in ['2026-01-15T09:30:00','2026-07-15T09:30:00']:
    calendar_examples[raw]=datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo('America/New_York')).astimezone(timezone.utc).isoformat()
print(json.dumps(dict(captured_at=datetime.now(timezone.utc).isoformat(),
    scope='Local database snapshot; company links are existing classifier outputs, not verified relevance or full US-market coverage',
    source_file=path.name,queries=queries,results=results,
    timestamp_probes=dict(sec_same_input_by_host_timezone=tz_results,
        rss_updated_only_returned_as_publication=rss_updated_only,
        us_0930_winter_and_summer_utc=calendar_examples)),ensure_ascii=False,indent=2))
