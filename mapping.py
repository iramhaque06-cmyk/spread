"""Curated event mapping: which market on each venue is the same real event.

``mapping.json`` is hand-checked — this module only reads it. rapidfuzz
powers :func:`suggest`, which offers candidate markets while you are
curating; nothing it returns is ever written back automatically.

The spread is always **Kalshi cents minus Polymarket cents**, and is
``None`` whenever either side is missing, so a one-sided event can never
masquerade as a zero gap.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from rapidfuzz import fuzz, process

from venues import DEFAULT_MAX_AGE, Quote, kalshi, polymarket

MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"
VENUES = ("kalshi", "polymarket")
FIELDS = ("event_key", "title", "kalshi", "polymarket", "note")

FETCHERS = {"kalshi": kalshi.fetch_quote, "polymarket": polymarket.fetch_quote}


@dataclass(frozen=True)
class EventPair:
    """Two venues' quotes for one event; either side may be missing."""

    event_key: str
    title: str
    quotes: dict  # venue -> Quote | None
    spread_points: float | None  # Kalshi cents - Polymarket cents

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
    )


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
