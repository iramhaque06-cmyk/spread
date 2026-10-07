#!/usr/bin/env python3
"""Pull open markets from venue A (Kalshi) in the SPEC category into SQLite.

The SPEC category is economic data events, which on Kalshi is the series
category "Economics". Each open market is stored as title, price (cents),
volume, resolution date and public URL in spread.db, table
``venue_a_markets``.

Rate limit: every request goes through :func:`venues.get_json`, which
caches responses on disk and sleeps ``--pause`` seconds (default 0.5)
between real calls, so a scan of 20 series costs at most ~15 requests.

    python fetch_venue_a.py               # first 20 economic series
    python fetch_venue_a.py --refresh     # ignore the response cache
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

import requests

import store
from venues import get_json, kalshi

CATEGORY = "Economics"  # SPEC: one category, economic data events
SERIES_LIMIT = 20  # bounded scan - be polite to the venue
MARKETS_PER_SERIES = 50
MAX_ERRORS = 5  # give up if the venue keeps failing

SCHEMA = """
CREATE TABLE IF NOT EXISTS venue_a_markets (
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


def fetch_economic_series(limit: int, *, max_age: int) -> list[dict]:
    """Series in the SPEC category: ``GET /series?category=Economics``.

    Kalshi ignores ``limit`` on this endpoint (it answers with every
    economic series), so the bound is applied here - otherwise a "20
    series" scan quietly becomes 894 requests.
    """
    body = get_json(
        f"{kalshi.API}/series",
        params={"category": CATEGORY, "limit": limit},
        max_age=max_age,
    )
    return (body.get("series") or [])[: max(limit, 1)]


def fetch_open_markets(series: dict, *, max_age: int) -> list[dict]:
    """Open markets of one series: ``GET /markets?series_ticker=...&status=open``."""
    body = get_json(
        f"{kalshi.API}/markets",
        params={
            "series_ticker": series["ticker"],
            "status": "open",
            "limit": MARKETS_PER_SERIES,
        },
        max_age=max_age,
    )
    return body.get("markets") or []


def row_from(market: dict, series: dict) -> dict:
    """One stored row; raises ValueError if the market has no price yet."""
    quote = kalshi.parse_market(market, series=series)  # title/price/volume/url
    return {
        "ticker": quote.market_id,
        "title": quote.title,
        "price_cents": quote.price_cents,
        "volume": quote.volume,
        "resolution_date": market.get("close_time")
        or market.get("expiration_time")
        or "",
        "url": quote.url,
        "series_ticker": series["ticker"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def save(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert rows; re-running the script must not duplicate anything."""
    conn.executescript(SCHEMA)
    for row in rows:
        conn.execute(
            """
            INSERT INTO venue_a_markets (
                ticker, title, price_cents, volume, resolution_date,
                url, series_ticker, fetched_at
            ) VALUES (:ticker, :title, :price_cents, :volume, :resolution_date,
                      :url, :series_ticker, :fetched_at)
            ON CONFLICT(ticker) DO UPDATE SET
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
        "--series", type=int, default=SERIES_LIMIT,
        help=f"how many economic series to scan (default {SERIES_LIMIT})",
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
        series_list = fetch_economic_series(args.series, max_age=max_age)
    except (requests.RequestException, ValueError) as exc:
        print(f"error: cannot list {CATEGORY} series: {exc}", file=sys.stderr)
        return 2
    if not series_list:
        print(f"error: no series returned for category {CATEGORY!r}", file=sys.stderr)
        return 2

    rows: list[dict] = []
    errors = 0
    skipped = 0
    for series in series_list:
        try:
            markets = fetch_open_markets(series, max_age=max_age)
        except (requests.RequestException, ValueError) as exc:
            errors += 1
            print(f"warning: {series.get('ticker')}: {exc}", file=sys.stderr)
            if errors > MAX_ERRORS:
                print("error: too many venue failures, giving up", file=sys.stderr)
                return 2
            continue
        for market in markets:
            try:
                rows.append(row_from(market, series))
            except (ValueError, KeyError, TypeError) as exc:
                skipped += 1
                print(f"warning: skipping {market.get('ticker')}: {exc}", file=sys.stderr)

    try:
        conn = store.connect(args.db)
        saved = save(conn, rows)
        total = conn.execute("SELECT COUNT(*) FROM venue_a_markets").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        print(f"error: cannot write {args.db}: {exc}", file=sys.stderr)
        return 2

    print(
        f"category={CATEGORY}  series scanned={len(series_list)}  "
        f"rows saved={saved}  total in db={total}  "
        f"skipped={skipped}  venue errors={errors}"
    )
    if not saved:
        print("error: no markets saved", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
