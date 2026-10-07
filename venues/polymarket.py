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

import requests

from . import DEFAULT_MAX_AGE, PRICE_MAX_AGE, Quote, get_json, now_iso

API = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SITE = "https://polymarket.com"


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
) -> list[dict]:
    """Public CLOB price history for one outcome token, newest last.

    The 1-day window at 1-minute fidelity is enough to date a recent move;
    a market that has not moved today falls back to the full history so we
    can still say how old it is rather than shrugging. A coarser ``fidelity``
    is what the one-time history backfill uses, so a day of real prices costs
    ~96 points instead of 1,441.
    """
    body = get_json(
        f"{CLOB}/prices-history",
        params={"market": token_id, "interval": interval, "fidelity": fidelity},
        max_age=max_age,
    )
    points = body.get("history") or []
    if points:
        return points
    body = get_json(
        f"{CLOB}/prices-history",
        params={"market": token_id, "interval": "max", "fidelity": 60},
        max_age=max_age,
    )
    return body.get("history") or []


def last_price_change(token_id: str, *, max_age: int = PRICE_MAX_AGE) -> str | None:
    """When this market's price last moved, ISO-8601 UTC, or None."""
    points = fetch_price_history(token_id, max_age=max_age)
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
