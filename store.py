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

import json
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

-- ---------------------------------------------------------------------------
-- The multi-venue catalog. ``events``/``observations`` above stay exactly as
-- they are for the curated three-event pipeline; these tables hold the wide
-- catalog, the auto-matched events, the odds quota and per-venue run state.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    venue        TEXT NOT NULL,
    market_id    TEXT NOT NULL,
    venue_type   TEXT NOT NULL DEFAULT '',
    event_ref    TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    close_time   TEXT NOT NULL DEFAULT '',
    outcomes     TEXT NOT NULL DEFAULT '[]',
    price        REAL,
    price_kind   TEXT NOT NULL DEFAULT '',
    volume       INTEGER,
    volume_24h   INTEGER,
    updated_at   TEXT NOT NULL DEFAULT '',
    price_as_of  TEXT,
    as_of_kind   TEXT NOT NULL DEFAULT 'none',
    url          TEXT NOT NULL DEFAULT '',
    rule_text    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    is_open      INTEGER NOT NULL DEFAULT 1,
    fetched_at   TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (venue, market_id)
);

CREATE INDEX IF NOT EXISTS markets_volume
    ON markets (venue, is_open, volume);
CREATE INDEX IF NOT EXISTS markets_close
    ON markets (is_open, close_time);

CREATE TABLE IF NOT EXISTS matched_events (
    event_key    TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    close_time   TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0,
    possible     INTEGER NOT NULL DEFAULT 0,
    rules_differ INTEGER NOT NULL DEFAULT 0,
    venue_count  INTEGER NOT NULL DEFAULT 0,
    gap          REAL,
    updated_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS matched_members (
    event_key    TEXT NOT NULL REFERENCES matched_events(event_key),
    venue        TEXT NOT NULL,
    market_id    TEXT NOT NULL,
    outcome      TEXT NOT NULL DEFAULT '',
    confidence   REAL NOT NULL DEFAULT 0,
    rules_differ INTEGER NOT NULL DEFAULT 0,
    matched_on   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (event_key, venue, market_id)
);

CREATE INDEX IF NOT EXISTS matched_members_market
    ON matched_members (venue, market_id);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    venue       TEXT NOT NULL,
    market_id   TEXT NOT NULL,
    price       REAL NOT NULL,
    volume_24h  INTEGER,
    observed_at TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'catalog'
);

CREATE UNIQUE INDEX IF NOT EXISTS market_snapshots_point
    ON market_snapshots (venue, market_id, observed_at, source);

CREATE INDEX IF NOT EXISTS market_snapshots_market
    ON market_snapshots (venue, market_id, observed_at);

CREATE INDEX IF NOT EXISTS market_snapshots_window
    ON market_snapshots (observed_at);

