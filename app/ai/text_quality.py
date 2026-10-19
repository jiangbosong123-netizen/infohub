"""Catch garbled model text before it is stored or shown.

GLM-4.6 now and then drops a stray symbol, digit or letter into a sentence and repeats the word
next to it. The 10-19 scan of 121,957 items found about 25 such fields, for example 股价异>异动,
通胀>通胀预期, 台*积电, 核心F核心 and “1恐怖主义1行为. The 10-18 daily report had 霸主# 地位 and
智能(体. These are model errors, not storage errors, and none may reach a page.

``problem`` returns why a text looks garbled, or None. The rules aim at near-zero false alarms on
correct Chinese news text; a flagged field is dropped or replaced by the original text, never
repaired by guessing.
"""
from __future__ import annotations

import re

_HAN = "㐀-䶿一-鿿"
# A markup symbol between two Chinese characters: 霸主# 地位, 异>异动, 台*积电.
_STRAY = re.compile(f"[{_HAN}] ?[#*`~\\\\{{}}<>^] ?[{_HAN}]")
# A word repeated around one stray character: 核心F核心, 委员>委员.
_ECHO = re.compile(f"([{_HAN}]{{2,4}})[A-Za-z0-9#*>)(\\][]\\1")
# Replacement, control and zero-width characters (ZWJ and ZWNJ are left to emoji).
_INVISIBLE = re.compile("[�\x00-\x08\x0b\x0c\x0e-\x1f​⁠﻿]")
# Scripts that never belong in a Chinese title, summary or reason. Katakana ・ and ー are allowed:
# Chinese text writes foreign names as 埃隆・马斯克.
_FOREIGN = re.compile("[Ѐ-ӿ֐-׿؀-ۿऀ-ॿ฀-๿"
                      "가-힯぀-ヺヽ-ヿ]")
_CHAR_LOOP = re.compile(f"([{_HAN}])\\1{{3,}}")
_PHRASE_LOOP = re.compile(f"([{_HAN}]{{2,6}})\\1{{2,}}")
_PAIRS = ("（）", "“”", "《》", "「」", "【】", "()", "[]")


def problem(text: object) -> str | None:
    """Why ``text`` looks garbled ("stray_symbol", "echo", ...), or None when it looks fine."""
    if not isinstance(text, str) or not text:
        return None
    if _INVISIBLE.search(text):
        return "invisible_character"
    if _STRAY.search(text):
        return "stray_symbol"
    if _ECHO.search(text):
        return "echo"
    if _FOREIGN.search(text):
        return "foreign_script"
    if _CHAR_LOOP.search(text) or _PHRASE_LOOP.search(text):
        return "repetition"
    for opening, closing in _PAIRS:
        if text.count(opening) != text.count(closing):
            return "unbalanced_" + opening
    return None


def usable(text: object) -> bool:
    return problem(text) is None
