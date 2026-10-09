#!/usr/bin/env python3
"""Export the catalog to static JSON under `data/`, ready for a CDN.

    python export.py                       # everything, from spread.db
    python export.py --db test_run.db      # a scratch database
    python export.py --prices-only         # home.json + events/ only (30 min job)
    python export.py --limit 2000          # bound how many markets go out
    python export.py --out /tmp/data       # somewhere else

The site is static, so every read it does has to be a plain file fetch. The
files are shaped for that and nothing else:

``data/catalog/manifest.json``
    The legend and the map. Carries the compact field names, per-venue counts
    and byte sizes, and the first/last key of every shard so a client can find
    one market without downloading the catalog.
``data/catalog/<venue>-<n>.json``
    The markets themselves, in compact keyed form, each shard at or under
    :data:`CATALOG_LIMIT` bytes. Shards are ordered by market id, so the
    manifest's ``from``/``to`` pair resolves any key by binary search.
``data/search/index-0.json``
    The search dictionary, and the first slice of postings. It carries every
    token shard's alphabet range, so a client knows which file a typed prefix
    belongs to without fetching one, then the postings for the front range.
    Shards are numbered from zero, so this is the one file a client fetches to
    start searching at all (PLAN.md §4).
``data/search/index-<n>.json``
    Postings for one alphabetical slice: normalized token -> market keys. The
    client binary-searches ``bounds`` in shard 0 and fetches exactly the slice
    the typed text lands in, never the rest.
``data/events/<id>.json``
    One matched event: its comparable prices with freshness already decided,
    whether it is publishable (two fresh real-money venues on a confident
    match, never a play-money one counted double), the price history the
    pipeline actually observed, the venue rule text and the source links.
``data/events-index.json``
    The bridge between the two: which matched event a market key belongs to,
    and each event's venue count, price range and gap. A search finds markets;
    this is what lets a result row show a comparison without fetching a
    hundred event files.
``data/home.json``
    The landing document: the widest gaps, the biggest recent moves, per-venue
    health and the sportsbook quota.

Two things this file deliberately does *not* do. It never re-derives a gap or
a freshness verdict: those come from :mod:`gaps` and :mod:`freshness`, the
same functions the curated page uses, so the static site cannot quietly widen
either rule. And it never invents a price point: history is the
``market_snapshots`` log, one row per market per run, so a chart is a record
of what was observed rather than a reconstruction.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import envfile
import freshness
import gaps
import matching
import store

DATA_DIR = Path(__file__).resolve().parent / "data"

# "1.5 MB" and "2 MB" as a browser would read them, so the limits hold under
# either the decimal or the binary reading.
CATALOG_LIMIT = 1_500_000
SEARCH_LIMIT = 2_000_000
# Space the greedy fill reserves for the wrapper around a shard's records, so
# it cannot land a hair over the limit before the exact check below runs.
WRAPPER_SLACK = 4096

HISTORY_POINTS = 96  # per market: one day at the 15 minute cadence
MOVES_WINDOW_HOURS = 24
TOP_GAPS = 50
TOP_MOVES = 50
RULE_TEXT_LIMIT = 600

# The compact catalog fields, and the legend written into the manifest so the
# short names stay readable from the file alone.
CATALOG_FIELDS = {
    "v": "venue",
    "i": "market_id",
    "y": "venue_type",
    "t": "title",
    "c": "category",
    "e": "close_time",
    "o": "outcomes, each {n: name, p: price}",
    "p": "price, in points (cents)",
    "k": "price_kind",
    "w": "volume",
    "d": "volume_24h",
    "u": "url",
    "s": "status",
    "a": "price_as_of",
    "b": "as_of_kind",
    "r": "rule_text, truncated",
    "f": "fetched_at",
    "x": "is_stale, judged here by freshness.py",
}

# The compact event-index fields, for the same reason: the file is read by JS.
EVENT_INDEX_FIELDS = {
    "i": "event id, as the filename under events/",
    "t": "title, the matched event's own",
    "c": "category, from the parent venue event",
    "e": "close_time",
    "g": "gap, in points; null when fewer than two fresh real-money prices",
    "n": "venue_count, how many venues cover this event",
    "y": "venue types present: prediction_market, sportsbook, play_money, forecast",
    "lo": "lowest comparable price, in points",
    "hi": "highest comparable price, in points",
    "x": "is_stale: the range shown had to fall back to prices past the freshness limit",
    "w": "combined volume across the event's markets",
    "p": "possible: the match is below the confidence floor",
    "r": "rules_differ: the venues' rules are not the same",
}


def market_key(venue: str, market_id: str) -> str:
    """The one string that names a market across every exported file."""
    return f"{venue}:{market_id}"


# --------------------------------------------------------------------------- #
# Catalog shards
# --------------------------------------------------------------------------- #


def compact_market(row: dict, *, now: datetime) -> dict:
    """One stored market in the compact form the catalog shards carry."""
    verdict = freshness.assess(row, now=now)
    outcomes = [
        {"n": item.get("name") or "", "p": item.get("price")}
        for item in (row.get("outcomes") or [])
    ]
    return {
        "v": row["venue"],
        "i": row["market_id"],
        "y": row["venue_type"],
        "t": row["title"],
        "c": row["category"],
        "e": row["close_time"],
        "o": outcomes,
        "p": None if row["price"] is None else round(float(row["price"]), 2),
        "k": row["price_kind"],
        "w": row["volume"],
        "d": row["volume_24h"],
        "u": row["url"],
        "s": row["status"],
        "a": row["price_as_of"],
        "b": row["as_of_kind"],
        "r": (row["rule_text"] or "")[:RULE_TEXT_LIMIT],
        "f": row["fetched_at"],
        "x": not verdict.fresh,
    }


def build_catalog(conn, out_dir: Path, *, now: datetime, limit: int | None = None) -> dict:
    """Write every open market to sharded, compact JSON; return the manifest."""
    rows = store.read_markets(conn, open_only=True, limit=limit)
    by_venue: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_venue[row["venue"]].append(row)

    catalog_dir = out_dir / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    _clear(catalog_dir, "*.json")

    venues: dict[str, dict] = {}
    total_bytes = 0
    total_shards = 0
    for venue in sorted(by_venue):
        # Sorting by market id is what makes the manifest's from/to range - and
        # a client's binary search over it - correct.
        records = [
            compact_market(row, now=now)
            for row in sorted(by_venue[venue], key=lambda item: item["market_id"])
        ]
        groups = _greedy_fill(records, [_size(record) for record in records], CATALOG_LIMIT)
        files, splits = _write_groups(
            catalog_dir,
            venue,
            groups,
            CATALOG_LIMIT,
            lambda number, all_groups, group, venue=venue: _catalog_document(
                venue, number, len(all_groups), group
            ),
        )
        venues[venue] = {
            "venue_type": records[0]["y"] if records else "",
            "count": len(records),
            "bytes": sum(item["bytes"] for item in files),
            "shards": files,
            "split_on_verify": splits,
        }
        total_bytes += venues[venue]["bytes"]
        total_shards += len(files)

    manifest = {
        "generated_at": _stamp(now),
        "limit_bytes": CATALOG_LIMIT,
        "fields": CATALOG_FIELDS,
        "venues": venues,
        "totals": {
            "markets": sum(item["count"] for item in venues.values()),
            "shards": total_shards,
            "bytes": total_bytes,
        },
    }
    _write(catalog_dir / "manifest.json", manifest)
    return manifest


def _catalog_document(venue: str, number: int, count: int, group: list) -> dict:
    return {
        "v": venue,
        "n": number,
        "shards": count,
        "from": group[0]["i"],
        "to": group[-1]["i"],
        "markets": group,
    }


# --------------------------------------------------------------------------- #
# Search index
# --------------------------------------------------------------------------- #


def index_text(row: dict) -> set[str]:
    """The normalized tokens one market is findable by.

    The title carries the meaning, so it is the bulk of it; the category and
    the venue name are added so that "economics" and "kalshi" both work as
    searches. The tokenizer is :mod:`matching`'s, so a search and a match agree
    on what "100,000" and "100k" mean.
    """
    text = matching.normalize(
        " ".join(part for part in (row["title"], row["category"], row["venue"]) if part)
    )
    return matching.tokens(text)


def build_search(conn, out_dir: Path, *, now: datetime, limit: int | None = None) -> dict:
    """Write the token index: a shard-1 dictionary plus alphabetical postings.

    A token's postings are cut into pieces before sharding. Without that, one
    very common token - "kalshi" appears on every Kalshi market - would be a
    single JSON value that no shard boundary could ever fit, and the size
    limit would fail on exactly the token a reader is most likely to type. A
    token whose postings span shards is listed against all of them in shard
    1's dictionary, and a client concatenates the pieces.
    """
    rows = store.read_markets(conn, open_only=True, limit=limit, by_volume=True)
    postings: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        key = market_key(row["venue"], row["market_id"])
        for token in index_text(row):
            postings[token].append(key)

    search_dir = out_dir / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    _clear(search_dir, "*.json")

    tokens = sorted(postings)
    entries = [
        (token, piece)
        for token in tokens
        for piece in _pieces(sorted(postings[token]))
    ]
    # Shard 1 also carries the dictionary - which shard(s) hold each token - so
    # it starts with a smaller postings budget than the shards after it. The
    # reservation is the worst case, one entry per token, so it cannot be an
    # underestimate.
    reserved = sum(len(token) + 14 for token in tokens) + WRAPPER_SLACK
    groups = _greedy_fill(
        entries,
        [_size({"t": token, "p": keys}) for token, keys in entries],
        SEARCH_LIMIT,
        first_limit=max(SEARCH_LIMIT // 4, SEARCH_LIMIT - reserved),
    )

    def document(number, all_groups, group):
        merged: dict[str, list[str]] = {}
        for token, keys in group:
            merged.setdefault(token, []).extend(keys)
        built = {
            "shard": number,
            "from": group[0][0],
            "to": group[-1][0],
            "postings": merged,
        }
        # Numbered from zero, so shard 0 is the entry point a client fetches
        # first and carries the dictionary (PLAN.md §4).
        if number == 0:
            built["generated_at"] = _stamp(now)
            built["markets"] = len(rows)
            built["vocab"] = len(tokens)
            built["bounds"] = [[item[0][0], item[-1][0]] for item in all_groups]
            built["tokens"] = _dictionary(all_groups)
        return built

    files, splits = _write_groups(
        search_dir, "index", groups, SEARCH_LIMIT, document, start=0
    )
    # A token whose postings were cut into pieces appears in more than one
    # group; the count a reader cares about is tokens, not pieces.
    for group, item in zip(groups, files):
        item["count"] = len({token for token, _keys in group})

    return {
        "generated_at": _stamp(now),
        "limit_bytes": SEARCH_LIMIT,
        "markets": len(rows),
        "vocab": len(tokens),
        "shards": len(files),
        "bounds": [[item[0][0], item[-1][0]] for item in groups],
        "files": files,
        "split_on_verify": splits,
    }


def _pieces(keys: list[str]):
    """Cut one token's postings so no piece is a large fraction of a shard."""
    piece: list[str] = []
    total = 0
    for key in keys:
        size = _size(key)
        if piece and total + size > SEARCH_LIMIT // 8:
            yield piece
            piece, total = [], 0
        piece.append(key)
        total += size
    if piece:
        yield piece


