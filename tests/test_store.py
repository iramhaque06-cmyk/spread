"""Round-trip tests for store.py (offline: Quotes parsed from fixtures)."""

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import store
from venues import kalshi, polymarket

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVENT_KEY = "fed-2026-10"
EVENT_TITLE = "Fed decision in October 2026"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fed_quotes():
    """The two venue Quotes for the same real-world event, from fixtures."""
    kalshi_quote = kalshi.parse_market(
        load("kalshi_market_KXFEDDECISION-26OCT-H0.json"),
        series=load("kalshi_series_KXFEDDECISION.json")["series"],
    )
    polymarket_quote = polymarket.parse_market(load("polymarket_market_2589812.json"))
    return kalshi_quote, polymarket_quote


class StoreRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "spread.db"
        self.conn = store.connect(self.path)
        self.addCleanup(self.conn.close)
        store.upsert_event(self.conn, EVENT_KEY, EVENT_TITLE)

    def test_read_back_equals_the_quote(self):
        kalshi_quote, _ = fed_quotes()
        store.upsert_observation(
            self.conn, EVENT_KEY, kalshi_quote, recorded_at="2026-10-05T00:00:00+00:00"
        )

        [row] = store.read_observations(self.conn, EVENT_KEY)
        self.assertEqual(row["event_key"], EVENT_KEY)
        self.assertEqual(row["event_title"], EVENT_TITLE)
        self.assertEqual(row["venue"], kalshi_quote.venue)
        self.assertEqual(row["source"], "Kalshi")
        self.assertEqual(row["market_id"], kalshi_quote.market_id)
        self.assertEqual(row["title"], kalshi_quote.title)
        self.assertEqual(row["price_cents"], kalshi_quote.price_cents)
        self.assertEqual(row["price_kind"], kalshi_quote.price_kind)
        self.assertEqual(row["volume"], kalshi_quote.volume)
        self.assertEqual(row["url"], kalshi_quote.url)
        self.assertEqual(row["last_updated"], kalshi_quote.last_updated)
        self.assertEqual(row["status"], kalshi_quote.status)
        self.assertEqual(row["recorded_at"], "2026-10-05T00:00:00+00:00")

    def test_one_row_per_venue_per_event(self):
        kalshi_quote, polymarket_quote = fed_quotes()
        for quote in (kalshi_quote, polymarket_quote):
            store.upsert_observation(self.conn, EVENT_KEY, quote)
        self.assertEqual(len(store.read_observations(self.conn)), 2)
        self.assertEqual(
            [row["source"] for row in store.read_observations(self.conn)],
            ["Kalshi", "Polymarket"],
        )

    def test_upsert_is_idempotent(self):
        kalshi_quote, _ = fed_quotes()
        for _ in range(3):
            store.upsert_observation(
                self.conn,
                EVENT_KEY,
                kalshi_quote,
                recorded_at="2026-10-05T00:00:00+00:00",
            )
        rows = store.read_observations(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_cents"], kalshi_quote.price_cents)

        # Re-storing the event row must not duplicate it either.
        store.upsert_event(self.conn, EVENT_KEY, EVENT_TITLE)
        self.assertEqual(len(store.read_observations(self.conn)), 1)

    def test_newer_observation_replaces_the_old_one(self):
        kalshi_quote, _ = fed_quotes()
        store.upsert_observation(self.conn, EVENT_KEY, kalshi_quote)
        store.upsert_observation(
            self.conn, EVENT_KEY, replace(kalshi_quote, price_cents=75.0)
        )

        [row] = store.read_observations(self.conn)
        self.assertEqual(row["price_cents"], 75.0)
        self.assertEqual(row["market_id"], kalshi_quote.market_id)

    def test_observations_survive_a_new_connection(self):
        kalshi_quote, polymarket_quote = fed_quotes()
        for quote in (kalshi_quote, polymarket_quote):
            store.upsert_observation(self.conn, EVENT_KEY, quote)
        self.conn.close()

        reopened = store.connect(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(len(store.read_observations(reopened)), 2)

    def test_reading_every_event_is_possible(self):
        kalshi_quote, polymarket_quote = fed_quotes()
        store.upsert_observation(self.conn, EVENT_KEY, kalshi_quote)
        store.upsert_event(self.conn, "cpi-2026-09", "CPI report, September 2026")
        store.upsert_observation(self.conn, "cpi-2026-09", polymarket_quote)

        rows = store.read_observations(self.conn)
        # Reading returns every event, ordered by event_key for stable output.
        self.assertEqual(
            [row["event_key"] for row in rows], ["cpi-2026-09", EVENT_KEY]
        )

    def test_observation_for_an_unknown_event_is_rejected(self):
        kalshi_quote, _ = fed_quotes()
        with self.assertRaises(sqlite3.IntegrityError):
            store.upsert_observation(self.conn, "no-such-event", kalshi_quote)

    def test_price_timestamps_volumes_and_rule_text_round_trip(self):
        kalshi_quote, polymarket_quote = fed_quotes()
        store.upsert_observation(self.conn, EVENT_KEY, kalshi_quote)
        store.upsert_observation(
            self.conn,
            EVENT_KEY,
            replace(
                polymarket_quote,
                price_as_of="2026-10-06T05:35:14+00:00",
                price_as_of_kind="last_price_change",
                traded_24h=True,
                volume_24h=326_528,
                fetched_at="2026-10-06T05:40:00+00:00",
                rules_text="resolution rules, verbatim",
            ),
        )

        rows = {row["venue"]: row for row in store.read_observations(self.conn)}
        self.assertEqual(rows["polymarket"]["price_as_of"], "2026-10-06T05:35:14+00:00")
        self.assertEqual(
            rows["polymarket"]["price_as_of_kind"], "last_price_change"
        )
        self.assertIs(rows["polymarket"]["traded_24h"], True)
        self.assertEqual(rows["polymarket"]["volume_24h"], 326_528)
        self.assertEqual(rows["polymarket"]["rules_text"], "resolution rules, verbatim")

    def test_unknown_volume_stays_unknown_rather_than_zero(self):
        kalshi_quote, _ = fed_quotes()
        store.upsert_observation(
            self.conn,
            EVENT_KEY,
            replace(kalshi_quote, traded_24h=None, volume_24h=None),
        )
        [row] = store.read_observations(self.conn)
        self.assertIsNone(row["traded_24h"])
        self.assertIsNone(row["volume_24h"])

    def test_fetch_time_falls_back_to_when_the_row_was_recorded(self):
        kalshi_quote, _ = fed_quotes()
        store.upsert_observation(
            self.conn,
            EVENT_KEY,
            replace(kalshi_quote, fetched_at=""),
            recorded_at="2026-10-05T00:00:00+00:00",
        )
        [row] = store.read_observations(self.conn)
        self.assertEqual(row["fetched_at"], "2026-10-05T00:00:00+00:00")

    def test_event_rule_summaries_are_stored_and_read_back(self):
        store.upsert_event(
            self.conn,
            EVENT_KEY,
            EVENT_TITLE,
            kalshi_rule="Yes if the number is above 1.",
            polymarket_rule="Yes if the number is at least 1.",
            rules_differ=True,
            rules_note="the boundary differs",
        )
        store.upsert_observation(self.conn, EVENT_KEY, fed_quotes()[0])

        [row] = store.read_observations(self.conn)
        self.assertEqual(row["event_kalshi_rule"], "Yes if the number is above 1.")
        self.assertEqual(
            row["event_polymarket_rule"], "Yes if the number is at least 1."
        )
        self.assertIs(row["event_rules_differ"], True)
        self.assertEqual(row["event_rules_note"], "the boundary differs")


class MigrationTest(unittest.TestCase):
    """An older spread.db keeps working: columns are added, rows are kept."""

    OLD_SCHEMA = """
        CREATE TABLE events (event_key TEXT PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE observations (
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

    def test_an_old_database_is_migrated_without_losing_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "old.db"

        old = sqlite3.connect(path)
        old.executescript(self.OLD_SCHEMA)
        old.execute("INSERT INTO events VALUES (?, ?)", (EVENT_KEY, EVENT_TITLE))
        old.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                EVENT_KEY,
                "kalshi",
                "KXTEST",
                "an old row",
                42.0,
                "last",
                7,
                "https://example.test/market",
                "2026-01-01T00:00:00Z",
                "active",
                "2026-01-01T00:00:01Z",
            ),
        )
        old.commit()
        old.close()

        conn = store.connect(path)
        self.addCleanup(conn.close)
        [row] = store.read_observations(conn)
        self.assertEqual(row["market_id"], "KXTEST")
        self.assertEqual(row["price_cents"], 42.0)
        self.assertIsNone(row["price_as_of"])
        self.assertIsNone(row["traded_24h"])
        self.assertEqual(row["price_as_of_kind"], "none")
        self.assertIs(row["event_rules_differ"], False)


if __name__ == "__main__":
    unittest.main()
