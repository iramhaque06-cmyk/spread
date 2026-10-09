"""Group markets about the same real-world event, across venues.

Matching is a judgement, so every judgement here is recorded rather than
hidden: each member carries a confidence and a ``rules_differ`` flag, and a
match below :data:`CONFIDENT` (0.85 on the 0-100 scale) is stored as
**possible** and can never be counted inside a gap (see gaps.py).

How a pair is accepted, in order:

0. **Candidate selection.** Every market is offered the markets it shares a
   token with - all of them for a rare token, only those inside the same
   three-day close window and venue type for a common one - and keeps the ten
   heaviest cross-venue candidates, each shared token weighted by inverse
   document frequency (:func:`candidate_pairs`). Nothing outside that list is
   ever scored.
1. **Cheap gate.** Both titles are normalized (case, punctuation, ``100,000``
   and ``100k`` become one canonical number) and must share at least two
   significant tokens. Nothing else is even scored.
2. **Fuzzy score.** rapidfuzz ``token_set_ratio``: order-insensitive, and it
   forgives one venue adding a clause the other omits, which is exactly how
   these venues word the same question.
3. **Strike check.** The numbers the two markets are actually betting on
   (:func:`strikes`) must be the same set: years, calendar dates and clock
   times are stripped out, because every market in a series carries them and
   they distinguish nothing. ``1600 or higher`` and ``2000 or higher`` are the
   same subject and different bets, so they are refused rather than stored as
   a near-match.
4. **Entity check.** The names a title commits to - teams from the alias
   table, capitalized words, tickers (:func:`entities`) - must overlap. A
   governor race in Iowa is not a governor race in California because the word
   "governor" appears in both.
5. **Direction check.** Above/below, before/after, win/lose and up/down must
   agree (:func:`directions`). One clean exception is the complementary pair:
   "above 100000 on Dec 31" and "below 100000 on Dec 31" are the same event
   from the two sides, so they match with the up side marked ``inverted`` and
   its price read as ``100 - price`` (see gaps.py).
6. **Date check.** Close dates must be within :data:`DATE_WINDOW_DAYS` of each
   other, or one of them must be missing.

One event holds at most one market per venue, enforced in
:func:`_one_market_per_venue` and asserted in :func:`_validate`.

Sports are matched separately and more strictly: the two teams come from an
alias table, the start times must be within a day, and only winner/moneyline
markets take part. Nothing else is attempted yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from rapidfuzz import fuzz

ALIAS_PATH = Path(__file__).resolve().parent / "team_aliases.json"

# Confidence is scored on 0-100. CONFIDENT (85) is the line between a match
# the site may publish and a "possible" one it may not; FLOOR (70) is the line
# below which a pair is not stored at all. Both are parameters of
# match_markets, so a caller can tighten them for a pass of its own.
CONFIDENT = 85.0  # at or above: a real match, publishable with two real-money venues
FLOOR = 70.0  # below this: not even worth storing as possible
MAX_MEMBERS = 10  # a bigger cluster is a warning sign, not an event
DATE_WINDOW_DAYS = 3  # non-sports: two markets about one event close together
SPORTS_DATE_WINDOW_DAYS = 1  # sports, per the brief
# Sports confidence is these three pieces of evidence, added up: the same team
# named on both sides, a start time we can place, and a winner/moneyline market
# rather than a spread. Nothing is hard-coded per match.
SPORTS_TEAM_POINTS = 45
SPORTS_DAY_POINTS = 25
SPORTS_MARKET_POINTS = 20

# ---- Candidate config: which pairs are compared at all ---------------------- #
# Separate from the scoring thresholds above on purpose: this decides who gets
# scored, not who gets matched. The numbers are here, not inline, because they
# are the dial that trades runtime against coverage on a 175k-market pass.
CANDIDATES_PER_MARKET = 10  # K: candidates kept per market, heaviest first
RARE_DF = 300  # at or below this document frequency a token links any two markets
BLOCK_BUCKET_DAYS = 3  # a common token pairs markets only this close together
MAX_BUCKET_PAIRS = 2000  # pairs enumerated per (common token, date bucket)
MAX_BUCKET_LOGS = 20  # capped buckets named one by one, then summarised

LOGGER = logging.getLogger(__name__)

# Function words only. Direction words are deliberately kept: "above 100" and
# "below 100" are opposite bets, and so are "before 2027" and "after 2027",
# so before/after/than/between/yes/no are all meaningful tokens here.
STOPWORDS = frozenset(
    """
    a an and are as at be by did do does for from has have in into is it
    its of on or that the their there these this to was were will with within
    if any about during end when which who whom whose
    """.split()
)

MONTHS = frozenset(
    """
    january february march april may june july august september october november
    december jan feb mar apr jun jul aug sep sept oct nov dec
    """.split()
)
WEEKDAYS = frozenset(
    """
    monday tuesday wednesday thursday friday saturday sunday mon tue tues wed
    thu thur thurs fri sat sun
    """.split()
)
# Units that make a number a quantity rather than a date. A number carries the
# unit it was written with, so "140" and "140 dollars" compare equal while
# "0.7 percent" and "0.7 points" do not.
UNITS = frozenset(
    """
    percent percentage points point dollars dollar usd cents cent bps basis
    degrees degree contracts contract shares share votes vote people persons
    seats seat goals goal runs run points touchdowns yards metres meters miles
    kilometres kilometers billion million trillion thousand seconds minutes
    hours days weeks months years
    """.split()
)
# Suffixes that turn a number into a date or a clock time, not a strike.
DATE_SUFFIXES = frozenset(
    "st nd rd th am pm utc gmt edt est cdt cst mdt mst pdt pst bst cet"
    .split()
)
DATE_CONTEXT = frozenset({"week", "day", "no", "number", "round", "leg"}) | MONTHS | WEEKDAYS

# The same entity, named two ways. Teams come from team_aliases.json
# (load_aliases); this is the much smaller table for everything else that gets
# told apart only by spelling - a ticker against its coin, an abbreviation
# against the body it stands for. Keys are matched on word boundaries,
# longest first, and both sides canonicalize to the same value.
ENTITY_ALIASES = {
    "fed": "federal-reserve",
    "federal reserve": "federal-reserve",
    "fomc": "federal-reserve",
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "xrp": "ripple",
    "doge": "dogecoin",
    "potus": "us-president",
    "president of the united states": "us-president",
    "scotus": "supreme-court",
    "supreme court": "supreme-court",
    "cpi": "consumer-price-index",
    "consumer price index": "consumer-price-index",
    "gop": "republican-party",
    "republican party": "republican-party",
    "republicans": "republican-party",
    "democrats": "democratic-party",
    "democratic party": "democratic-party",
}

# Direction families: a token on the left and a token on the right are
# opposite answers to the same question.
DIRECTION_FAMILIES = (
    (
        "threshold",
        {"above", "over", "more", "higher", "greater", "exceed", "exceeds", "least", "up"},
        {"below", "under", "less", "lower", "most", "down"},
    ),
    ("time", {"before", "prior", "until", "ahead"}, {"after", "since", "following"}),
    (
        "outcome",
        {"win", "wins", "won", "beat", "beats", "defeat", "defeats", "advance", "advances"},
        {"lose", "loses", "lost", "eliminated"},
    ),
    (
        "change",
        {"increase", "increases", "rise", "rises", "hike", "hikes", "gain", "gains"},
        {"decrease", "decreases", "fall", "falls", "cut", "cuts", "drop", "drops"},
    ),
)
# A number written like this is a year, not a strike, unless it carries a unit.
YEAR_RANGE = (1900, 2100)

_SPORTS_HINT = re.compile(
    r"\b(nfl|nba|mlb|nhl|ncaa|ncaaf|ncaab|wnba|mls|ufc|f1|premier league|"
    r"la liga|serie a|bundesliga|champions league|world cup|playoffs?|"
    r"super bowl|world series|stanley cup|finals?|match|game)\b",
    re.I,
)
_WIN_WORDS = re.compile(r"\b(win|wins|beat|beats|defeat|defeats|advance|"
                        r"progress|qualify|champion|champions)\b", re.I)


class Pair(NamedTuple):
    """One scored pair: its score, and what the two sides disagreed about.

    ``rules_differ`` means the two markets answer one question from opposite
    sides, which is where two venues can settle the same event differently.
    ``inverted`` names the side whose price has to be read as ``100 - price``
    before the two can be compared (see gaps.py): 0 or 1, indexing the pair as
    it is keyed, or -1 when neither side is flipped.
    """

    score: float
    rules_differ: bool
    inverted: int


@dataclass(frozen=True)
class Match:
    """One group of markets that look like the same real-world event."""

    event_key: str
    title: str
    category: str
    close_time: str
    confidence: float
    possible: bool
    rules_differ: bool
    matched_on: str  # "title" | "teams"
    # (venue, market_id, outcome, confidence, rules_differ, matched_on, inverted),
    # one per venue, each carrying the weakest pair it was actually scored in.
    members: tuple


# --------------------------------------------------------------------------- #
# Text normalization: two venues never word the same question identically.
# --------------------------------------------------------------------------- #

_DASHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                         "\u2013": "-", "\u2014": "-", "\u2212": "-"})


def normalize(text: str) -> str:
    """A comparable form of a title: numbers canonical, punctuation spaced."""
    text = (text or "").translate(_DASHES).lower()
    text = re.sub(r"(\d+(?:\.\d+)?)\s*k\b", _thousands, text)  # 100k -> 100000
    text = text.replace(",", "")
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"\1 percent", text)
    text = re.sub(r"[^a-z0-9.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _thousands(match: re.Match) -> str:
    return str(int(float(match.group(1)) * 1000))


def tokens(text: str) -> set[str]:
    """Significant words of an already-normalized title."""
    return {
        word
        for word in text.split()
        if word not in STOPWORDS and (len(word) > 2 or word.isdigit())
    }


def numbers(text: str) -> set[str]:
    """Canonical numbers in an already-normalized title (percent -> value)."""
    found = set()
    for value in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            number = float(value)
        except ValueError:
            continue
        found.add(str(int(number)) if number.is_integer() else f"{number:g}")
    return found


# --------------------------------------------------------------------------- #
# What a title commits to: its strikes, its names, its directions.
# --------------------------------------------------------------------------- #

_NUMBER_PREFIX = re.compile(r"(\d+(?:\.\d+)?)([a-z]*)")
_MONTH_WORDS = MONTHS | WEEKDAYS


def _strike_units(text: str) -> dict[str, str]:
    """``value -> unit`` for the numbers a title is actually betting on."""
    words = normalize(text).split()
    found: dict[str, str] = {}
    for index, word in enumerate(words):
        match = _NUMBER_PREFIX.fullmatch(word)
        if match is None:
            continue
        value, suffix = match.group(1), match.group(2)
        following = words[index + 1] if index + 1 < len(words) else ""
        previous = words[index - 1] if index else ""
        unit = suffix if suffix in UNITS else (following if following in UNITS else "")
        if suffix in DATE_SUFFIXES:  # 31st, 1pm, 5utc
            continue
        if previous in DATE_CONTEXT or following in _MONTH_WORDS:  # Oct 7, 7 October
            continue
        if not unit and len(value) == 4 and YEAR_RANGE[0] <= float(value) <= YEAR_RANGE[1]:
            continue  # a bare four-digit number in range is a year
        found.setdefault(value, unit)
    return found


def strikes(text: str) -> frozenset[str]:
    """The numbers a title bets on, with their units, dates stripped out.

    Years (``2026``), calendar dates (``Oct 7``, ``31st``) and clock times are
    removed: every market in a series carries them, so they distinguish
    nothing and must not earn a match. What remains is the part a market can
    be wrong about - the strike, the threshold, the count - each keyed with the
    unit it was written in (``0.7 percent``, ``95750 dollars``).
    """
    return frozenset(
        f"{value} {unit}" if unit else value
        for value, unit in _strike_units(text).items()
    )


def strikes_disagree(first: str, second: str) -> bool:
    """Whether two titles cannot be stating the same bet.

    Values must match exactly; a unit named on both sides must match too, so
    "140" and "140 dollars" agree while "0.7 percent" and "0.7 points" do not.
    """
    left, right = _strike_units(first), _strike_units(second)
    if set(left) != set(right):
        return True
    return any(
        left[value] and right[value] and left[value] != right[value] for value in left
    )


def _default_aliases() -> dict:
    """The team table, read once per process rather than once per title."""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        _ALIAS_CACHE = load_aliases()
    return _ALIAS_CACHE


def entities(text: str, aliases: dict | None = None) -> frozenset[str]:
    """The names a title commits to, canonicalized so two spellings agree.

    Three sources, in order of trust: the team alias table, the entity alias
    table (:data:`ENTITY_ALIASES`, where ``fed`` and ``federal reserve`` become
    one name), and finally capitalization itself - a word the venue capitalized
    is a proper noun, and a short all-caps word is a ticker. Months, weekdays
    and the first word of a title are not names.

    Returns ids like ``name:federal-reserve`` and ``team:new york knicks``.
    """
    aliases = _default_aliases() if aliases is None else aliases
    raw = text or ""
    normalized = normalize(raw)
    found: set[str] = set()
    leftovers = set(normalized.split())

    for key, names in aliases.items():
        for name in (key, *names):
            phrase = normalize(name)
            if phrase and re.search(rf"\b{re.escape(phrase)}\b", normalized):
                found.add(f"team:{key}")
                leftovers -= set(phrase.split())
                break
    for phrase in sorted(ENTITY_ALIASES, key=lambda item: (-len(item.split()), item)):
        if re.search(rf"\b{re.escape(phrase)}\b", normalized):
            found.add(f"name:{ENTITY_ALIASES[phrase]}")
            leftovers -= set(phrase.split())

    for index, word in enumerate(re.findall(r"[A-Za-z][A-Za-z.'-]*", raw)):
        cleaned = word.strip(".'-")
        lowered = normalize(cleaned)
        if not lowered or lowered in STOPWORDS or lowered in _MONTH_WORDS:
            continue
        canonical = ENTITY_ALIASES.get(lowered, lowered)
        if cleaned.isupper() and 2 <= len(cleaned) <= 6:  # BTC, NVDA, SOL
            found.add(f"name:{canonical}")
        elif index > 0 and cleaned[:1].isupper() and lowered in leftovers:
            found.add(f"name:{canonical}")
    return frozenset(found)


def _entities_overlap(first: frozenset, second: frozenset) -> bool:
    """Names must overlap - unless one side names nothing at all.

    A title with no recognizable names ("Will the price of gold rise?") gets
    no vote rather than a veto: this check exists to keep two differently named
    things apart, not to insist that every title is full of names.
    """
    if not first or not second:
        return True
    return bool(first & second)


def directions(text: str) -> dict[str, str]:
    """Which way each direction family points in a title.

    ``{"threshold": "up"}`` for "above 140", ``{"time": "after"}`` for "after
    2027". A family named both ways ("rise or fall") is ``mixed``, which
    matches nothing. A family only one side names proves nothing and is left
    out of the comparison.
    """
    words = set(normalize(text).split())
    found: dict[str, str] = {}
    for family, up, down in DIRECTION_FAMILIES:
        has_up, has_down = bool(words & up), bool(words & down)
        if has_up and has_down:
            found[family] = "mixed"
        elif has_up:
            found[family] = "up"
        elif has_down:
            found[family] = "down"
    return found


def directions_disagree(first: str, second: str) -> tuple[bool, bool]:
    """``(disagree, clean_inverse)`` for two titles' directions.

    A clean inverse is the one disagreement that is still one event: the
    threshold family pointing up on one side and down on the other, with
    nothing else disagreeing. "above 100000 on Dec 31" and "below 100000 on
    Dec 31" are the same bet read from the two sides, so the pair matches and
    the up side is marked inverted.
    """
    left, right = directions(first), directions(second)
    disagreeing = sorted(
        family for family in set(left) & set(right) if left[family] != right[family]
    )
    if not disagreeing:
        return False, False
    invertible = disagreeing == ["threshold"] and {
        left["threshold"],
        right["threshold"],
    } == {"up", "down"}
    return True, invertible


def _parse_date(value: str):
    if not value or value in ("NA", "na"):
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _days_apart(a, b) -> float | None:
    first, second = _parse_date(a), _parse_date(b)
    if first is None or second is None:
        return None
    return abs((first - second).total_seconds()) / 86400


# --------------------------------------------------------------------------- #
# Team aliases (sports)
# --------------------------------------------------------------------------- #


def load_aliases(path: Path | str = ALIAS_PATH) -> dict[str, tuple[str, ...]]:
    """Canonical team -> every spelling seen in a market title."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    teams = data.get("teams") or {}
    return {
        key: tuple(str(alias).lower() for alias in aliases)
        for key, aliases in teams.items()
    }


