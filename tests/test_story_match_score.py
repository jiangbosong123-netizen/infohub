import json
import random
import re
import unittest
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from app.provenance import object_json
from app.stories import MATCH_THRESHOLD, MAX_EVENT_HOURS, _Facts, _dt, _norm, _score, _titles, match_score


def reference_match_score(row, anchor):
    """titles-v3 match_score exactly as it was before per-item facts were cached."""
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
    return {
        "title": " ".join(words), "title_zh": zh,
        "published_at": (T0 + timedelta(hours=rng.choice([0, 5, 30, 71, 73, 200]) * rng.choice([1, -1]))).isoformat(),
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

    def test_cached_facts_reproduce_the_previous_score_exactly(self):
        compared = matched = 0
        for _, row, anchor in self.pairs(4000, 20261005):
            expected = reference_match_score(row, anchor)
            self.assertEqual(match_score(row, anchor), expected)
            compared += 1
            matched += expected >= MATCH_THRESHOLD
        self.assertGreater(matched, compared // 10)  # the generator reaches the threshold

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
