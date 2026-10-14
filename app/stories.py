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
import time
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

MATCH_VERSION = 'titles-v5'
MATCH_THRESHOLD = 0.72
MAX_EVENT_HOURS = 72
# Each article joins the event whose anchor it matches, so one event worded several ways can
# become several events whose articles match one another but whose anchors do not (Gemini
# breaking into three companies was 8 events of the titles-v4 rehearsal index). Events with
# MERGE_MIN_PAIRS articles matching across merge: on that index (57,924 articles) 821 merges in
# six rounds left 41,907 of 42,716 events, and 38 of 40 sampled merges joined one event (the
# others: funds each buying Amazon shares, one unclear). One matching pair is weaker: it joined
# different events in 2 of 15 sampled pairs. Copies of one headline already join the earliest
# copy's event as articles are placed.
MERGE_MIN_PAIRS = 2
MEMBER_CANDIDATES = 60
MEMBER_MIN_SHARED = 0.3
MERGE_ROUNDS = 10
# A chunk takes REFRESH_CHUNK items, so it holds the write lock well under the 30 s other writers
# wait. Chunks end at fixed counts so a reindex gives the same result each time: when a time limit
# set the boundaries, a second reindex moved 358 articles between events, against a few dozen with
# fixed chunks. REFRESH_CHUNK_SECONDS only guards a very slow host: past it, a chunk stops taking
# items (the rest stays queued) and finishes the merges for what it took.
REFRESH_CHUNK = 1500
REFRESH_CHUNK_SECONDS = 12
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


def _guard(item, other):
    """0.0 when a rule keeps two articles apart, 1.0 for a shared headline, else None."""
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
    return None


def _score(item, other, beat=None):
    """match_score on cached facts. With ``beat``, pairs that cannot reach a score that is
    both >= MATCH_THRESHOLD and > beat are skipped, so the result equals match_score
    whenever it passes that test and fails the test whenever match_score does."""
    guard = _guard(item, other)
    if guard is not None:
        return guard
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


class _MemberEvidence:
    """How well two events' articles match, from their membership when this round began."""

    def __init__(self, db, stories, facts):
        self.facts = facts
        self.scores = {}
        self.members = defaultdict(list)
        stories = sorted(stories)
        for start in range(0, len(stories), 500):
            chunk = stories[start:start + 500]
            for member in db.execute(
                    f"""SELECT si.story_id,i.*,s.type AS source_type FROM story_items si
                        JOIN items i ON i.id=si.item_id JOIN sources s ON s.id=i.source_id
                        WHERE si.story_id IN ({",".join("?" * len(chunk))}) AND COALESCE(i.tmt,1)!=0""", chunk):
                self.members[member['story_id']].append(dict(member))

    def fact(self, row):
        return _fact(self.facts, row)

    def score(self, a, b):
        """The best article pair's score if the events qualify for a merge, else 0.0.

        Title similarity is not symmetric; as when an article is placed, the later one is
        compared with the earlier one.
        """
        key = (a, b) if a < b else (b, a)
        if key not in self.scores:
            pairs, best = 0, 0.0
            for row in self.members.get(a, ()):
                for other in self.members.get(b, ()):
                    item, earlier = sorted((self.fact(row), self.fact(other)),
                                           key=lambda fact: (fact.published, fact.row['id']), reverse=True)
                    score = _score(item, earlier)
                    if score >= MATCH_THRESHOLD:
                        pairs, best = pairs + 1, max(best, score)
            self.scores[key] = best if pairs >= MERGE_MIN_PAIRS else 0.0
        return self.scores[key]

    def partners(self, nominated):
        partners = defaultdict(dict)
        for a, b in nominated:
            score = self.score(a, b)
            if score:
                partners[a][b] = partners[b][a] = score
        return partners


def _fact(facts, row):
    found = facts.get(row['id'])
    if found is None:
        found = facts[row['id']] = _Facts(row)
    return found