def _canonical_team(name: str, aliases: dict[str, tuple[str, ...]]) -> str | None:
    """The canonical key behind a team name written any way at all."""
    found = _found_teams(name, aliases)
    return next(iter(found)) if len(found) == 1 else None


_ALIAS_CACHE: dict | None = None


def _found_teams(title: str, aliases: dict[str, tuple[str, ...]]) -> set[str]:
    """Canonical teams named in a title, matched on word boundaries."""
    normalized = normalize(title)
    found = set()
    for key, names in aliases.items():
        for name in (key, *names):
            if re.search(rf"\b{re.escape(normalize(name))}\b", normalized):
                found.add(key)
                break
    return found


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #


def match_markets(
    records,
    *,
    aliases: dict[str, tuple[str, ...]] | None = None,
    threshold: float = CONFIDENT,
    floor: float = FLOOR,
    per_market: int = CANDIDATES_PER_MARKET,
    max_members: int = MAX_MEMBERS,
) -> list[Match]:
    """Match every open market against the others and return event groups.

    ``records`` may be :class:`venues.MarketRecord` objects or stored rows;
    both expose the same field names. The input order does not matter: the
    pass works on :func:`canonical_records`, so a shuffled feed and a shuffled
    interpreter both give the same events.
    """
    aliases = _default_aliases() if aliases is None else aliases
    records = canonical_records(records)
    titles = [normalize(_title(item)) for item in records]
    raw = [_title(item) for item in records]
    venues = [_venue(item) for item in records]

    pairs: dict[tuple[int, int], Pair] = {}
    _title_pairs(records, raw, titles, pairs, floor, per_market, aliases)

    matches = []
    for group in _components(len(records), pairs, max_members=max_members):
        members = _one_market_per_venue(group, pairs, venues)
        members = _connected(members, pairs)
        if len(members) < 2:
            continue
        evidence = _member_evidence(members, pairs)
        confidence = round(min(row[0] for row in evidence), 2)
        matches.append(
            _to_match(
                [records[index] for index in members],
                confidence,
                confidence < threshold or len(members) > max_members,
                any(row[1] for row in evidence),
                "title",
                evidence=evidence,
            )
        )

    matches.extend(_sports_matches(records, raw, aliases, threshold))
    matches = _dedupe(matches)
    _validate(matches)
    return matches


