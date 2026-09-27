"""Disposable reproductions against PR #1; no network or production writes.

Run with InfoHub's environment:
  python probes.py /path/to/checkout-of-b18d081
Prints observations, not a replacement for the project's regression tests.
"""
import base64
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from app import database, company_match
from app.ai.audit import save_result
from app.crawler import runner, fastnews, rss_source
from app.stories import refresh_derived
from app.web.routes import app, _topic_stats
from fastapi.testclient import TestClient

observations = {}
with tempfile.TemporaryDirectory() as folder:
    with patch.object(database, 'DB_PATH', Path(folder) / 'probe.db'):
        database.init_schema()
        company_match.invalidate_cache()
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','media')")
        now = datetime.now(timezone.utc).isoformat()
        runner.insert_item('test', dict(url='https://example.com/one',
            title='OpenAI launches a new model for coding', summary='Original report', published_at=now))
        with database.get_db() as db:
            item_id = db.execute('SELECT id FROM items').fetchone()[0]
            for model, score in [('model-a', 10), ('model-b', 90)]:
                save_result(db, item_id=item_id, analysis_type='curation', pipeline_version='curation-v1',
                    model=model, input_data={'title':'input'}, output_data={'score':score})
            observations['reanalysis_same_version'] = [dict(row) for row in db.execute(
                'SELECT model,output_json FROM nlp_results')]
        runner.insert_item('test', dict(url='https://example.com/one',
            title='Corrected headline', summary='Corrected report', published_at=now))
        with database.get_db() as db:
            observations['same_url_correction'] = dict(db.execute(
                'SELECT title,raw_summary FROM items WHERE id=?', (item_id,)).fetchone())
        client = TestClient(app, raise_server_exceptions=False)
        observations['no_success_source_health'] = client.get('/api/health').json()['status']
        # The API drops a newly ingested old article when consumers use publication time as a watermark.
        runner.insert_item('test', dict(url='https://example.com/late',
            title='Late-discovered historical report', published_at='2020-01-01T00:00:00+00:00'))
        response = client.get('/api/v1/items', params={'since': '2026-01-01T00:00:00+00:00'})
        observations['since_misses_late_arrival'] = 'https://example.com/late' not in [r['url'] for r in response.json()['data']]
        cursor = base64.urlsafe_b64encode(json.dumps([None,1]).encode()).decode().rstrip('=')
        observations['malformed_cursor_status'] = client.get('/api/v1/items',params={'cursor':cursor}).status_code
        observations['openapi_items_response_schema'] = client.get('/openapi.json').json()['paths']['/api/v1/items']['get']['responses']['200']['content']['application/json']['schema']
        runner.insert_item('test', dict(url='https://second.example/two',
            title='OpenAI launches a new model for coding', published_at=now))
        with database.get_db() as db:
            db.execute('UPDATE items SET score=80,tmt=1')
        refresh_derived()
        with patch('app.web.routes.llm_enabled', return_value=True):
            response = client.get('/topics/openai')
            observations['topic_count_grain'] = dict(
                selected_count=response.context['topic']['selected'] if hasattr(response,'context') else None)
            # A second client exposes the template context normally.
            response = TestClient(app).get('/topics/openai')
            observations['topic_count_grain'] = dict(selected_count=response.context['topic']['selected'],
                rendered_events=sum(len(day['rows']) for day in response.context['days']))
        definition = dict(key='test',name='Test',channel='ai',type='rss',url='https://example.com/feed',interval_minutes=30)
        with database.get_db() as db:
            db.execute("UPDATE sources SET enabled=0 WHERE key='test'")
        with patch.object(runner, 'all_sources', return_value=[definition]):
            runner.upsert_sources()
        with database.get_db() as db:
            observations['reintroduced_source_enabled'] = db.execute("SELECT enabled FROM sources WHERE key='test'").fetchone()[0]
        company_match.invalidate_cache()

with patch.object(fastnews.http, 'fetch', return_value=SimpleNamespace(json=lambda: {})):
    observations['malformed_cls_response'] = fastnews.fetch_cls({})
    observations['malformed_wscn_response'] = fastnews.fetch_wscn_live({})
with patch.object(rss_source.http, 'fetch', return_value=SimpleNamespace(content=b'<html>Access denied</html>')):
    try:
        observations['malformed_rss_response'] = rss_source.fetch_rss({'url':'https://example.com/feed'})
    except Exception as exc:
        observations['malformed_rss_response'] = type(exc).__name__

db = sqlite3.connect(':memory:')
db.row_factory = sqlite3.Row
with patch.object(database, 'MIGRATIONS', ((1,'failure probe',(
        'CREATE TABLE migration_probe(id INTEGER)', 'SELECT * FROM absent_table')),)):
    try:
        with db:
            database._apply_migrations(db)
    except sqlite3.OperationalError:
        pass
observations['failed_migration_leaves_ddl'] = bool(db.execute(
    "SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchone())
observations['failed_migration_markers'] = db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
db.close()
print(json.dumps(observations, ensure_ascii=False, indent=2))
