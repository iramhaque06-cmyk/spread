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
3. **Entity check.** Numbers must not contradict each other. Two markets that
   quote different thresholds are the *same topic*, not the same bet - they
   stay a "possible" match with ``rules_differ`` set, which is how the curated
   CPI pair behaves today.
4. **Date check.** Close dates must be within a window of each other, or one
   of them must be missing.

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

from rapidfuzz import fuzz

ALIAS_PATH = Path(__file__).resolve().parent / "team_aliases.json"

CONFIDENT = 85.0  # at or above: a real match
FLOOR = 70.0  # below this: not even worth storing as possible
MAX_MEMBERS = 10  # a bigger cluster is a warning sign, not an event
DATE_WINDOW_DAYS = 10  # non-sports
SPORTS_DATE_WINDOW_DAYS = 1  # sports, per the brief

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

# Function words only. Direction words (above, below, at least) are kept:
# "above 100" and "below 100" are opposite bets, and dropping them would let
# them match.
STOPWORDS = frozenset(
    """
    a an and are as at be before by did do does for from has have in into is it
    its of on or that the their there these this to was were will with within
    yes no if any about after during between end than then when which who whom
    """.split()
)

_SPORTS_HINT = re.compile(
    r"\b(nfl|nba|mlb|nhl|ncaa|ncaaf|ncaab|wnba|mls|ufc|f1|premier league|"
    r"la liga|serie a|bundesliga|champions league|world cup|playoffs?|"
    r"super bowl|world series|stanley cup|finals?|match|game)\b",
    re.I,
)
_WIN_WORDS = re.compile(r"\b(win|wins|beat|beats|defeat|defeats|advance|"
                        r"progress|qualify|champion|champions)\b", re.I)


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
    members: tuple  # (venue, market_id, outcome, confidence, rules_differ)


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
    aliases = load_aliases() if aliases is None else aliases
    records = canonical_records(records)
    titles = [normalize(_title(item)) for item in records]

    pairs: dict[tuple[int, int], tuple[float, bool]] = {}
    _title_pairs(records, titles, pairs, floor, per_market)

    groups = _components(len(records), pairs, max_members=max_members)
    matches = []
    for members in groups:
        group = [records[index] for index in members]
        scores = [
            pairs[key][0]
            for key in _pairs_within(members)
            if key in pairs
        ]
        confidence = round(min(scores), 2) if scores else 0.0
        rules_differ = any(pairs[key][1] for key in _pairs_within(members) if key in pairs)
        possible = confidence < threshold or len(members) > max_members
        matches.append(_to_match(group, confidence, possible, rules_differ, "title"))

    matches.extend(_sports_matches(records, titles, aliases, threshold, floor))
    return _dedupe(matches)


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


def _title_pairs(records, titles, pairs, floor, per_market) -> None:
    """Score every candidate pair, in a stable order, and keep the real ones."""
    weights, _ = candidate_pairs(records, titles, per_market=per_market)
    for first, second in sorted(weights):
        scored = _score(titles[first], titles[second], records[first], records[second])
        if scored is not None and scored[0] >= floor:
            pairs[(first, second)] = scored


def _score(text_a: str, text_b: str, record_a, record_b):
    """``(score, numbers_disagree)`` for one pair, or ``None`` to reject it."""
    shared = tokens(text_a) & tokens(text_b)
    if len(shared) < 2:
        return None
    score = float(fuzz.token_set_ratio(text_a, text_b))

    gap = _days_apart(_close(record_a), _close(record_b))
    if gap is not None and gap > DATE_WINDOW_DAYS:
        return None

    first, second = numbers(text_a), numbers(text_b)
    disagree = False
    if first and second:
        if first == second:
            score += 5
        else:
            disagree = True
            score = min(score, CONFIDENT - 5)  # possible, never a counted gap

    return (round(min(score, 100.0), 2), disagree)


def _sports_matches(records, titles, aliases, threshold, floor) -> list[Match]:
    """Teams + start date + winner markets only, per the brief's scope."""
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
            members = []
            for other in predictions:
                found = _found_teams(_title(records[other]), aliases)
                if team_key not in found or len(found) != 1:
                    continue
                if not _WIN_WORDS.search(_title(records[other])):
                    continue
                gap = _days_apart(_close(records[position]), _close(records[other]))
                if gap is not None and gap > SPORTS_DATE_WINDOW_DAYS:
                    continue
                score = 90.0 if gap is not None else 82.0
                members.append(
                    (other, score, "teams")
                )
            if not members:
                continue
            chosen = max(members, key=lambda item: item[1])
            confidence = chosen[1]
            group = [records[position], records[chosen[0]]]
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


def _to_match(group, confidence, possible, rules_differ, matched_on, outcomes=None) -> Match:
    outcomes = outcomes or {}
    members = []
    for item in group:
        venue = _venue(item)
        members.append(
            (
                venue,
                str(_field(item, "market_id") or ""),
                outcomes.get(venue, ""),
                confidence,
                1 if rules_differ else 0,
                matched_on,
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
