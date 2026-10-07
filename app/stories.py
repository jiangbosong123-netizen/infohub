from __future__ import annotations

"""Persistent events, conservative matching, and bounded candidate retrieval.

Existing URLs never get reused. Singleton events may merge after translation, and
multi-article events whose anchors come to match merge into the earlier one; the
original URL then redirects to the surviving event. Original articles stay intact,
and every membership records its match evidence.
"""
import heapq
import json
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import cached_property
from zoneinfo import ZoneInfo

from .database import get_db
from .provenance import display_title, object_json, publisher
from .topics import sync_topics, assign_topics

MATCH_VERSION = 'titles-v4'
MATCH_THRESHOLD = 0.72
MAX_EVENT_HOURS = 72
# The flash wires repeat one another's headline: in the 2026-09-11..10-03 rehearsal data
# 1,615 of 1,704 cross-wire repeats came within 30 minutes and 1,694 within 6 hours, while
# 125 of 174 repeats by the same wire (“美股盘前要闻速递”) were more than 12 hours apart.
HEADLINE_HOURS = 6
MIN_HEADLINE_CHARS = 8
# Daily stock-move pieces ("Why is Arm stock sliding today?", "What's Going On With Oracle Stock
# Tuesday?", 甲骨文股价今日下跌原因分析) reuse one template every trading day, so two of them match
# across days although each reports a different day's move. Two such pieces only match on the same
# US Eastern date, the day their "today" refers to: in the titles-v4 rehearsal result 180 of 456
# same-event pairs of them were on different dates (a fixed 12-hour cut would have split 6 same-day
# pairs, a morning and a late-evening piece, and kept 13 across the date line).
STOCK_MOVE_ZONE = ZoneInfo("America/New_York")
_MOVE_WORDS = (r"(?:up|down|rising|falling|sliding|climbing|rallying|surging|gaining|dropping|jumping|soaring|"
               r"sinking|tumbling|trading|popping|plunging|slumping|jumped|dropped|fell|rose|soared|sank|popped|"
               r"plunged|surged|tumbled|slid|climbed|rallied|gained|slumped|crashed|reversed)")
# A piece is about one day's move when it names the day; past-tense "why X stock jumped 30% in
# September" recaps and "here's why" opinion pieces are syndicated across days and are not included.
_TODAY = (r"(?:today|tonight|this\s+(?:morning|afternoon|week)|overnight|pre-?market|after[\s-]hours|"
          r"monday|tuesday|wednesday|thursday|friday)")
_STOCK_MOVE_EN = re.compile(rf"""(?ix)
    \bwhy\b.{{0,60}}?\bstocks?\b.{{0,40}}?\b{_TODAY}\b
  | \bwhy\s+(?:is|are)\s+.{{1,60}}?\bstocks?\s+{_MOVE_WORDS}\b
  | \bstocks?\s+just\s+{_MOVE_WORDS}\b
  | \bwhy\b.{{0,60}}?\bstocks?\s+(?:keeps?|continues?)\s+(?:going\s+)?{_MOVE_WORDS}\b
  | (?:what'?s|what\s+is)\s+going\s+on\s+with\s+.{{1,80}}?\bstock\b
  | \bstock\b.{{0,40}}\b{_TODAY}\b.{{0,20}}(?:here(?:'s|\s+is)\s+(?:why|what\s+happened)|what'?s\s+going\s+on)
  | \bstock\s+trades\s+(?:up|down)\b
  | \bstock\s+price\s+(?:up|down)\s+[\d.]+%
  | \bstock\s+price\s+ended\s+at\b
  | \b(?:green|red)\s+day\s+on\s+{_TODAY}\s+for\b
  | \b(?:QQQ|SPY|VOO|DIA|IWM)\s+is\s+(?:up|down)\b
  | \bstock\s+(?:price\s+)?(?:is\s+)?(?:up|down|rises?|falls?|gains?|drops?|jumps?|slides?|sinks?|soars?|climbs?|
        tumbles?|surges?|plunges?|rallies|underperforms|outperforms)\b.{{0,40}}\b{_TODAY}\b
  | \b(?:rises?|falls?|declines?|dips?|gains?)\s+(?:higher|more\s+steeply|less)\s+than\s+(?:the\s+)?(?:broader\s+)?market\b
""")
_STOCK_MOVE_ZH = re.compile(r"股价(?:今日|今天|周[一二三四五六日]|本周)|(?:今日|今天|周[一二三四五])股价|股价(?:异动|动态|走势如何)"
                            r"|股价.{0,8}发生了什么|(?:盘前|盘后|隔夜)股价|股价(?:盘前|盘后|隔夜)")