CREATE TABLE IF NOT EXISTS odds_quota (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    month        TEXT NOT NULL DEFAULT '',
    used         INTEGER NOT NULL DEFAULT 0,
    remaining    INTEGER,
    last_cost    INTEGER,
    calls        INTEGER NOT NULL DEFAULT 0,
    last_call_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS catalog_state (
    venue              TEXT PRIMARY KEY,
    last_catalog_ok_at TEXT NOT NULL DEFAULT '',
    last_prices_ok_at  TEXT NOT NULL DEFAULT '',
    markets            INTEGER NOT NULL DEFAULT 0,
    errors             TEXT NOT NULL DEFAULT '',
    notes              TEXT NOT NULL DEFAULT ''
);
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


def upsert_markets(conn: sqlite3.Connection, records, *, now: str | None = None) -> int:
    """Insert or update catalog rows, preserving everything a row already has.

    The same function serves both passes on purpose: the 6 hour catalog fills
    titles, categories, URLs and rules, and the 30 minute price refresh sends
    rows that carry only prices. ``COALESCE``/``NULLIF`` guards below make the
    second pass unable to blank out the first one's work.
    """
    stamp = now or _now()
    rows = []
    for record in records:
        rows.append(
            {
                "venue": record.venue,
                "market_id": record.market_id,
                "venue_type": record.venue_type,
                "event_ref": record.event_ref,
                "title": record.title,
                "category": record.category,
                "close_time": record.close_time,
                "outcomes": json.dumps(list(record.outcomes or [])),
                "price": record.price,
                "price_kind": record.price_kind,
                "volume": record.volume,
                "volume_24h": record.volume_24h,
                "updated_at": record.updated_at,
                "price_as_of": record.price_as_of,
                "as_of_kind": record.as_of_kind,
                "url": record.url,
                "rule_text": record.rule_text,
                "status": record.status,
                "is_open": 1 if record.is_open else 0,
                "fetched_at": record.fetched_at or stamp,
                "stamp": stamp,
            }
        )
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO markets (
            venue, market_id, venue_type, event_ref, title, category, close_time,
            outcomes, price, price_kind, volume, volume_24h, updated_at,
            price_as_of, as_of_kind, url, rule_text, status, is_open,
            fetched_at, first_seen_at, last_seen_at
        ) VALUES (
            :venue, :market_id, :venue_type, :event_ref, :title, :category,
            :close_time, :outcomes, :price, :price_kind, :volume, :volume_24h,
            :updated_at, :price_as_of, :as_of_kind, :url, :rule_text, :status,
            :is_open, :fetched_at, :stamp, :stamp
        )
        ON CONFLICT(venue, market_id) DO UPDATE SET
            venue_type   = excluded.venue_type,
            event_ref    = excluded.event_ref,
            title        = CASE WHEN excluded.title != ''
                                THEN excluded.title ELSE markets.title END,
            category     = CASE WHEN excluded.category != ''
                                THEN excluded.category ELSE markets.category END,
            close_time   = CASE WHEN excluded.close_time != ''
                                THEN excluded.close_time ELSE markets.close_time END,
            outcomes     = CASE WHEN excluded.outcomes NOT IN ('[]', '')
                                THEN excluded.outcomes ELSE markets.outcomes END,
            price        = COALESCE(excluded.price, markets.price),
            price_kind   = CASE WHEN excluded.price_kind != ''
                                THEN excluded.price_kind ELSE markets.price_kind END,
            volume       = COALESCE(excluded.volume, markets.volume),
            volume_24h   = COALESCE(excluded.volume_24h, markets.volume_24h),
            updated_at   = CASE WHEN excluded.updated_at != ''
                                THEN excluded.updated_at ELSE markets.updated_at END,
            price_as_of  = COALESCE(excluded.price_as_of, markets.price_as_of),
            as_of_kind   = CASE WHEN excluded.as_of_kind != 'none'
                                THEN excluded.as_of_kind ELSE markets.as_of_kind END,
            url          = CASE WHEN excluded.url != ''
                                THEN excluded.url ELSE markets.url END,
            rule_text    = CASE WHEN excluded.rule_text != ''
                                THEN excluded.rule_text ELSE markets.rule_text END,
            status       = CASE WHEN excluded.status != ''
                                THEN excluded.status ELSE markets.status END,
            is_open      = excluded.is_open,
            fetched_at   = excluded.fetched_at,
            last_seen_at = excluded.last_seen_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def mark_missing_closed(
    conn: sqlite3.Connection, venue: str, seen, *, now: str | None = None
) -> int:
    """Mark everything of `venue` that a full catalog pass did not see.

    Rows are never deleted: a market that closed last week still has history,
    and the site says "resolved" rather than forgetting it existed.
    """
    seen = list(seen)
    conn.execute("DROP TABLE IF EXISTS temp._seen_markets")
    conn.execute("CREATE TEMP TABLE _seen_markets (market_id TEXT PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO _seen_markets (market_id) VALUES (?)",
        ((str(market_id),) for market_id in seen),
    )
    cursor = conn.execute(
        "UPDATE markets SET is_open = 0, last_seen_at = ? "
        "WHERE venue = ? AND is_open = 1 AND market_id NOT IN "
        "(SELECT market_id FROM _seen_markets)",
        (now or _now(), venue),
    )
    conn.execute("DROP TABLE _seen_markets")
    conn.commit()
    return cursor.rowcount


def read_markets(
    conn: sqlite3.Connection,
    *,
    venue: str | None = None,
    venue_types: tuple | None = None,
    open_only: bool | None = True,
    limit: int | None = None,
    by_volume: bool = False,
) -> list[dict]:
    """Stored markets as plain dicts, with outcomes parsed and freshness data.

    ``traded_24h`` is derived (24 hour volume above zero) so a row can be fed
    straight to :func:`freshness.assess`, which is the same evidence Kalshi's
    own quote uses.
    """
    sql = "SELECT * FROM markets WHERE 1 = 1"
    params: list = []
    if venue:
        sql += " AND venue = ?"
        params.append(venue)
    if venue_types:
        sql += " AND venue_type IN (" + ",".join("?" * len(venue_types)) + ")"
        params.extend(venue_types)
    if open_only:
        sql += " AND is_open = 1"
    sql += " ORDER BY COALESCE(volume, 0) DESC, market_id" if by_volume else " ORDER BY market_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    out = []
    for raw in conn.execute(sql, params):
        row = dict(raw)
        try:
            row["outcomes"] = json.loads(row.get("outcomes") or "[]")
        except ValueError:
            row["outcomes"] = []
        volume_24h = row.get("volume_24h")
        row["traded_24h"] = None if volume_24h is None else volume_24h > 0
        out.append(row)
    return out


def top_markets_by_volume(
    conn: sqlite3.Connection, limit: int = 500, *, open_only: bool = True
) -> list[dict]:
    """The markets the 30 minute price refresh covers, best first."""
    return read_markets(conn, open_only=open_only, limit=limit, by_volume=True)


def insert_market_snapshots(conn: sqlite3.Connection, rows, *, at=None, source="catalog") -> int:
    """Append one price point per catalog market, idempotent per instant.

    ``markets`` holds only the latest price, which is all a gap needs but not
    enough for a chart or a movers list. This log is what turns repeated runs
    into a history: the 30 minute job adds a point per pass and the site reads
    back what the pipeline actually observed, never an interpolation.
    """
    stamp = at or _now()
    points = [
        (
            row["venue"],
            row["market_id"],
            float(row["price"]),
            None if row.get("volume_24h") is None else int(row["volume_24h"]),
            stamp,
            source,
        )
        for row in rows
        if row.get("price") is not None
    ]
    if not points:
        return 0
    cursor = conn.executemany(
        """
        INSERT OR IGNORE INTO market_snapshots (
            venue, market_id, price, volume_24h, observed_at, source
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        points,
    )
    conn.commit()
    return cursor.rowcount or 0


def read_market_history(
    conn: sqlite3.Connection, venue: str, market_id: str, *, limit: int = 96
) -> list[dict]:
    """The most recent `limit` stored price points for one market, oldest first."""
    rows = conn.execute(
        """
        SELECT observed_at AS t, price
        FROM market_snapshots
        WHERE venue = ? AND market_id = ?
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        (venue, market_id, limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def read_market_window_changes(
    conn: sqlite3.Connection, *, hours: int = 24, now: datetime | None = None
) -> list[dict]:
    """Per market: the first and last stored point inside the window.

    The movers list is built from this, so a market that never moved is left
    out rather than reported as a zero-length change.
    """
    horizon = freshness.as_utc(now) or _utc_now()
    cutoff = (horizon - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT s.venue, s.market_id, s.observed_at AS t, s.price,
               m.title, m.url, m.venue_type, m.is_open
        FROM market_snapshots s
        LEFT JOIN markets m
               ON m.venue = s.venue AND m.market_id = s.market_id
        WHERE s.observed_at >= ?
        ORDER BY s.venue, s.market_id, s.observed_at
        """,
        (cutoff,),
    ).fetchall()

    grouped: dict = {}
    for row in rows:
        entry = grouped.setdefault(
            (row["venue"], row["market_id"]),
            {
                "venue": row["venue"],
                "market_id": row["market_id"],
                "title": row["title"] or "",
                "url": row["url"] or "",
                "venue_type": row["venue_type"] or "",
                "is_open": bool(row["is_open"]) if row["is_open"] is not None else False,
                "points": [],
            },
        )
        entry["points"].append({"t": row["t"], "price": row["price"]})

    changes = []
    for entry in grouped.values():
        points = entry["points"]
        first, last = points[0], points[-1]
        if first["price"] == last["price"]:
            continue  # a movers list lists movement, not silence
        changes.append(
            {
                "venue": entry["venue"],
                "market_id": entry["market_id"],
                "title": entry["title"],
                "url": entry["url"],
                "venue_type": entry["venue_type"],
                "is_open": entry["is_open"],
                "from_price": first["price"],
                "to_price": last["price"],
                "at": last["t"],
                "points": len(points),
            }
        )
    changes.sort(
        key=lambda item: abs(item["to_price"] - item["from_price"]), reverse=True
    )
    return changes


def market_counts(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per venue: how many markets we hold, and how many are still open."""
    rows = conn.execute(
        "SELECT venue, COUNT(*) AS total, "
        "       SUM(CASE WHEN is_open = 1 THEN 1 ELSE 0 END) AS open, "
        "       SUM(CASE WHEN price IS NOT NULL THEN 1 ELSE 0 END) AS priced "
        "FROM markets GROUP BY venue ORDER BY venue"
    )
    return {
        row["venue"]: {
            "total": row["total"],
            "open": row["open"] or 0,
            "priced": row["priced"] or 0,
        }
        for row in rows
    }


def replace_matching(conn: sqlite3.Connection, events, members) -> tuple[int, int]:
    """Swap in a freshly computed matching result in one transaction.

    Matching is recomputed as a whole rather than patched: an event that no
    longer matches anything should disappear, not linger as a stale pair.
    """
    conn.execute("DELETE FROM matched_members")
    conn.execute("DELETE FROM matched_events")
    conn.executemany(
        """
        INSERT INTO matched_events (
            event_key, title, category, close_time, confidence, possible,
            rules_differ, venue_count, gap, updated_at
        ) VALUES (
            :event_key, :title, :category, :close_time, :confidence, :possible,
            :rules_differ, :venue_count, :gap, :updated_at
        )
        """,
        [dict(event) for event in events],
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO matched_members (
            event_key, venue, market_id, outcome, confidence, rules_differ, matched_on
        ) VALUES (
            :event_key, :venue, :market_id, :outcome, :confidence, :rules_differ, :matched_on
        )
        """,
        [dict(member) for member in members],
    )
    conn.commit()
    return len(events), len(members)


def read_matched_events(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    include_possible: bool = True,
    min_venues: int = 1,
) -> list[dict]:
    """Matched events, strongest first, with their member count."""
    sql = (
        "SELECT * FROM matched_events WHERE venue_count >= ?"
    )
    params: list = [min_venues]
    if not include_possible:
        sql += " AND possible = 0"
    sql += " ORDER BY confidence DESC, venue_count DESC, event_key"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    out = []
    for raw in conn.execute(sql, params):
        row = dict(raw)
        row["possible"] = bool(row["possible"])
        row["rules_differ"] = bool(row["rules_differ"])
        out.append(row)
    return out


def read_members(conn: sqlite3.Connection, event_key: str | None = None) -> list[dict]:
    """Members of one matched event, or of every event."""
    sql = "SELECT * FROM matched_members"
    params: tuple = ()
    if event_key is not None:
        sql += " WHERE event_key = ?"
        params = (event_key,)
    sql += " ORDER BY event_key, venue, market_id"
    return [dict(row) for row in conn.execute(sql, params)]


def record_odds_call(
    conn: sqlite3.Connection,
    *,
    remaining: int | None = None,
    used: int | None = None,
    cost: int | None = None,
    at: str | None = None,
) -> None:
    """Remember the provider's own quota headers after an odds call."""
    month = (at or _now())[:7]  # YYYY-MM, matching the monthly reset
    row = conn.execute("SELECT month, calls FROM odds_quota WHERE id = 1").fetchone()
    calls = 0 if row is None or row["month"] != month else (row["calls"] or 0)
    conn.execute(
        """
        INSERT INTO odds_quota (id, month, used, remaining, last_cost, calls, last_call_at)
        VALUES (1, :month, COALESCE(:used, 0), :remaining, :cost, :calls, :at)
        ON CONFLICT(id) DO UPDATE SET
            month        = excluded.month,
            used         = COALESCE(:used, odds_quota.used),
            remaining    = COALESCE(:remaining, odds_quota.remaining),
            last_cost    = COALESCE(:cost, odds_quota.last_cost),
            calls        = excluded.calls,
            last_call_at = excluded.last_call_at
        """,
        {
            "month": month,
            "used": used,
            "remaining": remaining,
            "cost": cost,
            "calls": calls + 1,
            "at": at or _now(),
        },
    )
    conn.commit()


def read_odds_quota(conn: sqlite3.Connection) -> dict:
    """The last quota the provider reported, or an empty summary."""
    row = conn.execute("SELECT * FROM odds_quota WHERE id = 1").fetchone()
    if row is None:
        return {"month": "", "used": 0, "remaining": None, "calls": 0, "last_call_at": ""}
    return dict(row)


def set_catalog_state(conn: sqlite3.Connection, venue: str, **fields) -> None:
    """Record how a venue's last catalog/prices pass went."""
    allowed = {
        "last_catalog_ok_at",
        "last_prices_ok_at",
        "markets",
        "errors",
        "notes",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown catalog_state fields: {sorted(unknown)}")
    fields.setdefault("markets", 0)
    conn.execute(
        "INSERT INTO catalog_state (venue, last_catalog_ok_at, last_prices_ok_at, "
        "markets, errors, notes) VALUES (:venue, :last_catalog_ok_at, "
        ":last_prices_ok_at, :markets, :errors, :notes) "
        "ON CONFLICT(venue) DO UPDATE SET "
        "  last_catalog_ok_at = CASE WHEN excluded.last_catalog_ok_at != '' "
        "       THEN excluded.last_catalog_ok_at ELSE catalog_state.last_catalog_ok_at END,"
        "  last_prices_ok_at  = CASE WHEN excluded.last_prices_ok_at != '' "
        "       THEN excluded.last_prices_ok_at ELSE catalog_state.last_prices_ok_at END,"
        "  markets            = CASE WHEN excluded.markets != 0 "
        "       THEN excluded.markets ELSE catalog_state.markets END,"
        "  errors             = excluded.errors,"
        "  notes              = excluded.notes",
        {
            "venue": venue,
            "last_catalog_ok_at": fields.get("last_catalog_ok_at", ""),
            "last_prices_ok_at": fields.get("last_prices_ok_at", ""),
            "markets": fields.get("markets", 0),
            "errors": fields.get("errors", ""),
            "notes": fields.get("notes", ""),
        },
    )
    conn.commit()


def read_catalog_state(conn: sqlite3.Connection) -> list[dict]:
    """Per-venue run state, for the smoke test and the Venues page."""
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM catalog_state ORDER BY venue")
    ]


def _now() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
