"""Read-only adapters for the prediction-market venues in this prototype.

Each adapter hands back the same shape, a :class:`Quote` describing one
market, so the rest of the program never touches venue-specific JSON.

A quote carries two kinds of timestamp and they are never interchangeable:

``fetched_at``
    when *this program* asked the venue for the data (provenance: it is what
    makes the page a live snapshot at a known moment).
``price_as_of``
    when the *price itself* last moved, as far as the venue publishes it.
    This is what :mod:`freshness` judges staleness from, and both venues
    make it awkward: Kalshi publishes no trade time at all, and Polymarket's
    Gamma ``updatedAt`` is shared by whole batches of markets.
``last_updated``
    the venue's own record stamp, kept verbatim and labelled in the page as
    a record/batch stamp rather than a trade time.

``get_json`` is the shared HTTP helper: every response is written to
``cache/`` and reused while it is fresh, so repeated runs stay well inside
the venues' rate limits.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
DEFAULT_MAX_AGE = 300  # seconds a cached response counts as fresh
PRICE_MAX_AGE = 60  # prices go stale fast: they are re-fetched every minute
REQUEST_PAUSE = 0.5  # seconds to wait after each real network call
TIMEOUT = 30
USER_AGENT = "spread-prototype/0.1 (read-only price comparison)"

# What kind of price a venue publishes. Play-money and forecast venues are
# shown next to the real-money ones but never inside a gap (see gaps.py).
VENUE_TYPES = {
    "kalshi": "prediction_market",
    "polymarket": "prediction_market",
    "manifold": "play_money",
    "odds_api": "sportsbook",
}
REAL_MONEY_TYPES = frozenset({"prediction_market", "sportsbook"})

# What a quote's ``price_as_of`` actually is, per venue, as observed.
AS_OF_KINDS = {
    "last_price_change": "last price change (public price history)",
    "last_trade": "last trade on the venue",
    "batch_stamp": "batch stamp (shared by many markets, not a trade time)",
    "none": "no trade time published by this venue",
}


@dataclass(frozen=True)
class MarketRecord:
    """One open market from any venue, normalized for the catalog.

    ``Quote`` remains the shape the curated three-event pipeline uses; this is
    the wider record: one row per market in the catalog, priced in 0-100
    points, with the venue's own timestamps kept apart from ours.

    ``outcomes`` is a tuple of ``{"name", "price", "price_raw"}`` dicts.
    Prediction markets price the YES side in ``price``; a sportsbook event has
    no single headline price (``price`` is ``None``) because its per-outcome
    de-vigged probabilities are the comparable numbers.
    """

    venue: str
    venue_type: str
    market_id: str
    # A price-only row legitimately has no title; the catalog pass owns it and
    # the store refuses to let a blank overwrite a real one.
    title: str = ""
    category: str = ""
    close_time: str = ""
    outcomes: tuple = ()
    price: float | None = None  # 0-100 points
    price_kind: str = ""  # last | mid | prob | implied_devig
    volume: int | None = None
    volume_24h: int | None = None
    traded_24h: bool | None = None  # did anything trade in the trailing 24 h
    updated_at: str = ""  # the venue's own record stamp
    price_as_of: str | None = None  # when the price itself last moved
    as_of_kind: str = "none"  # a key of AS_OF_KINDS
    url: str = ""
    rule_text: str = ""
    status: str = ""
    event_ref: str = ""  # the venue's parent event/series
    fetched_at: str = ""
    is_open: bool = True
    extra: tuple = field(default_factory=tuple)  # venue-specific leftovers


def outcome(name: str, price: float | None = None, **rest) -> dict:
    """One outcome of a market, in the shared shape."""
    item = {"name": name, "price": price}
    item.update({key: value for key, value in rest.items() if value is not None})
    return item


def chunks(items: list, size: int):
    """Split a list into consecutive slices of at most `size`."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass(frozen=True)
class Quote:
    """One market's price, normalized across venues."""

    venue: str  # e.g. "kalshi"
    market_id: str  # the venue's own market identifier
    title: str  # the question the market asks
    price_cents: float  # 0-100: last trade, or mid when there is no trade
    price_kind: str  # "last" or "mid" - which of those it is
    volume: int | None  # lifetime contracts traded, if the venue reports one
    url: str  # public page for this market
    last_updated: str  # ISO-8601 UTC timestamp from the venue (a *record* stamp)
    status: str  # the venue's own market status
    price_as_of: str | None = None  # ISO-8601 UTC of the last price move, if published
    price_as_of_kind: str = "none"  # a key of AS_OF_KINDS
    traded_24h: bool | None = None  # did any contract trade in the trailing 24 h
    volume_24h: int | None = None  # contracts traded in the trailing 24 h
    fetched_at: str = ""  # when *we* fetched it (provenance, not staleness)
    rules_text: str = ""  # the venue's own resolution rule, verbatim
    resolves_at: str = ""  # when the venue says this market settles


class RateLimit:
    """A venue's ceiling, in requests per second, with a small burst."""

    def __init__(self, per_second: float, burst: float = 1.0):
        self.per_second = per_second
        self.burst = burst