# CLS opens untitled telegraphs with a dateline whose digits are not part of the news.
_DATELINE = re.compile(r'^财联社\d{1,2}月\d{1,2}日电[，,]\s*')
# Sina 7x24 and WSCN live write “【headline】body”; CLS uses 【】 for column names instead.
_BRACKET_HEADLINE_SOURCES = frozenset({'sina', 'wscn_live'})


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


def _undated(title):
    return _DATELINE.sub('', title)


def _titles(row):
    return list(dict.fromkeys([_undated(row['title']), _undated(display_title(row))]))


def _headline(row):
    """The headline as the wire wrote it, normalized for exact comparison."""
    title = unicodedata.normalize('NFKC', _undated(row['title']).strip())
    if row.get('source_type') in _BRACKET_HEADLINE_SOURCES:
        bracketed = re.match(r'【([^】]+)】', title)
        if bracketed:
            title = bracketed.group(1)
    return _norm(title)


def _keys(row):
    # Inverted title trigrams avoid comparing every article to every event.
    tokens = set()
    for title in _titles(row):
        normalized = _norm(title)
        tokens.update(normalized[n:n+3] for n in range(max(0, len(normalized)-2)))
    return tokens


class _Facts:
    """What match_score derives from one item, each computed on first use as before."""

    def __init__(self, row):
        self.row = row

    @cached_property
    def published(self):
        return _dt(self.row['published_at'])

    @cached_property
    def companies(self):
        return set(json.loads(self.row['companies'] or '[]'))

    @cached_property
    def form(self):
        return object_json(self.row['extra']).get('form')

    @cached_property
    def numbers(self):
        return set(re.findall(r'\d+(?:\.\d+)*', _undated(self.row['title'])))

    @cached_property
    def headline(self):
        return _headline(self.row)

    @cached_property
    def stock_move(self):
        return any(_STOCK_MOVE_EN.search(title) or _STOCK_MOVE_ZH.search(title) for title in _titles(self.row))

    @cached_property
    def trading_day(self):
        return self.published.astimezone(STOCK_MOVE_ZONE).date()

    @cached_property
    def titles(self):
        return [(set(re.findall(r'\d+(?:\.\d+)*', title)), _norm(title)) for title in _titles(self.row)]


def match_score(row, anchor):
    return _score(_Facts(row), _Facts(anchor))


def _same_headline(item, other):
    """Two wires carrying one headline within HEADLINE_HOURS report the same news.

    This runs before the channel, event-type and number guards: the copies' numbers are those
    of the shared headline, and the channel of each feed (IT之家 is filed under AI, Sina under
    stocks) or the AI's event type for each copy is not a reason to split them. Disjoint
    companies still are, and official documents keep their own guard, since filings share
    boilerplate titles.
    """
    if item.row['official'] or other.row['official']:
        return False
    if len(item.headline) < MIN_HEADLINE_CHARS or item.headline != other.headline:
        return False
    return abs((item.published - other.published).total_seconds()) <= HEADLINE_HOURS * 3600


def _moves_of_different_days(item, other):
    return item.stock_move and other.stock_move and item.trading_day != other.trading_day


