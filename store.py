"""SQLite storage for the prototype: events and their venue observations.

One row per (event, venue): upserting replaces that venue's previous
observation, so the tables always hold the latest numbers for an event and
re-running a fetch changes nothing structurally.

Each read labels rows with a human source name (``source``), because every
number shown on the page has to say which venue it came from.

Three timestamps live side by side and mean different things, so they are
stored separately rather than merged:

``last_updated``
    the venue's own record stamp, verbatim. For Kalshi that is
    ``updated_time``; for Polymarket, Gamma's ``updatedAt``. Neither is a
    last-trade time, and the page says so next to each one.
``price_as_of``
    when the price itself last moved, where the venue publishes it.
    :mod:`freshness` judges staleness from this, or from 24 hour volume
    when there is no such timestamp.
``fetched_at``
    when this program last asked the venue. Provenance, not freshness.

``events`` also carries the curated resolution-rule one-liners and the
"rules differ" flag, so the rendered page stays a pure function of the
database, exactly like the prices.

``price_snapshots`` is append-only: one row per market per run, plus an
optional one-time backfill of Polymarket's public CLOB price history. It is
what the website's charts and the Movers page are built from, so a chart is a
record of prices this pipeline actually observed rather than a redrawing of
the current number. ``observations`` stays the latest-value table the flat
table page reads.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import freshness
from venues import Quote

DB_PATH = Path(__file__).resolve().parent / "spread.db"

SOURCE_LABELS = {"kalshi": "Kalshi", "polymarket": "Polymarket"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_key       TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    kalshi_rule     TEXT NOT NULL DEFAULT '',
    polymarket_rule TEXT NOT NULL DEFAULT '',
    rules_differ    INTEGER NOT NULL DEFAULT 0,
    rules_note      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS observations (
    event_key        TEXT NOT NULL REFERENCES events(event_key),
    venue            TEXT NOT NULL,
    market_id        TEXT NOT NULL,
    title            TEXT NOT NULL,
    price_cents      REAL NOT NULL,
    price_kind       TEXT NOT NULL,
    volume           INTEGER,
    url              TEXT NOT NULL,
    last_updated     TEXT NOT NULL,
    status           TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    price_as_of      TEXT,
    price_as_of_kind TEXT NOT NULL DEFAULT 'none',
    traded_24h       INTEGER,
    volume_24h       INTEGER,
    fetched_at       TEXT NOT NULL DEFAULT '',
    rules_text       TEXT NOT NULL DEFAULT '',
    resolves_at      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_key, venue)
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key   TEXT NOT NULL,
    venue       TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    price_cents REAL NOT NULL,
    volume      INTEGER,
    updated_at  TEXT NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'pipeline'
);

CREATE UNIQUE INDEX IF NOT EXISTS price_snapshots_point
    ON price_snapshots (event_key, venue, fetched_at, source);

CREATE INDEX IF NOT EXISTS price_snapshots_window
    ON price_snapshots (event_key, venue, fetched_at);
"""

# Columns added after the first working version. ``spread.db`` is gitignored
# and regenerated, but it also holds the two venue-scanner tables, so an
# existing file is migrated rather than deleted.
ADDED_COLUMNS = {
    "events": {
        "kalshi_rule": "TEXT NOT NULL DEFAULT ''",
        "polymarket_rule": "TEXT NOT NULL DEFAULT ''",
        "rules_differ": "INTEGER NOT NULL DEFAULT 0",
        "rules_note": "TEXT NOT NULL DEFAULT ''",
    },
    "observations": {
        "price_as_of": "TEXT",
        "price_as_of_kind": "TEXT NOT NULL DEFAULT 'none'",
        "traded_24h": "INTEGER",
        "volume_24h": "INTEGER",
        "fetched_at": "TEXT NOT NULL DEFAULT ''",
        "rules_text": "TEXT NOT NULL DEFAULT ''",
        "resolves_at": "TEXT NOT NULL DEFAULT ''",
    },
}


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the database at `path`, creating or migrating the schema if needed."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column an older database is missing, leaving its rows intact."""
    for table, columns in ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


def upsert_event(
    conn: sqlite3.Connection,
    event_key: str,
    title: str,
    *,
    kalshi_rule: str = "",
    polymarket_rule: str = "",
    rules_differ: bool = False,
    rules_note: str = "",
) -> None:
    """Store the human label for one real-world event, and its rule summaries."""
    conn.execute(
        "INSERT INTO events (event_key, title, kalshi_rule, polymarket_rule, "
        "rules_differ, rules_note) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(event_key) DO UPDATE SET "
        "  title = excluded.title,"
        "  kalshi_rule = excluded.kalshi_rule,"
        "  polymarket_rule = excluded.polymarket_rule,"
        "  rules_differ = excluded.rules_differ,"
        "  rules_note = excluded.rules_note",
        (
            event_key,
            title,
            kalshi_rule,
            polymarket_rule,
            1 if rules_differ else 0,
            rules_note,
        ),
    )
    conn.commit()