def _dictionary(groups) -> dict:
    """Which shard(s) hold each token, as the always-loaded lookup."""
    out: dict = {}
    for number, group in enumerate(groups):
        for token, _keys in group:
            existing = out.get(token)
            if existing is None:
                out[token] = number
            elif isinstance(existing, list):
                if existing[-1] != number:
                    existing.append(number)
            elif existing != number:
                out[token] = [existing, number]
    return out


# --------------------------------------------------------------------------- #
# Events and the home document
# --------------------------------------------------------------------------- #


def build_event(
    conn, event: dict, members: list[dict], markets_by_key: dict, *, now: datetime
) -> dict:
    """One matched event as the site reads it: prices, history, rules, sources."""
    points = gaps.event_prices(members, markets_by_key, now=now)
    summary = gaps.summarise(points)
    # A pairing below the matcher's confidence floor is shown, labelled, and
    # never counted in a gap (PLAN.md section 2.2). catalog.compute_gaps()
    # already holds that line for the stored column; this holds it for the
    # document, which is the copy the site reads.
    if event["possible"]:
        summary["gap"] = None
    # Publication is the same rule: a confident match with two fresh
    # real-money venues. A play-money or forecast venue may sit in the event,
    # labelled, without ever making it publishable.
    published = bool(summary["published"]) and not event["possible"]

    member_by_key = {
        market_key(member["venue"], member["market_id"]): member for member in members
    }
    prices = []
    for point in points:
        key = market_key(point["venue"], point["market_id"])
        member = member_by_key.get(key, {})
        row = markets_by_key.get((point["venue"], point["market_id"]), {})
        raw, margin = _book_terms(point, member, row)
        prices.append(
            {
                "venue": point["venue"],
                "market_id": point["market_id"],
                "venue_type": point["venue_type"],
                "outcome": member.get("outcome") or "",
                "price": point["price"],
                "raw": raw,
                "margin": margin,
                "fresh": point["fresh"],
                "real_money": point["real_money"],
                "gap_eligible": point["gap_eligible"],
                "basis": point["basis"],
                "reason": point["reason"],
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "price_as_of": row.get("price_as_of"),
                "as_of_kind": row.get("as_of_kind") or "none",
                "volume": row.get("volume"),
                "volume_24h": row.get("volume_24h"),
                "confidence": member.get("confidence"),
                "rules_differ": bool(member.get("rules_differ")),
                "matched_on": member.get("matched_on") or "",
            }
        )

    rules = {}
    history = {}
    sources = []
    for member in members:
        key = market_key(member["venue"], member["market_id"])
        row = markets_by_key.get((member["venue"], member["market_id"]), {})
        rules[key] = {
            "venue": member["venue"],
            "title": row.get("title") or "",
            "rule_text": row.get("rule_text") or "",
            "url": row.get("url") or "",
            "outcome": member.get("outcome") or "",
        }
        history[key] = store.read_market_history(
            conn, member["venue"], member["market_id"], limit=HISTORY_POINTS
        )
        sources.append(
            {
                "venue": member["venue"],
                "market_id": member["market_id"],
                "url": row.get("url") or "",
                "fetched_at": row.get("fetched_at") or "",
            }
        )

    return {
        "id": event["event_key"],
        "title": event["title"],
        "category": event["category"],
        "close_time": event["close_time"],
        "confidence": event["confidence"],
        "possible": event["possible"],
        "published": published,
        "rules_differ": event["rules_differ"],
        "gap": summary["gap"],
        "venue_count": event["venue_count"],
        "venues": summary["venues"],
        "fresh_count": summary["fresh"],
        "real_money_count": summary["real_money"],
        "updated_at": event["updated_at"] or _stamp(now),
        "prices": prices,
        "history": history,
        "rules": rules,
        "sources": sources,
    }


