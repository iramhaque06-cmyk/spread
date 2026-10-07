#!/usr/bin/env python3
"""Fetch every mapped event from both venues and write `spread.db` + `index.html`.

    python spread.py                    # use cached responses where still fresh
    python spread.py --refresh          # force a live request to both venues
    python spread.py --loop 300         # keep refreshing, rewriting the page
    python spread.py --refresh --dump-raw   # also save the raw API responses

Every run appends one price point per market to `price_snapshots`, rewrites
the flat table at `index.html`, and rewrites `site/data.json`, the single file
the website reads.

Every run prints, per venue, the timestamp the price is dated from and whether
that makes it fresh or stale; a pair with a stale side prints "excluded" rather
than a gap, matching the page it writes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import requests
from datetime import datetime, timezone
from pathlib import Path

import dataview
import freshness
import mapping
import render
import store
import venues
from venues import DEFAULT_MAX_AGE, Quote, kalshi, polymarket

RAW_DIR = Path(__file__).resolve().parent / "raw_samples"
MIN_LOOP_SECONDS = 60


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH, help="path to the SQLite file")
    parser.add_argument(
        "--refresh", action="store_true", help="ignore the response cache"
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="save the raw API response for one market per venue under raw_samples/",
    )
    parser.add_argument(
        "--loop",
        type=int,
        metavar="SECONDS",
        help=f"keep refreshing every SECONDS seconds (minimum {MIN_LOOP_SECONDS})",
    )
    parser.add_argument(
        "--data",
        default=dataview.DATA_PATH,
        help="where the website's data.json goes",
    )
    parser.add_argument(
        "--no-history-backfill",
        action="store_true",
        help="skip seeding Polymarket price history on a brand-new database",
    )
    args = parser.parse_args(argv)

    if args.loop is None:
        return cycle(args)

    seconds = max(MIN_LOOP_SECONDS, args.loop)
    print(f"refreshing every {seconds}s - press Ctrl-C to stop")
    try:
        while True:
            cycle(args)
            print(f"next refresh in {seconds}s\n")
            time.sleep(seconds)
    except KeyboardInterrupt:
        print("stopped")
        return 0


def cycle(args: argparse.Namespace) -> int:
    """One full pass: fetch both venues, store, then rewrite the page."""
    entries = mapping.load()
    # --loop always goes live: a cached answer would just repeat the last cycle.
    max_age = 0 if (args.refresh or args.loop) else DEFAULT_MAX_AGE
    pairs = mapping.fetch_pairs(entries, max_age=max_age)
    now = datetime.now(timezone.utc)

    conn = store.connect(args.db)
    for entry, event in zip(entries, pairs):
        store.upsert_event(
            conn,
            event.event_key,
            event.title,
            kalshi_rule=entry.get("kalshi_rule", ""),
            polymarket_rule=entry.get("polymarket_rule", ""),
            rules_differ=bool(entry.get("rules_differ")),
            rules_note=entry.get("rules_note", ""),
        )
        print(f"{event.event_key}  {event.title}")
        for venue in mapping.VENUES:
            quote = event.quotes.get(venue)
            if quote is None:
                print(f"  {venue:11} — no quote for this venue")
                continue
            store.upsert_observation(conn, event.event_key, quote)
            store.insert_snapshot(conn, event.event_key, quote)
            verdict = freshness.assess(quote, now=now)
            traded = "—" if quote.volume_24h is None else f"{quote.volume_24h:,}"
            print(
                f"  {venue:11} {quote.price_cents:6.1f}c ({quote.price_kind})"
                f"  vol={_count(quote.volume)}  vol_24h={traded}"
                f"  {'FRESH' if verdict.fresh else 'STALE'}: {verdict.basis}"
            )
            print(
                f"  {'':11} price_as_of={quote.price_as_of or '—'}"
                f"  updated={quote.last_updated or '—'}"
                f"  fetched={quote.fetched_at or '—'}"
            )

        gap = mapping.gap_for_pair(event, now=now)
        if gap is None:
            stale = [
                f"{venue}: {freshness.assess(event.quotes[venue], now=now).reason}"
                for venue in mapping.VENUES
                if event.quotes.get(venue) is not None
                and not freshness.assess(event.quotes[venue], now=now).fresh
            ]
            print(f"  spread      excluded - {', '.join(stale) or 'a venue is missing'}")
        else:
            print(f"  spread      {gap:+.1f} points (Kalshi - Polymarket)")

        summary = "differ" if entry.get("rules_differ") else "match"
        print(f"  rules       {summary}: {entry.get('kalshi_rule', '')}")
        for note in event.rule_notes:
            print(f"  rules check warning: {note}")
        if not event.rule_notes:
            print("  rules check ok - curated one-liners still match the live rule text")

    if not args.no_history_backfill:
        _backfill_history(conn, entries, max_age=max_age)

    if args.dump_raw:
        _dump_raw(entries[0], max_age=max_age)

    print(f"\n{args.db} now holds:")
    for row in store.read_observations(conn):
        print(
            f"  {row['event_key']}  {row['source']:11} {row['price_cents']:6.1f}c"
            f"  vol_24h={_count(row['volume_24h'])}  {row['url']}"
        )

    page = render.write_page(conn)
    data = dataview.write(conn, args.data, now=now)
    document = json.loads(data.read_text(encoding="utf-8"))
    points = sum(len(series) for event in document["events"] for series in event["history"].values())
    print(f"\nwrote {page}")
    print(
        f"wrote {data}  ({len(document['events'])} events, "
        f"{len(document['moves'])} moves, {points} history points)"
    )
    for status in document["venues_status"]:
        print(
            f"  {status['venue']:11} markets={status['market_count']}"
            f"  last_success={status['last_success_at']}"
        )
    conn.close()
    return 0


def _backfill_history(conn, entries: list[dict], *, max_age: int) -> None:
    """Seed real Polymarket price history once, so charts start with data.

    The CLOB exposes a day of history for any market, keyless, so the first
    run on a fresh database stores ~96 real points per market instead of
    leaving a one-point series. Kalshi publishes no price history at all, so
    its series genuinely begins at the first run - the site says so rather
    than interpolating points that never happened.
    """
    for entry in entries:
        slug = entry.get("polymarket")
        if not slug or store.has_snapshots(
            conn, entry["event_key"], "polymarket", source="clob"
        ):
            continue
        try:
            market = polymarket.fetch_market(slug, max_age=max_age)
            token_id = polymarket.yes_token_id(market)
            points = (
                polymarket.fetch_price_history(
                    token_id, max_age=max_age, interval="1d", fidelity=15
                )
                if token_id
                else []
            )
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(
                f"warning: {entry['event_key']}: history backfill skipped: {exc}",
                file=sys.stderr,
            )
            continue
        for point in points:
            at = datetime.fromtimestamp(
                int(point["t"]), timezone.utc
            ).isoformat(timespec="seconds")
            store.insert_snapshot(
                conn,
                entry["event_key"],
                Quote(
                    venue="polymarket",
                    market_id=slug,
                    title="",
                    price_cents=round(float(point["p"]) * 100, 2),
                    price_kind="last",
                    volume=None,
                    url="",
                    last_updated=at,
                    status="",
                    fetched_at=at,
                ),
                source="clob",
                at=at,
            )
        print(
            f"  history    seeded {len(points)} Polymarket points for "
            f"{entry['event_key']} from public CLOB history"
        )


def _dump_raw(entry: dict, *, max_age: int) -> None:
    """Save the raw response behind one market per venue, and print it.

    This is the evidence for "is the page live?": the JSON the venues are
    serving right now, straight from their own endpoints, unedited.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    samples: list[tuple[str, object]] = []

    ticker = entry.get("kalshi")
    if ticker:
        samples.append(
            (
                f"kalshi_market_{ticker}.json",
                venues.get_json(f"{kalshi.API}/markets/{ticker}", max_age=max_age),
            )
        )

    slug = entry.get("polymarket")
    if slug:
        body = venues.get_json(
            f"{polymarket.API}/markets", params={"slug": slug}, max_age=max_age
        )
        samples.append((f"polymarket_market_{slug}.json", body))
        token_id = polymarket.yes_token_id(body[0]) if body else None
        if token_id:
            samples.append(
                (
                    f"polymarket_clob_history_{slug}.json",
                    venues.get_json(
                        f"{polymarket.CLOB}/prices-history",
                        params={"market": token_id, "interval": "1d", "fidelity": 1},
                        max_age=max_age,
                    ),
                )
            )

    for name, body in samples:
        text = json.dumps(body, indent=2)
        (RAW_DIR / name).write_text(text + "\n", encoding="utf-8")
        print(f"\n--- raw API response: {name} ({len(text)} chars) ---")
        print(_preview(body, name, len(text)))


def _preview(body, name: str, size: int) -> str:
    """Print the response whole, except a long price history.

    The two market documents are small enough to show in full. A 1-minute
    price history runs to thousands of points, so only its tail is printed;
    the whole thing is on disk beside it.
    """
    points = body.get("history") if isinstance(body, dict) else None
    if isinstance(points, list) and len(points) > 10:
        return (
            f"{len(points)} points, {size} chars, saved in full to {RAW_DIR / name}.\n"
            "last three points:\n"
            + json.dumps({"history": points[-3:]}, indent=2)
        )
    return json.dumps(body, indent=2)


def _count(value) -> str:
    return "—" if value is None else f"{value:,}"


if __name__ == "__main__":
    raise SystemExit(main())
