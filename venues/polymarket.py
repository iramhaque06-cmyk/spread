"""Polymarket's public Gamma API: no authentication, read-only.

Docs: https://gamma-api.polymarket.com/docs

Markets are addressed by their slug (the same string that appears in
Polymarket's own URLs), and ``GET /markets?slug=`` returns the market with
its parent event embedded, which is all a canonical link needs.

Prices arrive in the 0-1 range and are normalized to cents, exactly as the
Kalshi adapter does, so both venues can share one :class:`Quote`.
"""

from __future__ import annotations

from . import DEFAULT_MAX_AGE, Quote, get_json

API = "https://gamma-api.polymarket.com"
SITE = "https://polymarket.com"


def fetch_market(slug: str, *, max_age: int = DEFAULT_MAX_AGE) -> dict:
    """Raw market record: ``GET /markets?slug=...`` (Gamma answers with a list)."""
    body = get_json(f"{API}/markets", params={"slug": slug}, max_age=max_age)
    if not body:
        raise ValueError(f"no Polymarket market with slug {slug!r}")
    return body[0]


def fetch_quote(slug: str, *, max_age: int = DEFAULT_MAX_AGE) -> Quote:
    """Fetch and normalize one market, with the response cached."""
    return parse_market(fetch_market(slug, max_age=max_age))


def parse_market(raw) -> Quote:
    """Turn a raw Gamma market record into a :class:`Quote`.

    ``raw`` may be the bare market object, the ``{"market": ...}`` wrapper,
    or the raw response list ``GET /markets?slug=`` returns.
    """
    if isinstance(raw, list):
        market = raw[0] if raw else {}
    else:
        market = raw.get("market", raw)
    if not market:
        raise ValueError("Gamma response contained no market")
    market_id = market["slug"]
    price_cents, price_kind = _price(market, market_id)
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
    for field, value in asdict(fetch_quote(slug, max_age=0)).items():
        print(f"{field}: {value}")