def _book_terms(point: dict, member: dict, row: dict) -> tuple[float | None, float | None]:
    """A sportsbook row's raw decimal odds and the book's own margin.

    The price the site compares is already de-vigged, so the raw figure and the
    margin it was derived from are carried alongside for the detail view - the
    brief asks for them in a collapsed section and nowhere else.

    The margin is the overround: the raw implied probabilities of a book's
    outcomes add up to more than one, and the excess is what the bookmaker
    keeps. It is only meaningful when every outcome of the market is present,
    which is why it comes from the stored market rather than from the one row
    being displayed.
    """
    if point["venue_type"] != "sportsbook":
        return None, None
    book = row.get("outcomes") or []
    decimals = [item.get("price_raw") for item in book if item.get("price_raw")]
    margin = None
    if decimals:
        margin = round((sum(1.0 / value for value in decimals) - 1.0) * 100, 2)
    wanted = member.get("outcome") or ""
    chosen = next(
        (item for item in book if (item.get("name") or "") == wanted),
        None,
    )
    if chosen is None and len(book) == 1:
        chosen = book[0]
    return (chosen or {}).get("price_raw"), margin


def build_event_index(events: list[dict], documents: dict, *, now) -> dict:
    """The bridge between the token index and the matched events.

    A search runs against the token index, which only knows markets. To draw a
    result row the way the brief asks - a venue count, the price range across
    those venues, and the gap - the client has to know whether the market it
    found belongs to a matched event, and if so what that event's numbers are.
    Walking every event file to find out would be a hundred requests; this one
    file answers it in one, and it is rebuilt from the same documents as the
    event files, so the row and the event page cannot disagree.
    """
    rows = []
    by_market: dict[str, str] = {}
    for event in events:
        document = documents.get(event["event_key"], {})
        prices = document.get("prices") or []
        # The bar spans the prices that could take part in a gap. When nothing
        # in the event is fresh it falls back to the real-money prices anyway,
        # flagged - a stale range with the staleness visible beats a row that
        # looks comparable because its range was quietly dropped. A play-money
        # price never widens a real-money range.
        comparable = [
            price["price"]
            for price in prices
            if price.get("gap_eligible") and price.get("price") is not None
        ]
        real = [
            price["price"]
            for price in prices
            if price.get("real_money") and price.get("price") is not None
        ]
        values = comparable or real
        volumes = [
            price["volume"] for price in prices if price.get("volume") is not None
        ]
        rows.append(
            {
                "i": event["event_key"],
                "t": event["title"],
                "c": event["category"],
                "e": event["close_time"],
                # From the document, so the index, the event page and home.json
                # cannot disagree about one event's gap.
                "g": document.get("gap"),
                "n": len({price["venue"] for price in prices}),
                "y": sorted(
                    {price["venue_type"] for price in prices if price.get("venue_type")}
                ),
                "lo": min(values) if values else None,
                "hi": max(values) if values else None,
                "x": not comparable and bool(values),
                "w": sum(volumes) if volumes else None,
                "p": bool(event["possible"]),
                "r": bool(event["rules_differ"]),
            }
        )
        for price in prices:
            by_market[market_key(price["venue"], price["market_id"])] = event[
                "event_key"
            ]

    rows.sort(
        key=lambda item: (-(item["g"] if item["g"] is not None else -1), item["i"])
    )
    return {
        "generated_at": _stamp(now),
        "fields": EVENT_INDEX_FIELDS,
        "count": len(rows),
        "events": rows,
        "markets": by_market,
    }