# --------------------------------------------------------------------------- #
# Candidate selection: which pairs are even worth scoring.
# --------------------------------------------------------------------------- #


def record_key(record) -> tuple[str, str]:
    """How one market is named, everywhere: ``(venue, market_id)``."""
    return (_venue(record), str(_field(record, "market_id") or ""))


def canonical_records(records) -> list:
    """The one order the whole pass depends on: ``(venue, market_id)``, combos out.

    Everything downstream names a market by its position here, so this sort is
    what makes a shuffled feed produce the same candidates and the same
    events. Combos are dropped rather than scored: a parlay bundles other
    markets, so any pair it joined would compare a bundle with a single
    question.
    """
    return sorted(
        (item for item in records if _title(item) and not _is_combo(item)),
        key=record_key,
    )


def _is_combo(record) -> bool:
    """Whether the venue flagged this market as a parlay/combo.

    Read defensively, because the field is optional: Kalshi drops combos at the
    source (``mve_filter=exclude``), so today this is False for every row the
    catalog can produce, and the guard is what stops a venue's parlays from
    ever being matched against single questions.
    """
    value = _field(record, "is_combo")
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no")
    return bool(value)


def _idf(document_frequency: int, total: int) -> float:
    """Inverse document frequency, smoothed so a universal token still weighs.

    Plain ``log(total / df)`` is exactly zero for a token every market holds,
    which is the weight the brief's Fed/Bitcoin/Trump tokens would get - and a
    zero weight cannot put two markets in one candidate list. The ``1 +``
    keeps every shared token worth something while a token only two markets
    hold still outweighs a common one many times over.
    """
    return math.log(1.0 + total / max(document_frequency, 1))


