from __future__ import annotations

"""Keep the publisher separate from the feed used to discover an article."""
import json
import re
from urllib.parse import urlsplit

# Aliases unify direct feeds and Google News attribution without collapsing
# unrelated sites by registrable-domain guesses (e.g. github.io).
PUBLISHERS = {
    'wallstreetcn.com': ('wallstreetcn', '华尔街见闻', ['华尔街见闻', 'wallstreetcn']),
    'cls.cn': ('cls', '财联社', ['财联社']),
    'sina.com.cn': ('sina', '新浪财经', ['新浪财经', 'sina finance']),
    'ithome.com': ('ithome', 'IT之家', ['it之家', 'ithome']),
    'techcrunch.com': ('techcrunch', 'TechCrunch', ['techcrunch']),
    'theverge.com': ('theverge', 'The Verge', ['the verge']),
    'cnbc.com': ('cnbc', 'CNBC', ['cnbc']),
    'openai.com': ('openai', 'OpenAI', ['openai']),
    'anthropic.com': ('anthropic', 'Anthropic', ['anthropic']),
    'sec.gov': ('sec', 'SEC', ['sec']),
    'hkexnews.hk': ('hkex', '港交所', ['港交所', 'hkex']),
}
AGGREGATORS = {'news.google.com', 'techmeme.com', 'www.techmeme.com', 'news.ycombinator.com'}


def object_json(value) -> dict:
    try:
        obj = json.loads(value or '{}') if isinstance(value, str) else value
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def publisher(row) -> tuple[str, str, bool]:
    """Return identity, readable label, and whether attribution is known.

    Unknown Google News publishers never become independent sources merely
    because two company-specific feeds happened to collect the same story.
    """
    extra = object_json(row.get('extra'))
    name = extra.get('publisher')
    name = name.strip() if isinstance(name, str) else ''
    if name:
        normalized = re.sub(r'\s+', ' ', name).casefold()
        for key, label, aliases in PUBLISHERS.values():
            if normalized in aliases or normalized == label.casefold():
                return key, label, True
        return 'publisher:' + normalized, name, True
    host = (urlsplit(row.get('url') or '').hostname or '').lower().removeprefix('www.')
    if not host or host in AGGREGATORS:
        return '', row.get('source_name') or host or '发布方待核实', False
    for domain, (key, label, _) in PUBLISHERS.items():
        if host == domain or host.endswith('.' + domain):
            return key, label, True
    return 'host:' + host, host, True


def display_title(row) -> str:
    translated = row.get('title_zh')
    return translated if translated and translated != '-' else row['title']