def build_home(
    conn, events: list[dict], documents: dict, *, now: datetime
) -> dict:
    """The landing document: widest gaps, biggest moves, venue health, quota.

    Built from the event documents already assembled, so the landing page and
    an event page can never disagree about a price - there is one computation
    per run, not two.
    """
    gap_rows = []
    for event in events:
        document = documents.get(event["event_key"], {})
        # The gap and the venue count come from the event document, which is the
        # same object the event page is built from - not from the stored column,
        # which can be a pass behind. One event, one gap, whichever file reads it.
        if document.get("gap") is None:
            continue
        gap_rows.append(
            {
                "id": event["event_key"],
                "title": event["title"],
                "category": event["category"],
                "close_time": event["close_time"],
                "gap": document["gap"],
                "venue_count": len(
                    {price["venue"] for price in document.get("prices", [])}
                ),
                "confidence": event["confidence"],
                "possible": event["possible"],
                "rules_differ": event["rules_differ"],
                "prices": [
                    {
                        "venue": price["venue"],
                        "venue_type": price["venue_type"],
                        "price": price["price"],
                        "fresh": price["fresh"],
                        "real_money": price["real_money"],
                    }
                    for price in document.get("prices", [])
                    if price["gap_eligible"]
                ],
            }
        )
    gap_rows.sort(key=lambda item: (-(item["gap"] or 0), item["id"]))

    moves = store.read_market_window_changes(conn, hours=MOVES_WINDOW_HOURS, now=now)
    for move in moves:
        move["change"] = round(move["to_price"] - move["from_price"], 2)

    counts = store.market_counts(conn)
    state = {item["venue"]: item for item in store.read_catalog_state(conn)}
    status = []
    # Every venue we know of, not only the ones holding rows: a venue that just
    # failed (the sportsbook without its key, say) has no markets and is
    # exactly the one whose error the page has to show.
    for venue in sorted(set(counts) | set(state)):
        own = counts.get(venue, {"total": 0, "open": 0, "priced": 0})
        venue_state = state.get(venue, {})
        status.append(
            {
                "venue": venue,
                "markets": own["total"],
                "open": own["open"],
                "priced": own["priced"],
                "last_catalog_ok_at": venue_state.get("last_catalog_ok_at", ""),
                "last_prices_ok_at": venue_state.get("last_prices_ok_at", ""),
                "errors": venue_state.get("errors", ""),
                "notes": venue_state.get("notes", ""),
            }
        )

    return {
        "generated_at": _stamp(now),
        "stats": {
            "markets": sum(item["markets"] for item in status),
            "open_markets": sum(item["open"] for item in status),
            "priced_markets": sum(item["priced"] for item in status),
            "matched_events": len(events),
            "events_with_gap": len(gap_rows),
        },
        "top_gaps": gap_rows[:TOP_GAPS],
        "recent_moves": moves[:TOP_MOVES],
        "venue_status": status,
        "odds_quota": store.read_odds_quota(conn),
    }