def _member_support(db, item, story_id):
    """The best match among an event's other articles if they would merge it, else 0.0."""
    scores = [score for score in (
        _score(item, _Facts(dict(other))) for other in db.execute(
            """SELECT i.*,s.type AS source_type FROM story_items si JOIN items i ON i.id=si.item_id
               JOIN sources s ON s.id=i.source_id
               WHERE si.story_id=? AND si.item_id!=? AND COALESCE(i.tmt,1)!=0""", (story_id, item.row['id'])))
        if score >= MATCH_THRESHOLD]
    return max(scores) if len(scores) >= MERGE_MIN_PAIRS else 0.0


def _nominate(db, rows, facts):
    """Pairs of events with articles that match across, found from the articles re-indexed now.

    Each re-indexed article is compared with the articles of other events published within
    MAX_EVENT_HOURS that share the most title trigrams with it; a match nominates the pair of
    events, and _MemberEvidence decides on all their articles. Two articles indexed before were
    compared when the later of them was indexed, so only pairs involving an article indexed now
    are compared (on a full reindex, every pair once).
    """
    rows = [row for row in rows if row['tmt'] != 0]
    if not rows:
        return set()
    window = timedelta(hours=MAX_EVENT_HOURS)
    lower = (_dt(rows[0]['published_at']) - window).isoformat()
    upper = (_dt(rows[-1]['published_at']) + window).isoformat()
    live = [dict(r) for r in db.execute(
        """SELECT si.story_id,i.*,s.type AS source_type FROM story_items si
           JOIN stories st ON st.id=si.story_id JOIN items i ON i.id=si.item_id JOIN sources s ON s.id=i.source_id
           WHERE st.redirect_to IS NULL AND COALESCE(i.tmt,1)!=0 AND i.published_at BETWEEN ? AND ?""",
        (lower, upper))]
    dirty = {row['id'] for row in rows}
    live.sort(key=lambda row: (_fact(facts, row).published, row['id']))
    times = [_fact(facts, row).published for row in live]
    keys = [_keys(row) for row in live]
    index = defaultdict(set)
    indexed = set()
    nominated = set()
    ahead = behind = 0
    for n, row in enumerate(live):
        if row['id'] not in dirty:
            continue
        # Articles indexed before enter while within the window ahead; re-indexed ones enter
        # after their own comparison, so a pair of them is compared once.
        while ahead < len(live) and times[ahead] <= times[n] + window:
            if live[ahead]['id'] not in dirty:
                indexed.add(ahead)
                for token in keys[ahead]:
                    index[token].add(ahead)
            ahead += 1
        while times[behind] < times[n] - window:
            if behind in indexed:
                indexed.discard(behind)
                for token in keys[behind]:
                    index[token].discard(behind)
            behind += 1
        candidates = Counter()
        for token in keys[n]:
            candidates.update(index[token])
        for other in heapq.nsmallest(MEMBER_CANDIDATES, candidates, key=lambda m: (-candidates[m], m)):
            # Titles sharing under MEMBER_MIN_SHARED of the shorter one's trigrams are not
            # compared: on the rehearsal index this skipped 7 of 443 qualifying pairs of events
            # and took the comparison from 90 s to 38 s.
            if candidates[other] < MEMBER_MIN_SHARED * min(len(keys[n]), len(keys[other])):
                continue
            pair = tuple(sorted((row['story_id'], live[other]['story_id'])))
            if pair[0] != pair[1] and pair not in nominated and _score(
                    _fact(facts, row), _fact(facts, live[other]), beat=0.0) >= MATCH_THRESHOLD:
                nominated.add(pair)
        indexed.add(n)
        for token in keys[n]:
            index[token].add(n)
    return nominated


