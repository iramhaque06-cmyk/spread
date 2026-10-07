"""Manifold's public API: no authentication, read-only, play money.

Docs: https://docs.manifold.markets/api

Manifold trades in mana, not dollars, so its prices can never take part in a
gap; the site shows them beside the real-money venues and says what they are.
Prices are ``probability`` in the 0-1 range and are normalized to points like
every other venue.

Its terms are explicit about this use: "Permitted uses: building bots,
automated trading systems, algorithmic tools, and integrations that interact
with Manifold"; scraping the site *outside* the API is what is prohibited.
The rate limit is 500 requests/minute per IP; this adapter self-limits to 3/s.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from . import (
    MarketRecord,
    PRICE_MAX_AGE,
    chunks,
    get_json,
    now_iso,
    outcome,
)

API = "https://api.manifold.markets"
SITE = "https://manifold.markets"
CATALOG_MAX_AGE = 3600

# ``search-markets`` is the only listing with a working open filter: the plain
# /v0/markets listing has no ``open`` parameter (verified: it 400s), and its
# offset caps at 1000, so the catalog walks ``beforeTime`` instead.
CATALOG_PAGE = 1000
PROBS_BATCH = 100  # /v0/market-probs accepts up to 100 ids per call


def list_open_markets(
    *, limit: int | None = None, max_age: int | None = None
) -> Iterator[MarketRecord]:
    """Every open Manifold market, newest first, cursor page at a time."""
    max_age = CATALOG_MAX_AGE if max_age is None else max_age
    before: int | None = None
    seen = 0
    while True:
        params = {"filter": "open", "sort": "newest", "limit": CATALOG_PAGE}
        if before is not None:
            params["beforeTime"] = before
        body = _bulk(f"{API}/v0/search-markets", params=params, max_age=max_age)
        markets = body if isinstance(body, list) else []
        for raw in markets:
            record = parse_market(raw)
            if record is not None:
                yield record
        seen += len(markets)
        if not markets or len(markets) < CATALOG_PAGE:
            return
        if limit is not None and seen >= limit:
            return
        created = [item.get("createdTime") for item in markets if item.get("createdTime")]
        if not created:
            return
        before = min(created)


def get_prices(
    ids, *, max_age: int | None = None, detailed: bool = True
) -> dict[str, MarketRecord]:
    """Latest probability per market id.

    ``detailed`` reads one market at a time and keeps the last-bet timestamp
    that dates the price; the batch endpoint is cheaper but only returns the
    probability, leaving the price undateable. Matched events and the top
    markets use the detailed path.
    """
    max_age = PRICE_MAX_AGE if max_age is None else max_age
    out: dict[str, MarketRecord] = {}
    ids = list(ids)
    if detailed:
        for market_id in ids:
            _add_single(out, market_id, max_age=max_age)
        return out

    for chunk in chunks(ids, PROBS_BATCH):
        if len(chunk) < 2:
            # ``market-probs`` validates ``ids`` with a schema that requires an
            # array, and requests encodes a one-element list as a *bare* value
            # (``?ids=abc``) - which Manifold answers 400 "Expected array,
            # received string". Measured live on 2026-10-07, so a lone id takes
            # the single-market path instead of being padded with a duplicate.
            _add_single(out, chunk[0], max_age=max_age)
            continue
        params = {"ids": chunk}  # requests repeats the parameter: ids=a&ids=b
        body = _bulk(f"{API}/v0/market-probs", params=params, max_age=max_age)
        for market_id, value in (body or {}).items():
            if not isinstance(value, dict):
                continue
            price = value.get("prob")
            if price is None:
                continue
            out[market_id] = MarketRecord(
                venue="manifold",
                venue_type="play_money",
                market_id=market_id,
                price=round(float(price) * 100, 2),
                price_kind="prob",
                fetched_at=now_iso(),
            )
    return out


def _add_single(out: dict, market_id: str, *, max_age) -> None:
    """Read one Manifold market and keep it, for the lone-id paths."""
    body = _bulk(f"{API}/v0/market/{market_id}", max_age=max_age)
    if isinstance(body, dict) and body.get("id"):
        record = parse_market(body)
        if record is not None:
            out[market_id] = record


def parse_market(raw: dict, *, fetched_at: str = "") -> MarketRecord | None:
    """One catalog row, or ``None`` when the record has no id."""
    market_id = raw.get("id") or ""
    if not market_id:
        return None
    probability = raw.get("probability")
    price = None if probability is None else round(float(probability) * 100, 2)
    volume_24h = _int(raw.get("volume24Hours"))
    groups = raw.get("groupSlugs") or []
    return MarketRecord(
        venue="manifold",
        venue_type="play_money",
        market_id=market_id,
        title=raw.get("question") or "",
        category=str(groups[0]) if groups else "",
        close_time=_iso_ms(raw.get("closeTime")),
        outcomes=(outcome("Yes", price),),
        price=price,
        price_kind="prob",
        volume=_int(raw.get("volume")),
        volume_24h=volume_24h,
        traded_24h=None if volume_24h is None else volume_24h > 0,
        updated_at=_iso_ms(raw.get("lastUpdatedTime")) or _iso_ms(raw.get("createdTime")),
        # A Manifold bet is a trade, so its last-bet time really is the last
        # time the price moved - unlike Kalshi, which publishes none.
        price_as_of=_iso_ms(raw.get("lastBetTime")) or None,
        as_of_kind="last_trade" if raw.get("lastBetTime") else "none",
        url=raw.get("url") or f"{SITE}/market/{market_id}",
        rule_text=(raw.get("textDescription") or "").strip(),
        status="open" if not raw.get("isResolved") else "resolved",
        event_ref=str(groups[0]) if groups else "",
        fetched_at=fetched_at or now_iso(),
    )


def _bulk(url: str, **params):
    """A catalog/price call: the venue rate limiter is the pacing, not a sleep."""
    return get_json(url, pause=0.0, **params)


def _int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _iso_ms(value) -> str:
    """Manifold timestamps are milliseconds since the epoch, like JavaScript.

    A few markets carry sentinel or far-future close times that no calendar
    can represent; Windows raises OSError on those rather than clamping, so an
    unrepresentable stamp becomes an empty string ("not published") instead of
    taking the whole catalog pass down.
    """
    if not value:
        return ""
    try:
        moment = datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return ""
    if not 1970 <= moment.year <= 9999:
        return ""
    return moment.isoformat(timespec="seconds")