def export_all(
    conn, out_dir: Path = DATA_DIR, *, now=None, limit=None, prices_only=False
) -> dict:
    """Write every static artifact and return a per-file report.

    ``prices_only`` is the 30 minute job: it still appends the price snapshots
    that history and the movers list are built from, and still rewrites
    ``home.json`` and the event files, but leaves the catalog shards and the
    search index for the 6 hourly pass that owns them.
    """
    now = freshness.as_utc(now) or datetime.now(timezone.utc)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"generated_at": _stamp(now), "out": str(out_dir)}

    rows = store.read_markets(conn, open_only=True, limit=limit)
    markets_by_key = {(row["venue"], row["market_id"]): row for row in rows}
    report["markets"] = len(rows)

    report["snapshots"] = store.insert_market_snapshots(conn, rows, at=_stamp(now))

    if not prices_only:
        catalog = build_catalog(conn, out_dir, now=now, limit=limit)
        report["catalog"] = {
            "totals": catalog["totals"],
            "venues": {
                venue: {
                    "count": item["count"],
                    "shards": len(item["shards"]),
                    "max_bytes": max((shard["bytes"] for shard in item["shards"]), default=0),
                }
                for venue, item in catalog["venues"].items()
            },
        }
        report["search"] = build_search(conn, out_dir, now=now, limit=limit)

    events = store.read_matched_events(conn)
    documents = {}
    for event in events:
        members = store.read_members(conn, event["event_key"])
        documents[event["event_key"]] = build_event(
            conn, event, members, markets_by_key, now=now
        )

    events_dir = out_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    _clear(events_dir, "*.json")
    for event in events:
        _write(
            events_dir / f"{_safe_id(event['event_key'])}.json",
            documents[event["event_key"]],
        )
    report["events"] = {
        "count": len(events),
        "with_gap": sum(
            1 for event in events if documents[event["event_key"]]["gap"] is not None
        ),
        "published": sum(
            1 for event in events if documents[event["event_key"]]["published"]
        ),
        "possible": sum(1 for event in events if event["possible"]),
    }

    home = build_home(conn, events, documents, now=now)
    report["home"] = {
        "file": "home.json",
        "top_gaps": len(home["top_gaps"]),
        "recent_moves": len(home["recent_moves"]),
        "bytes": _write(out_dir / "home.json", home),
    }

    # Rebuilt with the events, in both cadences: it is derived from the same
    # documents, so the 30 minute pass keeps a search result row in step with
    # the event page it links to.
    event_index = build_event_index(events, documents, now=now)
    report["event_index"] = {
        "file": "events-index.json",
        "events": event_index["count"],
        "markets": len(event_index["markets"]),
        "bytes": _write(out_dir / "events-index.json", event_index),
    }
    return report


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