# Self-imposed ceilings. Kalshi documents token budgets for *authenticated*
# requests only, so the keyless catalog stays at a polite 3/s; Polymarket's
# Gamma limit is 300 req/10 s (30/s) and Manifold's is 500/min (8.3/s), so the
# numbers here sit well below each published ceiling on purpose.
LIMITS = {
    "api.elections.kalshi.com": RateLimit(3.0),
    "gamma-api.polymarket.com": RateLimit(10.0, burst=5.0),
    "clob.polymarket.com": RateLimit(15.0, burst=5.0),
    "api.manifold.markets": RateLimit(5.0),
    "api.the-odds-api.com": RateLimit(1.0),
}
DEFAULT_LIMIT = RateLimit(2.0)
_LIMITERS: dict = {}


class RateLimiter:
    """A token bucket, so bursts are allowed but the average rate is bounded."""

    def __init__(self, rate_per_second: float, burst: float = 1.0, clock=time.monotonic):
        self.rate = max(rate_per_second, 0.01)
        self.capacity = max(burst, 1.0)
        self.tokens = self.capacity
        self.updated = clock()
        self.clock = clock
        self.slept = 0.0  # total seconds this limiter has held a caller

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until one request's worth of budget is available."""
        while True:
            now = self.clock()
            self.tokens = min(
                self.capacity, self.tokens + (now - self.updated) * self.rate
            )
            self.updated = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return self.slept
            wait = (tokens - self.tokens) / self.rate
            self.slept += wait
            time.sleep(wait)


class RetryableError(RuntimeError):
    """A failure worth retrying: 429, or a 5xx from the venue."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _limiter_for(url: str, limiter: RateLimiter | None = None) -> RateLimiter:
    if limiter is not None:
        return limiter
    host = urlparse(url).hostname or ""
    if host not in _LIMITERS:
        limit = LIMITS.get(host, DEFAULT_LIMIT)
        _LIMITERS[host] = RateLimiter(limit.per_second, limit.burst)
    return _LIMITERS[host]


def _backoff(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter; a venue's Retry-After wins."""
    if retry_after is not None:
        return max(0.0, min(float(retry_after), 60.0))
    base = 0.5 * (4**attempt)  # 0.5 s, 2 s, 8 s
    return min(base + random.uniform(0, base / 2), 60.0)


def request_json(
    url: str,
    params: dict | None = None,
    *,
    max_age: int = DEFAULT_MAX_AGE,
    headers: dict | None = None,
    retries: int = 2,
    limiter: RateLimiter | None = None,
    pause: float | None = None,
):
    """GET JSON with the disk cache, the venue's rate limit and backoff.

    Returns ``(body, response_headers)``. ``max_age=0`` forces a real request;
    the answer is still cached for the next run. Only successful responses are
    cached, so a 429 never poisons the cache with an error body.

    ``pause`` is the courtesy gap after a real call. It defaults to
    :data:`REQUEST_PAUSE` for the small curated pipeline; bulk callers pass 0
    and let the venue's :class:`RateLimiter` do the pacing, because half a
    second after every one of 500 calls is minutes of nothing.
    """
    path = _cache_path(url, params)
    if path.exists() and (time.time() - path.stat().st_mtime) <= max_age:
        return json.loads(path.read_text(encoding="utf-8")), {}

    _limiter_for(url, limiter).acquire()
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableError(
                    f"HTTP {response.status_code}",
                    retry_after=_retry_after(response.headers),
                )
            response.raise_for_status()  # a 4xx is our bug: fail fast
            data = response.json()
            break
        except (requests.RequestException, ValueError, RetryableError) as exc:
            last = exc
            retryable = isinstance(exc, RetryableError) or not isinstance(
                exc, requests.HTTPError
            )
            if attempt >= retries or not retryable:
                raise
            time.sleep(_backoff(attempt, getattr(exc, "retry_after", None)))
    else:  # pragma: no cover - the loop always breaks or raises
        raise last or RetryableError("request failed")

    time.sleep(REQUEST_PAUSE if pause is None else pause)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data, dict(response.headers)


def get_json(
    url: str,
    params: dict | None = None,
    *,
    max_age: int = DEFAULT_MAX_AGE,
    pause: float | None = None,
):
    """GET a JSON body, serving it from ``cache/`` while still fresh.

    Kept as the two-argument-style helper the adapters already call; the
    rate limit, retries and headers live in :func:`request_json`.
    """
    return request_json(url, params, max_age=max_age, pause=pause)[0]


def _retry_after(headers) -> float | None:
    value = (headers or {}).get("Retry-After")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # an HTTP-date, which we do not parse
        return None


def now_iso() -> str:
    """The current UTC time, to the second: our own fetch stamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cache_path(url: str, params: dict | None) -> Path:
    key = url + "|" + json.dumps(params or {}, sort_keys=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "root"
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-_")[:60] or "root"
    return CACHE_DIR / f"{label}_{digest}.json"