def _merge_stories(db, affected, anchors, facts_of, wanted, matched=None):
    """Merge live stories that the per-item pass leaves apart because each keeps its anchor.

    A published multi-article event keeps its anchor, so two events that formed separately stay
    apart even once their anchors match, for example after a matcher change or a translation.
    ``wanted`` holds those cases, recorded when an anchor was kept although it matched another
    event's anchor or shared a headline with one of its members. Stories are visited by anchor
    time and each joins its best earlier partner, then redirects there like a merged singleton;
    its anchor goes on representing the members that came with it. ``matched`` adds the pairs
    of events whose articles match across (_MemberEvidence), unless a rule keeps their anchors
    apart: an event must not take in events whose version, amount, company, filing, event type
    or day contradict its own, or whose anchor lies more than MAX_EVENT_HOURS from its own. A
    partner already merged in this call counts only if the event it went into matches directly,
    anchor against anchor. The caller repeats the call on the merged memberships, so A~B and
    B~C join A, B and C only when A's and C's anchors do not contradict each other. Returns
    {merged story: story it joined}.
    """
    def key(sid):
        return (anchors[sid]['published_at'], sid)

    def compatible(a, b):
        return _guard(facts_of(anchors[a]), facts_of(anchors[b])) != 0.0

    candidates = [(sid, target, score) for sid, targets in wanted.items() for target, score in targets.items()]
    candidates += [(sid, target, score) for sid, targets in (matched or {}).items()
                   for target, score in targets.items()
                   if sid in anchors and target in anchors and compatible(sid, target)]
    partners = defaultdict(dict)
    for story_id, target, score in candidates:
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
    return merged


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


def refresh_derived(chunk: int | None = REFRESH_CHUNK, max_chunks: int | None = None) -> dict:
    """Index changed items, earliest first, ``chunk`` items per transaction.

    A matcher or topic change queues every item; indexed in one transaction that held the write
    lock for over four minutes on the rehearsal data (57,924 items), longer than the 30 s other
    writers wait. Each chunk commits on its own, and the queue is worked off in the order a
    single pass would take, so later chunks see earlier ones as already indexed. ``None``
    indexes the whole queue in one transaction. With ``max_chunks`` the rest stays queued.
    """
    totals = dict(processed=0, matched=0, merged=0, chunks=0)
    done = set()
    while True:
        result = _refresh_chunk(chunk, done)
        ids = set(result.pop('ids'))
        # Items queued again while this refresh ran (another writer edited them) are indexed
        # again; a chunk of nothing but such items stops the refresh, which leaves them queued
        # for the next one instead of chasing a queue that refills.
        repeated = ids <= done
        done |= ids
        for key in ('processed', 'matched', 'merged'):
            totals[key] += result[key]
        totals['chunks'] += 1
        if (not result['remaining'] or not result['processed'] or repeated
                or totals['chunks'] == max_chunks):
            return dict(totals, remaining=result['remaining'], stories=result['stories'])