def _dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _bytes(value) -> int:
    """Bytes a value serializes to.

    ``len()`` on the string counts characters, and a real catalog is full of
    non-ASCII titles - accented names, emoji, curly quotes - which are two to
    four bytes each. Measuring characters is what let a shard land over the
    limit the first time this ran, so every size here goes through UTF-8.
    """
    return len(_dumps(value).encode("utf-8"))


def _size(value) -> int:
    """Exact bytes a value costs inside a JSON array, plus its separator."""
    return _bytes(value) + 1


def _greedy_fill(items, sizes, limit: int, *, first_limit: int | None = None) -> list[list]:
    """Split items into runs that fit `limit` bytes, in order.

    The first run is allowed a different limit, which is how the search index
    pays for the dictionary it also carries. ``limit`` is an estimate only:
    :func:`_write_groups` re-measures the real serialization afterwards.
    """
    groups: list[list] = []
    current: list = []
    ceiling = first_limit if first_limit is not None else limit
    total = WRAPPER_SLACK
    for item, size in zip(items, sizes):
        if current and total + size > ceiling:
            groups.append(current)
            current, total, ceiling = [], WRAPPER_SLACK, limit
        current.append(item)
        total += size
    if current:
        groups.append(current)
    return groups


def _write_groups(
    directory: Path, stem: str, groups, limit: int, document, *, start: int = 1
) -> tuple[list, int]:
    """Write one document per group, guaranteed to fit `limit` bytes.

    ``document(number, all_groups, group)`` builds the real document, so the
    size that is checked is the size that lands on disk. A group that still
    exceeds the limit is halved and the whole thing is measured again; a group
    of one is left alone, because there is nothing left to split (and one
    market record is nowhere near a megabyte). ``start`` is 1 for the catalog
    (``kalshi-1.json``) and 0 for the search index (``index-0.json``), whose
    first shard is the dictionary a client has to fetch before it can search.
    """
    splits = 0
    while True:
        documents = [
            document(start + offset, groups, group)
            for offset, group in enumerate(groups)
        ]
        oversize = [
            index
            for index, doc in enumerate(documents)
            if _size(doc) > limit and len(groups[index]) > 1
        ]
        if not oversize:
            break
        for index in reversed(oversize):
            half = len(groups[index]) // 2
            groups[index : index + 1] = [groups[index][:half], groups[index][half:]]
            splits += 1

    files = []
    for offset, group in enumerate(groups):
        number = start + offset
        path = directory / f"{stem}-{number}.json"
        files.append(
            {
                "file": f"{directory.name}/{path.name}",
                "shard": number,
                "count": len(group),
                "pieces": len(group),
                "bytes": _write(path, documents[offset]),
                "from": _edge(group, first=True),
                "to": _edge(group, first=False),
            }
        )
    return files, splits


