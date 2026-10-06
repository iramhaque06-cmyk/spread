#!/usr/bin/env python3
"""Fetch every mapped event from both venues and write `spread.db`.

    python spread.py            # use cached responses where still fresh
    python spread.py --refresh  # force a live request to both venues
"""

from __future__ import annotations

import argparse

import mapping
import render
import store
from venues import DEFAULT_MAX_AGE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH, help="path to the SQLite file")
    parser.add_argument(
        "--refresh", action="store_true", help="ignore the response cache"
    )
    args = parser.parse_args(argv)

    pairs = mapping.fetch_pairs(
        mapping.load(), max_age=0 if args.refresh else DEFAULT_MAX_AGE
    )

    conn = store.connect(args.db)
    for event in pairs:
        store.upsert_event(conn, event.event_key, event.title)
        print(f"{event.event_key}  {event.title}")
        for venue in mapping.VENUES:
            quote = event.quotes.get(venue)
            if quote is None:
                print(f"  {venue:11} — no quote for this venue")
                continue
            store.upsert_observation(conn, event.event_key, quote)
            print(
                f"  {venue:11} {quote.price_cents:6.1f}c ({quote.price_kind})"
                f"  vol={quote.volume:,}  updated={quote.last_updated}"
            )
        if event.spread_points is None:
            print(f"  spread      n/a — missing {', '.join(event.missing)}")
        else:
            print(f"  spread      {event.spread_points:+.1f} points (Kalshi - Polymarket)")

    print(f"\n{args.db} now holds:")
    for row in store.read_observations(conn):
        print(
            f"  {row['event_key']}  {row['source']:11} {row['price_cents']:6.1f}c"
            f"  vol={row['volume']:,}  {row['url']}"
        )

    page = render.write_page(conn)
    print(f"\nwrote {page}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
