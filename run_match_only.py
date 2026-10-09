#!/usr/bin/env python3
"""Run only steps 1-2 of the wide pipeline and print a reviewable summary.

This is the interrupted-plan slice: it does the wide catalog refresh,
the matching pass, the price refresh for matched events + top-by-volume,
gap scoring, and the export. It writes the static artifacts under
``data_staging/`` instead of ``data/``, so nothing here touches the live
site or the real ``data/`` tree.

After step 2 (matching) it does **not** continue onward by itself any more
than the rest of the pipeline; the only reason it runs the later steps at
all is that the user asked for a single command that produces the artifacts
to inspect. If you want to stop after matching in the future, pass
``--stop-after-matching``.

    python run_match_only.py --db test_run.db    # a scratch database
    python run_match_only.py --stop-after-matching
    python run_match_only.py --refresh           # force live requests
    python run_match_only.py --limit 5000        # bounded catalog slice
    python run_match_only.py --allow-real-db     # write the live spread.db

The live ``spread.db`` is what the published site is built from, so writing
it is refused unless ``--allow-real-db`` says that is really the intent. For
a scratch run, work on a copy (``cp spread.db test_run.db``) and pass ``--db``.

The printed summary is the part to review:

* how long the matching run took and its peak memory (RSS where available)
* the count of ``matched_events`` by category and by venue pair
* 30 random matched pairs as "venue A title | venue B title | score"
* 15 pairs with the lowest scores

Nothing here is uploaded anywhere; ``data_staging/`` is a local scratch
tree that you can delete when you are done with it.
"""
from __future__ import annotations

import argparse
import gc
import itertools
import json
import os
import random
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import catalog
import envfile
import export
import gaps
import matching
import store
import venues.kalshi as kalshi
import venues.manifold as manifold
import venues.odds_api as odds_api
import venues.polymarket as polymarket
import store as store_mod
import envfile as envfile_mod

ROOT = Path(__file__).resolve().parent
STAGING = ROOT / "data_staging"