def _score(item, other, beat=None):
    """match_score on cached facts. With ``beat``, pairs that cannot reach a score that is
    both >= MATCH_THRESHOLD and > beat are skipped, so the result equals match_score
    whenever it passes that test and fails the test whenever match_score does."""
    row, anchor = item.row, other.row
    if abs((item.published - other.published).total_seconds()) > MAX_EVENT_HOURS * 3600:
        return 0.0
    if _moves_of_different_days(item, other):
        return 0.0
    companies, others = item.companies, other.companies
    if companies and others and not companies.intersection(others):
        return 0.0
    if _same_headline(item, other):
        return 1.0
    if row['channel'] != anchor['channel'] and not (companies & others):
        return 0.0
    a_event, b_event = row['event_type'], anchor['event_type']
    if a_event and b_event and a_event != 'other' and b_event != 'other' and a_event != b_event:
        return 0.0
    # Similar-looking official filings must not collapse different documents.
    if row['official'] and anchor['official'] and row['url'] != anchor['url']:
        if item.form or other.form:
            return 0.0
        if 'hkexnews.hk' in row['url'] and 'hkexnews.hk' in anchor['url']:
            return 0.0
    if item.numbers and other.numbers and item.numbers != other.numbers:
        return 0.0
    best = 0.0
    for a_numbers, na in item.titles:
        for b_numbers, nb in other.titles:
            # Avoid mixing different model versions, fiscal quarters and amounts.
            if a_numbers and b_numbers and a_numbers != b_numbers:
                continue
            if min(len(na),len(nb)) < 8:
                continue
            if beat is not None:
                # ratio() is 2*matches/(len(na)+len(nb)) and matches never exceed the
                # shorter title, so this bound is never below the ratio.
                bound = 2.0 * min(len(na), len(nb)) / (len(na) + len(nb))
                if bound < MATCH_THRESHOLD or bound <= beat or bound <= best:
                    continue
            # Containment alone is insufficient: a generic short title can be a
            # prefix of many unrelated followups.
            ratio = SequenceMatcher(None, na, nb, autojunk=False).ratio()
            best = max(best, ratio)
    return best


