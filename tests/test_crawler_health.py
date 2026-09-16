import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from app import company_match, config, database
from app.crawler import googlenews,hkex_source,sec_source,runner
from app.web.routes import app


class CrawlerHealthTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder=Path(folder.name)
        patcher=patch.object(database,'DB_PATH',Path(folder.name)/'test.db')
        patcher.start();self.addCleanup(patcher.stop)
        blob_patcher=patch.object(config,'BLOB_PATH',Path(folder.name)/'blobs')
        blob_patcher.start();self.addCleanup(blob_patcher.stop)
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
        self.assertEqual(body['runtime']['environment_id'], config.ENVIRONMENT_ID)
        self.assertEqual(body['runtime']['process_role'], config.PROCESS_ROLE)
        self.assertFalse(body['runtime']['durable_jobs_enabled'])
        self.assertNotIn('database_path', body['runtime'])
        self.assertEqual(body['jobs']['states']['pending'], 0)
        self.assertEqual(body['jobs']['expired_running'], 0)
        self.assertEqual(body['sources']['issues'], 1)
        self.assertEqual(body['pipeline']['status'], 'degraded')
        self.assertTrue(body['dataset']['dataset_id'].startswith('dataset_'))
        self.assertTrue(body['dataset']['epoch'].startswith('epoch_'))
        self.assertEqual(body['dataset']['high_water'], 0)
        self.assertTrue(body['dataset']['owner_matches_environment'])
        self.assertIsNone(body['dataset']['latest_checkpoint'])
        self.assertIn(body['status'], {'ok','degraded'})

    def test_liveness_does_not_require_database_but_readiness_does(self):
        unavailable = self.folder / 'missing' / 'not-initialized.db'
        with patch.object(database, 'DB_PATH', unavailable):
            client = TestClient(app)
            live = client.get('/api/live')
            ready = client.get('/api/ready')
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()['status'], 'live')
        self.assertEqual(ready.status_code, 503)
        self.assertIn('database_unavailable', ready.json()['issues'])

    def test_current_worker_is_required_for_release_readiness(self):
        from datetime import datetime, timedelta, timezone
        from app import runtime_health
        from app.web import routes

        runtime_path = self.folder / 'runtime'
        now = datetime.now(timezone.utc)
        with patch.object(config, 'RUNTIME_PATH', runtime_path), patch.object(
            routes, 'DURABLE_JOBS_ENABLED', True
        ):
            runtime_health.write_worker_heartbeat(
                worker_id='worker-test', started_at=now.isoformat(), heartbeat_at=now
            )
            ready = TestClient(app).get('/api/health')
            self.assertEqual(ready.status_code, 200)
            self.assertTrue(ready.json()['readiness']['ready'])

            runtime_health.write_worker_heartbeat(
                worker_id='worker-test', started_at=now.isoformat(),
                heartbeat_at=now - timedelta(
                    seconds=config.WORKER_HEARTBEAT_TTL_SECONDS + 1
                ),
            )
            stale = TestClient(app).get('/api/health')
            portal = TestClient(app).get('/')
            self.assertEqual(stale.status_code, 503)
            self.assertIn('worker_stale', stale.json()['readiness']['issues'])
            self.assertEqual(portal.status_code, 200)

    def test_old_ready_job_marks_pipeline_backlog_stale(self):
        from datetime import datetime, timedelta, timezone
        from app.jobs import enqueue_job

        enqueue_job(
            kind='crawl',
            idempotency_key='stale-health-fixture',
            scheduled_for=datetime.now(timezone.utc) - timedelta(
                seconds=config.PIPELINE_JOB_STALE_SECONDS + 1
            ),
        )
        response = TestClient(app).get('/api/pipeline')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'degraded')
        self.assertIn('durable_job_backlog_stale', response.json()['issues'])
        self.assertEqual(response.json()['ingest']['raw_records'], 0)
        self.assertEqual(response.json()['ingest']['observations'], 0)
        self.assertEqual(response.json()['ingest']['source_time_states'], {})
