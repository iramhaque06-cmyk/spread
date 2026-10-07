"""Polymarket's public Gamma API: no authentication, read-only.

Docs: https://gamma-api.polymarket.com/docs

Markets are addressed by their slug (the same string that appears in
Polymarket's own URLs), and ``GET /markets?slug=`` returns the market with
its parent event embedded, which is all a canonical link needs.

Prices arrive in the 0-1 range and are normalized to cents, exactly as the
Kalshi adapter does, so both venues can share one :class:`Quote`.

Prices are dated from the public CLOB price history, not from Gamma's
``updatedAt``: on 2026-10-06 that Gamma field was byte-identical across all
three curated markets, so it times a batch of records rather than any one
market's last trade. The CLOB's last history point does time one market, is
keyless and read-only, and is what :mod:`freshness` judges from.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Iterator

import requests

from . import (
    DEFAULT_MAX_AGE,
    PRICE_MAX_AGE,
    MarketRecord,
    Quote,
    get_json,
    now_iso,
    outcome,
)

API = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SITE = "https://polymarket.com"
CATALOG_MAX_AGE = 3600  # the open catalog is re-listed every 6 hours

# Gamma serves 100 markets per page. Plain ``offset`` pagination is refused
# past ~2,000 ("422 offset too large, use /markets/keyset"), so the catalog
# walks the keyset cursor instead. The *response* field is ``next_cursor`` but
# the *request* parameter is ``after_cursor`` - sending next_cursor back is
# silently ignored and every page repeats the first one, which is exactly the
# trap this adapter fell into once already. Verified live: after_cursor gives
# zero overlap between pages, and the catalog has 20,000+ open markets.
CATALOG_PAGE = 100


def list_open_markets(*, limit: int | None = None, max_age: int | None = None) -> Iterator[MarketRecord]:
    """Every open market Polymarket has, keyset cursor page at a time."""
    max_age = CATALOG_MAX_AGE if max_age is None else max_age
    cursor: str | None = None
    seen = 0
    while True:
        params = {"closed": "false", "limit": CATALOG_PAGE}
        if cursor:
            params["after_cursor"] = cursor
        body = _bulk(f"{API}/markets/keyset", params=params, max_age=max_age)
        markets = body.get("markets") if isinstance(body, dict) else body
        markets = markets or []
        for raw in markets:
            record = parse_open_market(raw)
            if record is not None:
                yield record
        seen += len(markets)
        following = body.get("next_cursor") if isinstance(body, dict) else None
        # A cursor that does not move would repeat one page forever.
        if not following or following == cursor or not markets:
            return
        cursor = following
        if limit is not None and seen >= limit:
            return


def get_prices(
    ids, *, max_age: int | None = None, detailed: bool = False
) -> dict[str, MarketRecord]:
    """Latest price for each id. ``GET /markets?id=`` answers with one row.

    ``detailed`` also reads the public CLOB history, which is the only place a
    Polymarket price carries a real last-moved timestamp. The 30 minute bulk
    refresh leaves it off (Gamma's 24 hour volume is enough for freshness);
    matched events ask for it.
    """
    max_age = PRICE_MAX_AGE if max_age is None else max_age
    out: dict[str, MarketRecord] = {}
    for market_id in list(ids):
        # Stored ids are slugs; Gamma's ``?id=`` wants the numeric id and
        # answers 422 for a slug, so the slug lookup is the right one here.
        body = _bulk(
            f"{API}/markets", params={"slug": market_id}, max_age=max_age
        )
        if not body:
            continue
        raw = body[0] if isinstance(body, list) else body
        stamp, kind = (None, "none")
        if detailed:
            token_id = yes_token_id(raw)
            if token_id:
                try:
                    stamp = last_price_change(token_id, max_age=max_age, pause=0.0)
                except (requests.RequestException, ValueError, KeyError) as exc:
                    print(
                        f"warning: {market_id}: CLOB history unavailable: {exc}",
                        file=sys.stderr,
                    )
                if stamp:
                    kind = "last_price_change"
        record = parse_open_market(raw, price_as_of=stamp, as_of_kind=kind)
        if record is not None:
            out[market_id] = record
    return out


def _bulk(url: str, **params):
    """A catalog/price call: the venue rate limiter is the pacing, not a sleep."""
    return get_json(url, pause=0.0, **params)


def _jsonish(value):
    """Gamma ships several list fields as JSON-encoded strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _outcomes(market: dict) -> tuple:
    """Every outcome with its own price, when Gamma publishes them."""
    names = _jsonish(market.get("outcomes"))
    prices = _jsonish(market.get("outcomePrices"))
    if not isinstance(names, list) or not names:
        return ()
    parsed = []
    for index, name in enumerate(names):
        value = None
        if isinstance(prices, list) and index < len(prices):
            try:
                value = round(float(prices[index]) * 100, 2)
            except (TypeError, ValueError):
                value = None
        parsed.append(outcome(str(name), value))
    return tuple(parsed)


def _category(market: dict) -> str:
    """Gamma leaves ``category`` empty; the parent event or a tag names it."""
    if market.get("category"):
        return str(market["category"])
    events = market.get("events") or []
    if events and isinstance(events[0], dict):
        if events[0].get("category"):
            return str(events[0]["category"])
        for tag in events[0].get("tags") or []:
            if isinstance(tag, dict) and tag.get("label"):
                return str(tag["label"])
    for tag in market.get("tags") or []:
        if isinstance(tag, dict) and tag.get("label"):
            return str(tag["label"])
    return ""


def parse_open_market(
    raw,
    *,
    price_as_of: str | None = None,
    as_of_kind: str = "none",
    fetched_at: str = "",
) -> MarketRecord | None:
    """One catalog row, or ``None`` when the record has no identifier."""
    market = raw.get("market", raw) if isinstance(raw, dict) else {}
    market_id = market.get("slug") or ""
    if not market_id:
        return None

    price = None
    kind = ""
    try:
        price, kind = _price(market, market_id)
    except ValueError:
        price = None
        kind = ""
    headline = price
    outcomes = _outcomes(market)
    if headline is None and outcomes:
        headline = outcomes[0].get("price")
        kind = kind or "last"
    volume_24h = _volume_24h(market)
    return MarketRecord(
        venue="polymarket",
        venue_type="prediction_market",
        market_id=market_id,
        title=market.get("question") or "",
        category=_category(market),
        close_time=market.get("endDate") or market.get("endDateIso") or "",
        outcomes=outcomes or ((outcome("Yes", headline),)),
        price=headline,
        price_kind=kind,
        volume=_volume(market),
        volume_24h=volume_24h,
        traded_24h=None if volume_24h is None else volume_24h > 0,
        updated_at=market.get("updatedAt") or "",
        price_as_of=price_as_of,
        as_of_kind=as_of_kind,
        url=market_url(market),
        rule_text=(market.get("description") or "").strip(),
        status=_status(market),
        event_ref=(market.get("events") or [{}])[0].get("slug", "")
        if market.get("events")
        else "",
        fetched_at=fetched_at or now_iso(),
    )


def fetch_market(slug: str, *, max_age: int = DEFAULT_MAX_AGE) -> dict:
    """Raw market record: ``GET /markets?slug=...`` (Gamma answers with a list)."""
    body = get_json(f"{API}/markets", params={"slug": slug}, max_age=max_age)
    if not body:
        raise ValueError(f"no Polymarket market with slug {slug!r}")
    return body[0]


def fetch_quote(slug: str, *, max_age: int = DEFAULT_MAX_AGE) -> Quote:
    """Fetch and normalize one market, with every response cached."""
    market = fetch_market(slug, max_age=max_age)
    price_as_of, kind = _price_as_of(market)
    return parse_market(
        market,
        price_as_of=price_as_of,
        price_as_of_kind=kind,
        fetched_at=now_iso(),
    )


def fetch_price_history(
    token_id: str,
    *,
    max_age: int = PRICE_MAX_AGE,
    interval: str = "1d",
    fidelity: int = 1,
    pause: float | None = None,
) -> list[dict]:
    """Public CLOB price history for one outcome token, newest last.

    The 1-day window at 1-minute fidelity is enough to date a recent move;
    a market that has not moved today falls back to the full history so we
    can still say how old it is rather than shrugging. A coarser ``fidelity``
    is what the one-time history backfill uses, so a day of real prices costs
    ~96 points instead of 1,441.
    """
    # Only pass the pacing override when a bulk caller asked for one, so the
    # curated pipeline's calls keep exactly the shape they always had.
    pacing = {} if pause is None else {"pause": pause}
    body = get_json(
        f"{CLOB}/prices-history",
        params={"market": token_id, "interval": interval, "fidelity": fidelity},
        max_age=max_age,
        **pacing,
    )
    points = body.get("history") or []
    if points:
        return points
    body = get_json(
        f"{CLOB}/prices-history",
        params={"market": token_id, "interval": "max", "fidelity": 60},
        max_age=max_age,
        **pacing,
    )
    return body.get("history") or []


def last_price_change(
    token_id: str, *, max_age: int = PRICE_MAX_AGE, pause: float | None = None
) -> str | None:
    """When this market's price last moved, ISO-8601 UTC, or None."""
    points = fetch_price_history(token_id, max_age=max_age, pause=pause)
    if not points:
        return None
    stamp = points[-1].get("t")
    if stamp in (None, ""):
        return None
    return datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(timespec="seconds")


def yes_token_id(market: dict) -> str | None:
    """The YES outcome's CLOB token id; Gamma ships it as a JSON string."""
    ids = market.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except ValueError:
            return None
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return None


def _price_as_of(market: dict) -> tuple[str | None, str]:
    """Date the price from the CLOB, falling back to labelling Gamma's stamp."""
    token_id = yes_token_id(market)
    if token_id:
        try:
            stamp = last_price_change(token_id)
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(
                f"warning: {market.get('slug')}: CLOB price history unavailable: {exc}",
                file=sys.stderr,
            )
            stamp = None
        if stamp:
            return stamp, "last_price_change"

    stamp = market.get("updatedAt") or None
    return (stamp, "batch_stamp") if stamp else (None, "none")


def parse_market(
    raw,
    *,
    price_as_of: str | None = None,
    price_as_of_kind: str = "none",
    fetched_at: str = "",
) -> Quote:
    """Turn a raw Gamma market record into a :class:`Quote`.

    ``raw`` may be the bare market object, the ``{"market": ...}`` wrapper,
    or the raw response list ``GET /markets?slug=`` returns. The price
    timestamp is passed in rather than looked up, so parsing stays offline
    and testable: :func:`fetch_quote` is what reaches the CLOB.
    """
    if isinstance(raw, list):
        market = raw[0] if raw else {}
    else:
        market = raw.get("market", raw)
    if not market:
        raise ValueError("Gamma response contained no market")
    market_id = market["slug"]
    price_cents, price_kind = _price(market, market_id)
    volume_24h = _volume_24h(market)
    return Quote(
        venue="polymarket",
        market_id=market_id,
        title=market.get("question") or "",
        price_cents=price_cents,
        price_kind=price_kind,
        volume=_volume(market),
        url=market_url(market),
        last_updated=market.get("updatedAt") or "",
        status=_status(market),
        price_as_of=price_as_of,
        price_as_of_kind=price_as_of_kind,
        traded_24h=None if volume_24h is None else volume_24h > 0,
        volume_24h=volume_24h,
        fetched_at=fetched_at,
        rules_text=(market.get("description") or "").strip(),
        resolves_at=market.get("endDate") or market.get("endDateIso") or "",
    )


def market_url(market: dict) -> str:
    """Canonical public page: ``/event/{event}/{market}`` when known.

    Gamma embeds the parent event(s) on the list endpoint; without them we
    fall back to ``/market/{slug}``, which Polymarket redirects to the same
    page.
    """
    slug = market["slug"]
    events = market.get("events") or []
    if events and events[0].get("slug"):
        return f"{SITE}/event/{events[0]['slug']}/{slug}"
    return f"{SITE}/market/{slug}"


def _price(market: dict, market_id: str) -> tuple[float, str]:
    """Last trade price in cents, falling back to the bid/ask midpoint."""
    last = _number(market.get("lastTradePrice"))
    if last is not None:
        return round(last * 100, 2), "last"

    bid = _number(market.get("bestBid"))
    ask = _number(market.get("bestAsk"))
    if bid is not None and ask is not None:
        return round((bid + ask) / 2 * 100, 2), "mid"

    raise ValueError(f"{market_id}: no last trade and no bid/ask to price from")


def _volume(market: dict) -> int | None:
    """Lifetime volume as an integer; Gamma reports it two ways."""
    value = market.get("volumeNum")
    if value in (None, ""):
        value = market.get("volume")
    if value in (None, ""):
        return None
    return int(float(value))


def _volume_24h(market: dict) -> int | None:
    """Traded in the trailing 24 hours; Gamma reports it as ``volume24hr``."""
    value = market.get("volume24hr")
    if value in (None, ""):
        value = market.get("volume24hrClob")
    if value in (None, ""):
        return None
    return int(float(value))


def _status(market: dict) -> str:
    if market.get("closed"):
        return "closed"
    return "active" if market.get("active") else "inactive"


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


if __name__ == "__main__":
    import sys
    from dataclasses import asdict

    slug = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "will-there-be-no-change-in-fed-interest-rates-after-the-october-2026-meeting-20260617190324031"
    )
    quote = fetch_quote(slug, max_age=0)
    for field, value in asdict(quote).items():
        print(f"{field}: {value}")
