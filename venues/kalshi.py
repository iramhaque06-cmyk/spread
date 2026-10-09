"""Kalshi's public market-data API: no authentication, read-only.

Docs: https://docs.kalshi.com/getting_started/quick_start_market_data

A market alone is not enough for a link: Kalshi's public pages are addressed
as ``/markets/{series}/{series-slug}/{event}``, so a quote needs the series
title for the slug. ``fetch_quote`` walks market -> event -> series, caching
each call.

A Kalshi title is not yet a question: the brackets of one series are all
called "SOL price on Oct 7, 2026?" (1,350 markets shared that one title), so
:func:`market_title` folds the strike back in - "SOL price on Oct 7, 2026,
above $140?" - from the fields Kalshi actually publishes for it.
"""

from __future__ import annotations

import re
from typing import Iterator

from matching import normalize, tokens

from . import (
    DEFAULT_MAX_AGE,
    PRICE_MAX_AGE,
    MarketRecord,
    Quote,
    chunks,
    get_json,
    now_iso,
    outcome,
)

API = "https://api.elections.kalshi.com/trade-api/v2"
SITE = "https://kalshi.com"
SERIES_MAX_AGE = 86400  # series titles (and so URL slugs) rarely move
CATALOG_MAX_AGE = 3600  # the open catalog is re-listed every 6 hours

# The catalog is read page by page. 1000 is the largest page Kalshi serves.
CATALOG_PAGE = 1000
# ``mve_filter=exclude`` drops the KXMVECROSSCATEGORY parlay combos that are
# ~99.9% of all open markets (measured: 199,731 of 200,000 rows scanned).
# A parlay is not one real-world event, so it cannot be compared to anything.
PARLAY_FILTER = "exclude"
# Prices refresh in batches: 100 tickers fit in one query string comfortably
# (verified live: 100 requested, 100 returned).
PRICE_BATCH = 100


def list_open_markets(*, limit: int | None = None, max_age: int | None = None) -> Iterator[MarketRecord]:
    """Every open non-parlay market, newest page at a time.

    One ``GET /series`` returns all 14,714 series in a single response
    (Kalshi ignores ``limit`` here), so every market can be given its series
    title, category and canonical URL without a request per market. The open
    events listing then maps an event ticker to its series.
    """
    max_age = CATALOG_MAX_AGE if max_age is None else max_age
    series = series_map()
    events = event_map(max_age=max_age)

    cursor: str | None = None
    seen = 0
    while True:
        params = {
            "status": "open",
            "limit": CATALOG_PAGE,
            "mve_filter": PARLAY_FILTER,
        }
        if cursor:
            params["cursor"] = cursor
        body = _bulk(f"{API}/markets", params=params, max_age=max_age)
        markets = body.get("markets") or []
        for raw in markets:
            record = parse_open_market(raw, series=series, events=events)
            if record is not None:
                yield record
        seen += len(markets)
        cursor = body.get("cursor")
        if not cursor or not markets or (limit is not None and seen >= limit):
            return


def get_prices(ids, *, max_age: int | None = None) -> dict[str, MarketRecord]:
    """Latest price evidence for a set of tickers, in batches of 100.

    Only the price fields are filled in: the catalog pass owns titles, URLs
    and rules, and a price refresh must not blank them out.
    """
    max_age = PRICE_MAX_AGE if max_age is None else max_age
    out: dict[str, MarketRecord] = {}
    for chunk in chunks(list(ids), PRICE_BATCH):
        body = _bulk(
            f"{API}/markets",
            params={"tickers": ",".join(chunk)},
            max_age=max_age,
        )
        for raw in body.get("markets") or []:
            record = parse_price(raw)
            if record is not None:
                out[record.market_id] = record
    return out


def series_map(*, max_age: int = SERIES_MAX_AGE) -> dict[str, dict]:
    """``series_ticker -> series record`` for every series Kalshi has."""
    body = _bulk(f"{API}/series", max_age=max_age)
    return {
        item["ticker"]: item
        for item in (body.get("series") or [])
        if item.get("ticker")
    }


