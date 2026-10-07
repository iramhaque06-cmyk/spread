"""The Odds API v4: licensed sports odds for the sports events we already match.

Docs: https://the-odds-api.com/liveapi/guides/v4/

Only this aggregated API is ever contacted. No sportsbook or betting site is
scraped, and no bookmaker page is fetched, ever.

Quota discipline (the free plan is 500 credits a month, reset on the 1st):

* ``/v4/sports`` and ``/v4/sports/{sport}/events`` cost **nothing**, so the
  sports catalog is built for free.
* ``/v4/sports/{sport}/odds`` costs 1 credit per market per region; we ask for
  one market (``h2h``) in one region (``us``), so one request is 1 credit.
* Every response carries ``x-requests-remaining`` / ``x-requests-used`` /
  ``x-requests-last``. This adapter records them and refuses to spend past
  **80% of the monthly quota**, leaving the last fifth for the user's own use.
* When the quota runs out the caller keeps the last known odds; nothing is
  invented and nothing is refetched in a loop.

The API key is read from ``ODDS_API_KEY`` (see envfile.py). It is never
printed, logged or included in an error: URLs are redacted on the way out.
"""

from __future__ import annotations

import sys
from typing import Iterator

import envfile
from . import (
    MarketRecord,
    get_json,
    now_iso,
    outcome,
    request_json,
)

API = "https://api.the-odds-api.com/v4"
FREE_MONTHLY_CREDITS = 500
# Stop at 80% used: the remaining fifth belongs to the account owner, not us.
QUOTA_FLOOR = int(FREE_MONTHLY_CREDITS * 0.2)
# One credit is worth a whole 30-minute cycle, so a cached answer is reused
# rather than spent twice.
ODDS_MAX_AGE = 1500
SPORTS_MAX_AGE = 21600  # the in-season list changes slowly
MARKETS = "h2h"  # moneyline only, to begin with (the brief's scope)
REGIONS = "us"

# The last quota headers seen, for the caller to store and the smoke test to
# print. Nothing here is a secret.
LAST_QUOTA: dict = {"remaining": None, "used": None, "last": None, "at": ""}


class QuotaExhausted(RuntimeError):
    """Raised instead of spending the last 20% of the monthly quota."""


def api_key() -> str:
    """The key from the environment, redacted in every path that can fail."""
    return envfile.get("ODDS_API_KEY")


def list_sports(*, max_age: int = SPORTS_MAX_AGE) -> list[dict]:
    """In-season sports. Free: this endpoint costs no credits."""
    body = get_json(
        f"{API}/sports", params={"apiKey": api_key()}, max_age=max_age, pause=0.0
    )
    return body if isinstance(body, list) else []


def list_open_markets(
    *,
    sports: list[str] | None = None,
    limit: int | None = None,
    max_age: int | None = None,
) -> Iterator[MarketRecord]:
    """Upcoming events as moneyline markets. Costs no credits.

    Prices are deliberately absent: odds cost quota, so the catalog lists what
    exists for free and :func:`get_prices` spends credits only on the events
    the matcher actually wants.
    """
    max_age = SPORTS_MAX_AGE if max_age is None else max_age
    wanted = sports if sports is not None else [
        item["key"] for item in list_sports(max_age=max_age) if item.get("active") and item.get("key")
    ]
    seen = 0
    for sport in wanted:
        params = {"apiKey": api_key()}
        try:
            body = get_json(
                f"{API}/sports/{sport}/events",
                params=params,
                max_age=max_age,
                pause=0.0,
            )
        except Exception as exc:  # a dead sport must not stop the catalog
            print(
                f"warning: odds_api events for {sport}: {envfile.redact(exc)}",
                file=sys.stderr,
            )
            continue
        for event in body if isinstance(body, list) else []:
            record = parse_event(event, sport=sport)
            if record is not None:
                yield record
                seen += 1
                if limit is not None and seen >= limit:
                    return


def get_prices(
    ids, *, max_age: int = ODDS_MAX_AGE, allow_spend: bool = True
) -> dict[str, MarketRecord]:
    """Odds for the given ``sport:event_id`` markets, de-vigged.

    One request per sport covers every event asked for in that sport, so the
    cost is one credit per sports book per call no matter how many events are
    in it.
    """
    by_sport: dict[str, list[str]] = {}
    for market_id in ids:
        sport, _, event_id = str(market_id).partition(":")
        if sport and event_id:
            by_sport.setdefault(sport, []).append(event_id)

    out: dict[str, MarketRecord] = {}
    for sport, event_ids in by_sport.items():
        if allow_spend and not has_budget():
            raise QuotaExhausted(
                f"odds quota floor reached ({LAST_QUOTA.get('remaining')} credits "
                f"left of {FREE_MONTHLY_CREDITS}); keeping the last known odds"
            )
        params = {
            "apiKey": api_key(),
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "decimal",
            "eventIds": ",".join(event_ids),
        }
        body, headers = request_json(
            f"{API}/sports/{sport}/odds", params=params, max_age=max_age, pause=0.0
        )
        _record_quota(headers)
        for event in body if isinstance(body, list) else []:
            record = parse_event(event, sport=sport, priced=True)
            if record is not None:
                out[record.market_id] = record
    return out


