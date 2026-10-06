"""Read-only adapters for the prediction-market venues in this prototype.

Each adapter hands back the same shape, a :class:`Quote` describing one
market, so the rest of the program never touches venue-specific JSON.

``get_json`` is the shared HTTP helper: every response is written to
``cache/`` and reused while it is fresh, so repeated runs stay well inside
the venues' rate limits.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
DEFAULT_MAX_AGE = 300  # seconds a cached response counts as fresh
REQUEST_PAUSE = 0.5  # seconds to wait after each real network call
TIMEOUT = 30
USER_AGENT = "spread-prototype/0.1 (read-only price comparison)"


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
    last_updated: str  # ISO-8601 UTC timestamp from the venue
    status: str  # the venue's own market status


def get_json(url: str, params: dict | None = None, *, max_age: int = DEFAULT_MAX_AGE):
    """GET a JSON document, serving it from ``cache/`` while still fresh.

    ``max_age=0`` forces a real request; the answer is still cached for the
    next run. The cache key covers the URL and its query parameters.
    """
    path = _cache_path(url, params)
    if path.exists() and (time.time() - path.stat().st_mtime) <= max_age:
        return json.loads(path.read_text(encoding="utf-8"))

    response = requests.get(
        url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    data = response.json()
    time.sleep(REQUEST_PAUSE)  # keep the gap between real calls polite

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data


def _cache_path(url: str, params: dict | None) -> Path:
    key = url + "|" + json.dumps(params or {}, sort_keys=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "root"
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-")[:60] or "root"
    return CACHE_DIR / f"{label}_{digest}.json"
