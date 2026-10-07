import json
import random
import re
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from app.provenance import display_title, object_json
from app.stories import (MATCH_THRESHOLD, MAX_EVENT_HOURS, STOCK_MOVE_ZONE, _STOCK_MOVE_EN, _STOCK_MOVE_ZH, _Facts,
                         _dt, _norm, _score, match_score)


def undated(title):
    return re.sub(r'^财联社\d{1,2}月\d{1,2}日电[，,]\s*', '', title)


def reference_headline(row):
    title = unicodedata.normalize('NFKC', undated(row['title']).strip())
    if row.get('source_type') in ('sina', 'wscn_live') and re.match(r'【[^】]+】', title):
        title = title[1:title.index('】')]
    return _norm(title)


def stock_move(row):
    titles = [undated(row['title']), undated(display_title(row))]
    return any(_STOCK_MOVE_EN.search(title) or _STOCK_MOVE_ZH.search(title) for title in titles)


def reference_match_score(row, anchor):
    """titles-v4 match_score written out rule by rule, without cached facts or pruning."""
    gap = abs((_dt(row['published_at']) - _dt(anchor['published_at'])).total_seconds())
    if gap > MAX_EVENT_HOURS * 3600:
        return 0.0
    if stock_move(row) and stock_move(anchor) and (
            _dt(row['published_at']).astimezone(STOCK_MOVE_ZONE).date()
            != _dt(anchor['published_at']).astimezone(STOCK_MOVE_ZONE).date()):
        return 0.0
    companies = set(json.loads(row['companies'] or '[]'))
    others = set(json.loads(anchor['companies'] or '[]'))
    if companies and others and not companies.intersection(others):
        return 0.0
    headline = reference_headline(row)
    if (not row['official'] and not anchor['official'] and gap <= 6 * 3600
            and len(headline) >= 8 and headline == reference_headline(anchor)):
        return 1.0
    if row['channel'] != anchor['channel'] and not (companies & others):
        return 0.0
    a_event, b_event = row['event_type'], anchor['event_type']
    if a_event and b_event and a_event != 'other' and b_event != 'other' and a_event != b_event:
        return 0.0
    if row['official'] and anchor['official'] and row['url'] != anchor['url']:
        if object_json(row['extra']).get('form') or object_json(anchor['extra']).get('form'):
            return 0.0
        if 'hkexnews.hk' in row['url'] and 'hkexnews.hk' in anchor['url']:
            return 0.0
    original_numbers = set(re.findall(r'\d+(?:\.\d+)*', undated(row['title'])))
    anchor_numbers = set(re.findall(r'\d+(?:\.\d+)*', undated(anchor['title'])))
    if original_numbers and anchor_numbers and original_numbers != anchor_numbers:
        return 0.0
    best = 0.0
    for a in dict.fromkeys([undated(row['title']), undated(display_title(row))]):
        for b in dict.fromkeys([undated(anchor['title']), undated(display_title(anchor))]):
            a_numbers = set(re.findall(r'\d+(?:\.\d+)*', a))
            b_numbers = set(re.findall(r'\d+(?:\.\d+)*', b))
            if a_numbers and b_numbers and a_numbers != b_numbers:
                continue
            na, nb = _norm(a), _norm(b)
            if min(len(na), len(nb)) < 8:
                continue
            best = max(best, SequenceMatcher(None, na, nb, autojunk=False).ratio())
    return best


PHRASES = [
    "OpenAI launches a new coding agent for developers",
    "OpenAI releases GPT 5.1 with faster reasoning",
    "Nvidia reports record quarterly revenue of 39 billion",
    "Anthropic introduces Claude for enterprise teams",
    "腾讯发布新一代混元大模型",
    "阿里云上线通义千问 3 版本",
    "Robot maker unveils humanoid platform",
    "Why is OpenAI stock rising today",
]
T0 = datetime(2026, 9, 20, 8, tzinfo=timezone.utc)