def has_budget() -> bool:
    """Whether another credit may be spent under the 80% rule."""
    remaining = LAST_QUOTA.get("remaining")
    if remaining is None:
        return True  # nothing spent yet this run
    return int(remaining) > QUOTA_FLOOR


def _record_quota(headers) -> None:
    headers = headers or {}
    LAST_QUOTA.update(
        {
            "remaining": _int(headers.get("x-requests-remaining")),
            "used": _int(headers.get("x-requests-used")),
            "last": _int(headers.get("x-requests-last")),
            "at": now_iso(),
        }
    )


def parse_event(
    event: dict, *, sport: str = "", priced: bool = False
) -> MarketRecord | None:
    """One moneyline market for an event.

    Without odds this is a catalog row: teams and start time, no price. With
    odds it carries every outcome as raw decimal **and** de-vigged points.
    """
    event_id = event.get("id") or ""
    if not event_id:
        return None
    home = str(event.get("home_team") or "")
    away = str(event.get("away_team") or "")
    title = f"{away} @ {home}".strip(" @") or event.get("sport_title") or ""
    outcomes: tuple = ()
    price = None
    price_as_of = None
    as_of_kind = "none"
    if priced:
        raw_outcomes, last_update = _best_book(event)
        outcomes = devig(raw_outcomes)
        if outcomes:
            price = outcomes[0].get("price")
        if last_update:
            price_as_of = last_update
            as_of_kind = "last_price_change"
    return MarketRecord(
        venue="odds_api",
        venue_type="sportsbook",
        market_id=f"{sport or event.get('sport_key', '')}:{event_id}",
        title=title,
        category=str(event.get("sport_title") or event.get("sport_key") or ""),
        close_time=str(event.get("commence_time") or ""),
        outcomes=outcomes,
        price=price,
        price_kind="implied_devig" if outcomes else "",
        volume=None,
        volume_24h=None,
        updated_at=price_as_of or "",
        price_as_of=price_as_of,
        as_of_kind=as_of_kind,
        url="",  # no bookmaker links: this site compares, it does not send bets
        rule_text="",
        status="open",
        event_ref=event_id,
        fetched_at=now_iso(),
    )


def _best_book(event: dict) -> tuple[list[tuple[str, float]], str | None]:
    """Prefer the book with the most complete moneyline, then the freshest.

    Odds are stored per outcome, not per book: the comparison is
    "what does the sportsbook market think", and one book's two-way moneyline
    is the sharpest single number we can honestly de-vig.
    """
    best: tuple[int, str, list] = (0, "", [])
    for book in event.get("bookmakers") or []:
        for market in book.get("markets") or []:
            if market.get("key") != MARKETS:
                continue
            rows = []
            for item in market.get("outcomes") or []:
                try:
                    rows.append((str(item.get("name")), float(item.get("price"))))
                except (TypeError, ValueError):
                    continue
            stamp = str(market.get("last_update") or book.get("last_update") or "")
            if len(rows) > best[0] or (len(rows) == best[0] and stamp > best[1]):
                best = (len(rows), stamp, rows)
    return best[2], (best[1] or None)


def devig(raw_outcomes) -> tuple:
    """Decimal odds -> implied probability -> normalized, in 0-100 points.

    Implied probabilities in a real book sum to more than 1 (the vig). Dividing
    each by that sum removes it, which is the only way two books - or a book and
    a prediction market - can be compared at all. The raw decimal price is kept
    next to the de-vigged one so the page can show what was actually quoted.
    """
    implied = []
    for name, decimal in raw_outcomes or []:
        try:
            decimal = float(decimal)
        except (TypeError, ValueError):
            continue
        if decimal <= 1.0:  # decimal odds are always greater than 1
            continue
        implied.append((name, 1.0 / decimal, decimal))
    total = sum(item[1] for item in implied)
    if total <= 0:
        return ()
    return tuple(
        outcome(name, round(probability / total * 100, 2), price_raw=decimal)
        for name, probability, decimal in implied
    )


def _int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
