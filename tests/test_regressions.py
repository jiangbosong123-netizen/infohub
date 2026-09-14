import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from app import database, company_match, ranking
from app.ai import daily, pipeline
from app.crawler import runner
from app.web import routes


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(database, 'DB_PATH', Path(self.temp.name) / 'test.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type,tier) VALUES('test','Test','ai','rss','info')")
        self.client = TestClient(routes.app)

    def item(self, title='original', **fields):
        raw = dict(url='https://example.com/' + title, title=title,
                   published_at='2026-09-11T16:00:00+00:00')
        raw.update(fields)
        self.assertTrue(runner.insert_item('test', raw))
        with database.get_db() as db:
            return db.execute('SELECT max(id) FROM items').fetchone()[0]

    def test_connections_close_and_rollback(self):
        db = database.get_db()
        with self.assertRaises(ValueError):
            with db:
                db.execute("UPDATE sources SET name='wrong'")
                raise ValueError()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT name FROM sources').fetchone()[0], 'Test')

    def test_day_boundaries_and_hidden_items(self):
        for title, ts in [('before','2026-09-09T15:59:59+00:00'),
                          ('start','2026-09-09T16:00:00+00:00'),
                          ('last','2026-09-10T15:59:59+00:00'),
                          ('after','2026-09-10T16:00:00+00:00')]:
            self.item(title, published_at=ts)
        hidden = self.item('hidden')
        with database.get_db() as db:
            db.execute('UPDATE items SET tmt=0 WHERE id=?', (hidden,))
        self.assertEqual({i['title'] for i in daily._collect('2026-09-10')['items']}, {'start','last'})

    def test_utc_and_official_provenance(self):
        with database.get_db() as db:
            db.execute("UPDATE sources SET tier='official'")
        self.item(published_at='2026-09-12T00:00:00+08:00', official=0)
        with database.get_db() as db:
            row = db.execute('SELECT * FROM items').fetchone()
        self.assertEqual(row['published_at'], '2026-09-11T16:00:00+00:00')
        self.assertEqual(row['official'], 1)
        self.item('naive', published_at='2026-09-12T00:00:00')
        self.assertFalse(runner.insert_item('test', dict(url='javascript:alert(1)', title='bad')))

    def test_concurrent_dedup(self):
        raw = dict(url='https://example.com/same?utm_source=x', title='same', published_at='bad')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: runner.insert_item('test', raw), range(8)))
        self.assertEqual(sum(results), 1)

    def test_search_translations_hidden_and_literal_wildcards(self):
        one = self.item('alpha')
        two = self.item('needle')
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='独特的中文标题' WHERE id=?", (one,))
            db.execute('UPDATE items SET tmt=0 WHERE id=?', (two,))
        database.init_schema()  # migration is idempotent
        self.assertEqual(len(self.client.get('/search', params={'q':'独特的'}).context['items']), 1)
        self.assertEqual(len(self.client.get('/search', params={'q':'中文'}).context['items']), 1)
        self.assertEqual(self.client.get('/search', params={'q':'needle'}).context['items'], [])
        self.assertEqual(self.client.get('/search', params={'q':'%'}).context['items'], [])
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='另一种译文' WHERE id=?", (one,))
        self.assertEqual(self.client.get('/search', params={'q':'独特的'}).context['items'], [])
        self.assertEqual(len(self.client.get('/search', params={'q':'另一种'}).context['items']), 1)

    def test_ai_untrusted_results_and_official_retention(self):
        one = self.item(official=1, channel='stock', event_type='earnings')
        result = dict(id=one, score=88, tmt=False, summary_zh=[], reason={}, title_zh='译文', event_type=[])
        with patch.object(pipeline.config, 'llm_enabled', return_value=True), patch.object(
                pipeline, '_call_llm', return_value=[None, dict(id=99999,score=1), result, result]):
            self.assertEqual(pipeline.process_pending(), 1)
        with database.get_db() as db:
            row = db.execute('SELECT * FROM items').fetchone()
            audit = db.execute('SELECT * FROM nlp_results WHERE item_id=?', (one,)).fetchone()
        self.assertEqual((row['score'], row['tmt'], row['event_type']), (88,1,'earnings'))
        self.assertEqual(audit['pipeline_version'], pipeline.CURATION_VERSION)

    def test_ai_transient_failure_stays_pending(self):
        self.item()
        with patch.object(pipeline.config, 'llm_enabled', return_value=True), patch.object(
                pipeline, '_call_llm', side_effect=[ValueError('1301'), TimeoutError()]):
            self.assertEqual(pipeline.process_pending(), 0)
        with database.get_db() as db:
            self.assertIsNone(db.execute('SELECT score FROM items').fetchone()[0])

    def test_ai_batch_reserves_oldest_backlog(self):
        ids=[self.item('pending'+str(n)) for n in range(10)]
        with patch.object(pipeline.config,'llm_enabled',return_value=True), patch.object(pipeline,'_call_llm',return_value=[]) as call:
            pipeline.process_pending(limit=4)
        sent=[row['id'] for row in call.call_args.args[0]]
        self.assertEqual(sent,[ids[0],ids[-1],ids[-2],ids[-3]])

    def test_ai_invalid_boolean_is_not_hidden(self):
        one = self.item()
        with patch.object(pipeline.config, 'llm_enabled', return_value=True), patch.object(
                pipeline, '_call_llm', return_value=[dict(id=one,score=80,tmt='false')]):
            self.assertEqual(pipeline.process_pending(), 0)

    def test_filtered_title_and_negative_score_fallback(self):
        one = self.item()
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='-', score=-1 WHERE id=?", (one,))
            row = db.execute('SELECT * FROM items').fetchone()
        self.assertEqual(routes._decorate(routes._query_items(mode='all'))[0]['title'], 'original')
        self.assertGreaterEqual(ranking.item_heat(row), 0)
        self.assertEqual(daily._collect('2026-09-12')['items'][0]['title'], 'original')

    def test_source_partial_failure_is_visible(self):
        raws = [dict(url='https://example.com/ok',title='ok',published_at='bad'),dict(_error='one company failed')]
        with patch.dict(runner.FETCHERS, {'fake':lambda _: raws}):
            new, ok, message = runner.run_source(dict(key='test',type='fake'))
        self.assertEqual((new,ok), (1,False))
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT fail_count FROM sources').fetchone()[0], 0)
            self.assertIn('部分失败',db.execute('SELECT last_error FROM sources').fetchone()[0])
        self.assertIn('one company failed',message)
        self.assertEqual(runner.retry_interval(10,3), 80)
        self.assertEqual(runner.retry_interval(10,99), 360)

    def test_source_channel_change_reclassifies_history(self):
        one = self.item(channel='ai')
        source = dict(key='test', name='Test', channel='robot', tier='info',
                      type='rss', url='https://example.com/feed', interval_minutes=30)
        with patch.object(runner, 'all_sources', return_value=[source]):
            runner.upsert_sources()
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT channel FROM items WHERE id=?', (one,)).fetchone()[0],
                             'robot')
            self.assertEqual(db.execute('SELECT count(*) FROM derived_dirty WHERE item_id=?',
                                        (one,)).fetchone()[0], 1)

    def test_markdown_sanitization(self):
        html = daily.render_markdown('# 标题\n<script>alert(1)</script>\n<img src="x" onerror="alert(2)">\n[坏链接](javascript:alert)\n[来源](https://example.com)')
        self.assertNotIn('<script',html)
        self.assertNotIn('onerror',html)
        self.assertNotIn('javascript:',html)
        self.assertIn('https://example.com',html)
        self.assertIn('<h1>',html)

    def test_channel_scoped_hotspots_and_pages(self):
        from app.stories import refresh_derived
        for channel in ('ai','stock','robot'):
            self.item(channel + ' unrelated headline',channel=channel,published_at=datetime.now(timezone.utc).isoformat())
        refresh_derived()
        self.assertEqual([x['channel'] for x in routes._top_clusters(8,'ai')], ['ai'])
        for path in ('/','/hot','/daily','/search','/health','/topics','/saved'):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_saved_page_only_returns_requested_visible_items(self):
        visible = self.item('saved visible')
        hidden = self.item('saved hidden')
        other = self.item('not requested')
        with database.get_db() as db:
            db.execute('UPDATE items SET tmt=0 WHERE id=?', (hidden,))
        response = self.client.get('/saved', params={'ids':f'{visible},{hidden},bad,-1'})
        rendered = [item['id'] for day in response.context['days'] for item in day['rows']]
        self.assertEqual(rendered, [visible])
        self.assertNotIn(other, rendered)

    def test_selected_mode_without_llm(self):
        self.item()
        with patch.object(routes, 'llm_enabled', return_value=False):
            self.assertEqual(len(routes._query_items()), 1)

    def test_company_low_score_does_not_bypass_selection(self):
        one = self.item(companies=['nvidia'])
        with database.get_db() as db:
            db.execute('UPDATE items SET score=45 WHERE id=?', (one,))
        with patch.object(routes, 'llm_enabled', return_value=True):
            self.assertEqual(routes._query_items(), [])
            self.assertEqual(len(routes._query_items(mode='all')), 1)

    def test_selected_feed_deduplicates_events_and_keeps_corroborated_news(self):
        from app.stories import refresh_derived
        now = datetime.now(timezone.utc).isoformat()
        one = self.item('OpenAI launches a major coding model', score=40, published_at=now)
        two = self.item('OpenAI launches a major coding model today', score=40,
                        url='https://second.example/second', published_at=now)
        refresh_derived()
        with patch.object(routes, 'llm_enabled', return_value=True):
            selected = routes._query_items(mode='selected')
            self.assertEqual(len(selected), 1)
            self.assertIn(selected[0]['id'], (one, two))
            self.assertEqual(len(routes._query_items(mode='all')), 2)
            topic = self.client.get('/topics/openai')
            self.assertEqual(sum(len(day['rows']) for day in topic.context['days']), 1)

        single = self.item('A routine single-source company update', score=60,
                           url='https://example.com/routine', published_at=now)
        refresh_derived()
        with patch.object(routes, 'llm_enabled', return_value=True):
            self.assertNotIn(single, {row['id'] for row in routes._query_items(mode='selected')})
        with database.get_db() as db:
            db.execute('UPDATE items SET score=70 WHERE id=?', (single,))
        with patch.object(routes, 'llm_enabled', return_value=True):
            self.assertIn(single, {row['id'] for row in routes._query_items(mode='selected')})

    def test_search_index_delete(self):
        one = self.item('searchable')
        with database.get_db() as db:
            db.execute('DELETE FROM items WHERE id=?', (one,))
            db.execute("INSERT INTO items_fts(items_fts,rank) VALUES('integrity-check',1)")
        self.assertEqual(self.client.get('/search',params={'q':'searchable'}).context['items'], [])

    def test_pagination_keeps_category(self):
        with database.get_db() as db:
            for i in range(61):
                db.execute("INSERT INTO items(source_id,url,title,channel,ai_cat,score,published_at,fetched_at) VALUES(1,?,?,'ai','model',80,?,?)",
                           (str(i),str(i),'2026-09-12T00:00:00+00:00','2026-09-12T00:00:00+00:00'))
        response = self.client.get('/', params={'channel':'ai','cat':'model'})
        self.assertTrue(response.context['has_next'])
        self.assertIn('cat=model&amp;page=2',response.text)
        self.assertTrue(self.client.get('/',params={'channel':'ai','cat':'model','page':2}).context['has_next'])
        self.assertFalse(self.client.get('/',params={'channel':'ai','cat':'model','page':3}).context['has_next'])


if __name__ == '__main__':
    unittest.main()