def _refresh_chunk(limit: int | None, done: set) -> dict:
    """Index up to ``limit`` changed items atomically, with one writer across cron/CLI invocations.

    Derived work is local and deterministic. A failure rolls back assignments
    and leaves the dirty queue intact for the next run. ``done`` holds the items indexed by
    earlier chunks of this refresh; their events' anchors do not queue them again.
    """
    deadline = None if limit is None else time.monotonic() + REFRESH_CHUNK_SECONDS
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        definitions = sync_topics(db)
        # A matcher change must revisit existing memberships; match_reason acts
        # as a lightweight index version without another schema table.
        db.execute("""INSERT OR IGNORE INTO derived_dirty(item_id)
            SELECT item_id FROM story_items WHERE match_reason!=?""", (MATCH_VERSION,))
        # Editing an anchor can invalidate its other members; recheck the whole
        # event rather than leaving stale assignments. The same holds for the anchor of an event
        # that was merged into another one. On a reindex in chunks every anchor is queued, and
        # members an earlier chunk indexed would otherwise be queued again by each later one
        # (12,500 extra of 57,924 on the rehearsal data), unlike a single pass.
        members = [item_id for item_id, in db.execute("""SELECT si.item_id FROM stories st JOIN story_items si ON si.story_id=st.id
            WHERE st.anchor_item_id IN (SELECT item_id FROM derived_dirty)
            UNION SELECT member.item_id FROM stories st
            JOIN story_items holder ON holder.item_id=st.anchor_item_id
            JOIN story_items member ON member.story_id=holder.story_id
            WHERE st.redirect_to IS NOT NULL AND st.anchor_item_id IN (SELECT item_id FROM derived_dirty)""")]
        db.executemany('INSERT OR IGNORE INTO derived_dirty(item_id) VALUES(?)',
                       [(item_id,) for item_id in members if item_id not in done])
        rows = [dict(r) for r in db.execute('''SELECT i.*,s.name AS source_name,s.type AS source_type
                  FROM derived_dirty d JOIN items i ON i.id=d.item_id JOIN sources s ON s.id=i.source_id
                  ORDER BY i.published_at,i.id LIMIT ?''', (-1 if limit is None else limit,))]
        affected = set()
        processed = []
        anchors = {}
        by_token = defaultdict(set)
        lower = (_dt(rows[0]['published_at'])-timedelta(hours=MAX_EVENT_HOURS)).isoformat() if rows else '9999'
        upper = (_dt(rows[-1]['published_at'])+timedelta(hours=MAX_EVENT_HOURS)).isoformat() if rows else '9999'
        if rows:
            # A merged event can reach past the window (each merged event's articles lie within
            # MAX_EVENT_HOURS of its own anchor), so the window also spans every article of the
            # events the rows belong to: a rechecked article must find its event's anchor and
            # former anchors, or it leaves the event.
            db.execute('CREATE TEMP TABLE IF NOT EXISTS refresh_rows(item_id INTEGER PRIMARY KEY)')
            db.execute('DELETE FROM refresh_rows')
            db.executemany('INSERT INTO refresh_rows(item_id) VALUES(?)', [(row['id'],) for row in rows])
            span = db.execute('''SELECT MIN(i.published_at),MAX(i.published_at) FROM refresh_rows r
                                 JOIN story_items si ON si.item_id=r.item_id
                                 JOIN story_items m ON m.story_id=si.story_id JOIN items i ON i.id=m.item_id''').fetchone()
            if span[0]:
                lower = min(_dt(lower), _dt(span[0])).isoformat()
                upper = max(_dt(upper), _dt(span[1])).isoformat()
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
            if deadline is not None and processed and time.monotonic() > deadline:
                break
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
                    other_day = _moves_of_different_days(facts_of(row), facts_of(old_anchor))
                    stay = 0.0 if other_day else max(
                        _score(facts_of(row), facts_of(anchor))
                        for anchor in (old_anchor, *former.get(old_id, ())) if anchor['id'] != row['id'])
                    # An event merged in because its articles matched this one's brought articles
                    # that need not match an anchor here; they stay while they match its articles.
                    if stay < MATCH_THRESHOLD and not other_day:
                        stay = _member_support(db, facts_of(row), old_id)
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
            processed.append(row['id'])
        rows = rows[:len(processed)]
        # Merging two events can make a third qualify against their union, so merges repeat
        # until none is left; each round decides on the memberships it starts with.
        member_facts = {}
        nominated = _nominate(db, rows, member_facts)
        merged = 0
        for _ in range(MERGE_ROUNDS):
            evidence = _MemberEvidence(db, {story for pair in nominated for story in pair}, member_facts)
            merges = _merge_stories(db, affected, anchors, facts_of, wanted, evidence.partners(nominated))
            merged += len(merges)
            nominated = {tuple(sorted((merges.get(a, a), merges.get(b, b)))) for a, b in nominated}
            nominated = {pair for pair in nominated if pair[0] != pair[1]}
            wanted = {}
            if not merges or not nominated:
                break
        _refresh_stats(db,affected)
        return dict(processed=len(rows),matched=matched,merged=merged,ids=processed,
                    remaining=db.execute('SELECT COUNT(*) FROM derived_dirty').fetchone()[0],
                    stories=db.execute('SELECT COUNT(*) FROM stories WHERE item_count>0 AND redirect_to IS NULL').fetchone()[0])