def _edge(group, *, first: bool):
    """The first/last market id (catalog) or token (search) of a group."""
    item = group[0] if first else group[-1]
    return item["i"] if isinstance(item, dict) else item[0]


def _safe_id(event_key: str) -> str:
    """An event key that is safe as a filename on every platform."""
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in event_key)


def _write(path: Path, document) -> int:
    """Write one JSON document compactly and return the bytes on disk.

    ``newline="\n"`` is explicit: without it Python translates the trailing
    newline on Windows, the file grows by a byte, and the size the manifest
    reports stops matching the size on disk.
    """
    text = _dumps(document)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return len(text.encode("utf-8")) + 1


def shard_sizes(out_dir: Path, which: str) -> list[tuple[str, int]]:
    """Every shard of one kind in `out_dir`, with its size in bytes, sorted."""
    directory = Path(out_dir) / which
    # as_posix(): these paths are what the site fetches, so they are web paths,
    # not whatever separator the machine that built them happens to use.
    return [
        (path.relative_to(out_dir).as_posix(), path.stat().st_size)
        for path in sorted(directory.glob("*.json"))
        if path.name != "manifest.json"
    ]


def oversize_artifacts(out_dir: Path) -> list[tuple[str, int, int]]:
    """Every exported file over its own limit, as (path, bytes, limit).

    The write path already guarantees this; the smoke test re-checks it on the
    files that actually landed, because a size promise is worth verifying
    against the artifacts rather than against the code that produced them.
    """
    over = []
    for which, limit in (("catalog", CATALOG_LIMIT), ("search", SEARCH_LIMIT)):
        for name, size in shard_sizes(out_dir, which):
            if size > limit:
                over.append((name, size, limit))
    return over


def _clear(directory: Path, pattern: str) -> int:
    """Remove a previous run's shards, so a shrinking catalog shrinks on disk."""
    removed = 0
    for path in directory.glob(pattern):
        path.unlink()
        removed += 1
    return removed


def _stamp(now) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=store.DB_PATH)
    parser.add_argument("--out", default=DATA_DIR)
    parser.add_argument("--limit", type=int, help="max markets to export")
    parser.add_argument(
        "--prices-only",
        action="store_true",
        help="home.json, events/ and price snapshots only (the 30 minute job)",
    )
    args = parser.parse_args(argv)

    conn = None
    try:
        started = time.time()
        conn = store.connect(args.db)
        report = export_all(conn, args.out, limit=args.limit, prices_only=args.prices_only)
    except Exception as exc:  # a redacted error, never a traceback carrying a key
        print(f"export failed: {envfile.redact(exc)}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()

    print(
        f"exported {report['markets']} markets to {report['out']} "
        f"[{time.time() - started:.1f}s]"
    )
    print(f"  snapshots: +{report['snapshots']} price points")
    if "catalog" in report:
        totals = report["catalog"]["totals"]
        print(
            f"  catalog: {totals['markets']} markets in {totals['shards']} shards, "
            f"{totals['bytes']:,} bytes"
        )
        for venue, item in sorted(report["catalog"]["venues"].items()):
            print(
                f"    {venue:11} {item['count']:6} markets  {item['shards']:3} shards"
                f"  max {item['max_bytes']:,} bytes"
            )
    if "search" in report:
        search = report["search"]
        print(f"  search: {search['vocab']:,} tokens in {search['shards']} shards")
        for item in search["files"]:
            print(
                f"    {item['file']:22} {item['count']:6} tokens"
                f"  {item['bytes']:,} bytes"
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