def random_item(rng, base=None):
    title = base or rng.choice(PHRASES)
    words = title.split()
    for _ in range(rng.randint(0, 3)):
        operation = rng.random()
        if operation < 0.3 and len(words) > 3:
            words.pop(rng.randrange(len(words)))
        elif operation < 0.6:
            words.insert(rng.randrange(len(words) + 1), rng.choice(["new", "today", "AI", "2026", "update", "发布"]))
        else:
            words[rng.randrange(len(words))] = rng.choice(["launched", "releases", "人工智能", "v2", "Q3", "plan"])
    zh = rng.choice([None, "-", "", "新模型发布 " + rng.choice(["今日", "正式", "3.5"]), rng.choice(PHRASES)])
    title = " ".join(words)
    # How the wires decorate a headline: a CLS dateline, or Sina/WSCN's 【headline】body.
    wrap = rng.random()
    if wrap < 0.25:
        title = f"财联社10月{rng.choice([2, 3])}日电，" + title
    elif wrap < 0.5:
        title = f"【{title}】据报道，{rng.choice(['10月2日', '今日', '截至发稿'])}，" + rng.choice(PHRASES)
    return {
        "title": title, "title_zh": zh,
        "source_type": rng.choice(["rss", "sina", "cls", "wscn_live", "googlenews"]),
        "published_at": (T0 + timedelta(hours=rng.choice([0, 5, 7, 30, 71, 73, 200]) * rng.choice([1, -1]))).isoformat(),
        "companies": json.dumps(rng.choice([[], ["openai"], ["nvidia"], ["openai", "nvidia"]])),
        "channel": rng.choice(["ai", "ai", "stock"]),
        "event_type": rng.choice(["", "other", "earnings", "product"]),
        "official": rng.choice([0, 0, 1]),
        "url": rng.choice(["https://a.example/1", "https://b.example/2", "https://www1.hkexnews.hk/x"]),
        "extra": rng.choice(["{}", '{"form": "8-K"}', "not json"]),
    }


class StoryMatchScoreTests(unittest.TestCase):
    def pairs(self, count, seed):
        rng = random.Random(seed)
        for _ in range(count):
            row = random_item(rng)
            anchor = random_item(rng, base=row["title"] if rng.random() < 0.6 else None)
            yield rng, row, anchor

    def test_cached_facts_reproduce_the_rules_exactly(self):
        compared = matched = by_headline = stock_moves = 0
        for _, row, anchor in self.pairs(4000, 20261005):
            expected = reference_match_score(row, anchor)
            self.assertEqual(match_score(row, anchor), expected)
            compared += 1
            matched += expected >= MATCH_THRESHOLD
            by_headline += _Facts(row).headline == _Facts(anchor).headline and row['title'] != anchor['title']
            stock_moves += stock_move(row) and stock_move(anchor)
        self.assertGreater(matched, compared // 10)  # the generator reaches the threshold
        self.assertGreater(stock_moves, compared // 40)  # and pairs of daily stock-move pieces
        self.assertGreater(by_headline, compared // 40)  # and differently decorated copies of a headline

    def test_pruned_score_decides_like_the_exact_score(self):
        decided = 0
        for rng, row, anchor in self.pairs(4000, 7):
            exact = reference_match_score(row, anchor)
            for beat in (0.0, rng.random(), exact, max(0.0, exact - 1e-9), 0.95):
                pruned = _score(_Facts(row), _Facts(anchor), beat=beat)
                passes = exact >= MATCH_THRESHOLD and exact > beat
                self.assertLessEqual(pruned, exact)
                self.assertEqual(pruned >= MATCH_THRESHOLD and pruned > beat, passes)
                if passes:
                    self.assertEqual(pruned, exact)
                    decided += 1
        self.assertGreater(decided, 1000)


if __name__ == "__main__":
    unittest.main()
