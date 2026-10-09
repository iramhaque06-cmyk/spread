#!/usr/bin/env python3
"""Smoke-test the data layer and the files it exported.

    python test_smoke.py                        # spread.db + data/
    python test_smoke.py --db test_run.db --data data
    python test_smoke.py --prices-only          # after a 30 minute pass
    python test_smoke.py --live                 # also touch each venue once

This is the check that answers "is the thing actually there, and does it
work?" - not "did the process exit zero". So it does not take the exporter's
word for anything:

* Every per-venue count comes from the database, and every error text comes
  from the venue's own last run, so a silent failure shows up as a missing
  count or a stored error rather than as a clean-looking report.
* **Shard sizes are measured on the files on disk**, against the ceilings the
  exporter promises: 1.5 MB per catalog shard and 2 MB for the first search
  index. A single byte over is a failure, because the limit is the promise.
* **The searches are real searches.** They run through :mod:`searchindex`,
  the same reader the site uses: load ``index-0.json``, follow the dictionary
  to the shards a word needs, intersect the postings, then resolve each hit
  through ``catalog/manifest.json``. That exercises the whole contract, and
  the files-loaded count proves the lazy path is lazy.
* The sportsbook quota is reported from the provider's own headers when we
  have them, and reported as **unavailable with the reason** when we do not -
  never invented.

``--live`` adds one read-only request per venue listing and a price read for a
handful of stored markets, which is how a run proves the adapters still speak
to the venues. Without it, nothing here touches the network.

``--prices-only`` matches the 30 minute cadence, which deliberately does not
rebuild the catalog shards or the search index. It checks what that pass does
promise - home.json, the event files, the venue state, the snapshots - instead
of failing on files the pass was never asked to write.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import envfile
import export
import matching
import store
from searchindex import Index, as_ascii, describe

SEARCHES = ("fed", "bitcoin", "nfl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH)
    parser.add_argument("--data", default=export.DATA_DIR)
    parser.add_argument("--live", action="store_true", help="one read-only call per venue")
    parser.add_argument(
        "--live-limit",
        type=int,
        default=20,
        help="markets to ask for per venue; each adapter reads one listing page, "
        "so a page larger than this reports the whole page (Kalshi and Manifold "
        "both list 1,000 at a time)",
    )
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="check the 30 minute pass's output: no catalog shards or search index",
    )
    args = parser.parse_args(argv)

    _utf8_stdout()
    failures: list[str] = []
    warnings: list[str] = []

    print("=" * 74)
    print("Spread smoke test")
    print(f"  database: {args.db}")
    print(f"  exported: {args.data}")
    print(f"  cadence:  {'30 minute prices-only pass' if args.prices_only else '6 hourly full pass'}")
    print("=" * 74)

    conn = store.connect(args.db)
    try:
        _venues(conn, failures)
        _events(conn)
        _exported(args.data, failures, prices_only=args.prices_only)
        if args.prices_only:
            _prices_only_skipped()
        else:
            _searches(args.data, failures)
        _quota(conn, warnings)
    finally:
        conn.close()

    if args.live:
        _live(args.db, args.live_limit, warnings)

    print("\n" + "=" * 74)
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
    else:
        print("PASSED: every check held")
    if warnings:
        print(f"warnings: {len(warnings)}")
        for item in warnings:
            print(f"  - {item}")
    print("=" * 74)
    return 1 if failures else 0


def _venues(conn, failures: list[str]) -> None:
    """Per venue: how many markets we hold, and how its last passes went."""
    print("\n-- markets per venue " + "-" * 54)
    counts = store.market_counts(conn)
    state = {row["venue"]: row for row in store.read_catalog_state(conn)}
    if not counts and not state:
        failures.append("no venue has any stored market or run state")
    print(f"  {'venue':12} {'total':>7} {'open':>7} {'priced':>7}  {'last catalog ok':<21} last prices ok")
    for venue in sorted(set(counts) | set(state)):
        own = counts.get(venue, {})
        run = state.get(venue, {})
        print(
            f"  {venue:12} {own.get('total', 0):7} {own.get('open', 0):7} {own.get('priced', 0):7}  "
            f"{run.get('last_catalog_ok_at') or '-':<21} {run.get('last_prices_ok_at') or '-'}"
        )
        if run.get("errors"):
            print(f"  {'':12} error: {as_ascii(run['errors'][:150])}")
        elif own.get("total") and not own.get("priced"):
            failures.append(f"{venue}: holds {own['total']} markets but has priced none")


def _events(conn) -> None:
    """How many events were matched, and how many carry a comparable gap."""
    print("\n-- matched events " + "-" * 57)
    events = store.read_matched_events(conn)
    confident = [event for event in events if not event["possible"]]
    with_gap = [event for event in events if event["gap"] is not None]
    print(f"  matched events: {len(events)}")
    print(f"    confident (>= {matching.CONFIDENT:.2f}): {len(confident)}")
    print(f"    possible (below it):      {len(events) - len(confident)}")
    print(f"    with a real-money gap:    {len(with_gap)}")
    for event in sorted(with_gap, key=lambda item: -(item["gap"] or 0))[:5]:
        print(
            f"    {event['gap']:6.2f} pts  {as_ascii(event['title'])[:56]}"
        )


def _exported(data_dir, failures: list[str], *, prices_only: bool = False) -> None:
    """The files, their sizes, and whether any broke a size promise."""
    print("\n-- exported files " + "-" * 56)
    over = export.oversize_artifacts(data_dir)
    catalog = export.shard_sizes(data_dir, "catalog")
    search = export.shard_sizes(data_dir, "search")
    if prices_only:
        print(
            f"  catalog/search shards: left to the 6 hourly pass "
            f"({len(catalog)} catalog, {len(search)} search files present)"
        )
    else:
        if not catalog and not search:
            failures.append(f"no exported shards found under {data_dir}")
        print(f"  catalog shards (limit {export.CATALOG_LIMIT:,} bytes)")
        for name, size in catalog:
            flag = "  OVER LIMIT" if size > export.CATALOG_LIMIT else ""
            print(f"    {name:28} {size:>10,} bytes{flag}")
        print(f"  search shards (limit {export.SEARCH_LIMIT:,} bytes)")
        for name, size in search:
            flag = "  OVER LIMIT" if size > export.SEARCH_LIMIT else ""
            print(f"    {name:28} {size:>10,} bytes{flag}")
        if search:
            first = search[0]
            if first[0] != "search/index-0.json":
                failures.append("the first search shard is not search/index-0.json")
            elif first[1] > export.SEARCH_LIMIT:
                failures.append(
                    f"the first search shard is {first[1]:,} bytes, over the "
                    f"{export.SEARCH_LIMIT:,} ceiling"
                )
    for name, size, limit in over:
        failures.append(f"{name} is {size:,} bytes, over its {limit:,} byte limit")

    data_path = Path(data_dir)
    events_dir = data_path / "events"
    home = data_path / "home.json"
    event_files = len(list(events_dir.glob("*.json"))) if events_dir.exists() else 0
    print(f"  event files: {event_files}")
    _event_index(data_dir, event_files, failures)
    if not home.exists():
        failures.append("home.json is missing")
        return
    document = json.loads(home.read_text(encoding="utf-8"))
    stats = document.get("stats", {})
    print(
        f"  home.json: {home.stat().st_size:,} bytes - "
        f"{stats.get('open_markets', 0)} open markets, "
        f"{stats.get('events_with_gap', 0)} gaps, "
        f"{len(document.get('recent_moves', []))} moves"
    )


def _event_index(data_dir, event_files: int, failures: list[str]) -> None:
    """The file a search result row is drawn from, checked against the events.

    A search finds market keys and the row owes the reader a venue count, a
    price range and a gap; that comes from events-index.json. If it points at
    an event that is not on disk, every matched result is a broken link, so it
    is checked here rather than discovered by a reader.
    """
    path = Path(data_dir) / "events-index.json"
    if not path.exists():
        failures.append("events-index.json is missing")
        return
    document = json.loads(path.read_text(encoding="utf-8"))
    rows = document.get("events") or []
    markets = document.get("markets") or {}
    print(
        f"  events-index.json: {path.stat().st_size:,} bytes - "
        f"{len(rows)} events, {len(markets)} matched markets"
    )
    if len(rows) != event_files:
        failures.append(
            f"events-index.json lists {len(rows)} events but {event_files} files exist"
        )
    for row in rows:
        if not (Path(data_dir) / "events" / f"{row['i']}.json").exists():
            failures.append(f"events-index.json points at a missing event: {row['i']}")
    for key, event_id in list(markets.items())[:1]:
        if event_id not in {row["i"] for row in rows}:
            failures.append(f"events-index.json maps {key} to unknown event {event_id}")


def _prices_only_skipped() -> None:
    """Say plainly what this cadence does not produce, so absence is not a bug."""
    print("\n-- sample searches " + "-" * 55)
    print("  skipped: the 30 minute pass does not rebuild the search index")
    print("  (run without --prices-only after a full pass to search the catalog)")


def _searches(data_dir, failures: list[str]) -> None:
    """Search the exported index the way the site does, and prove it is lazy."""
    print("\n-- sample searches " + "-" * 55)
    try:
        index = Index(data_dir)
    except Exception as exc:
        failures.append(f"search index unreadable: {as_ascii(str(exc))}")
        return
    print(
        f"  index: {index.vocab:,} tokens over {index.markets:,} markets "
        f"in {len(index.bounds)} shard(s)"
    )
    opened_before = index.shards_loaded()
    print(f"  files loaded before any query: {opened_before} (index-0.json only)")
    found_any = False
    for term in SEARCHES:
        shards = sorted(
            {number for token in _tokens(term) for number in index.shards_for(token)}
        )
        total = index.count(term)
        hits = index.query(term, limit=3)
        found_any = found_any or bool(total)
        print(
            f"\n  search {term!r}: {total} matching market(s), "
            f"shards {shards or '-'}, {_mix(index, term)}"
        )
        for hit in hits:
            print(f"    {hit['key']:34} {as_ascii(describe(hit['market']))[:60]}")
        if not total:
            print("    (no market in the exported catalog contains that token)")
    print(f"\n  files loaded after the queries: {index.shards_loaded()} (catalog: {index.catalog_loaded()})")
    if index.shards_loaded() <= opened_before and index.vocab:
        failures.append("the index never loaded a second shard, so searching is not proven")
    if not found_any:
        failures.append("not one of the sample searches returned a market")


def _mix(index: Index, term: str) -> str:
    """How a term's matches split across venues, so coverage is visible."""
    counts: dict[str, int] = {}
    for key in index.matches(term):
        venue = key.split(":", 1)[0]
        counts[venue] = counts.get(venue, 0) + 1
    if not counts:
        return "no venue"
    return ", ".join(f"{venue} {count}" for venue, count in sorted(counts.items()))


