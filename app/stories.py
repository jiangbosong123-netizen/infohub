from __future__ import annotations

"""Persistent events, conservative matching, and bounded candidate retrieval.

Existing URLs never get reused. Singleton events may merge after translation;
the original URL then redirects to the surviving event. Original articles stay
intact, and every membership records its match evidence.
"""
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from .database import get_db
from .provenance import display_title, object_json, publisher
from .topics import sync_topics, assign_topics

MATCH_VERSION = 'titles-v3'
MATCH_THRESHOLD = 0.72
MAX_EVENT_HOURS = 72


def _dt(value):
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(text):
    text = text.casefold()
    # Common editorial synonyms should not split the same launch into separate
    # events. Numeric/version guards and entity constraints still apply later.
    text = text.replace('人工智能', 'ai').replace('发布', '推出').replace('上线', '推出')
    text = re.sub(r'\b(?:launches|launched|releases|released|introduces|introduced)\b',
                  'launch', text)
    return re.sub(r'[\W_]+', '', text)


def _titles(row):
    return list(dict.fromkeys([row['title'], display_title(row)]))


def _keys(row):
    # Inverted title trigrams avoid comparing every article to every event.
    tokens = set()
    for title in _titles(row):
        normalized = _norm(title)
        tokens.update(normalized[n:n+3] for n in range(max(0, len(normalized)-2)))
    return tokens


def match_score(row, anchor):
    if abs((_dt(row['published_at']) - _dt(anchor['published_at'])).total_seconds()) > MAX_EVENT_HOURS * 3600:
        return 0.0
    companies = set(json.loads(row['companies'] or '[]'))
    others = set(json.loads(anchor['companies'] or '[]'))
    if companies and others and not companies.intersection(others):
        return 0.0
    if row['channel'] != anchor['channel'] and not (companies & others):
        return 0.0
    a_event, b_event = row['event_type'], anchor['event_type']
    if a_event and b_event and a_event != 'other' and b_event != 'other' and a_event != b_event:
        return 0.0
    # Similar-looking official filings must not collapse different documents.
    if row['official'] and anchor['official'] and row['url'] != anchor['url']:
        if object_json(row['extra']).get('form') or object_json(anchor['extra']).get('form'):
            return 0.0
        if 'hkexnews.hk' in row['url'] and 'hkexnews.hk' in anchor['url']:
            return 0.0
    original_numbers = set(re.findall(r'\d+(?:\.\d+)*', row['title']))
    anchor_numbers = set(re.findall(r'\d+(?:\.\d+)*', anchor['title']))
    if original_numbers and anchor_numbers and original_numbers != anchor_numbers:
        return 0.0
    best = 0.0
    for a in _titles(row):
        for b in _titles(anchor):
            # Avoid mixing different model versions, fiscal quarters and amounts.
            a_numbers = set(re.findall(r'\d+(?:\.\d+)*', a))
            b_numbers = set(re.findall(r'\d+(?:\.\d+)*', b))
            if a_numbers and b_numbers and a_numbers != b_numbers:
                continue
            na, nb = _norm(a), _norm(b)
            if min(len(na),len(nb)) < 8:
                continue
            # Containment alone is insufficient: a generic short title can be a
            # prefix of many unrelated followups.
            ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio()
            best = max(best, ratio)
    return best


def _refresh_stats(db, affected):
    from .ranking import item_heat
    groups = defaultdict(list)
    cutoff = (datetime.now(timezone.utc)-timedelta(days=14)).isoformat()
    db.execute('CREATE TEMP TABLE IF NOT EXISTS refresh_story_ids(id TEXT PRIMARY KEY)')
    db.execute('DELETE FROM refresh_story_ids')
    db.execute('INSERT OR IGNORE INTO refresh_story_ids SELECT id FROM stories WHERE last_at>=?',(cutoff,))
    db.executemany('INSERT OR IGNORE INTO refresh_story_ids(id) VALUES(?)',[(sid,) for sid in affected])
    for row in db.execute('''SELECT i.*,s.name AS source_name,si.story_id FROM story_items si
                             JOIN items i ON i.id=si.item_id JOIN sources s ON s.id=i.source_id
                             WHERE COALESCE(i.tmt,1)!=0 AND si.story_id IN (SELECT id FROM refresh_story_ids)'''):
        groups[row['story_id']].append(dict(row))
    db.execute('UPDATE stories SET item_count=0,source_count=0,heat=0 WHERE id IN (SELECT id FROM refresh_story_ids)')
    for sid, rows in groups.items():
        # Prefer a first-hand source when choosing the event's display headline.
        representative = max(rows, key=lambda r:(r['official'],r['score'] if r['score'] is not None else 0,r['published_at']))
        identities = {identity for identity, _, known in (publisher(r) for r in rows) if known}
        slugs = sorted({slug for r in rows for slug in json.loads(r['companies'] or '[]')})
        heat = max(item_heat(r) for r in rows) * (1 + .2 * min(max(0,len(identities)-1),5))
        old_anchor = db.execute('SELECT anchor_item_id FROM stories WHERE id=?',(sid,)).fetchone()[0]
        if old_anchor not in {r['id'] for r in rows}:
            db.execute('UPDATE stories SET anchor_item_id=? WHERE id=?',(min(rows,key=lambda r:r['published_at'])['id'],sid))
        db.execute('''UPDATE stories SET title=?,channel=?,url=?,heat=?,source_count=?,item_count=?,
                      first_at=?,last_at=?,company_slugs=? WHERE id=?''',
                   (display_title(representative),representative['channel'],representative['url'],round(heat,4),
                    len(identities),len(rows),min(r['published_at'] for r in rows),max(r['published_at'] for r in rows),
                    json.dumps(slugs,ensure_ascii=False),sid))