def upsert_observation(
    conn: sqlite3.Connection,
    event_key: str,
    quote: Quote,
    *,
    recorded_at: str | None = None,
) -> None:
    """Store `quote` as its venue's latest observation for the event.

    Committed immediately, so a later read (in this or any other
    connection) sees it.
    """
    conn.execute(
        """
        INSERT INTO observations (
            event_key, venue, market_id, title, price_cents, price_kind,
            volume, url, last_updated, status, recorded_at, price_as_of,
            price_as_of_kind, traded_24h, volume_24h, fetched_at, rules_text,
            resolves_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key, venue) DO UPDATE SET
            market_id        = excluded.market_id,
            title            = excluded.title,
            price_cents      = excluded.price_cents,
            price_kind       = excluded.price_kind,
            volume           = excluded.volume,
            url              = excluded.url,
            last_updated     = excluded.last_updated,
            status           = excluded.status,
            recorded_at      = excluded.recorded_at,
            price_as_of      = excluded.price_as_of,
            price_as_of_kind = excluded.price_as_of_kind,
            traded_24h       = excluded.traded_24h,
            volume_24h       = excluded.volume_24h,
            fetched_at       = excluded.fetched_at,
            rules_text       = excluded.rules_text,
            resolves_at      = excluded.resolves_at
        """,
        (
            event_key,
            quote.venue,
            quote.market_id,
            quote.title,
            quote.price_cents,
            quote.price_kind,
            quote.volume,
            quote.url,
            quote.last_updated,
            quote.status,
            recorded_at or _now(),
            quote.price_as_of,
            quote.price_as_of_kind,
            None if quote.traded_24h is None else int(quote.traded_24h),
            quote.volume_24h,
            quote.fetched_at or recorded_at or _now(),
            quote.rules_text,
            quote.resolves_at,
        ),
    )
    conn.commit()


def insert_snapshot(
    conn: sqlite3.Connection,
    event_key: str,
    quote: Quote,
    *,
    source: str = "pipeline",
    at: str | None = None,
) -> bool:
    """Append one price point for a market; True when a new row landed.

    ``at`` overrides the timestamp, which is how the CLOB backfill files its
    historical points at the time they happened rather than at fetch time.
    The unique index makes a repeated run idempotent instead of duplicating
    the same instant.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO price_snapshots (
            event_key, venue, market_id, price_cents, volume,
            updated_at, fetched_at, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            quote.venue,
            quote.market_id,
            quote.price_cents,
            quote.volume,
            quote.last_updated or at or quote.fetched_at or _now(),
            at or quote.fetched_at or _now(),
            source,
        ),
    )
    conn.commit()
    return bool(cursor.rowcount)


def has_snapshots(
    conn: sqlite3.Connection, event_key: str, venue: str, *, source: str | None = None
) -> bool:
    """Whether any price point is stored for this market."""
    sql = "SELECT 1 FROM price_snapshots WHERE event_key = ? AND venue = ?"
    params: list = [event_key, venue]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def read_history(
    conn: sqlite3.Connection, event_key: str, venue: str, *, limit: int = 96
) -> list[dict]:
    """The most recent `limit` price points for one market, oldest first."""
    rows = conn.execute(
        """
        SELECT fetched_at AS t, price_cents AS price
        FROM price_snapshots
        WHERE event_key = ? AND venue = ?
        ORDER BY fetched_at DESC
        LIMIT ?
        """,
        (event_key, venue, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def read_window_changes(
    conn: sqlite3.Connection, *, hours: int = 24, now: datetime | None = None
) -> list[tuple[str, str, dict, dict]]:
    """Per event and venue: the first and last price point in the window.

    Returns ``(event_key, venue, first, last)`` where each point is a dict of
    ``t`` and ``price``. This is what turns the snapshot log into Movers rows
    and into the sidebar's live-moves list.
    """
    horizon = freshness.as_utc(now) or _utc_now()
    cutoff = horizon - timedelta(hours=hours)
    rows = conn.execute(
        """
        SELECT event_key, venue, fetched_at AS t, price_cents AS price
        FROM price_snapshots
        WHERE fetched_at >= ?
        ORDER BY event_key, venue, fetched_at
        """,
        (cutoff.isoformat(timespec="seconds"),),
    ).fetchall()

    grouped: dict = {}
    for row in rows:
        grouped.setdefault((row["event_key"], row["venue"]), []).append(
            {"t": row["t"], "price": row["price"]}
        )
    return [
        (event_key, venue, points[0], points[-1])
        for (event_key, venue), points in grouped.items()
    ]


def read_observations(
    conn: sqlite3.Connection, event_key: str | None = None
) -> list[dict]:
    """Latest observation per venue, labelled with its source.

    Optionally restricted to one event. Rows carry the event's title as
    ``event_title`` and its curated rule summaries as ``event_*`` fields
    alongside the venue's own market fields. Booleans come back as booleans,
    not as SQLite's 0/1.
    """
    sql = """
        SELECT o.*, e.title AS event_title,
               e.kalshi_rule     AS event_kalshi_rule,
               e.polymarket_rule AS event_polymarket_rule,
               e.rules_differ    AS event_rules_differ,
               e.rules_note      AS event_rules_note
        FROM observations o
        JOIN events e ON e.event_key = o.event_key
    """
    params: tuple = ()
    if event_key is not None:
        sql += " WHERE o.event_key = ?"
        params = (event_key,)
    sql += " ORDER BY o.event_key, o.venue"

    rows = []
    for raw in conn.execute(sql, params):
        row = dict(raw)
        row["source"] = SOURCE_LABELS.get(row["venue"], row["venue"])
        row["traded_24h"] = None if row["traded_24h"] is None else bool(row["traded_24h"])
        row["event_rules_differ"] = bool(row["event_rules_differ"])
        rows.append(row)
    return rows


def _now() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
