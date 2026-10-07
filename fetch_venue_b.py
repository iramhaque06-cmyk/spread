#!/usr/bin/env python3
"""Pull open markets from venue B (Polymarket) in the SPEC category into SQLite.

The SPEC category is economic data events. Gamma has no working category
filter (`?category=Economics` is silently ignored - verified: it returned
non-economic events), so the category is Polymarket's tag ``economy``,
which carries every seed market in SPEC.md (Fed decision, CPI, jobs).

Same table shape as ``fetch_venue_a.venue_a_markets``, plus a ``venue``
column, stored in spread.db as ``venue_b_markets``. Note: Polymarket has
no series, so ``series_ticker`` holds the parent event slug - the nearest
analogue, and what the adapter uses to build the canonical URL.

Rate limit: every request goes through :func:`venues.get_json`, which
caches responses on disk and sleeps ``--pause`` seconds (default 0.5)
between real calls.

    python fetch_venue_b.py               # first 50 open economy events
    python fetch_venue_b.py --refresh     # ignore the response cache
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

import requests

import store
from venues import get_json, polymarket

VENUE = "polymarket"
TAG = "economy"  # SPEC category on Polymarket (verified: Fed/CPI/jobs all carry it)
EVENTS_LIMIT = 50  # bounded scan - be polite to the venue
MARKETS_PER_EVENT = 50
MAX_ERRORS = 5  # give up if the venue keeps failing

SCHEMA = """
CREATE TABLE IF NOT EXISTS venue_b_markets (
    venue           TEXT NOT NULL,
    ticker          TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    price_cents     REAL NOT NULL,
    volume          INTEGER,
    resolution_date TEXT,
    url             TEXT NOT NULL,
    series_ticker   TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
"""


def fetch_economic_events(
    limit: int, *, max_age: int, tag: str = TAG
) -> list[dict]:
    """Open events in the SPEC category: ``GET /events?tag_slug=economy``."""
    body = get_json(
        f"{polymarket.API}/events",
        params={"tag_slug": tag, "closed": "false", "limit": limit},
        max_age=max_age,
    )
    # Gamma honours `limit` today; bound ourselves anyway (Kalshi's /series
    # did not, and an unbounded scan turned 20 requests into 894).
    return (body if isinstance(body, list) else [])[: max(limit, 1)]


def is_open(market: dict) -> bool:
    """Events can contain already-resolved markets; only keep live ones."""
    return not market.get("closed") and bool(market.get("active"))


def row_from(market: dict, event: dict) -> dict:
    """One stored row; raises ValueError if the market has no price yet."""
    # Attach the parent event so the adapter builds the canonical
    # /event/{event}/{market} link instead of the redirecting fallback.
    quote = polymarket.parse_market(dict(market, events=[{"slug": event["slug"]}]))
    return {
        "venue": VENUE,
        "ticker": quote.market_id,
        "title": quote.title,
        "price_cents": quote.price_cents,
        "volume": quote.volume,
        "resolution_date": market.get("endDate")
        or market.get("endDateIso")
        or "",
        "url": quote.url,
        "series_ticker": event["slug"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def save(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert rows; re-running the script must not duplicate anything."""
    conn.executescript(SCHEMA)
    for row in rows:
        conn.execute(
            """
            INSERT INTO venue_b_markets (
                venue, ticker, title, price_cents, volume, resolution_date,
                url, series_ticker, fetched_at
            ) VALUES (:venue, :ticker, :title, :price_cents, :volume,
                      :resolution_date, :url, :series_ticker, :fetched_at)
            ON CONFLICT(ticker) DO UPDATE SET
                venue           = excluded.venue,
                title           = excluded.title,
                price_cents     = excluded.price_cents,
                volume          = excluded.volume,
                resolution_date = excluded.resolution_date,
                url             = excluded.url,
                series_ticker   = excluded.series_ticker,
                fetched_at      = excluded.fetched_at
            """,
            row,
        )
    conn.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH, help="path to the SQLite file")
    parser.add_argument(
        "--events", type=int, default=EVENTS_LIMIT,
        help=f"how many open category events to scan (default {EVENTS_LIMIT})",
    )
    parser.add_argument(
        "--tag", default=TAG, help=f"category tag to scan (default {TAG})"
    )
    parser.add_argument(
        "--pause", type=float, default=0.5,
        help="seconds to wait between real requests (default 0.5)",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="ignore the response cache"
    )
    args = parser.parse_args(argv)

    # One rate limit for the whole script, applied by the shared HTTP helper.
    import venues

    venues.REQUEST_PAUSE = max(args.pause, 0.0)
    max_age = 0 if args.refresh else venues.DEFAULT_MAX_AGE

    try:
        events = fetch_economic_events(args.events, max_age=max_age, tag=args.tag)
    except (requests.RequestException, ValueError) as exc:
        print(f"error: cannot list events for tag {args.tag!r}: {exc}", file=sys.stderr)
        return 2
    if not events:
        print(f"error: no open events for tag {args.tag!r}", file=sys.stderr)
        return 2

    rows: list[dict] = []
    errors = 0
    skipped = 0
    closed = 0
    for event in events:
        slug = event.get("slug") or ""
        markets = (event.get("markets") or [])[:MARKETS_PER_EVENT]
        for market in markets:
            if not is_open(market):
                closed += 1
                continue
            try:
                rows.append(row_from(market, event))
            except (ValueError, KeyError, TypeError) as exc:
                skipped += 1
                print(
                    f"warning: skipping {market.get('slug') or market.get('id')}: {exc}",
                    file=sys.stderr,
                )
        if not slug:
            errors += 1
            print("warning: event without a slug skipped", file=sys.stderr)
            if errors > MAX_ERRORS:
                print("error: too many venue failures, giving up", file=sys.stderr)
                return 2

    try:
        conn = store.connect(args.db)
        saved = save(conn, rows)
        total = conn.execute("SELECT COUNT(*) FROM venue_b_markets").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        print(f"error: cannot write {args.db}: {exc}", file=sys.stderr)
        return 2

    print(
        f"tag={args.tag}  events scanned={len(events)}  rows saved={saved}  "
        f"total in db={total}  no-price skipped={skipped}  "
        f"closed markets skipped={closed}  venue errors={errors}"
    )
    if not saved:
        print("error: no markets saved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