def event_map(*, max_age: int = CATALOG_MAX_AGE) -> dict[str, dict]:
    """``event_ticker -> event record`` for every open event.

    Paginated 200 at a time; the events listing is the only place a market's
    parent series ticker appears.
    """
    out: dict[str, dict] = {}
    cursor: str | None = None
    while True:
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        body = _bulk(f"{API}/events", params=params, max_age=max_age)
        events = body.get("events") or []
        for item in events:
            if item.get("event_ticker"):
                out[item["event_ticker"]] = item
        cursor = body.get("cursor")
        if not cursor or not events:
            return out


def parse_open_market(
    raw: dict,
    *,
    series: dict | None = None,
    events: dict | None = None,
    fetched_at: str = "",
) -> MarketRecord | None:
    """One catalog row, or ``None`` when the record has no ticker at all.

    A market with no quote is kept with ``price=None`` rather than dropped: it
    is open, it belongs in the catalog, and the page can say it has no price
    yet instead of pretending it does not exist.
    """
    market = raw.get("market", raw)
    ticker = market.get("ticker") or ""
    if not ticker:
        return None

    event = (events or {}).get(market.get("event_ticker") or "") or {}
    info = (series or {}).get(event.get("series_ticker") or "") or {}
    price, kind = _price_or_none(market)
    volume_24h = _volume_24h(market)
    return MarketRecord(
        venue="kalshi",
        venue_type="prediction_market",
        market_id=ticker,
        title=market_title(market, event=event),
        category=(info.get("category") or event.get("category") or ""),
        close_time=market.get("close_time") or market.get("expiration_time") or "",
        outcomes=(outcome("Yes", price),),
        price=price,
        price_kind=kind,
        volume=_volume(market),
        volume_24h=volume_24h,
        traded_24h=None if volume_24h is None else volume_24h > 0,
        updated_at=market.get("updated_time") or "",
        # Kalshi publishes no trade time: price_as_of stays empty and
        # freshness is inferred from volume_24h (see freshness.py).
        price_as_of=None,
        as_of_kind="none",
        url=market_url(info, market["event_ticker"]) if info else f"{SITE}/markets/{ticker}",
        rule_text=rules_text(market),
        status=market.get("status") or "",
        event_ref=market.get("event_ticker") or "",
        fetched_at=fetched_at or now_iso(),
    )


def parse_price(raw: dict) -> MarketRecord | None:
    """A price-only row for the 30 minute refresh."""
    market = raw.get("market", raw)
    ticker = market.get("ticker") or ""
    if not ticker:
        return None
    price, kind = _price_or_none(market)
    volume_24h = _volume_24h(market)
    return MarketRecord(
        venue="kalshi",
        venue_type="prediction_market",
        market_id=ticker,
        price=price,
        price_kind=kind,
        volume=_volume(market),
        volume_24h=volume_24h,
        traded_24h=None if volume_24h is None else volume_24h > 0,
        updated_at=market.get("updated_time") or "",
        status=market.get("status") or "",
        fetched_at=now_iso(),
    )


def _bulk(url: str, **params):
    """A catalog/price call: the venue rate limiter is the pacing, not a sleep."""
    return get_json(url, pause=0.0, **params)


def _price_or_none(market: dict) -> tuple[float | None, str]:
    """Last trade in points, else the bid/ask midpoint, else nothing."""
    try:
        return _price(market, market.get("ticker") or "")
    except ValueError:
        return None, ""


def fetch_market(ticker: str, *, max_age: int = DEFAULT_MAX_AGE) -> dict:
    """Raw market record: ``GET /markets/{ticker}``."""
    return get_json(f"{API}/markets/{ticker}", max_age=max_age).get("market") or {}


def fetch_event(event_ticker: str, *, max_age: int = DEFAULT_MAX_AGE) -> dict:
    """Raw event record, which is where the series ticker lives."""
    return get_json(f"{API}/events/{event_ticker}", max_age=max_age).get("event") or {}


