import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from app import database, company_match
from app.crawler.runner import insert_item
from app.provenance import publisher
from app.stories import refresh_derived
from app.web.routes import app


class StoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(database,'DB_PATH',Path(folder.name)/'test.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        database.init_schema()
        company_match.invalidate_cache()
        self.addCleanup(company_match.invalidate_cache)
        with database.get_db() as db:
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('test','Test feed','ai','rss')")
        self.client = TestClient(app)
        self.number = 0

    def item(self,title,**kwargs):
        self.number += 1
        raw=dict(title=title,url=f'https://example.com/{self.number}',
                 published_at=datetime.now(timezone.utc).isoformat(),companies=['openai'])
        raw.update(kwargs)
        self.assertTrue(insert_item('test',raw))
        with database.get_db() as db:
            return db.execute('SELECT max(id) FROM items').fetchone()[0]

    def story_id(self,item_id):
        with database.get_db() as db:
            row=db.execute('SELECT story_id FROM story_items WHERE item_id=?',(item_id,)).fetchone()
            return row[0] if row else None

    def test_persistent_id_and_incremental_queue(self):
        one=self.item('OpenAI announces a new coding platform for developers')
        self.assertEqual(refresh_derived()['processed'],1)
        sid=self.story_id(one)
        self.assertEqual(refresh_derived()['processed'],0)
        two=self.item('OpenAI announces a new coding platform for developers today')
        refresh_derived()
        self.assertEqual(self.story_id(two),sid)
        self.assertEqual(self.story_id(one),sid)
        self.assertEqual(self.client.get('/story/'+sid).status_code,200)

    def test_cross_language_translation_and_redirect(self):
        one=self.item('OpenAI releases a new coding platform for developers')
        two=self.item('OpenAI 发布全新开发者编程平台')
        refresh_derived()
        old=self.story_id(two)
        first=self.story_id(one)
        self.assertNotEqual(old,first)
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='OpenAI 发布全新开发者编程平台' WHERE id=?",(one,))
        refresh_derived()
        self.assertEqual(self.story_id(one),self.story_id(two))
        survivor=self.story_id(one)
        abandoned=old if old!=survivor else first
        response=self.client.get('/story/'+abandoned,follow_redirects=False)
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.headers['location'],'/story/'+survivor)

    def test_editorial_launch_synonyms_merge_and_matcher_version_reindexes(self):
        one = self.item('OpenAI 发布人工智能助手英文测试版')
        two = self.item('OpenAI 推出 AI 助手英文测试版')
        refresh_derived()
        self.assertEqual(self.story_id(one), self.story_id(two))
        with database.get_db() as db:
            db.execute("UPDATE story_items SET match_reason='titles-v2' WHERE item_id=?", (one,))
        self.assertGreaterEqual(refresh_derived()['processed'], 1)
        self.assertEqual(self.story_id(one), self.story_id(two))

    def test_different_versions_companies_and_periods_stay_separate(self):
        ids=[self.item('OpenAI releases GPT-4.1 coding model'),
             self.item('OpenAI releases GPT-4.2 coding model'),
             self.item('OpenAI releases GPT-4.1 coding model',companies=['anthropic']),
             self.item('OpenAI releases GPT-4.1 coding model',published_at=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat())]
        refresh_derived()
        self.assertEqual(len({self.story_id(i) for i in ids}),4)

    def test_distinct_filings_do_not_merge(self):
        one=self.item('OpenAI quarterly financial earnings report',official=1,extra={'form':'10-Q'})
        two=self.item('OpenAI quarterly financial earnings report',official=1,extra={'form':'10-Q'})
        refresh_derived()
        self.assertNotEqual(self.story_id(one),self.story_id(two))

    def test_publisher_count_not_feed_count(self):
        title='OpenAI releases a major new model for developers'
        self.item(title,url='https://wallstreetcn.com/articles/1')
        self.item(title,url='https://news.google.com/rss/articles/1',extra={'publisher':'华尔街见闻'})
        self.item(title,url='https://news.google.com/rss/articles/2',extra={'publisher':'TechCrunch'})
        self.item(title,url='https://news.google.com/rss/articles/3')
        refresh_derived()
        with database.get_db() as db:
            row=db.execute('SELECT source_count,item_count FROM stories WHERE redirect_to IS NULL').fetchone()
            self.assertEqual(tuple(row),(2,4))

    def test_multiple_topics_and_word_boundaries(self):
        one=self.item('OpenAI launches coding agent with MCP tools')
        two=self.item('A fragment of a research article',companies=[])
        refresh_derived()
        with database.get_db() as db:
            topics={r[0] for r in db.execute('SELECT topic_slug FROM item_topics WHERE item_id=?',(one,))}
            other={r[0] for r in db.execute('SELECT topic_slug FROM item_topics WHERE item_id=?',(two,))}
        self.assertTrue({'openai','coding','agent','mcp'}<=topics)
        self.assertNotIn('agent',other)

    def test_hidden_item_leaves_topics_and_story(self):
        one=self.item('OpenAI launches a new coding agent')
        refresh_derived()
        sid=self.story_id(one)
        with database.get_db() as db:
            db.execute('UPDATE items SET tmt=0 WHERE id=?',(one,))
        refresh_derived()
        self.assertIsNone(self.story_id(one))
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM item_topics').fetchone()[0],0)
        self.assertEqual(self.client.get('/story/'+sid).status_code,404)
        page=self.client.get('/topics/openai')
        self.assertEqual(page.context['topic']['total'],0)

    def test_topic_edit_replaces_membership_and_keeps_raw_summary(self):
        one=self.item('OpenAI introduces new tools',summary='Original wording')
        refresh_derived()
        with database.get_db() as db:
            db.execute("UPDATE items SET title='Anthropic introduces new tools',summary='AI edited wording' WHERE id=?",(one,))
        refresh_derived()
        with database.get_db() as db:
            topics={r[0] for r in db.execute('SELECT topic_slug FROM item_topics WHERE item_id=?',(one,))}
            self.assertEqual(db.execute('SELECT raw_summary FROM items').fetchone()[0],'Original wording')
        self.assertIn('anthropic',topics)
        self.assertNotIn('openai',topics)

    def test_topic_selected_counts_and_pagination(self):
        for n in range(22):
            self.item(f'OpenAI new research item number {n}')
        with database.get_db() as db:
            db.execute('UPDATE items SET score=70')
            db.execute('UPDATE items SET score=40 WHERE id=1')
        refresh_derived()
        with patch('app.web.routes.CURATED_FEED_ENABLED', True):
            response=self.client.get('/topics/openai')
            self.assertEqual(response.context['topic']['selected'],21)
            self.assertEqual(response.context['topic']['total'],22)
            self.assertTrue(response.context['has_next'])
            self.assertEqual(self.client.get('/topics/openai?page=2').context['has_next'],False)
        self.assertEqual(self.client.get('/topics/missing').status_code,404)
        self.assertEqual(self.client.get('/topics/openai?page=0').status_code,422)

    def test_failed_index_rolls_back_and_retries(self):
        self.item('OpenAI releases coding agent tools')
        with patch('app.stories.assign_topics',side_effect=ValueError('test failure')):
            with self.assertRaises(ValueError):
                refresh_derived()
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM stories').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM derived_dirty').fetchone()[0],1)
        self.assertEqual(refresh_derived()['processed'],1)

    def test_translated_title_cannot_erase_version_guard(self):
        one=self.item('OpenAI launches GPT-4.1 for developers')
        two=self.item('OpenAI launches GPT-4.2 for developers')
        with database.get_db() as db:
            db.execute("UPDATE items SET title_zh='OpenAI 发布面向开发者的新模型'")
        refresh_derived()
        self.assertNotEqual(self.story_id(one),self.story_id(two))

    def test_duplicate_discovery_merges_company_links(self):
        with database.get_db() as db:
            for slug in ('openai','microsoft'):
                db.execute("INSERT INTO companies(slug,name,market,aliases) VALUES(?,?,'PRIVATE','[]')",(slug,slug))
            db.execute("INSERT INTO sources(key,name,channel,type) VALUES('other','Other feed','ai','rss')")
        one=self.item('Partnership for enterprise customers',url='https://example.com/partnership')
        self.assertFalse(insert_item('other',dict(title='Partnership for enterprise customers',
            url='https://example.com/partnership?utm_source=second',companies=['microsoft'],
            published_at=datetime.now(timezone.utc).isoformat())))
        with database.get_db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM item_discoveries WHERE item_id=?',(one,)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM item_companies WHERE item_id=?',(one,)).fetchone()[0],2)

    def test_anchor_edit_rechecks_existing_members(self):
        one=self.item('OpenAI launches GPT-4.1 for developers')
        two=self.item('OpenAI launches GPT-4.1 for developers today')
        refresh_derived()
        self.assertEqual(self.story_id(one),self.story_id(two))
        with database.get_db() as db:
            db.execute("UPDATE items SET title='OpenAI launches GPT-4.2 for developers' WHERE id=?",(one,))
        refresh_derived()
        self.assertNotEqual(self.story_id(one),self.story_id(two))

    def test_unknown_aggregator_has_no_publisher_vote(self):
        self.assertFalse(publisher(dict(url='https://news.google.com/rss/articles/1'))[2])
        self.assertEqual(publisher(dict(url='https://www.wallstreetcn.com/a'))[0],
                         publisher(dict(url='https://news.google.com/a',extra='{"publisher":"华尔街见闻"}'))[0])


if __name__=='__main__':
    unittest.main()
