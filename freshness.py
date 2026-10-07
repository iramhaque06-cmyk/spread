"""Is the price behind a quote still fresh enough to compare?

The rule the page follows is simple: **a price older than 24 hours is greyed
out and left out of the gap.** Applying it honestly is harder than it sounds,
because neither venue publishes a plain "last trade time". What each one
actually offers, measured against the live APIs on 2026-10-06:

Kalshi
    No trade timestamp at all (``/markets/{ticker}/trades`` and both
    candlestick paths answer 404 without an API key, which this prototype
    never uses). ``updated_time`` looks like one but is not: the
    Fed-decision market had traded 109,329 contracts in the last 24 hours
    while its record stamp sat 130.8 hours old. The evidence Kalshi *does*
    publish is ``volume_24h``: a non-zero 24 hour volume proves at least one
    contract traded inside the window, so the last trade is inside it too.
    Zero proves the opposite.
Polymarket
    Gamma's ``updatedAt`` is a batch stamp - on the same run it was identical
    to the microsecond across all three curated markets - so it identifies
    nothing about one market's price. The per-market truth comes from the
    public CLOB price history (``/prices-history``), whose last point is the
    last time that market's price moved.

The precedence is therefore: a real timestamp if we have one, otherwise 24
hour traded volume, and if a price can be neither measured nor proven traded
it is treated as **stale**, never as fresh. Quiet markets go stale on their
own; the page just stops pretending otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STALE_AFTER = timedelta(hours=24)
STALE_AFTER_HOURS = STALE_AFTER.total_seconds() / 3600


@dataclass(frozen=True)
class Verdict:
    """Freshness of one venue's price, with the evidence behind it."""

    fresh: bool
    age_hours: float | None  # how old the price is, when it can be measured
    basis: str  # the evidence, in words, for the page
    reason: str  # why it is stale; empty when fresh


def assess(source, *, now: datetime | None = None) -> Verdict:
    """Judge one quote (or stored observation row) against the 24 hour rule.

    ``source`` may be a :class:`venues.Quote` or a plain mapping/row from the
    database: both expose the same field names, so one implementation serves
    the adapters, the page and the tests.
    """
    now = as_utc(now) or datetime.now(timezone.utc)

    as_of = _field(source, "price_as_of")
    if as_of:
        return _from_timestamp(source, as_of, now)

    traded = _field(source, "traded_24h")
    if traded is not None:
        return _from_volume(source, bool(traded))

    return Verdict(
        fresh=False,
        age_hours=None,
        basis="no trade time and no 24 hour volume published",
        reason="cannot be shown to be fresher than 24 hours",
    )


def _from_timestamp(source, as_of: str, now: datetime) -> Verdict:
    """Judge from a real timestamp, when the venue publishes one."""
    try:
        moment = parse_iso(as_of)
    except ValueError:
        return Verdict(
            fresh=False,
            age_hours=None,
            basis=f"unreadable timestamp {as_of!r}",
            reason="timestamp could not be parsed",
        )

    age = (now - moment).total_seconds() / 3600
    kind = _field(source, "price_as_of_kind") or "none"
    moved = human_age(age)
    basis = f"price last moved {moved} ago, from its {kind.replace('_', ' ')}"
    if age <= STALE_AFTER_HOURS:
        return Verdict(fresh=True, age_hours=age, basis=basis, reason="")
    return Verdict(
        fresh=False,
        age_hours=age,
        basis=basis,
        reason=f"price older than {STALE_AFTER_HOURS:.0f} hours ({human_age(age)})",
    )


def _from_volume(source, traded: bool) -> Verdict:
    """Judge from trailing 24 hour volume, the only signal Kalshi publishes.

    Non-zero volume proves a trade inside the window, so the last trade - and
    with it the last trade price - is inside it as well.
    """
    volume = _field(source, "volume_24h")
    counted = f"{volume:,}" if isinstance(volume, int) else "some"
    if traded:
        return Verdict(
            fresh=True,
            age_hours=None,
            basis=f"traded in the last 24 hours ({counted} contracts)",
            reason="",
        )
    return Verdict(
        fresh=False,
        age_hours=None,
        basis="no contract traded in the last 24 hours",
        reason="no trade in the last 24 hours",
    )


def _field(source, name: str):
    """Read one field from a Quote, a database row dict, or any mapping."""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a missing zone as UTC.

    Raises ValueError on anything unreadable, which callers treat as
    unmeasurable - and therefore stale - rather than guessing.
    """
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def as_utc(moment: datetime | None) -> datetime | None:
    """Coerce a datetime to UTC, assuming UTC when it carries no zone."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def human_age(age: float) -> str:
    """A compact human age: minutes under an hour, hours beyond it."""
    if age < 1:
        return f"{max(age, 0) * 60:.0f} min"
    return f"{age:.1f} h"
