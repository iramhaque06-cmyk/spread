"""Offline tests for dataview.py: the exact shape the website reads.

The site brief fixes the document's keys, so they are asserted literally here.
Anything the website renders has to survive this file: a renamed key or a
newly computed gap would show up as a failure rather than as a page that
quietly loses a column.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dataview
import store
from venues import Quote

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

TOP_LEVEL = {"generated_at", "events", "moves", "venues_status"}
EVENT_KEYS = {
    "id",
    "title",
    "category",
    "status",
    "resolves_at",
    "gap",
    "rules_differ",
    "venues",
    "history",
}
VENUE_KEYS = {"venue", "price", "volume", "updated_at", "url", "rule_text", "is_stale"}


def quote(venue: str, price: float, **fields) -> Quote:
    base = dict(
        venue=venue,
        market_id=f"{venue}-market",
        title=f"{venue} market",
        price_cents=price,
        price_kind="last",
        volume=1_000,
        url=f"https://example.test/{venue}",
        last_updated="2026-09-30T18:52:10Z",
        status="active",
        price_as_of="2026-10-06T11:00:00+00:00",
        price_as_of_kind="last_price_change",
        traded_24h=True,
        volume_24h=100,
        fetched_at="2026-10-06T11:59:00+00:00",
        rules_text=f"{venue} rule text",
        resolves_at="2026-10-14T12:25:00Z",
    )
    base.update(fields)
    return Quote(**base)


class DataViewTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "spread.db")
        self.addCleanup(self.conn.close)

    def add(self, key, *, kalshi=None, polymarket=None, title="Test event", rules_differ=False):
        store.upsert_event(
            self.conn,
            key,
            title,
            kalshi_rule="K rule",
            polymarket_rule="P rule",
            rules_differ=rules_differ,
        )
        for item in (kalshi, polymarket):
            if item is not None:
                store.upsert_observation(self.conn, key, item)

    def build(self):
        return dataview.build(self.conn, now=NOW)


class ShapeTest(DataViewTestCase):
    def test_top_level_keys_are_exactly_the_agreed_shape(self):
        self.add("only-kalshi", kalshi=quote("kalshi", 40.0))
        document = self.build()
        self.assertEqual(set(document), TOP_LEVEL)

    def test_event_and_venue_keys_are_exactly_the_agreed_shape(self):
        self.add("pair", kalshi=quote("kalshi", 40.0), polymarket=quote("polymarket", 35.0))
        [event] = self.build()["events"]
        self.assertEqual(set(event), EVENT_KEYS)
        for venue in event["venues"]:
            self.assertEqual(set(venue), VENUE_KEYS)

    def test_venues_status_keys(self):
        self.add("pair", kalshi=quote("kalshi", 40.0))
        [status] = [
            item for item in self.build()["venues_status"] if item["venue"] == "kalshi"
        ]
        self.assertEqual(
            set(status), {"venue", "last_success_at", "market_count", "notes"}
        )
        self.assertEqual(status["market_count"], 1)
        self.assertEqual(status["last_success_at"], "2026-10-06T11:59:00+00:00")
        self.assertTrue(status["notes"])

    def test_moves_keys(self):
        self.add("pair", kalshi=quote("kalshi", 40.0))
        store.insert_snapshot(
            self.conn, "pair", quote("kalshi", 40.0), at=(NOW - timedelta(hours=3)).isoformat()
        )
        store.insert_snapshot(self.conn, "pair", quote("kalshi", 44.0), at=NOW.isoformat())
        [move] = self.build()["moves"]
        self.assertEqual(
            set(move), {"event_id", "venue", "from_price", "to_price", "at"}
        )


class GapTest(DataViewTestCase):
    def test_gap_is_max_minus_min_of_fresh_venues(self):
        self.add("pair", kalshi=quote("kalshi", 25.0), polymarket=quote("polymarket", 31.0))
        [event] = self.build()["events"]
        self.assertEqual(event["gap"], 6.0)

    def test_gap_is_unsigned_even_when_the_sides_swap(self):
        self.add("pair", kalshi=quote("kalshi", 31.0), polymarket=quote("polymarket", 25.0))
        [event] = self.build()["events"]
        self.assertEqual(event["gap"], 6.0)

    def test_a_stale_price_is_excluded_and_leaves_the_gap_null(self):
        self.add(
            "pair",
            kalshi=quote("kalshi", 25.0, price_as_of="2026-10-01T00:00:00+00:00"),
            polymarket=quote("polymarket", 31.0),
        )
        [event] = self.build()["events"]
        by_venue = {item["venue"]: item for item in event["venues"]}
        self.assertTrue(by_venue["kalshi"]["is_stale"])
        self.assertFalse(by_venue["polymarket"]["is_stale"])
        # Fewer than two fresh prices: no gap may be invented.
        self.assertIsNone(event["gap"])

    def test_kalshi_can_be_current_while_its_record_stamp_looks_old(self):
        # The documented nuance: freshness comes from 24 hour volume, so the
        # stamp below can be months old while the price is current.
        self.add(
            "pair",
            kalshi=quote(
                "kalshi",
                25.0,
                price_as_of=None,
                traded_24h=True,
                volume_24h=119_581,
                last_updated="2026-07-02T15:15:48Z",
            ),
            polymarket=quote("polymarket", 31.0),
        )
        [event] = self.build()["events"]
        kalshi_row = {item["venue"]: item for item in event["venues"]}["kalshi"]
        self.assertFalse(kalshi_row["is_stale"])
        self.assertEqual(kalshi_row["updated_at"], "2026-07-02T15:15:48Z")
        self.assertEqual(event["gap"], 6.0)

    def test_no_trade_in_24_hours_is_stale(self):
        self.add(
            "pair",
            kalshi=quote("kalshi", 25.0, price_as_of=None, traded_24h=False, volume_24h=0),
            polymarket=quote("polymarket", 31.0),
        )
        [event] = self.build()["events"]
        self.assertTrue(event["venues"][0]["is_stale"])
        self.assertIsNone(event["gap"])

    def test_two_fresh_venues_of_three_use_the_min_and_max(self):
        self.add(
            "trio",
            kalshi=quote("kalshi", 10.0, price_as_of="2026-10-01T00:00:00+00:00"),
            polymarket=quote("polymarket", 20.0),
        )
        # A third venue stands in for a future one, stored under its own name.
        store.upsert_observation(
            self.conn, "trio", replace(quote("polymarket", 45.0), venue="other")
        )
        document = self.build()
        [event] = [item for item in document["events"] if item["id"] == "trio"]
        self.assertEqual(event["gap"], 25.0)


class EventFactsTest(DataViewTestCase):
    def test_status_is_active_while_a_venue_still_lists_the_market(self):
        self.add("pair", kalshi=quote("kalshi", 40.0), polymarket=quote("polymarket", 35.0))
        [event] = self.build()["events"]
        self.assertEqual(event["status"], "active")

    def test_status_is_resolved_when_every_venue_is_closed(self):
        self.add(
            "pair",
            kalshi=quote("kalshi", 40.0, status="closed"),
            polymarket=quote("polymarket", 35.0, status="closed"),
        )
        [event] = self.build()["events"]
        self.assertEqual(event["status"], "resolved")

    def test_resolves_at_is_the_earliest_venue_date(self):
        self.add(
            "pair",
            kalshi=quote("kalshi", 40.0, resolves_at="2026-11-06T00:00:00Z"),
            polymarket=quote("polymarket", 35.0, resolves_at="2026-10-14T00:00:00Z"),
        )
        [event] = self.build()["events"]
        self.assertEqual(event["resolves_at"], "2026-10-14T00:00:00Z")

    def test_rules_differ_flag_reaches_the_event(self):
        self.add(
            "pair",
            kalshi=quote("kalshi", 40.0),
            polymarket=quote("polymarket", 35.0),
            rules_differ=True,
        )
        [event] = self.build()["events"]
        self.assertTrue(event["rules_differ"])

    def test_history_is_grouped_by_venue_and_oldest_first(self):
        self.add("pair", kalshi=quote("kalshi", 40.0))
        for minutes, price in ((30, 41.0), (10, 42.0)):
            store.insert_snapshot(
                self.conn,
                "pair",
                quote("kalshi", price),
                at=(NOW - timedelta(minutes=minutes)).isoformat(),
            )
        [event] = self.build()["events"]
        history = event["history"]["kalshi"]
        self.assertEqual([point["price"] for point in history], [41.0, 42.0])
        self.assertEqual(
            [point["t"] for point in history],
            sorted(point["t"] for point in history),
        )

    def test_an_event_with_no_observations_does_not_appear(self):
        store.upsert_event(self.conn, "empty", "Nothing here")
        self.assertEqual(self.build()["events"], [])


class MovesTest(DataViewTestCase):
    def test_only_changed_prices_become_moves(self):
        self.add("moved", kalshi=quote("kalshi", 40.0))
        self.add("still", kalshi=quote("kalshi", 40.0, market_id="k2"))
        for key in ("moved", "still"):
            store.insert_snapshot(
                self.conn, key, quote("kalshi", 40.0), at=(NOW - timedelta(hours=2)).isoformat()
            )
        store.insert_snapshot(self.conn, "moved", quote("kalshi", 44.0), at=NOW.isoformat())
        store.insert_snapshot(self.conn, "still", quote("kalshi", 40.0), at=NOW.isoformat())

        moves = self.build()["moves"]
        self.assertEqual([move["event_id"] for move in moves], ["moved"])
        self.assertEqual(moves[0]["from_price"], 40.0)
        self.assertEqual(moves[0]["to_price"], 44.0)

    def test_a_lone_point_in_the_window_is_not_a_move(self):
        # An out-of-window point is never the baseline: a price observed once
        # in the window cannot be shown to have moved inside it, even when an
        # older snapshot disagrees.
        self.add("old", kalshi=quote("kalshi", 40.0))
        store.insert_snapshot(
            self.conn, "old", quote("kalshi", 10.0), at=(NOW - timedelta(hours=30)).isoformat()
        )
        store.insert_snapshot(self.conn, "old", quote("kalshi", 40.0), at=NOW.isoformat())
        self.assertEqual(self.build()["moves"], [])

    def test_the_baseline_is_the_first_point_inside_the_window(self):
        self.add("old", kalshi=quote("kalshi", 40.0))
        store.insert_snapshot(
            self.conn, "old", quote("kalshi", 10.0), at=(NOW - timedelta(hours=30)).isoformat()
        )
        store.insert_snapshot(
            self.conn, "old", quote("kalshi", 30.0), at=(NOW - timedelta(hours=4)).isoformat()
        )
        store.insert_snapshot(self.conn, "old", quote("kalshi", 40.0), at=NOW.isoformat())
        [move] = self.build()["moves"]
        # 10.0 is 30 hours old, so the move is 30.0 to 40.0, not 10.0 to 40.0.
        self.assertEqual(move["from_price"], 30.0)
        self.assertEqual(move["to_price"], 40.0)
        self.assertEqual(move["at"], NOW.isoformat())

    def test_moves_are_ranked_by_size(self):
        for key, target in (("small", 41.0), ("big", 55.0)):
            self.add(key, kalshi=quote("kalshi", 40.0, market_id=key))
            store.insert_snapshot(
                self.conn, key, quote("kalshi", 40.0), at=(NOW - timedelta(hours=2)).isoformat()
            )
            store.insert_snapshot(self.conn, key, quote("kalshi", target), at=NOW.isoformat())
        self.assertEqual(
            [move["event_id"] for move in self.build()["moves"]], ["big", "small"]
        )


class WriteTest(DataViewTestCase):
    def test_write_creates_the_directory_and_the_document(self):
        self.add("pair", kalshi=quote("kalshi", 40.0))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "nested" / "data.json"
        written = dataview.write(self.conn, path, now=NOW)
        self.assertEqual(written, path)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(document), TOP_LEVEL)
        self.assertEqual(document["generated_at"], NOW.isoformat(timespec="seconds"))


if __name__ == "__main__":
    unittest.main()
