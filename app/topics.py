from __future__ import annotations

"""Versioned, many-to-many topic assignment with inspectable matching evidence."""
import json
import re

import yaml

from .config import BASE_DIR

GROUPS = [('company', '公司与模型', '跟踪关注的公司、模型与产品'),
          ('technology', '技术方向', '沿着技术领域发现相关进展'),
          ('format', '内容形态', '按研究、产品、行业和经营信息浏览')]


def sync_topics(db):
    configured = yaml.safe_load((BASE_DIR / 'config/topics.yaml').read_text())['topics']
    definitions = {t['slug']: dict(t) for t in configured}
    for company in db.execute('SELECT slug,name,name_zh,aliases FROM companies ORDER BY id'):
        name = company['name_zh'] or company['name']
        topic = definitions.setdefault(company['slug'], dict(
            slug=company['slug'], name=name, group='company',
            description=f'持续跟踪{name}的产品、业务与官方披露。', keywords=[]))
        topic['keywords'] = list(dict.fromkeys(topic['keywords'] + json.loads(company['aliases'])))
    changed = False
    enabled = [r[0] for r in db.execute('SELECT slug FROM topics WHERE enabled=1')]
    for slug in set(enabled) - set(definitions):
        db.execute('UPDATE topics SET enabled=0 WHERE slug=?', (slug,))
        changed = True
    for index, topic in enumerate(definitions.values()):
        rules = json.dumps({key:topic.get(key, []) for key in ('keywords','categories','events')}, ensure_ascii=False, sort_keys=True)
        old = db.execute('SELECT rules,enabled FROM topics WHERE slug=?', (topic['slug'],)).fetchone()
        changed |= not old or old['rules'] != rules or not old['enabled']
        db.execute('''INSERT INTO topics(slug,name,group_key,description,rules,position)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET
                      name=excluded.name,group_key=excluded.group_key,description=excluded.description,
                      rules=excluded.rules,position=excluded.position,enabled=1''',
                   (topic['slug'],topic['name'],topic['group'],topic['description'],rules,index))
    if changed:
        db.execute('INSERT OR IGNORE INTO derived_dirty(item_id) SELECT id FROM items')
    result = []
    for row in db.execute('SELECT * FROM topics WHERE enabled=1 ORDER BY position'):
        rules = json.loads(row['rules'])
        patterns = []
        for word in rules['keywords']:
            pattern = re.escape(word)
            if word.isascii():
                pattern = r'(?<![A-Za-z0-9])' + pattern + r'(?![A-Za-z0-9])'
            patterns.append((word, re.compile(pattern, re.IGNORECASE)))
        result.append((row['slug'],rules,patterns))
    return result


def assign_topics(db, row, definitions):
    db.execute('DELETE FROM item_topics WHERE item_id=?', (row['id'],))
    if row['tmt'] == 0:
        return
    # Prefer original evidence. Do not classify using an AI recommendation.
    text = row['title'] + '\n' + (row.get('raw_summary') or '')
    for slug, rules, patterns in definitions:
        evidence = [word for word, pattern in patterns if pattern.search(text)]
        if row.get('ai_cat') and row['ai_cat'] in rules['categories']:
            evidence.append('分类:' + row['ai_cat'])
        if row.get('event_type') and row['event_type'] in rules['events']:
            evidence.append('事件:' + row['event_type'])
        if evidence:
            db.execute('INSERT INTO item_topics(item_id,topic_slug,evidence) VALUES(?,?,?)',
                       (row['id'],slug,json.dumps(evidence[:8],ensure_ascii=False)))