def _merge_stories(db, affected, anchors, facts_of, wanted):
    """Merge live stories that the per-item pass leaves apart because each keeps its anchor.

    A published multi-article event keeps its anchor, so two events that formed separately stay
    apart even once their anchors match, for example after a matcher change or a translation.
    ``wanted`` holds those cases, recorded when an anchor was kept although it matched another
    event's anchor or shared a headline with one of its members. Stories are visited by anchor
    time and each joins its best earlier partner, then redirects there like a merged singleton;
    its anchor goes on representing the members that came with it. A partner already merged in
    this pass counts only if the event it went into matches directly, anchor against anchor, so
    merges never chain A~B~C into one event when A and C do not match.
    """
    def key(sid):
        return (anchors[sid]['published_at'], sid)

    partners = defaultdict(dict)
    for story_id, targets in wanted.items():
        for target, score in targets.items():
            if story_id != target and story_id in anchors and target in anchors:
                later, earlier = sorted((story_id, target), key=key, reverse=True)
                partners[later][earlier] = max(score, partners[later].get(earlier, 0.0))
    merged = {}

    def surviving(sid):
        while sid in merged:
            sid = merged[sid]
        return sid

    for later in sorted(partners, key=key):
        options = []
        for earlier, score in partners[later].items():
            target = surviving(earlier)
            if target != earlier:
                score = _score(facts_of(anchors[later]), facts_of(anchors[target]))
                if score < MATCH_THRESHOLD:
                    continue
            options.append((-score, key(target), target))
        if not options:
            continue
        target = min(options)[2]
        db.execute('UPDATE story_items SET story_id=? WHERE story_id=?', (target, later))
        db.execute('UPDATE stories SET redirect_to=? WHERE id=?', (target, later))
        merged[later] = target
        affected.update((later, target))
    return len(merged)


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
        # The same holds for the anchor of an event that was merged into another one.
        db.execute("""INSERT OR IGNORE INTO derived_dirty(item_id)
            SELECT member.item_id FROM stories st
            JOIN story_items holder ON holder.item_id=st.anchor_item_id
            JOIN story_items member ON member.story_id=holder.story_id
            WHERE st.redirect_to IS NOT NULL AND st.anchor_item_id IN (SELECT item_id FROM derived_dirty)""")
        rows = [dict(r) for r in db.execute('''SELECT i.*,s.name AS source_name,s.type AS source_type
                  FROM derived_dirty d JOIN items i ON i.id=d.item_id JOIN sources s ON s.id=i.source_id
                  ORDER BY i.published_at,i.id''')]
        affected = set()
        anchors = {}
        by_token = defaultdict(set)
        lower = (_dt(rows[0]['published_at'])-timedelta(hours=MAX_EVENT_HOURS)).isoformat() if rows else '9999'
        upper = (_dt(rows[-1]['published_at'])+timedelta(hours=MAX_EVENT_HOURS)).isoformat() if rows else '9999'
        for row in db.execute('''SELECT st.id AS story_id,i.*,s.type AS source_type FROM stories st
                                  JOIN items i ON i.id=st.anchor_item_id JOIN sources s ON s.id=i.source_id
                                  WHERE st.redirect_to IS NULL AND COALESCE(i.tmt,1)!=0 AND i.published_at BETWEEN ? AND ?''',(lower,upper)):
            anchor = dict(row)
            anchors[anchor['story_id']] = anchor
            for token in _keys(anchor):
                by_token[token].add(anchor['story_id'])
        # The anchors of events merged into a live one (by _merge_stories, or a singleton that
        # moved there) that are still its members. The members they brought matched them, not
        # the surviving anchor, so a re-checked member may stay by matching one of them. New
        # articles and merges only match surviving anchors: former anchors never chain events.
        former = defaultdict(list)
        redirects = dict(db.execute('SELECT id,redirect_to FROM stories WHERE redirect_to IS NOT NULL').fetchall())

        def resolved(story_id):
            seen = set()
            while story_id in redirects and story_id not in seen:
                seen.add(story_id)
                story_id = redirects[story_id]
            return story_id

        for row in db.execute('''SELECT st.redirect_to,si.story_id,i.*,s.type AS source_type FROM stories st
                                  JOIN items i ON i.id=st.anchor_item_id JOIN sources s ON s.id=i.source_id
                                  JOIN story_items si ON si.item_id=i.id
                                  WHERE st.redirect_to IS NOT NULL AND COALESCE(i.tmt,1)!=0
                                    AND i.published_at BETWEEN ? AND ?''',(lower,upper)):
            anchor = dict(row)
            holder = anchor['story_id']
            if (holder in anchors and resolved(anchor['redirect_to']) == holder
                    and anchor['id'] not in {a['id'] for a in (anchors[holder], *former[holder])}):
                former[holder].append(anchor)
        matched = 0
        facts = {}

        def facts_of(item):
            # Keyed by object identity; keeping the item in the entry pins that identity.
            entry = facts.get(id(item))
            if entry is None:
                entry = facts[id(item)] = (item, _Facts(item))
            return entry[1]

        # Headlines are compared with every member, not only anchors: a wire's copy often
        # arrives after the event was anchored on another source's wording. Members still
        # waiting to be re-indexed join as they are assigned below.
        by_headline = defaultdict(dict)
        wanted = defaultdict(dict)  # retained anchor's story -> other stories its anchor matches

        def remember_headline(item, story_id):
            if len(facts_of(item).headline) >= MIN_HEADLINE_CHARS:
                by_headline[facts_of(item).headline][item['id']] = (item, story_id)

        hours = timedelta(hours=HEADLINE_HOURS)
        if rows:
            for member in db.execute('''SELECT si.story_id,i.*,s.type AS source_type FROM items i
                                        JOIN story_items si ON si.item_id=i.id JOIN sources s ON s.id=i.source_id
                                        WHERE i.published_at BETWEEN ? AND ? AND COALESCE(i.tmt,1)!=0
                                          AND i.id NOT IN (SELECT item_id FROM derived_dirty)''',
                                     ((_dt(rows[0]['published_at'])-hours).isoformat(),
                                      (_dt(rows[-1]['published_at'])+hours).isoformat())):
                member = dict(member)
                remember_headline(member, member['story_id'])

        for row in rows:
            assign_topics(db,row,definitions)
            old = db.execute('SELECT story_id FROM story_items WHERE item_id=?',(row['id'],)).fetchone()
            old_id = old['story_id'] if old else None
            if old_id:
                affected.add(old_id)
            if row['tmt'] == 0:
                db.execute('DELETE FROM story_items WHERE item_id=?',(row['id'],))
            else:
                best_id, best_score = None, 0.0
                # An event's own anchor looks only at other events, for a merge partner below.
                own = old_id if anchors.get(old_id, {}).get('id') == row['id'] else None
                # The earliest member carrying the same headline decides, as an anchor would.
                same = [(member['published_at'], member['id'], sid)
                        for member, sid in by_headline.get(facts_of(row).headline, {}).values()
                        if member['id'] != row['id'] and sid != own
                        and _same_headline(facts_of(row), facts_of(member))
                        and _score(facts_of(row), facts_of(member)) == 1.0]
                if same:
                    best_id, best_score = min(same)[2], 1.0
                else:
                    # Counter.update counts in C and heapq.nsmallest is documented as equivalent to
                    # sorted(...)[:n]; both keep the scores identical while a full reindex of tens
                    # of thousands of items no longer sorts every candidate list completely.
                    candidates = Counter()
                    for token in _keys(row):
                        candidates.update(by_token[token])
                    for sid in heapq.nsmallest(120, candidates, key=lambda s:(-candidates[s],s)):
                        if sid == old_id:
                            continue
                        score = _score(facts_of(row), facts_of(anchors[sid]), beat=best_score)
                        if score >= MATCH_THRESHOLD and score > best_score:
                            best_id,best_score = sid,score
                old_anchor = anchors.get(old_id)
                old_count = db.execute('SELECT COUNT(*) FROM story_items WHERE story_id=?',(old_id,)).fetchone()[0] if old_id else 0
                if old_anchor and old_anchor['id'] == row['id']:
                    # A published multi-article event retains its anchor and URL; if the anchor
                    # now matches another event, _merge_stories joins the two after this loop.
                    if old_count > 1:
                        if best_id is not None and best_id != old_id:
                            wanted[old_id][best_id] = max(best_score, wanted[old_id].get(best_id, 0.0))
                        best_id,best_score = old_id,1.0
                    elif best_id is None:
                        best_id,best_score = old_id,1.0
                elif old_anchor:
                    # A member stays with its event unless another one matches strictly better.
                    # Leaving for any other match made consecutive titles-v3 reindexes move about
                    # 7,300 of 57,924 items back and forth between near-duplicate events. The
                    # anchor of an event merged into this one also counts for its old members.
                    # Nor does a day's stock-move piece stay under another day's: former anchors
                    # of that day could otherwise keep each other there.
                    stay = 0.0 if _moves_of_different_days(facts_of(row), facts_of(old_anchor)) else max(
                        _score(facts_of(row), facts_of(anchor))
                        for anchor in (old_anchor, *former.get(old_id, ())) if anchor['id'] != row['id'])
                    if stay >= MATCH_THRESHOLD and stay >= best_score:
                        best_id,best_score = old_id,stay
                if best_id is None:
                    best_id = old_id if old_count == 1 else uuid.uuid4().hex
                    db.execute('''INSERT INTO stories(id,anchor_item_id,title,channel,url,first_at,last_at)
                                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET anchor_item_id=excluded.anchor_item_id''',
                               (best_id,row['id'],display_title(row),row['channel'],row['url'],row['published_at'],row['published_at']))
                    best_score = 1.0
                # A story joined by headline whose anchor lies outside the window keeps it there.
                if (best_id not in anchors and not same) or anchors.get(best_id, {}).get('id') == row['id']:
                    anchors[best_id] = row
                    for token in _keys(row):
                        by_token[token].add(best_id)
                db.execute('''INSERT INTO story_items(item_id,story_id,match_reason,match_score) VALUES(?,?,?,?)
                              ON CONFLICT(item_id) DO UPDATE SET story_id=excluded.story_id,
                              match_reason=excluded.match_reason,match_score=excluded.match_score''',
                           (row['id'],best_id,MATCH_VERSION,best_score))
                remember_headline(row, best_id)
                if old_id and old_id != best_id:
                    former[old_id] = [anchor for anchor in former.get(old_id, ()) if anchor['id'] != row['id']]
                if old_id and old_id != best_id and old_count == 1:
                    db.execute('UPDATE stories SET redirect_to=? WHERE id=?',(best_id,old_id))
                    anchors.pop(old_id,None)
                    for ids in by_token.values():
                        ids.discard(old_id)
                    if best_id in anchors and anchors[best_id]['id'] != row['id']:
                        former[best_id].append(row)
                affected.add(best_id)
                matched += 1
            db.execute('INSERT OR IGNORE INTO indexed_items(item_id) VALUES(?)',(row['id'],))
            db.execute('DELETE FROM derived_dirty WHERE item_id=?',(row['id'],))
        merged = _merge_stories(db, affected, anchors, facts_of, wanted)
        _refresh_stats(db,affected)
        return dict(processed=len(rows),matched=matched,merged=merged,
                    stories=db.execute('SELECT COUNT(*) FROM stories WHERE item_count>0 AND redirect_to IS NULL').fetchone()[0])