def _date_bucket(record, days: int = BLOCK_BUCKET_DAYS) -> int | None:
    """Which ``days``-wide close-date window a market falls in, or ``None``.

    Two markets in the same bucket close within ``days`` of each other, which
    is the evidence a common token needs before it counts as a link.
    """
    moment = _parse_date(_close(record))
    if moment is None:
        return None
    return int(moment.timestamp() // (days * 86400))


def bucket_pairs(group, cap: int) -> tuple[dict, int]:
    """Pairs inside one bucket, nearest market ids first, at most `cap`.

    Enumerating by distance rather than by a prefix is the point: a capped
    bucket still gives every market neighbours, where spending the whole cap
    on the alphabetically first markets is the very failure this candidate
    pass exists to fix. Returns the partner lists and the pair count.
    """
    partners: dict[int, list[int]] = defaultdict(list)
    made = 0
    for width in range(1, len(group)):
        for index in range(len(group) - width):
            if made >= cap:
                return partners, made
            first, second = group[index], group[index + width]
            partners[first].append(second)
            partners[second].append(first)
            made += 1
    return partners, made


def _log_capped(capped: list[str]) -> None:
    """Say which buckets were cut short; a silent cap hides lost comparisons."""
    for line in capped[:MAX_BUCKET_LOGS]:
        LOGGER.warning("candidate bucket capped: %s", line)
    if len(capped) > MAX_BUCKET_LOGS:
        LOGGER.warning(
            "candidate buckets capped: %d more kept open uncompared",
            len(capped) - MAX_BUCKET_LOGS,
        )


def candidate_pairs(
    records,
    titles=None,
    *,
    per_market: int = CANDIDATES_PER_MARKET,
    rare_df: int = RARE_DF,
    bucket_days: int = BLOCK_BUCKET_DAYS,
    max_bucket_pairs: int = MAX_BUCKET_PAIRS,
) -> tuple[dict, dict]:
    """Weighted cross-venue candidate pairs, ranked per market.

    Every token a market holds offers it candidates, and each shared token
    contributes :func:`_idf`. A rare token (``df <= rare_df``) offers every
    market that holds it, with no positional cut; a common one offers only
    markets in the same (close-date window, venue_type) bucket, so "trump"
    cannot drag the whole catalog into one comparison set. Markets without a
    close date take no common-token links at all: nothing proves they sit in
    the same window. Each market then keeps its ``per_market`` heaviest
    cross-venue candidates, ties broken by ``(venue, market_id)``.

    `records` must already be in :func:`canonical_records` order, because the
    returned keys are positions into it. Returns ``(weights, stats)`` where
    ``weights`` maps ``(i, j)`` with ``i < j`` to the pair's token weight.
    """
    if titles is None:
        titles = [normalize(_title(item)) for item in records]
    total = len(records)
    stats = {
        "records": total,
        "rare_tokens": 0,
        "common_tokens": 0,
        "bucket_pairs": 0,
        "capped_buckets": 0,
        "candidates": 0,
        "pairs": 0,
    }
    if total < 2:
        return {}, stats

    per_record = []
    document_frequency: Counter = Counter()
    for title in titles:
        found = tokens(title)
        per_record.append(found)
        document_frequency.update(found)
    if not document_frequency:
        return {}, stats

    buckets = [
        (_field(item, "venue_type") or "", _date_bucket(item, bucket_days))
        for item in records
    ]
    venues = [_venue(item) for item in records]

    # Tokens are iterated in sorted order because they are sets: the index must
    # not depend on how the interpreter happened to hash a word this run.
    rare_index: dict[str, list[int]] = defaultdict(list)
    common_index: dict[tuple, list[int]] = defaultdict(list)
    for position, found in enumerate(per_record):
        for token in sorted(found):
            if document_frequency[token] <= rare_df:
                rare_index[token].append(position)
            elif buckets[position][1] is not None:
                common_index[(token, buckets[position])].append(position)
    stats["rare_tokens"] = len(rare_index)
    stats["common_tokens"] = len({token for token, _ in common_index})

    # One enumeration per (token, bucket), not one per market that holds it.
    common_partners: dict[tuple, dict[int, list[int]]] = {}
    capped: list[str] = []
    for key in sorted(common_index, key=lambda item: (item[0], str(item[1]))):
        group = common_index[key]
        if len(group) < 2:
            continue
        partners, made = bucket_pairs(group, max_bucket_pairs)
        common_partners[key] = partners
        stats["bucket_pairs"] += made
        possible = len(group) * (len(group) - 1) // 2
        if made < possible:
            stats["capped_buckets"] += 1
            capped.append(
                f"token {key[0]!r} in {key[1][0] or 'unknown type'} "
                f"closing {key[1][1]}: {len(group)} markets, "
                f"kept {made} of {possible} pairs"
            )

    weights: dict[tuple[int, int], float] = {}
    for position, found in enumerate(per_record):
        totals: dict[int, float] = defaultdict(float)
        for token in sorted(found):
            document_frequency_of = document_frequency[token]
            weight = _idf(document_frequency_of, total)
            if document_frequency_of <= rare_df:
                others = rare_index[token]
            else:
                others = common_partners.get(
                    (token, buckets[position]), {}
                ).get(position, ())
            for other in others:
                if other != position:
                    totals[other] += weight
        ranked = sorted(
            (other for other in totals if venues[other] != venues[position]),
            key=lambda other: (-totals[other], record_key(records[other])),
        )
        for other in ranked[:per_market]:
            stats["candidates"] += 1
            weights[(min(position, other), max(position, other))] = totals[other]

    stats["pairs"] = len(weights)
    _log_capped(capped)
    return weights, stats


def _title_pairs(records, raw, titles, pairs, floor, per_market, aliases) -> None:
    """Score every candidate pair, in a stable order, and keep the real ones."""
    weights, _ = candidate_pairs(records, titles, per_market=per_market)
    for first, second in sorted(weights):
        scored = _score(
            titles[first],
            titles[second],
            records[first],
            records[second],
            raw_a=raw[first],
            raw_b=raw[second],
            aliases=aliases,
        )
        if scored is not None and scored.score >= floor:
            pairs[(first, second)] = scored


def _member_evidence(members, pairs) -> list[tuple[float, bool, bool]]:
    """Per member: its own weakest scored pair, and what that pair disagreed on.

    The pass used to stamp an event's number on every member, so a market that
    only just cleared the floor looked exactly as certain as the pair holding
    the group together. A member now carries the minimum over the pairs it was
    actually scored in, which are cross-venue by construction.

    ``inverted`` is per member, not per pair: in "above 100" against "below
    100" only the up side's price has to be turned around, and marking both
    sides would flip the comparison twice (see gaps.py).
    """
    evidence = []
    for position in members:
        incident = [
            (key, pairs[key])
            for key in _pairs_within(members)
            if position in key and key in pairs
        ]
        evidence.append(
            (
                round(min(pair.score for _, pair in incident), 2),
                any(pair.rules_differ for _, pair in incident),
                any(
                    pair.inverted >= 0 and key[pair.inverted] == position
                    for key, pair in incident
                ),
            )
        )
    return evidence


def _one_market_per_venue(group, pairs, venues) -> list[int]:
    """One market per venue: that venue's best-connected market keeps its place.

    Two markets of the same venue in one event are two different questions - a
    run of brackets, or a venue's own outcomes - and storing them together
    invents a comparison nobody made. The market with the strongest total
    score against the other venues' members stays; the rest go back to being
    unmatched, and a group that shrinks below two is dropped.
    """
    chosen: list[int] = []
    for venue in sorted({venues[position] for position in group}):
        options = [position for position in group if venues[position] == venue]
        if len(options) == 1:
            chosen.extend(options)
            continue
        cross_venue = [
            key
            for key in _pairs_within(group)
            if venues[key[0]] != venue and venues[key[1]] != venue
        ]
        chosen.append(
            max(
                sorted(options),
                key=lambda position: (
                    sum(
                        pairs[key].score
                        for key in cross_venue
                        if position in key and key in pairs
                    ),
                    -position,
                ),
            )
        )
    return sorted(chosen)


def _connected(members, pairs) -> list[int]:
    """Drop any member left with no scored pair inside the group.

    Moving a same-venue market out can leave the rest with nothing tying them
    together; an event of unconnected markets would claim a relationship no
    pair ever scored.
    """
    kept = list(members)
    while len(kept) >= 2:
        remaining = [
            position
            for position in kept
            if any(key in pairs for key in _pairs_within(kept) if position in key)
        ]
        if len(remaining) == len(kept):
            return kept
        kept = remaining
    return []


def _validate(matches) -> None:
    """The invariant a stored event must never break, asserted not assumed.

    One market per venue is the point of an event: two markets from one venue
    are two questions, and a gap across them would compare a venue with
    itself. The pass above enforces it; this checks it, so a future change
    that breaks it fails here instead of on the site.
    """
    for match in matches:
        venues = [member[0] for member in match.members]
        assert len(set(venues)) == len(venues), (
            f"{match.event_key}: a venue appears more than once: {venues}"
        )
        assert len(match.members) >= 2, f"{match.event_key}: an event needs two markets"


def _score(
    text_a: str,
    text_b: str,
    record_a,
    record_b,
    *,
    raw_a: str | None = None,
    raw_b: str | None = None,
    aliases: dict | None = None,
) -> Pair | None:
    """``Pair`` for one candidate, or ``None`` to reject it.

    The gates run cheapest first and each one is decisive: a title that fails
    the strike, entity or direction check is not a weaker match, it is a
    different question, and storing it as "possible" only moves the mistake
    into the export.
    """
    if len(tokens(text_a) & tokens(text_b)) < 2:
        return None
    gap = _days_apart(_close(record_a), _close(record_b))
    if gap is not None and gap > DATE_WINDOW_DAYS:
        return None

    if strikes_disagree(text_a, text_b):
        return None
    disagree, invertible = directions_disagree(text_a, text_b)
    if disagree and not invertible:
        return None
    if invertible and not _same_instant(_close(record_a), _close(record_b)):
        return None  # an inverse needs the same moment, not merely a close one
    if not _entities_overlap(
        entities(raw_a or text_a, aliases), entities(raw_b or text_b, aliases)
    ):
        return None

    score = float(fuzz.token_set_ratio(text_a, text_b))
    if _strike_units(text_a):  # the same numbers, stated independently
        score += 5
    return Pair(
        score=round(min(score, 100.0), 2),
        # Opposite sides of one question is exactly where two venues can settle
        # differently, so the pair carries the caveat rather than hiding it.
        rules_differ=invertible,
        # Which side of the pair is the "up" one: that is the side whose yes
        # means the opposite of the other's, so its price is the one that has
        # to be turned around before the two can be compared.
        inverted=0 if invertible and directions(text_a)["threshold"] == "up" else
        (1 if invertible else -1),
    )


def _same_instant(first: str, second: str) -> bool:
    """Whether two close stamps name the same moment (unknown counts as same)."""
    gap = _days_apart(first, second)
    return gap is None or gap == 0.0


def _sports_matches(records, raw, aliases, threshold) -> list[Match]:
    """Teams + start date + winner markets only, per the brief's scope.

    Confidence here is the evidence itself, not a magic number: naming the
    same team, playing on a day we can place, and being a winner market rather
    than a spread or a total. A market that quotes a strike ("win by 5+ ",
    "score 120 points") is a different question and is left out.
    """
    sportsbook, predictions = [], []
    for position, item in enumerate(records):
        if _is_sportsbook(item):
            sportsbook.append(position)
        elif _found_teams(_title(item), aliases):
            predictions.append(position)
    if not sportsbook or not predictions:
        return []

    matches = []
    for position in sportsbook:
        away, home = _teams_in_title(_title(records[position]))
        for team in (home, away):
            team_key = _canonical_team(team, aliases) if team else None
            if team_key is None:
                continue
            candidates = []
            for other in predictions:
                found = _found_teams(_title(records[other]), aliases)
                if team_key not in found or len(found) != 1:
                    continue
                if not _WIN_WORDS.search(_title(records[other])):
                    continue
                if strikes(raw[other]):  # a spread or a total, not the moneyline
                    continue
                gap = _days_apart(_close(records[position]), _close(records[other]))
                if gap is not None and gap > SPORTS_DATE_WINDOW_DAYS:
                    continue
                # Same team + a winner market, and the day when we know it.
                confidence = SPORTS_TEAM_POINTS + SPORTS_MARKET_POINTS
                if gap is not None:
                    confidence += SPORTS_DAY_POINTS
                candidates.append((other, confidence))
            if not candidates:
                continue
            # Every candidate carries the same evidence, so the first in
            # canonical order wins and the choice is reproducible.
            chosen, confidence = min(candidates)
            group = [records[position], records[chosen]]
            matches.append(
                _to_match(
                    group,
                    confidence,
                    confidence < threshold,
                    False,
                    "teams",
                    outcomes={_venue(records[position]): team},
                )
            )
    return matches


def _is_sportsbook(record) -> bool:
    return _field(record, "venue_type") == "sportsbook"


def _teams_in_title(title: str) -> tuple[str, str]:
    """The ``away @ home`` pair a sportsbook market is titled with."""
    away, _, home = (title or "").partition(" @ ")
    return away.strip(), home.strip()


def _to_match(
    group, confidence, possible, rules_differ, matched_on, outcomes=None, evidence=None
) -> Match:
    """One event row, with each member carrying its own scored evidence.

    ``evidence`` runs parallel to ``group``: ``(confidence, rules_differ,
    inverted)`` per member. Without it (the sports path) every member shares
    the group's number and is never inverted.
    """
    outcomes = outcomes or {}
    members = []
    for index, item in enumerate(group):
        venue = _venue(item)
        own = evidence[index] if evidence else (confidence, rules_differ, False)
        members.append(
            (
                venue,
                str(_field(item, "market_id") or ""),
                outcomes.get(venue, ""),
                own[0],
                1 if own[1] else 0,
                matched_on,
                1 if own[2] else 0,
            )
        )
    members.sort()
    key = hashlib.sha1("|".join(f"{m[0]}:{m[1]}" for m in members).encode()).hexdigest()[:12]
    titles = [_title(item) for item in group]
    return Match(
        event_key=f"ev-{key}",
        title=max(titles, key=len) if titles else "",
        category=_field(group[0], "category") or "",
        close_time=min((_close(item) for item in group if _close(item)), default=""),
        confidence=round(float(confidence), 2),
        possible=bool(possible),
        rules_differ=bool(rules_differ),
        matched_on=matched_on,
        members=tuple(members),
    )


def _components(size: int, pairs: dict, *, max_members: int = MAX_MEMBERS) -> list[list[int]]:
    """Group markets with a greedy maximum-spanning forest.

    Plain connected components are not safe here: A-on-Kalshi matching
    B-on-Manifold and B matching C-on-Kalshi is enough to merge A, B and C
    through transitivity, and across 175,000 markets that snowballs into
    hundred-market blobs. Edges are therefore added strongest-first, and an
    edge is skipped when merging would push a group past ``max_members``.
    A three-venue event still forms; a generic phrase no longer does.
    """
    parent = list(range(size))
    weight = [1] * size

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for (first, second), _ in sorted(pairs.items(), key=lambda item: -item[1][0]):
        root_a, root_b = find(first), find(second)
        if root_a == root_b or weight[root_a] + weight[root_b] > max_members:
            continue
        parent[root_b] = root_a
        weight[root_a] += weight[root_b]

    grouped: dict[int, list[int]] = defaultdict(list)
    for node in range(size):
        grouped[find(node)].append(node)
    return [members for members in grouped.values() if len(members) > 1]


def _pairs_within(members) -> list[tuple[int, int]]:
    ordered = sorted(members)
    return [
        (ordered[i], ordered[j])
        for i in range(len(ordered))
        for j in range(i + 1, len(ordered))
    ]


def _dedupe(matches: list[Match]) -> list[Match]:
    """One event per set of markets; the strongest statement wins."""
    best: dict[frozenset, Match] = {}
    for match in matches:
        key = frozenset((member[0], member[1]) for member in match.members)
        if key not in best or match.confidence > best[key].confidence:
            best[key] = match
    return sorted(best.values(), key=lambda item: (-item.confidence, item.event_key))


def _title(record) -> str:
    return str(_field(record, "title") or "")


def _close(record) -> str:
    return str(_field(record, "close_time") or "")


def _venue(record) -> str:
    return str(_field(record, "venue") or "")


def _field(record, name: str):
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)
