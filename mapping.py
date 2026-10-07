"""Curated event mapping: which market on each venue is the same real event.

``mapping.json`` is hand-checked — this module only reads it. rapidfuzz
powers :func:`suggest`, which offers candidate markets while you are
curating; nothing it returns is ever written back automatically.

The spread is always **Kalshi cents minus Polymarket cents**, and is
``None`` whenever either side is missing, so a one-sided event can never
masquerade as a zero gap. It is also ``None`` whenever either side's price
is stale: :func:`gap_for_pair` refuses to compare a price the venues cannot
show to be fresher than 24 hours old.

Each entry also carries a hand-checked one-line summary of both venues'
resolution rules and a ``rules_differ`` flag, because two markets that look
like the same event can still settle on different numbers. :func:`rule_drift`
re-checks the curated wording against the rule text the venues are serving
right now, so a rule change shows up as a warning instead of as a silently
wrong page.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from rapidfuzz import fuzz, process

import freshness
from venues import DEFAULT_MAX_AGE, Quote, kalshi, polymarket

MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"
VENUES = ("kalshi", "polymarket")
FIELDS = (
    "event_key",
    "title",
    "category",
    "kalshi",
    "polymarket",
    "note",
    "kalshi_rule",
    "polymarket_rule",
    "rules_differ",
    "rules_note",
    "kalshi_rule_terms",
    "polymarket_rule_terms",
)

FETCHERS = {"kalshi": kalshi.fetch_quote, "polymarket": polymarket.fetch_quote}


@dataclass(frozen=True)
class EventPair:
    """Two venues' quotes for one event; either side may be missing."""

    event_key: str
    title: str
    quotes: dict  # venue -> Quote | None
    spread_points: float | None  # Kalshi cents - Polymarket cents
    rule_notes: tuple[str, ...] = ()  # curated rule text the live rules no longer match

    @property
    def missing(self) -> tuple[str, ...]:
        """Venues with no quote, so callers can explain a missing spread."""
        return tuple(venue for venue in VENUES if self.quotes.get(venue) is None)


def load(path: Path | str = MAPPING_PATH) -> list[dict]:
    """Read the curated mapping, rejecting entries a typo would break later."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("mapping must be a JSON array of event entries")
    for number, entry in enumerate(entries, start=1):
        for field in ("event_key", "title"):
            if not entry.get(field):
                raise ValueError(f"mapping entry {number} has no {field!r}")
        if not any(entry.get(venue) for venue in VENUES):
            raise ValueError(
                f"mapping entry {number} ({entry.get('event_key')}) "
                "names no market on either venue"
            )
        unknown = set(entry) - set(FIELDS)
        if unknown:
            raise ValueError(
                f"mapping entry {number} has unknown fields: {sorted(unknown)}"
            )
        for venue in VENUES:
            if entry.get(venue) and not entry.get(f"{venue}_rule"):
                raise ValueError(
                    f"mapping entry {number} ({entry.get('event_key')}) names a "
                    f"{venue} market but has no {venue}_rule one-liner"
                )
        if all(entry.get(venue) for venue in VENUES):
            if not isinstance(entry.get("rules_differ"), bool):
                raise ValueError(
                    f"mapping entry {number} ({entry.get('event_key')}) pairs both "
                    "venues but has no true/false rules_differ"
                )
    return entries


def spread_between(
    kalshi_cents: float | None, polymarket_cents: float | None
) -> float | None:
    """Kalshi cents minus Polymarket cents; None if either side is missing."""
    if kalshi_cents is None or polymarket_cents is None:
        return None
    return round(kalshi_cents - polymarket_cents, 2)


def pair(entry: dict, quotes: dict) -> EventPair:
    """Combine a mapping entry with the quotes fetched for it."""
    kalshi_quote = quotes.get("kalshi")
    polymarket_quote = quotes.get("polymarket")
    return EventPair(
        event_key=entry["event_key"],
        title=entry["title"],
        quotes=quotes,
        spread_points=spread_between(
            kalshi_quote.price_cents if kalshi_quote else None,
            polymarket_quote.price_cents if polymarket_quote else None,
        ),
        rule_notes=tuple(rule_drift(entry, quotes)),
    )


def gap_for_pair(pair: EventPair, *, now=None) -> float | None:
    """The comparable gap, or ``None`` when a stale price makes it meaningless.

    The arithmetic itself is still :func:`spread_between`; this only decides
    whether both sides are fresh enough to be worth comparing at all.
    """
    for venue in VENUES:
        quote = pair.quotes.get(venue)
        if quote is None or not freshness.assess(quote, now=now).fresh:
            return None
    return pair.spread_points


def rule_drift(entry: dict, quotes: dict) -> list[str]:
    """Warn when a venue's live rule text no longer contains the curated terms.

    ``{venue}_rule_terms`` names the numbers a curated one-liner turns on -
    a 0.7%, a 100,000. If the venue stops serving them, the one-liner is out
    of date and the page must not keep asserting it silently. An entry with
    no terms is reported as unchecked rather than as verified.
    """
    notes = []
    for venue in VENUES:
        if not entry.get(venue):
            continue
        quote = quotes.get(venue)
        text = (quote.rules_text if quote else "") or ""
        terms = entry.get(f"{venue}_rule_terms") or []
        if not text:
            notes.append(f"{venue}: no live rule text available to check against")
            continue
        if not terms:
            notes.append(f"{venue}: rule wording is not machine-checked")
            continue
        missing = [term for term in terms if term.lower() not in text.lower()]
        if missing:
            notes.append(
                f"{venue}: live rules no longer mention "
                + ", ".join(repr(term) for term in missing)
            )
    return notes


def fetch_pairs(
    entries: list[dict] | None = None, *, max_age: int = DEFAULT_MAX_AGE
) -> list[EventPair]:
    """Fetch every mapped market and pair the quotes event by event.

    A venue that is unmapped, unreachable, or unparseable leaves that side
    missing (with a warning on stderr) instead of failing the whole run.
    """
    pairs = []
    for entry in load() if entries is None else entries:
        quotes: dict = {}
        for venue in VENUES:
            market_id = entry.get(venue)
            if not market_id:
                quotes[venue] = None
                continue
            try:
                quotes[venue] = FETCHERS[venue](market_id, max_age=max_age)
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"warning: {entry['event_key']} {venue}: {exc}", file=sys.stderr)
                quotes[venue] = None
        pairs.append(pair(entry, quotes))
    return pairs


def suggest(
    query: str,
    candidates: Iterable[str],
    *,
    limit: int = 5,
    score_cutoff: int = 40,
) -> list[tuple[str, float]]:
    """Fuzzy candidates for `query`, best first as ``(candidate, score)``.

    token_set_ratio: the venues word the same question differently, and it
    ignores word order, where WRatio would reward a long question for
    sharing a few common words. Guidance only — mapping.json stays curated
    by hand.
    """
    choices = list(candidates)
    if not choices:
        return []
    matches = process.extract(
        query, choices, scorer=fuzz.token_set_ratio, limit=limit, score_cutoff=score_cutoff
    )
    return [(choice, round(score, 1)) for choice, score, _ in matches]