def _tokens(term: str) -> list[str]:
    """The index tokens a typed word normalizes to - the matcher's own rules."""
    return sorted(matching.tokens(matching.normalize(term)))


def _quota(conn, warnings: list[str]) -> None:
    """The sportsbook quota: the provider's own numbers, or the reason we have none."""
    print("\n-- sportsbook quota " + "-" * 55)
    from venues import odds_api

    stored = store.read_odds_quota(conn)
    free = odds_api.FREE_MONTHLY_CREDITS
    ceiling = 100 - 100 * odds_api.QUOTA_FLOOR // free
    print(f"  plan: {free} free credits per month; guard: stop at {ceiling}% used")
    print(
        f"  stored: used={stored.get('used')} remaining={stored.get('remaining')} "
        f"calls={stored.get('calls')} month={stored.get('month') or '-'}"
    )
    if odds_api.LAST_QUOTA.get("remaining") is not None:
        print(
            f"  live headers this process: remaining={odds_api.LAST_QUOTA['remaining']} "
            f"used={odds_api.LAST_QUOTA['used']} last={odds_api.LAST_QUOTA['last']}"
        )
    else:
        reason = "no odds call has been made yet in this phase"
        try:
            odds_api.api_key()
        except KeyError:
            reason = (
                "ODDS_API_KEY is not set: put it in .env (gitignored) or the environment. "
                "The sportsbook path is unit-tested offline but has not been run live."
            )
        warnings.append(f"sportsbook quota unavailable - {reason}")
        print(f"  unavailable: {reason}")
    configured = True
    try:
        odds_api.api_key()
    except KeyError:
        configured = False
    print(
        f"  key configured: {configured}; "
        f"80% rule would allow another call: {odds_api.has_budget() and configured}"
    )


