"""Kalshi's public market-data API: no authentication, read-only.

Docs: https://docs.kalshi.com/getting_started/quick_start_market_data

A market alone is not enough for a link: Kalshi's public pages are addressed
as ``/markets/{series}/{series-slug}/{event}``, so a quote needs the series
title for the slug. ``fetch_quote`` walks market -> event -> series, caching
each call.
"""

from __future__ import annotations

import re

from . import DEFAULT_MAX_AGE, Quote, get_json, now_iso

API = "https://api.elections.kalshi.com/trade-api/v2"
SITE = "https://kalshi.com"
SERIES_MAX_AGE = 86400  # series titles (and so URL slugs) rarely move


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
    return parse_market(market, series=series, fetched_at=now_iso())


def parse_market(raw: dict, *, series: dict, fetched_at: str = "") -> Quote:
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
        title=market.get("title") or "",
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