def refresh_derived() -> dict:
    """Index changed items atomically, with one writer across cron/CLI invocations.

    Derived work is local and deterministic. A failure rolls back assignments
    and leaves the dirty queue intact for the next run.
    """
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        definitions = sync_topics(db)
        # A matcher change must revisit existing memberships; match_reason acts
        # as a lightweight index version without another schema table.
        db.execute("""INSERT OR IGNORE INTO derived_dirty(item_id)
            SELECT item_id FROM story_items WHERE match_reason!=?""", (MATCH_VERSION,))
        # Editing an anchor can invalidate its other members; recheck the whole
        # event in the same transaction rather than leaving stale assignments.
        db.execute("""INSERT OR IGNORE INTO derived_dirty(item_id)
            SELECT si.item_id FROM stories st JOIN story_items si ON si.story_id=st.id
            WHERE st.anchor_item_id IN (SELECT item_id FROM derived_dirty)""")
        rows = [dict(r) for r in db.execute('''SELECT i.*,s.name AS source_name FROM derived_dirty d
                  JOIN items i ON i.id=d.item_id JOIN sources s ON s.id=i.source_id
                  ORDER BY i.published_at,i.id''')]
        affected = set()
        anchors = {}
        by_token = defaultdict(set)
        lower = (_dt(rows[0]['published_at'])-timedelta(hours=MAX_EVENT_HOURS)).isoformat() if rows else '9999'
        upper = (_dt(rows[-1]['published_at'])+timedelta(hours=MAX_EVENT_HOURS)).isoformat() if rows else '9999'
        for row in db.execute('''SELECT st.id AS story_id,i.* FROM stories st JOIN items i ON i.id=st.anchor_item_id
                                  WHERE st.redirect_to IS NULL AND COALESCE(i.tmt,1)!=0 AND i.published_at BETWEEN ? AND ?''',(lower,upper)):
            anchor = dict(row)
            anchors[anchor['story_id']] = anchor
            for token in _keys(anchor):
                by_token[token].add(anchor['story_id'])
        matched = 0
        for row in rows:
            assign_topics(db,row,definitions)
            old = db.execute('SELECT story_id FROM story_items WHERE item_id=?',(row['id'],)).fetchone()
            old_id = old['story_id'] if old else None
            if old_id:
                affected.add(old_id)
            if row['tmt'] == 0:
                db.execute('DELETE FROM story_items WHERE item_id=?',(row['id'],))
            else:
                candidates = defaultdict(int)
                for token in _keys(row):
                    for sid in by_token[token]:
                        candidates[sid] += 1
                best_id, best_score = None, 0.0
                for sid in sorted(candidates,key=lambda s:(-candidates[s],s))[:120]:
                    if sid == old_id:
                        continue
                    score = match_score(row,anchors[sid])
                    if score >= MATCH_THRESHOLD and score > best_score:
                        best_id,best_score = sid,score
                old_anchor = anchors.get(old_id)
                old_count = db.execute('SELECT COUNT(*) FROM story_items WHERE story_id=?',(old_id,)).fetchone()[0] if old_id else 0
                # A published multi-article event retains its anchor and URL.
                if old_anchor and old_anchor['id'] == row['id'] and old_count > 1:
                    best_id,best_score = old_id,1.0
                elif old_anchor and match_score(row,old_anchor) >= MATCH_THRESHOLD:
                    if best_id is None:
                        best_id,best_score = old_id,1.0
                if best_id is None:
                    best_id = old_id if old_count == 1 else uuid.uuid4().hex
                    db.execute('''INSERT INTO stories(id,anchor_item_id,title,channel,url,first_at,last_at)
                                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET anchor_item_id=excluded.anchor_item_id''',
                               (best_id,row['id'],display_title(row),row['channel'],row['url'],row['published_at'],row['published_at']))
                    best_score = 1.0
                if best_id not in anchors or anchors[best_id]['id'] == row['id']:
                    anchors[best_id] = row
                    for token in _keys(row):
                        by_token[token].add(best_id)
                db.execute('''INSERT INTO story_items(item_id,story_id,match_reason,match_score) VALUES(?,?,?,?)
                              ON CONFLICT(item_id) DO UPDATE SET story_id=excluded.story_id,
                              match_reason=excluded.match_reason,match_score=excluded.match_score''',
                           (row['id'],best_id,MATCH_VERSION,best_score))
                if old_id and old_id != best_id and old_count == 1:
                    db.execute('UPDATE stories SET redirect_to=? WHERE id=?',(best_id,old_id))
                    anchors.pop(old_id,None)
                    for ids in by_token.values():
                        ids.discard(old_id)
                affected.add(best_id)
                matched += 1
            db.execute('INSERT OR IGNORE INTO indexed_items(item_id) VALUES(?)',(row['id'],))
            db.execute('DELETE FROM derived_dirty WHERE item_id=?',(row['id'],))
        _refresh_stats(db,affected)
        return dict(processed=len(rows),matched=matched,
                    stories=db.execute('SELECT COUNT(*) FROM stories WHERE item_count>0 AND redirect_to IS NULL').fetchone()[0])
