from __future__ import annotations

"""把一条新闻匹配到关注公司（按 watchlist 别名）。ASCII 别名按词匹配，中文按子串匹配。"""
import json
import re
from functools import lru_cache

from .database import get_db


@lru_cache(maxsize=1)
def _load_matchers() -> list[tuple[str, re.Pattern]]:
    """返回 [(company_slug, compiled_pattern)]。公司清单由 init-db 写入 DB，这里加缓存。"""
    matchers = []
    with get_db() as db:
        rows = db.execute("SELECT slug, aliases FROM companies").fetchall()
    for row in rows:
        for alias in json.loads(row["aliases"]):
            if not alias:
                continue
            if alias.startswith("=") and len(alias) > 1:  # 覆盖范围公司的单词简称：区分大小写（D31）
                pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias[1:])}(?![A-Za-z0-9])")
            elif re.fullmatch(r"[\x20-\x7e]+", alias):  # 纯 ASCII → 词边界匹配
                pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
            else:  # 中文等 → 子串匹配
                pattern = re.compile(re.escape(alias), re.IGNORECASE)
            matchers.append((row["slug"], pattern))
    return matchers


@lru_cache(maxsize=1)
def _load_bound_matchers() -> dict[str, list[re.Pattern]]:
    """Coverage companies (D31): in their own feed the short name and the ticker are enough.

    Case-sensitive, so 'ON' the ticker matches but 'on' the word does not; never used to match other
    feeds, where those names are too ambiguous.
    """
    from .coverage import bound_aliases
    with get_db() as db:
        rows = db.execute("SELECT slug, name, ticker FROM companies WHERE origin='coverage'").fetchall()
    return {row["slug"]: [re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])")
                          for name in bound_aliases(row["name"], row["ticker"])]
            for row in rows}


def invalidate_cache() -> None:
    _load_matchers.cache_clear()
    _load_bound_matchers.cache_clear()


def mentions_in_own_feed(slug: str, text: str) -> bool:
    """Whether an entry of ``slug``'s own feed names it, by its usual aliases or its feed-only ones."""
    return slug in match_companies(text) or any(
        pattern.search(text or "") for pattern in _load_bound_matchers().get(slug, []))


def match_companies(text: str) -> list[str]:
    """返回命中的公司 slug 列表（去重、保序）。"""
    if not text:
        return []
    hits: list[str] = []
    for slug, pattern in _load_matchers():
        if pattern.search(text) and slug not in hits:
            hits.append(slug)
    return hits
