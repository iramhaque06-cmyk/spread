"""Export `site/data.json`: the one file the website reads.

The shape is fixed by the site brief, so it is built in one place and never
assembled ad hoc:

    {
      "generated_at": ISO-8601 UTC,
      "events": [{
        "id", "title", "category", "status", "resolves_at", "gap",
        "rules_differ", "venues": [{"venue", "price", "volume", "updated_at",
                                    "url", "rule_text", "is_stale"}],
        "history": {"<venue>": [{"t", "price"}]}
      }],
      "moves": [{"event_id", "venue", "from_price", "to_price", "at"}],
      "venues_status": [{"venue", "last_success_at", "market_count", "notes"}]
    }

Prices, gaps and moves are in **cents** (the page calls them points).
``t`` and ``at`` are ISO-8601 UTC. ``history`` is built from the
``price_snapshots`` table, which grows by one row per market per run - so a
sparkline is a record of what this pipeline actually observed, never a
reconstruction.

Two fields are worth reading twice:

``updated_at``
    the venue's own stamp for the price. Kalshi publishes only a *record*
    stamp, which lags real trading (it sat 130.8 hours old on a market that
    had traded 119,581 contracts in the last day), so this is not always the
    moment the price last moved.
``is_stale``
    whether the price can be shown to be older than 24 hours, judged from
    the best evidence each venue offers (see :mod:`freshness`). For Kalshi
    that evidence is 24 hour traded volume, not its record stamp, so
    ``is_stale`` can be false while ``updated_at`` looks old - and that is
    the honest reading, which the Methodology page spells out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import freshness
import mapping
import store

DATA_PATH = Path(__file__).resolve().parent / "site" / "data.json"
HISTORY_POINTS = 96  # per venue: one day at the backfill's 15 minute cadence
MOVES_WINDOW_HOURS = 24
VENUE_NOTES = {
    "kalshi": (
        "Kalshi trade-api v2, public and keyless. Publishes no last-trade time: "
        "freshness is inferred from 24 hour traded volume, and the updated stamp "
        "shown here is Kalshi's market-record time, which lags real trading."
    ),
    "polymarket": (
        "Polymarket Gamma and CLOB APIs, public and keyless. The price timestamp "
        "comes from the CLOB price history; Gamma's own updated stamp is a batch "
        "value shared by whole sets of markets."
    ),
}
VENUE_URLS = {"kalshi": "https://kalshi.com", "polymarket": "https://polymarket.com"}


def build(conn, *, now: datetime | None = None) -> dict:
    """Assemble the whole document from what is stored."""
    now = freshness.as_utc(now) or datetime.now(timezone.utc)
    rows = store.read_observations(conn)
    entries = {item["event_key"]: item for item in mapping.load()}

    events = []
    for event_key, grouped in _by_event(rows).items():
        entry = entries.get(event_key, {})
        events.append(_event(conn, event_key, grouped, entry, now))

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "events": events,
        "moves": _moves(conn, now),
        "venues_status": _venues_status(rows, now),
    }


def write(conn, path: Path | str = DATA_PATH, *, now: datetime | None = None) -> Path:
    """Write `data.json` and return its path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = build(conn, now=now)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _by_event(rows: list[dict]) -> dict:
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["event_key"], {})[row["venue"]] = row
    return grouped


def _venue_order(grouped: dict) -> list[str]:
    """Known venues in their curated order, then anything else we hold.

    The curated order fixes which venue gets chart colour one and which gets
    colour two. A venue the pipeline stores without a mapping entry still
    belongs in the export: dropping it would silently shrink an event's price
    rows and could turn a real two-venue gap into ``null``.
    """
    known = [venue for venue in mapping.VENUES if venue in grouped]
    extra = sorted(venue for venue in grouped if venue not in mapping.VENUES)
    return known + extra


def _event(conn, event_key: str, grouped: dict, entry: dict, now) -> dict:
    order = _venue_order(grouped)
    venues = []
    for venue in order:
        row = grouped[venue]
        verdict = freshness.assess(row, now=now)
        venues.append(
            {
                "venue": venue,
                "price": round(row["price_cents"], 2),
                "volume": row["volume"],
                "updated_at": row["price_as_of"] or row["last_updated"] or row["fetched_at"],
                "url": row["url"],
                "rule_text": row["rules_text"] or "",
                "is_stale": not verdict.fresh,
            }
        )

    compares = [item["price"] for item in venues if not item["is_stale"]]
    gap = round(max(compares) - min(compares), 2) if len(compares) >= 2 else None
    resolves = sorted(item for item in (row["resolves_at"] for row in grouped.values()) if item)

    return {
        "id": event_key,
        "title": next(iter(grouped.values()))["event_title"],
        "category": entry.get("category") or "Economic data",
        "status": _status(grouped),
        "resolves_at": resolves[0] if resolves else None,
        "gap": gap,
        "rules_differ": _rules_differ(grouped, entry),
        "venues": venues,
        "history": {
            venue: store.read_history(conn, event_key, venue, limit=HISTORY_POINTS)
            for venue in order
        },
    }


def _rules_differ(grouped: dict, entry: dict) -> bool:
    """The curated verdict for this event, from the copy the pipeline stores.

    ``events.rules_differ`` is written on every run from the same mapping
    entry, so the two agree in normal operation; the database row is read
    first because the export is meant to be a function of what is stored, and
    the mapping entry is the fallback for an event with no stored copy.
    """
    for row in grouped.values():
        if "event_rules_differ" in row:
            return bool(row["event_rules_differ"])
    return bool(entry.get("rules_differ"))


def _status(grouped: dict) -> str:
    """A pair is active while any venue still has a live market."""
    active = any((row["status"] or "").lower() == "active" for row in grouped.values())
    return "active" if active else "resolved"


def _moves(conn, now) -> list[dict]:
    """Price changes over the last 24 hours, one row per event and venue."""
    moves = []
    for event_key, venue, first, last in store.read_window_changes(
        conn, hours=MOVES_WINDOW_HOURS, now=now
    ):
        if first["price"] == last["price"]:
            continue  # a mover page lists movement, not silence
        moves.append(
            {
                "event_id": event_key,
                "venue": venue,
                "from_price": round(first["price"], 2),
                "to_price": round(last["price"], 2),
                "at": last["t"],
            }
        )
    moves.sort(key=lambda item: abs(item["to_price"] - item["from_price"]), reverse=True)
    return moves


def _venues_status(rows: list[dict], now) -> list[dict]:
    """Per-venue health: when we last pulled it, how much we hold, and caveats."""
    statuses = []
    for venue in mapping.VENUES:
        own = [row for row in rows if row["venue"] == venue]
        fetched = sorted(row["fetched_at"] for row in own if row["fetched_at"])
        statuses.append(
            {
                "venue": venue,
                "last_success_at": fetched[-1] if fetched else None,
                "market_count": len(own),
                "notes": VENUE_NOTES.get(venue, ""),
            }
        )
    return statuses


def venue_site(venue: str) -> str:
    """The venue's own public site, for the Venues page."""
    return VENUE_URLS.get(venue, "")
