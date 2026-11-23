"""Map a market to one canonical category.

Categories matter for signal 7 (category prior): in some markets a small group of
people knows the answer before the public does (corporate announcements, awards,
personnel decisions, government or military actions); in others the answer is
revealed publicly and simultaneously (a game's final whistle, a price print).

Rules are plain keyword lists checked in order — first match wins. They take the
venue's own tags (if any) plus the question text, so they work for operator data too.
"""

from __future__ import annotations

import re

CATEGORIES = [
    "personnel",            # nominations, appointments, resignations, firings
    "corporate",            # company announcements, CEOs, IPOs, launches, deals
    "awards_entertainment", # awards, entertainment, celebrity, media mentions
    "government_military",  # military action, diplomacy, executive/legislative/court actions
    "elections",
    "economics_data",       # central-bank decisions, macro data releases
    "sports",
    "crypto_price",
    "price_threshold",      # stock / commodity / index price above-below questions
    "public_metric",        # publicly observable counts/rankings (tweet counts, case counts, leaderboards)
    "other",
]

# (category, tags that imply it, regex on the lower-cased question)
_RULES: list[tuple[str, set[str], str | None]] = [
    ("sports", {"sports", "nba", "nfl", "nhl", "mlb", "soccer", "football", "tennis", "f1",
                "formula1", "ufc", "mma", "boxing", "golf", "cricket", "esports", "ncaa",
                "cbb", "cfb", "epl", "champions-league", "basketball", "baseball", "hockey",
                "olympics", "chess"},
     r"\b(vs\.?|o/u|spread|moneyline|grand prix|match|game \d)\b"),
    ("crypto_price", set(),
     r"(\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge)\b.*(up or down|above|below|price|reach|hit|dip|between|less than|more than|\$))"
     r"|(price of (bitcoin|btc|ethereum|eth|solana|sol|xrp|doge)\b)"),
    ("price_threshold", set(),
     r"(up or down|close (above|below|at)|(above|below|reach|hit|dip to) \$|price of|\bat or above\b|finish (above|below))"),
    ("personnel", set(),
     r"\b(nominat\w*|appoint\w*|resign\w*|fired|step down|steps down|out as|next ceo|new ceo|"
     r"ceo of|chair(man)?|cabinet|secretary|confirm(ed)? as|leave office|replace[ds]?|hire[ds]?|"
     r"sign with|rejoins?|successor|coach)\b"),
    ("public_metric", set(),
     r"(\d+\s*[-–]\s*\d+\+? (tweets|posts|times)|post \d+.*tweets|tweet \d+|how many|number of|"
     r"\bcases\b|index be|ai lab|ai model|text arena|lmarena|leaderboard|\btransit\b|median home|"
     r"gas price|temperature|#1 on|top spot|ranking)"),
    ("awards_entertainment", {"awards", "oscars", "grammys", "emmys", "culture", "pop-culture",
                              "movies", "music", "celebrities", "tv", "mentions", "youtube",
                              "tweets-markets", "box-office"},
     r"\b(award|oscar|grammy|emmy|golden globe|nobel|ballon d'or|person of the year|album|"
     r"song|movie|box office|billboard|spotify|netflix|say|mention|tweet|post)\b"),
    ("elections", {"elections", "global-elections", "world-elections", "us-elections", "primaries"},
     r"\b(election|elected|primary|mayor|governor|senate race|runoff|referendum|vote share)\b"),
    ("economics_data", {"fed", "fed-rates", "economy", "inflation", "cpi", "gdp", "jobs-report",
                        "economic-policy"},
     r"\b(fed|fomc|interest rate|rate cut|rate hike|bps|cpi|inflation|gdp|unemployment|"
     r"jobs report|payrolls|recession)\b"),
    ("government_military", {"geopolitics", "military", "war", "ukraine", "israel", "iran",
                             "middle-east", "ceasefire", "trump-presidency", "politics",
                             "courts", "scotus", "congress", "world", "diplomacy-ceasefire"},
     r"\b(strike|attack|invade|invasion|capture|ceasefire|peace deal|sanction|tariff|"
     r"executive order|bill|law|supreme court|ruling|shutdown|treaty|deal|summit|meet with|"
     r"military|troops|missile|war|pardon|indict\w*|ban)\b"),
    ("corporate", {"business", "tech", "companies", "ai", "stocks", "ipos", "earnings",
                   "big-tech", "economy"},
     r"\b(ipo|ceo|earnings|revenue|acquire|acquisition|merger|launch|release|announce|"
     r"layoffs|valuation|listed on|listing|token launch|openai|anthropic|nvidia|apple|google|tesla|microsoft|meta|amazon)\b"),
]


def categorize(question: str, tags: list[str] | None = None) -> str:
    q = (question or "").lower()
    tagset = {t.lower() for t in (tags or [])}
    # Pass 1: question-text rules (most specific signal about what the market asks).
    for cat, _tags, pattern in _RULES:
        if pattern and re.search(pattern, q):
            # Sports keyword like "vs" is only trusted if a sports tag is also present
            # or there is no conflicting non-sports tag.
            if cat == "sports" and tagset and not (tagset & _RULES[0][1]):
                continue
            # Words like "deal" or "ban" are ambiguous; a business/tech tag plus a
            # corporate keyword means it is a company question.
            if cat == "government_military":
                _, corp_tags, corp_pat = _RULES[-1]
                if tagset & corp_tags and re.search(corp_pat, q):
                    return "corporate"
            return cat
    # Pass 2: tags alone.
    for cat, tag_hints, _ in _RULES:
        if tagset & tag_hints:
            return cat
    return "other"