def fetch_series(series_ticker: str, *, max_age: int = SERIES_MAX_AGE) -> dict:
    """Raw series record: ``GET /series/{ticker}``."""
    return get_json(f"{API}/series/{series_ticker}", max_age=max_age).get("series") or {}


def fetch_quote(ticker: str, *, max_age: int = DEFAULT_MAX_AGE) -> Quote:
    """Fetch and normalize one market, with every step cached."""
    market = fetch_market(ticker, max_age=max_age)
    event = fetch_event(market["event_ticker"], max_age=max_age)
    series = fetch_series(event["series_ticker"])
    return parse_market(market, series=series, event=event, fetched_at=now_iso())


def parse_market(
    raw: dict, *, series: dict, event: dict | None = None, fetched_at: str = ""
) -> Quote:
    """Turn a raw Kalshi market record into a :class:`Quote`.

    ``raw`` may be the bare market object or the ``{"market": ...}``
    response body. Prices arrive in dollars and are normalized to cents.

    Kalshi publishes no trade time, so ``price_as_of`` stays empty and
    freshness is decided from ``volume_24h`` instead; ``updated_time`` is
    stored in ``last_updated`` and labelled on the page as the record's own
    stamp, which it is (it sat 130.8 hours old on a market that had traded
    109,329 contracts in the last day).
    """
    market = raw.get("market", raw)
    ticker = market["ticker"]
    price_cents, price_kind = _price(market, ticker)
    volume_24h = _volume_24h(market)
    return Quote(
        venue="kalshi",
        market_id=ticker,
        title=market_title(market, event=event),
        price_cents=price_cents,
        price_kind=price_kind,
        volume=_volume(market),
        url=market_url(series, market["event_ticker"]),
        last_updated=market.get("updated_time") or "",
        status=market.get("status") or "",
        price_as_of=None,
        price_as_of_kind="none",
        traded_24h=None if volume_24h is None else volume_24h > 0,
        volume_24h=volume_24h,
        fetched_at=fetched_at,
        rules_text=rules_text(market),
        resolves_at=market.get("close_time") or market.get("expiration_time") or "",
    )


# The fields that carry what one market of a series is betting on. Read from
# Kalshi's own records rather than guessed: ``subtitle`` is the bracket label
# where a series has one ("Hike 0bps", "$3,445 or above"), ``yes_sub_title``
# carries it on most series, and the numeric strikes fill the rest.
STRIKE_TEXT_FIELDS = ("yes_sub_title", "subtitle")


def market_title(market: dict, *, event: dict | None = None) -> str:
    """The question one market asks, with its subject and strike folded in.

    Kalshi titles the brackets of a series identically - "SOL price on Oct 7,
    2026?" appears on 1,350 markets - and puts the part that tells them apart
    in ``subtitle``/``yes_sub_title``. A title without it names a series, not
    a bet: neither a reader nor the matcher can tell $84-or-below from
    $157-or-above. The strike is inserted where the question mark is, so the
    result reads as one sentence.

    The other half of the same problem is the per-fixture market: "Will over
    5.5 goals be scored?" is 203 markets, one per game, because only the
    event says which game it is. When the event names a subject the market
    title never mentions, the event goes in front.
    """
    event_title = str((event or {}).get("title") or "").strip()
    base = str(market.get("title") or "").strip() or event_title
    if event_title and base != event_title:
        fuller = _fuller_statement(base, event_title)
        if fuller:
            base = fuller
        elif _another_subject(base, event_title):
            base = f"{_event_subject(event_title)}: {base}"
    strike = market_strike(market)
    if not strike or _title_names(base, strike):
        return base
    if base.endswith("?"):
        return f"{base[:-1].rstrip()}, {strike}?"
    return f"{base} - {strike}" if base else strike


