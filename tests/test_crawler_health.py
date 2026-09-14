import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from app import database,company_match
from app.crawler import googlenews,hkex_source,sec_source,runner
from app.web.routes import app


class CrawlerHealthTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher=patch.object(database,'DB_PATH',Path(folder.name)/'test.db')
        patcher.start();self.addCleanup(patcher.stop)
        database.init_schema()
        company_match.invalidate_cache();self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('google-news','Google News','stock','googlenews')")

    def test_invalid_google_response_fails(self):
        with patch.object(googlenews.http,'fetch',return_value=SimpleNamespace(content=b'<html>too many requests</html>')):
            with self.assertRaises(RuntimeError):
                googlenews.fetch_company_news('test','Test',['Test'])

    def test_valid_empty_feed_is_not_failure(self):
        rss=b'<?xml version="1.0"?><rss version="2.0"><channel><title>News</title><link>https://example.com</link><description>News</description></channel></rss>'
        with patch.object(googlenews.http,'fetch',return_value=SimpleNamespace(content=rss)):
            self.assertEqual(googlenews.fetch_company_news('test','Test',['Test']),[])

    def test_hkex_missing_stock_id_is_visible(self):
        with patch.object(hkex_source,'resolve_stock_id',return_value=None):
            with self.assertRaises(RuntimeError):
                hkex_source._fetch_company(dict(hkex_stock_id='',code='0000',slug='test'),'20260901','20260902')

    def test_hkex_invalid_response_is_not_empty_success(self):
        with patch.object(hkex_source.http,'fetch',return_value=SimpleNamespace(text='{}')):
            with self.assertRaises(RuntimeError):
                hkex_source._fetch_company(dict(hkex_stock_id='1',slug='test'),'20260901','20260902')

    def test_sec_missing_cik_is_reported(self):
        with database.get_db() as db:
            db.execute("INSERT INTO companies(slug,name,market,ticker) VALUES('test','Test','US','TEST')")
        with patch.object(sec_source,'resolve_missing_ciks'):
            result=sec_source.fetch_sec({})
        self.assertIn('_error',result[0])

    def test_reconcile_records_partial_status_without_backoff(self):
        with database.get_db() as db:
            for slug in ['first','second']:
                db.execute("INSERT INTO companies(slug,name,market,aliases) VALUES(?,?,'US','[]')",(slug,slug))
        with patch.object(googlenews,'fetch_company_news',side_effect=[[],RuntimeError('offline')]):
            result=googlenews.run_reconcile()
        self.assertEqual(result['first']['inserted'],0)
        with database.get_db() as db:
            source=db.execute('SELECT * FROM sources').fetchone()
            self.assertEqual(source['fail_count'],0)
            self.assertIn('部分失败',source['last_error'])
            self.assertEqual(db.execute('SELECT count(*) FROM fetch_log').fetchone()[0],1)
        response=TestClient(app).get('/health')
        self.assertEqual(response.context['sources'][0]['status'],'partial')

    def test_full_failure_backs_off_and_success_clears(self):
        runner._record('google-news',False,0,'offline')
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT fail_count FROM sources').fetchone()[0],1)
        runner._record('google-news',True,0,'')
        with database.get_db() as db:
            source=db.execute('SELECT * FROM sources').fetchone()
            self.assertEqual(source['fail_count'],0)
            self.assertIsNone(source['last_error'])

    def test_machine_health_reports_pipeline_backlog(self):
        with database.get_db() as db:
            db.execute("""INSERT INTO items(source_id,url,title,channel,published_at,fetched_at)
                VALUES(1,'https://example.com/pending','Pending','ai',?,?)""",
                ('2026-09-14T10:00:00+00:00','2026-09-14T10:01:00+00:00'))
        response = TestClient(app).get('/api/health')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['items']['pending_score'], 1)
        self.assertEqual(body['items']['pending_tmt'], 1)
        self.assertEqual(body['items']['derived_pending'], 1)
        self.assertEqual(body['schema_version'], 1)
        self.assertEqual(body['nlp']['stored_results'], 0)
        self.assertIn(body['status'], {'ok','degraded'})
