#!/usr/bin/env python3
"""Check the stored events against the invariants the matcher promises.

    python verify_events.py                # check spread.db
    python verify_events.py --db test.db   # a scratch database

Three things must never be true of the stored tables. Each one is a number a
reader cannot check for themselves, so each is reported as a **failure** with
a non-zero exit status rather than a warning that scrolls past:

1. **A venue appears twice in one event.** An event holds at most one market
   per venue (:func:`matching._one_market_per_venue`, asserted in
   :func:`matching._validate`). Two markets from one venue are two questions,
   and a gap between them would compare a venue with itself.
2. **An event is published without two fresh real-money venues.** Publication
   means a confident match (never ``possible``) with at least two distinct
   ``prediction_market``/``sportsbook`` venues quoting a fresh price. A
   play-money or community-forecast venue may sit in the event and never
   counts toward those two.
3. **The members do not state the same numbers.** Every member's extracted
   strikes must be one set (:func:`matching.strikes`): the same thresholds,
   the same units, years and dates stripped. A member stating nothing, or a
   different strike, is a pair that should never have been stored.

Read-only: the script opens the database, prints what it found and exits.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

import gaps
import matching
import store


def problems(conn, *, now=None) -> list[str]:
    """Every violation in the stored matching tables, as sentences."""
    events = store.read_matched_events(conn, include_possible=True)
    members = store.read_members(conn)
    by_event: dict[str, list[dict]] = defaultdict(list)
    for member in members:
        by_event[member["event_key"]].append(member)

    wanted = {(member["venue"], member["market_id"]) for member in members}
    markets = {
        (row["venue"], row["market_id"]): row
        for row in store.read_markets(conn, open_only=False)
        if (row["venue"], row["market_id"]) in wanted
    }

    found: list[str] = []
    for event in events:
        found.extend(
            _event_problems(
                event, by_event.get(event["event_key"], []), markets, now=now
            )
        )
    return found


def _event_problems(event, members, markets, *, now) -> list[str]:
    """The three checks, for one event."""
    key = event["event_key"]
    found: list[str] = []

    venues = Counter(member["venue"] for member in members)
    if len(members) < 2:
        found.append(f"{key}: an event needs two markets, it holds {len(members)}")
    for venue, count in sorted(venues.items()):
        if count > 1:
            tickers = sorted(
                member["market_id"] for member in members if member["venue"] == venue
            )
            found.append(
                f"{key}: {venue} appears {count} times ({', '.join(tickers)})"
            )

    if event["published"]:
        if event["possible"]:
            found.append(f"{key}: published while still a possible match")
        summary = gaps.summarise(gaps.event_prices(members, markets, now=now))
        fresh = summary["fresh_venues"]
        if len(fresh) < 2:
            found.append(
                f"{key}: published with {len(fresh)} fresh real-money venue(s)"
                f" ({', '.join(fresh) or 'none'})"
            )

    witness: tuple[str, frozenset] | None = None
    for member in members:
        row = markets.get((member["venue"], member["market_id"]))
        title = (row or {}).get("title") or ""
        stated = matching.strikes(title)
        if witness is None:
            witness = (f"{member['venue']} {member['market_id']}", stated)
            continue
        other, expected = witness
        if stated != expected:
            found.append(
                f"{key}: {other} states {_show(expected)} but "
                f"{member['venue']} {member['market_id']} states {_show(stated)}"
                f" ({title!r})"
            )
    return found


def _show(strikes: frozenset) -> str:
    return "{" + ", ".join(sorted(strikes)) + "}" if strikes else "no numbers"


def report(conn, *, now=None, stream=sys.stdout) -> tuple[int, int, int]:
    """Print the verdict; return ``(events, members, problems)``."""
    events = store.read_matched_events(conn, include_possible=True)
    found = problems(conn, now=now)
    members = len(store.read_members(conn))
    for line in found:
        print(f"FAIL: {line}", file=stream)
    if found:
        print(
            f"verify_events: {len(found)} problem(s) across {len(events)} events",
            file=stream,
        )
    else:
        print(
            f"verify_events: {len(events)} events, {members} members, no problems",
            file=stream,
        )
    return len(events), members, len(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH)
    args = parser.parse_args(argv)

    conn = store.connect(args.db)
    try:
        _, _, found = report(conn)
    finally:
        conn.close()
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