def _fuller_statement(base: str, event_title: str) -> str:
    """The longer of two near-duplicate statements, or ``""`` when they differ.

    Kalshi states a series question twice - once on the market, once on the
    event - and the event's copy is often the one that carries the hour
    ("BTC price on Oct 7, 2026 at 1pm EDT?" against the market's "Bitcoin
    price on Oct 7, 2026?"). Two titles that agree on almost every word are
    one question, so the fuller wording wins.
    """
    words, theirs = tokens(normalize(base)), tokens(normalize(event_title))
    if not words or not theirs:
        return ""
    shared = words & theirs
    if len(shared) < 3 or len(shared) < min(len(words), len(theirs)) - 1:
        return ""
    return event_title if len(event_title) > len(base) else base


def _event_subject(event_title: str) -> str:
    """What an event is about, before its own market-type label.

    Kalshi writes an event as "Torino vs Udinese: Total Goals" - the fixture,
    then the market type the event covers. Only the first half names the thing
    its markets are about, so that is what a market title is missing.
    """
    head, separator, _ = event_title.rpartition(":")
    return (head if separator else event_title).strip() or event_title


def _another_subject(base: str, event_title: str) -> bool:
    """Whether the event names a subject the market title never mentions.

    The catalog is full of markets titled by their *type* - "Will over 5.5
    goals be scored?" is 203 markets, "Tie in the 1st half" 234 - because only
    the event says which fixture they belong to. The test is whether the
    event's subject is mostly missing from the market title: every one of
    those shares under half its words, while an event label that restates the
    question ("Fed decision in Oct 2026?" for a title about the Federal
    Reserve in October 2026) shares more than half and is left alone.
    """
    subject = _event_subject(event_title)
    words = tokens(normalize(subject))
    if not words:
        return False
    ours = tokens(normalize(base))
    if not ours:
        return True
    shared = sum(
        1 for word in words if any(_same_word(word, mine) for mine in ours)
    )
    return shared * 2 < len(words)


def _same_word(first: str, second: str) -> bool:
    """Whether two words are one word written shorter ("Oct"/"October")."""
    if first == second:
        return True
    return len(first) >= 3 and len(second) >= 3 and (
        first.startswith(second) or second.startswith(first)
    )


def market_strike(market: dict) -> str:
    """What this market bets on, in Kalshi's own words.

    Kalshi's own bracket label first, then the numeric strike the API exposes
    (``strike_type`` with ``floor_strike``/``cap_strike``), then any custom
    strike it publishes as a small mapping.
    """
    return _strike_label(market) or _numeric_strike(market) or _custom_strike(market)


def _strike_label(market: dict) -> str:
    """Kalshi's own label for the outcome, when it has one.

    ``subtitle`` is the bracket where a series has one - "Hike 0bps", "$3,445
    or above" - and ``yes_sub_title`` is the outcome the market is about: the
    same bracket on the price series, and the candidate on the election ones.
    A bracket is preferred when the two disagree, because that is the strike;
    otherwise the outcome label wins, which is what tells "Wes Moore" apart
    from "Raphael Warnock". Some series prefix it with a bare separator
    (":: Democratic party"), which is punctuation and not part of the label.
    """
    labels = [
        str(market.get(field) or "").strip().lstrip(":").strip()
        for field in STRIKE_TEXT_FIELDS
    ]
    labels = [label for label in labels if label]
    for label in labels:
        if re.search(r"\d", label):
            return label
    return labels[0] if labels else ""
    return _numeric_strike(market) or _custom_strike(market)


def _numeric_strike(market: dict) -> str:
    """``floor_strike``/``cap_strike`` as a phrase, when there is no label."""
    kind = str(market.get("strike_type") or "").strip().lower()
    floor, cap = _strike_number(market.get("floor_strike")), _strike_number(
        market.get("cap_strike")
    )
    if kind == "between" and floor and cap:
        return f"{floor} to {cap}"
    if kind in ("less", "less_or_equal") and cap:
        return f"{cap} or below"
    if kind in ("greater", "greater_or_equal") and floor:
        return f"{floor} or above"
    return ""


