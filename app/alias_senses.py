"""Company aliases that also name something else (10-20, reported by the AI research system).

A watchlist alias matches as a whole word in any case, so some names caught other things. The
10-19 backup had these:
- AMD linked 49 times to wet or dry AMD, the eye disease (age-related macular degeneration);
- Apple linked 254 times to the fruit (apple pie, orchards, picking), and 207 of those were kept;
- Meta linked to meta-analysis.

``company_sense(alias, text)`` says whether the alias, found in ``text``, means the company. Only
the aliases listed here are checked; any other alias that matches means its company. The company
still matches through another alias, such as AAPL or Tim Cook.
"""
from __future__ import annotations

import re

_WORD = "(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"

# The eye disease: "wet AMD", "dry AMD", nAMD, or retina and macula words anywhere in the text.
_EYE = re.compile(r"\b(?:wet|dry|neovascular|exudative|atrophic)[- ]AMD\b|\bnAMD\b|macula|retina|ophthalm|"
                  r"intravitreal|geographic atrophy", re.IGNORECASE)
# The company is written Apple or APPLE; the fruit is lower case or comes with orchard words.
_APPLE = re.compile(_WORD.format("(?:Apple|APPLE)"))
_FRUIT = re.compile(r"(?<!-to-)\bapples\b(?!-to-)|orchard|cider|crab ?apple|\bBig Apple\b|\bApple Cup\b|"
                    r"\b(?:candy|caramel|toffee) apples?\b|"
                    r"\bapple (?:pie|picking|festival|fest|harvest|tree|variet\w*|grower|crop|juice|butter|"
                    r"sauce|season|farm\w*)s?\b", re.IGNORECASE)  # plurals too: "Fried Apple Pies" (10-20)
# Product names settle it for the company, whatever else the text says (Apple TV+ streaming "It's the
# Great Pumpkin", an iOS story with an apple-picking joke).
_APPLE_PRODUCTS = re.compile(r"\biPhone|\biPad|\bMac(?:Book|OS|s)?\b|\b(?:iOS|watchOS|visionOS)\b|Vision Pro|"
                             r"App Store|AirPods|\bApple (?:Inc|Watch|TV|Music|Pay|Intelligence|stock|shares)|"
                             r"Cupertino|Tim Cook|\$?AAPL\b", re.IGNORECASE)
_FRUIT_ZH = re.compile("苹果(?:期货|园|树|汁|醋|派|种植|采摘|产区|果农)|果园|果农|水果|红富士|烟台苹果|阿克苏|洛川")
# The company is Meta or META, not "meta" and not a Meta-analysis.
_META = re.compile(_WORD.format("(?:Meta|META)") + r"(?!-(?:analys|regress|review))")


def company_sense(alias: str, text: str) -> bool:
    key = alias.casefold()
    if key == "amd":
        return not _EYE.search(text)
    if key == "apple":
        return bool(_APPLE_PRODUCTS.search(text)) or (bool(_APPLE.search(text)) and not _FRUIT.search(text))
    if key == "苹果":
        return bool(_APPLE_PRODUCTS.search(text)) or not _FRUIT_ZH.search(text)
    if key == "meta":
        return bool(_META.search(text))
    return True