def rss_bytes() -> int | None:
    """Current process RSS where the platform exposes it."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is KB on Linux, bytes on macOS in CPython's wrapping.
        return int(usage.ru_maxrss)
    except Exception:
        return None


def describe_rss(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} GB"
    if value >= 1_000:
        return f"{value / 1_000:.1f} MB"
    return f"{value} kB"


def sample_pairs(conn, *, n=30, seed=17):
    """Random matched pairs from the stored matching tables."""
    rows = conn.execute(
        """
        SELECT m.event_key, m.venue, m.market_id, m.confidence
        FROM matched_members m
        ORDER BY m.event_key, m.venue, m.market_id
        """
    ).fetchall()

    by_event: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_event[row["event_key"]].append(dict(row))

    pairs: list[tuple[str, str, str, str, float]] = []
    for event_key, members in by_event.items():
        venues = sorted(members, key=lambda item: (item["venue"], item["market_id"]))
        for i in range(len(venues)):
            for j in range(i + 1, len(venues)):
                a, b = venues[i], venues[j]
                rows_for_pair = conn.execute(
                    """
                    SELECT mm.confidence
                    FROM matched_members mm
                    WHERE mm.event_key = ?
                    ORDER BY mm.venue, mm.market_id
                    """,
                    (event_key,),
                ).fetchall()
                confidence = (
                    min(row["confidence"] for row in rows_for_pair)
                    if rows_for_pair
                    else 0.0
                )
                pairs.append(
                    (
                        a["venue"],
                        a["market_id"],
                        b["venue"],
                        b["market_id"],
                        float(confidence),
                    )
                )

    if not pairs:
        return []

    rng = random.Random(seed)
    rng.shuffle(pairs)
    chosen = pairs[:n]
    out: list[tuple[str, str, str]] = []
    for venue_a, market_a, venue_b, market_b, confidence in chosen:
        title_a = conn.execute(
            "SELECT title FROM markets WHERE venue = ? AND market_id = ?",
            (venue_a, market_a),
        ).fetchone()
        title_b = conn.execute(
            "SELECT title FROM markets WHERE venue = ? AND market_id = ?",
            (venue_b, market_b),
        ).fetchone()
        out.append(
            (
                title_a["title"] if title_a else market_a,
                title_b["title"] if title_b else market_b,
                f"{confidence:.2f}",
            )
        )
    return out


def lowest_pairs(conn, *, n=15):
    """Lowest-confidence matched pairs, ties broken by event key."""
    rows = conn.execute(
        """
        SELECT m.event_key, m.venue, m.market_id, m.confidence,
               e.title AS event_title,
               mk.title AS market_title
        FROM matched_members m
        JOIN matched_events e ON e.event_key = m.event_key
        LEFT JOIN markets mk ON mk.venue = m.venue AND mk.market_id = m.market_id
        ORDER BY m.confidence ASC, m.event_key ASC, m.venue, m.market_id
        """
    ).fetchall()

    by_event: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_event[row["event_key"]].append(dict(row))

    lowered: list[tuple[str, str, str]] = []
    for event_key, members in sorted(by_event.items()):
        venues = sorted(members, key=lambda m: (m["venue"], m["market_id"]))
        for i in range(len(venues)):
            for j in range(i + 1, len(venues)):
                a, b = venues[i], venues[j]
                title_a = a["market_title"] or a["market_id"]
                title_b = b["market_title"] or b["market_id"]
                confidence = a["confidence"]
                lowered.append((title_a, title_b, f"{confidence:.2f}"))
                if len(lowered) >= n:
                    return lowered
    return lowered


def venue_pair_counts(conn) -> dict[str, int]:
    """How many matched events touch each venue pair."""
    rows = conn.execute(
        """
        SELECT e.event_key, m.venue
        FROM matched_members m
        JOIN matched_events e ON e.event_key = m.event_key
        ORDER BY e.event_key, m.venue
        """
    ).fetchall()

    pairs: dict[str, int] = Counter()
    for event_key, group in itertools.groupby(
        rows, key=lambda item: item["event_key"]
    ):
        venues = sorted({item["venue"] for item in group})
        if len(venues) < 2:
            continue
        for i in range(len(venues)):
            for j in range(i + 1, len(venues)):
                pairs[f"{venues[i]} / {venues[j]}"] += 1
    return dict(pairs)


def category_counts(conn) -> dict[str, int]:
    """How many matched events fall in each category."""
    rows = conn.execute(
        "SELECT COALESCE(category, '') AS category FROM matched_events ORDER BY event_key"
    ).fetchall()
    return dict(Counter(row["category"] for row in rows))


def _stamp(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def _catalog_now(conn, max_age: int | None, *, limit: int | None = None, now=None):
    """Refresh the wide catalog from every listed venue.

    The work lives in :func:`catalog.sync_catalog`, not here. This script used
    to carry its own copy of the listing loop, which is how two versions of
    one closing rule could drift apart and leave one of them closing markets on
    a partial listing.
    """
    print("catalog step: listing every open market from each venue")
    report = catalog.sync_catalog(conn, max_age=max_age, limit=limit, now=now)
    for venue, item in report.items():
        print(
            f"  {venue:11} listed={item.get('listed', 0):6} "
            f"open={item.get('open', 0):6} closed={item.get('closed', 0):4} "
            f"errors={bool(item.get('errors'))}"
        )
        if item.get("refused"):
            print(f"    kept open: {item['refused']}")
    return report


def _matching_now(conn):
    """Match the stored open markets and replace the matched tables."""
    print("matching step: grouping open markets into real-world events")
    rows = store.read_markets(conn, open_only=True)
    before = len(rows)
    gc.collect()
    peak_before = rss_bytes()
    t0 = time.perf_counter()
    matches = matching.match_markets(rows)
    elapsed = time.perf_counter() - t0
    gc.collect()
    peak_after = rss_bytes()
    confident = [m for m in matches if not m.possible]
    by_key = {(row["venue"], row["market_id"]): row for row in rows}
    events, members = [], []
    for match in matches:
        event_members = [
            {
                "event_key": match.event_key,
                "venue": venue,
                "market_id": market_id,
                "outcome": outcome,
                "confidence": confidence,
                "rules_differ": rules_differ,
                "matched_on": matched_on,
                "inverted": inverted,
            }
            for (
                venue,
                market_id,
                outcome,
                confidence,
                rules_differ,
                matched_on,
                inverted,
            ) in match.members
        ]
        summary = gaps.summarise(gaps.event_prices(event_members, by_key))
        events.append(
            {
                "event_key": match.event_key,
                "title": match.title,
                "category": match.category,
                "close_time": match.close_time,
                "confidence": match.confidence,
                "possible": 1 if match.possible else 0,
                "published": 1 if summary["published"] and not match.possible else 0,
                "rules_differ": 1 if match.rules_differ else 0,
                "venue_count": len({member[0] for member in match.members}),
                "gap": None,
                "updated_at": _stamp(),
            }
        )
        members.extend(event_members)
    store.replace_matching(conn, events, members)
    peak = max(peak_before or 0, peak_after or 0)
    print(
        f"  markets scanned: {before:,}"
        f"  matched events: {len(matches):,}"
        f"  confident: {len(confident):,}  possible: {len(matches) - len(confident):,}"
        f"  members: {len(members):,}"
        f"  seconds: {elapsed:.2f}"
        f"  peak rss: {describe_rss(peak)}"
    )
    return {
        "markets": before,
        "events": len(matches),
        "confident": len(confident),
        "possible": len(matches) - len(confident),
        "members": len(members),
        "seconds": elapsed,
        "peak_rss": describe_rss(peak),
        "peak_rss_bytes": peak,
    }


def _prices_now(conn, max_age: int | None):
    """Refresh prices for top-by-volume and every matched-market."""
    print("prices step: top-by-volume + matched markets")
    top_ids: dict[str, set] = defaultdict(set)
    for row in store.top_markets_by_volume(conn, top=500):
        top_ids[row["venue"]].add(row["market_id"])
    matched_ids: dict[str, set] = defaultdict(set)
    for member in store.read_members(conn):
        matched_ids[member["venue"]].add(member["market_id"])

    fetchers = {
        "kalshi": kalshi.get_prices,
        "polymarket": polymarket.get_prices,
        "manifold": manifold.get_prices,
        "odds_api": odds_api.get_prices,
    }
    report: dict = {}
    for venue in fetchers:
        bulk = sorted(top_ids.get(venue, set()) - matched_ids.get(venue, set()))
        matched = sorted(matched_ids.get(venue, set()))
        if not bulk and not matched:
            continue
        updated = 0
        errors = ""
        try:
            for ids, detailed in ((bulk, False), (matched, True)):
                if not ids:
                    continue
                prices = _fetch_prices(venue, ids, detailed=detailed, max_age=max_age)
                updated += store.upsert_markets(conn, list(prices.values()), now=_stamp())
        except Exception as exc:
            errors = envfile.redact(exc)
            print(f"warning: {venue} prices failed: {errors}", file=sys.stderr)
            store.set_catalog_state(
                conn,
                venue,
                last_prices_ok_at="" if errors else _stamp(),
                errors=errors,
            )
            report[venue] = {
                "top": len(top_ids.get(venue, set())),
                "matched": len(matched_ids.get(venue, set())),
                "updated": updated,
                "errors": errors,
            }
            print(
                f"  {venue:11} top={len(top_ids.get(venue, set())):6} "
                f"matched={len(matched_ids.get(venue, set())):6} "
                f"updated={updated:6} errors={bool(errors)}"
            )
        quota = dict(odds_api.LAST_QUOTA)
        if quota.get("remaining") is not None:
            store.record_odds_call(
                conn,
                remaining=quota.get("remaining"),
                used=quota.get("used"),
                cost=quota.get("last"),
                at=quota.get("at") or _stamp(),
            )
        report["odds_quota"] = store.read_odds_quota(conn)
    return report


def _fetch_prices(venue: str, ids, *, detailed: bool, max_age):
    fetcher = {
        "kalshi": kalshi.get_prices,
        "polymarket": polymarket.get_prices,
        "manifold": manifold.get_prices,
        "odds_api": odds_api.get_prices,
    }[venue]
    if venue in ("polymarket", "manifold"):
        return fetcher(ids, max_age=max_age, detailed=detailed)
    return fetcher(ids, max_age=max_age)


def _gaps_now(conn):
    """Store the gap on each matched event from fresh real-money prices."""
    print("gaps step: scoring fresh real-money gaps")
    rows = store.read_markets(conn, open_only=True)
    by_key = {(row["venue"], row["market_id"]): row for row in rows}
    events = store.read_matched_events(conn)
    updated = 0
    with_gap = 0
    published = 0
    for event in events:
        members = store.read_members(conn, event["event_key"])
        summary = gaps.summarise(gaps.event_prices(members, by_key))
        live = 1 if summary["published"] and not event["possible"] else 0
        conn.execute(
            "UPDATE matched_events SET gap = ?, published = ? WHERE event_key = ?",
            (summary["gap"], live, event["event_key"]),
        )
        updated += 1
        published += live
        if summary["gap"] is not None:
            with_gap += 1
    conn.commit()
    print(
        f"  events scored: {updated:,}  with a fresh gap: {with_gap:,}"
        f"  published: {published:,}"
    )
    return {"events": updated, "with_gap": with_gap, "published": published}


def _export_now(conn):
    """Export static artifacts under ``data_staging/``."""
    print("export step: writing data_staging/")
    report = export.export_all(conn, out_dir=STAGING, limit=None, prices_only=False)
    print(f"  exported {report['markets']:,} markets to {STAGING}")
    if "catalog" in report:
        totals = report["catalog"]["totals"]
        print(
            f"  catalog: {totals['markets']:,} markets in {totals['shards']:,} shards, "
            f"{totals['bytes']:,} bytes"
        )
        for venue, item in sorted(report["catalog"]["venues"].items()):
            print(
                f"    {venue:11} {item['count']:6} markets  "
                f"{item['shards']:3} shards  max {item['max_bytes']:,} bytes"
            )
    if "search" in report:
        search = report["search"]
        print(f"  search: {search['vocab']:,} tokens in {search['shards']} shards")
        for item in search["files"]:
            print(
                f"    {item['file']:22} {item['count']:6} tokens  {item['bytes']:,} bytes"
            )
    print(
        f"  events: {report['events']['count']} files, "
        f"{report['events']['with_gap']} with a gap, "
        f"{report['events']['published']} published, "
        f"{report['events']['possible']} possible"
    )
    print(
        f"  home: {report['home']['top_gaps']} gaps, "
        f"{report['home']['recent_moves']} moves, {report['home']['bytes']:,} bytes"
    )
    print(
        f"  events-index: {report['event_index']['events']} events, "
        f"{report['event_index']['markets']} matched markets, "
        f"{report['event_index']['bytes']:,} bytes"
    )
    return report


def _summary(conn, match_report, export_report):
    """The reviewable summary the user asked for."""
    print("\n" + "=" * 74)
    print("MATCH RUN SUMMARY")
    print("=" * 74)

    print(f"\nmatching run took: {match_report['seconds']:.2f} seconds")
    print(f"matching peak memory: {match_report['peak_rss']}")

    cats = category_counts(conn)
    print("\nmatched_events by category:")
    for category, count in sorted(cats.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {category or '(unspecified)':32} {count:,}")

    pairs = venue_pair_counts(conn)
    print("\nmatched_events by venue pair:")
    for pair, count in sorted(pairs.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {pair:20} {count:,}")

    sample = sample_pairs(conn, n=30)
    print(f"\n30 random matched pairs (venue A title | venue B title | score):")
    for i, (title_a, title_b, score) in enumerate(sample, start=1):
        print(f"  {i:2}. {title_a} | {title_b} | {score}")

    low = lowest_pairs(conn, n=15)
    print(f"\n15 lowest-score matched pairs (venue A title | venue B title | score):")
    for i, (title_a, title_b, score) in enumerate(low, start=1):
        print(f"  {i:2}. {title_a} | {title_b} | {score}")

    staging_files = sorted(p for p in STAGING.rglob("*") if p.is_file())
    print(f"\ndata_staging/ files written: {len(staging_files):,}")
    total_bytes = sum(p.stat().st_size for p in staging_files)
    print(f"data_staging/ total size: {total_bytes:,} bytes")
    print("=" * 74)


def _refuse_live_db(db: str, *, allowed: bool) -> str:
    """Why this run must not touch `db`, or ``""`` when it may.

    ``spread.db`` is the file the published site is built from, and this
    script rewrites the catalog and the matching tables in place. A mistaken
    run is therefore not a failed experiment, it is wrong numbers on a live
    page, so the live path needs to be asked for explicitly.
    """
    if allowed:
        return ""
    if Path(db).resolve() != Path(store.DB_PATH).resolve():
        return ""
    return (
        f"refusing to write the live database {Path(store.DB_PATH).resolve()}\n"
        "  it is what the published site is built from. Work on a copy instead:\n"
        "    cp spread.db test_run.db && python run_match_only.py --db test_run.db\n"
        "  or pass --allow-real-db if writing the live file is really the intent."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH)
    parser.add_argument("--stop-after-matching", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="force live requests")
    parser.add_argument("--limit", type=int, help="max markets per venue (catalog)")
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="skip catalog + matching, only refresh prices + export",
    )
    parser.add_argument(
        "--allow-real-db",
        action="store_true",
        help="permit writing the live spread.db (the published site's source)",
    )
    args = parser.parse_args(argv)

    refusal = _refuse_live_db(args.db, allowed=args.allow_real_db)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    max_age = 0 if args.refresh else None
    limit = args.limit

    print("=" * 74)
    print("Spread match-only run")
    print(f"  database: {args.db}")
    print(f"  staging:  {STAGING}")
    print(f"  refresh:  {args.refresh}")
    print(f"  limit:    {limit or 'unlimited'}")
    print("=" * 74)

    conn = store.connect(args.db)
    try:
        gc.collect()
        peak_before = rss_bytes()

        if not args.prices_only:
            _catalog_now(conn, max_age=max_age, limit=limit)

        if args.stop_after_matching:
            match_report = _matching_now(conn)
        else:
            match_report = _matching_now(conn)

        if args.stop_after_matching:
            print("\nStopped after matching, as requested.")
            print("Add --stop-after-matching to keep the rest of the pipeline off for now.")
            return 0

        _prices_now(conn, max_age=max_age)
        _gaps_now(conn)
        print()
        export_report = _export_now(conn)
        _summary(conn, match_report, export_report)
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
