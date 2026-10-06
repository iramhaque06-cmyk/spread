"""SQLite storage for the prototype: events and their venue observations.

One row per (event, venue): upserting replaces that venue's previous
observation, so the tables always hold the latest numbers for an event and
re-running a fetch changes nothing structurally.

Each read labels rows with a human source name (``source``), because every
number shown on the page has to say which venue it came from.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from venues import Quote

DB_PATH = Path(__file__).resolve().parent / "spread.db"

SOURCE_LABELS = {"kalshi": "Kalshi", "polymarket": "Polymarket"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    title     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    event_key    TEXT NOT NULL REFERENCES events(event_key),
    venue        TEXT NOT NULL,
    market_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    price_cents  REAL NOT NULL,
    price_kind   TEXT NOT NULL,
    volume       INTEGER,
    url          TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    status       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (event_key, venue)
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the database at `path`, creating it and the schema if needed."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_event(conn: sqlite3.Connection, event_key: str, title: str) -> None:
    """Store the human label for one real-world event."""
    conn.execute(
        "INSERT INTO events (event_key, title) VALUES (?, ?) "
        "ON CONFLICT(event_key) DO UPDATE SET title = excluded.title",
        (event_key, title),
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
            volume, url, last_updated, status, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_key, venue) DO UPDATE SET
            market_id    = excluded.market_id,
            title        = excluded.title,
            price_cents  = excluded.price_cents,
            price_kind   = excluded.price_kind,
            volume       = excluded.volume,
            url          = excluded.url,
            last_updated = excluded.last_updated,
            status       = excluded.status,
            recorded_at  = excluded.recorded_at
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
        ),
    )
    conn.commit()


def read_observations(
    conn: sqlite3.Connection, event_key: str | None = None
) -> list[dict]:
    """Latest observation per venue, labelled with its source.

    Optionally restricted to one event. Rows carry the event's title as
    ``event_title`` alongside the venue's own market fields.
    """
    sql = """
        SELECT o.*, e.title AS event_title
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
        rows.append(row)
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