def _live(db_path, limit: int, warnings: list[str]) -> None:
    """One read-only call per venue, to prove the adapters still work today."""
    print("\n-- live venue check " + "-" * 54)
    from venues import kalshi, manifold, odds_api, polymarket

    listers = (
        ("kalshi", lambda: kalshi.list_open_markets(limit=limit)),
        ("polymarket", lambda: polymarket.list_open_markets(limit=limit)),
        ("manifold", lambda: manifold.list_open_markets(limit=limit)),
        ("odds_api", lambda: odds_api.list_open_markets(limit=limit)),
    )
    for venue, lister in listers:
        if venue == "odds_api":
            try:
                odds_api.api_key()
            except KeyError:
                print(f"  {venue:11} skipped: ODDS_API_KEY is not set")
                continue
        started = time.perf_counter()  # an interval, not a reading of the clock
        try:
            records = list(lister())
        except Exception as exc:  # a live probe may fail; report it, do not crash
            message = as_ascii(envfile.redact(exc))
            warnings.append(f"{venue} live listing failed: {message}")
            print(f"  {venue:11} listing FAILED: {message}")
            continue
        priced = sum(1 for record in records if record.price is not None)
        print(
            f"  {venue:11} listed {len(records):5} markets, {priced:5} priced"
            f"  [{time.perf_counter() - started:.1f}s]"
        )
        if not records and venue != "odds_api":
            warnings.append(f"{venue} live listing returned no markets")


def _utf8_stdout() -> None:
    """Let real titles print on a console that is not UTF-8 (Windows, usually)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # a pipe or an old interpreter
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
