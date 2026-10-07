#!/usr/bin/env python3
"""Fill `spread.db` from every venue: catalog, prices, matching, gaps.

    python catalog.py                      # everything, whole catalog
    python catalog.py --limit 2000         # a bounded slice per venue
    python catalog.py --catalog-only       # just the 6-hourly listing pass
    python catalog.py --prices-only        # just the 30-minute refresh
    python catalog.py --db other.db --refresh

Order of a full pass, and why:

1. **Catalog** (:func:`sync_catalog) — every open market from Kalshi,
   Polymarket, Manifold and The Odds API, upserted. Parlay combos are
   excluded by the Kalshi adapter; nothing is scraped anywhere.
2. **Matching** (:func:`run_matching) — group markets about the same event,
   with a confidence and a rules_differ flag, before spending anything on
   prices.
3. **Prices** (:func:`refresh_prices) — the top markets by volume plus every
   market in a matched event. The sportsbook adapter spends quota here, and
   only here, under its 80% guard.
4. **Gaps** (:func:`compute_gaps) — max minus min of fresh real-money prices.

There is deliberately no separate `--catalog-only` order dependency: each step
is idempotent and reads only what the previous one stored.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import envfile
import gaps
import matching
import store
import venues
from venues import kalshi, manifold, odds_api, polymarket

CATALOG_VENUES = {
    "kalshi": kalshi.list_open_markets,
    "polymarket": polymarket.list_open_markets,
    "manifold": manifold.list_open_markets,
    "odds_api": odds_api.list_open_markets,
}
# Which venues can refresh a price for an arbitrary stored market id.
PRICE_FETCHERS = {
    "kalshi": kalshi.get_prices,
    "polymarket": polymarket.get_prices,
    "manifold": manifold.get_prices,
    "odds_api": odds_api.get_prices,
}
TOP_BY_VOLUME = 500
MATCH_BATCH = 40000  # records matched per pass, bounded so a run stays sane


def sync_catalog(
    conn, *, venues_wanted=None, limit=None, max_age=None, now=None
) -> dict:
    """Upsert every open market from each venue; close what disappeared."""
    report = {}
    wanted = venues_wanted or list(CATALOG_VENUES)
    for venue in wanted:
        lister = CATALOG_VENUES.get(venue)
        if lister is None:
            report[venue] = {"error": "no adapter"}
            continue
        started = time.time()
        seen: list[str] = []
        errors = ""
        try:
            records = lister(limit=limit, max_age=max_age) if max_age is not None \
                else lister(limit=limit)
            batch = []
            for record in records:
                batch.append(record)
                seen.append(record.market_id)
                if len(batch) >= 500:
                    store.upsert_markets(conn, batch, now=_stamp(now))
                    batch = []
            if batch:
                store.upsert_markets(conn, batch, now=_stamp(now))
            closed = store.mark_missing_closed(conn, venue, seen, now=_stamp(now))
        except Exception as exc:  # one venue must never sink the run
            errors = envfile.redact(exc)
            closed = 0
            print(f"warning: {venue} catalog failed: {errors}", file=sys.stderr)
        counts = store.market_counts(conn).get(venue, {})
        store.set_catalog_state(
            conn,
            venue,
            last_catalog_ok_at="" if errors else _stamp(now),
            markets=counts.get("open", 0),
            errors=errors,
            notes=f"listed={len(seen)} closed={closed} in {time.time() - started:.1f}s",
        )
        report[venue] = {
            "listed": len(seen),
            "open": counts.get("open", 0),
            "closed": closed,
            "errors": errors,
        }
    return report


def run_matching(conn, *, limit=None, now=None) -> dict:
    """Match the stored open markets and replace the matched tables."""
    rows = store.read_markets(conn, open_only=True, limit=limit or MATCH_BATCH)
    matches = matching.match_markets(rows)
    events, members = [], []
    for match in matches:
        events.append(
            {
                "event_key": match.event_key,
                "title": match.title,
                "category": match.category,
                "close_time": match.close_time,
                "confidence": match.confidence,
                "possible": 1 if match.possible else 0,
                "rules_differ": 1 if match.rules_differ else 0,
                "venue_count": len({member[0] for member in match.members}),
                "gap": None,
                "updated_at": _stamp(now),
            }
        )
        for venue, market_id, outcome, confidence, rules_differ, matched_on in match.members:
            members.append(
                {
                    "event_key": match.event_key,
                    "venue": venue,
                    "market_id": market_id,
                    "outcome": outcome,
                    "confidence": confidence,
                    "rules_differ": rules_differ,
                    "matched_on": matched_on,
                }
            )
    store.replace_matching(conn, events, members)
    confident = [match for match in matches if not match.possible]
    return {
        "markets": len(rows),
        "events": len(matches),
        "confident": len(confident),
        "possible": len(matches) - len(confident),
        "members": len(members),
    }


def refresh_prices(conn, *, top=TOP_BY_VOLUME, max_age=None, now=None) -> dict:
    """Refresh the top markets by volume plus every market in a matched event.

    The two sets are fetched differently on purpose. The top-of-volume set is
    wide, so it takes the cheap path (one request per market on Gamma, one
    batched call on Kalshi). Matched events are few and are what a gap is built
    from, so they pay for the expensive path that carries a real price time.
    """
    top_ids: dict[str, set] = {}
    for row in store.top_markets_by_volume(conn, top):
        top_ids.setdefault(row["venue"], set()).add(row["market_id"])
    matched_ids: dict[str, set] = {}
    for member in store.read_members(conn):
        matched_ids.setdefault(member["venue"], set()).add(member["market_id"])

    report = {}
    for venue in PRICE_FETCHERS:
        bulk = top_ids.get(venue, set())
        matched = matched_ids.get(venue, set())
        if not bulk and not matched:
            continue
        errors = ""
        updated = 0
        try:
            for ids, detailed in ((sorted(bulk - matched), False), (sorted(matched), True)):
                if not ids:
                    continue
                prices = _fetch_prices(venue, ids, detailed=detailed, max_age=max_age)
                updated += store.upsert_markets(
                    conn, list(prices.values()), now=_stamp(now)
                )
        except Exception as exc:
            errors = envfile.redact(exc)
            print(f"warning: {venue} prices failed: {errors}", file=sys.stderr)
        store.set_catalog_state(
            conn,
            venue,
            last_prices_ok_at="" if errors else _stamp(now),
            errors=errors,
        )
        report[venue] = {
            "top": len(bulk),
            "matched": len(matched),
            "updated": updated,
            "errors": errors,
        }

    quota = odds_api.LAST_QUOTA
    if quota.get("remaining") is not None:
        store.record_odds_call(
            conn,
            remaining=quota.get("remaining"),
            used=quota.get("used"),
            cost=quota.get("last"),
            at=quota.get("at") or _stamp(now),
        )
    report["odds_quota"] = store.read_odds_quota(conn)
    return report


def compute_gaps(conn, *, now=None, include_possible: bool = False) -> dict:
    """Store the gap on each matched event, from fresh real-money prices only."""
    rows = store.read_markets(conn, open_only=True)
    by_key = {(row["venue"], row["market_id"]): row for row in rows}
    events = store.read_matched_events(conn, include_possible=include_possible)
    updated = 0
    none_yet = 0
    for event in events:
        members = store.read_members(conn, event["event_key"])
        summary = gaps.summarise(
            gaps.event_prices(members, by_key, now=now)
        )
        if summary["gap"] is None:
            none_yet += 1
        conn.execute(
            "UPDATE matched_events SET gap = ? WHERE event_key = ?",
            (summary["gap"], event["event_key"]),
        )
        updated += 1
    conn.commit()
    return {"events": updated, "with_gap": updated - none_yet, "without_gap": none_yet}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH)
    parser.add_argument("--limit", type=int, help="max markets per venue (catalog)")
    parser.add_argument("--top", type=int, default=TOP_BY_VOLUME)
    parser.add_argument("--venues", default="", help="comma-separated subset")
    parser.add_argument("--catalog-only", action="store_true")
    parser.add_argument("--prices-only", action="store_true")
    parser.add_argument("--match-only", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="ignore the cache")
    args = parser.parse_args(argv)

    max_age = 0 if args.refresh else None
    wanted = [name for name in args.venues.split(",") if name] or None
    conn = store.connect(args.db)
    now = datetime.now(timezone.utc)

    if args.prices_only or args.match_only:
        steps = []
        if args.match_only:
            steps.append(("matching", lambda: run_matching(conn, now=now)))
        steps.append(("prices", lambda: refresh_prices(conn, top=args.top, max_age=max_age, now=now)))
        steps.append(("gaps", lambda: compute_gaps(conn, now=now)))
    elif args.catalog_only:
        steps = [("catalog", lambda: sync_catalog(conn, venues_wanted=wanted, limit=args.limit, max_age=max_age, now=now))]
    else:
        steps = [
            ("catalog", lambda: sync_catalog(conn, venues_wanted=wanted, limit=args.limit, max_age=max_age, now=now)),
            ("matching", lambda: run_matching(conn, now=now)),
            ("prices", lambda: refresh_prices(conn, top=args.top, max_age=max_age, now=now)),
            ("gaps", lambda: compute_gaps(conn, now=now)),
        ]

    for name, step in steps:
        started = time.time()
        result = step()
        print(f"{name}: {result}  [{time.time() - started:.1f}s]", flush=True)

    print("\nmarket counts:", store.market_counts(conn))
    conn.close()
    return 0


def _fetch_prices(venue: str, ids, *, detailed: bool, max_age):
    """Call a venue's price fetcher, using the detailed form where it has one."""
    fetcher = PRICE_FETCHERS[venue]
    if venue in ("polymarket", "manifold"):
        return fetcher(ids, max_age=max_age, detailed=detailed)
    return fetcher(ids, max_age=max_age)


def _stamp(now) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