def _strike_number(value) -> str:
    """A strike as text, without a trailing ``.0`` on whole numbers.

    ``repr`` and not ``%g``: ``95749.99`` must not print as ``95750``, because
    that is a different strike from the one this market settles on.
    """
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _custom_strike(market: dict) -> str:
    """A custom strike, as ``key value`` pairs, opaque ids left out.

    A custom strike is a small mapping (``{"Hike": "0"}``). Its values are the
    part that distinguishes one market from its siblings, but some are
    internal ids (``{"soccer_team": "111193d4-..."}``), which tell a reader
    nothing and would only make a title look broken.
    """
    custom = market.get("custom_strike")
    if not isinstance(custom, dict):
        return ""
    parts = [
        f"{key} {value}".strip()
        for key, value in sorted(custom.items())
        if value not in (None, "") and not _is_id(str(value))
    ]
    return ", ".join(parts)


def _is_id(value: str) -> bool:
    """Whether a value is an opaque identifier rather than a readable strike."""
    return bool(re.fullmatch(r"[0-9a-fA-F-]{16,}", value.strip()))


def _title_names(title: str, strike: str) -> bool:
    """Whether the title already says the strike, in words or in numbers.

    Titles that already carry their strike ("Will the Federal Reserve Hike
    rates by 0bps...?") must not have it repeated, while a bracket series
    whose title is only the series name gets it added.
    """
    text = title.lower()
    if not text:
        return False
    label = strike.lower()
    if label in text:
        return True
    numbers = re.findall(r"\d+(?:\.\d+)?", label)
    if not numbers:
        return False
    return all(number in text for number in numbers)


def rules_text(market: dict) -> str:
    """Kalshi's own resolution rule, verbatim, primary then secondary."""
    parts = [
        str(market.get("rules_primary") or "").strip(),
        str(market.get("rules_secondary") or "").strip(),
    ]
    return "\n\n".join(part for part in parts if part)


def market_url(series: dict, event_ticker: str) -> str:
    """Canonical public page for an event of a series."""
    return (
        f"{SITE}/markets/{series['ticker'].lower()}/"
        f"{slugify(series['title'])}/{event_ticker.lower()}"
    )


def slugify(title: str) -> str:
    """Kalshi's URL slug for a series title: "Fed meeting" -> "fed-meeting"."""
    return re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")


def _price(market: dict, ticker: str) -> tuple[float, str]:
    """Last trade price in cents, falling back to the bid/ask midpoint."""
    last = _dollars(market.get("last_price_dollars"))
    if last is not None:
        return round(last * 100, 2), "last"

    bid = _dollars(market.get("yes_bid_dollars"))
    ask = _dollars(market.get("yes_ask_dollars"))
    if bid is not None and ask is not None:
        return round((bid + ask) / 2 * 100, 2), "mid"

    raise ValueError(f"{ticker}: no last trade and no bid/ask to price from")


def _volume(market: dict) -> int | None:
    """Lifetime volume as an integer; Kalshi reports it either way."""
    value = market.get("volume")
    if value is None:
        value = market.get("volume_fp")
    if value in (None, ""):
        return None
    return int(float(value))


def _volume_24h(market: dict) -> int | None:
    """Contracts traded in the trailing 24 hours - Kalshi's only trade clock.

    Non-zero means a contract traded inside the window, so the last trade (and
    the last trade price) is inside it too.
    """
    value = market.get("volume_24h")
    if value is None:
        value = market.get("volume_24h_fp")
    if value in (None, ""):
        return None
    return int(float(value))


def _dollars(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)



if __name__ == "__main__":
    import sys
    from dataclasses import asdict

    ticker = sys.argv[1] if len(sys.argv) > 1 else "KXFEDDECISION-26OCT-H0"
    for field, value in asdict(fetch_quote(ticker, max_age=0)).items():
        print(f"{field}: {value}")
