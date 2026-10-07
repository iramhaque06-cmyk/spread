"""Offline tests for fetch_venue_b (committed Polymarket fixture)."""

import json
import unittest
from pathlib import Path

import fetch_venue_b

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVENT = {"slug": "fed-decision-in-october-20260617190323537"}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def columns(schema: str) -> set:
    """Column names of the single CREATE TABLE in a schema string."""
    body = schema.split("(", 1)[1].rsplit(")", 1)[0]
    names = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith("PRIMARY"):
            continue
        names.add(line.split()[0])
    return names


class RowFromTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market = load("polymarket_market_2589812.json")[0]

    def row(self):
        return fetch_venue_b.row_from(self.market, EVENT)

    def test_stores_the_five_required_fields(self):
        row = self.row()
        self.assertEqual(
            row["title"],
            "Will there be no change in Fed interest rates after the October 2026 meeting?",
        )
        self.assertEqual(row["price_cents"], 81.0)
        self.assertEqual(row["volume"], 7_586_201)
        self.assertEqual(row["resolution_date"], "2026-10-29T03:59:00Z")
        self.assertEqual(
            row["url"],
            "https://polymarket.com/event/fed-decision-in-october-20260617190323537/"
            "will-there-be-no-change-in-fed-interest-rates-after-the-october-"
            "2026-meeting-20260617190324031",
        )

    def test_same_schema_as_venue_a_plus_the_venue_column(self):
        import fetch_venue_a

        venue_a = columns(fetch_venue_a.SCHEMA)
        row_keys = set(self.row())
        self.assertIn("venue", row_keys)
        self.assertEqual(row_keys - {"venue"}, venue_a)
        self.assertEqual(self.row()["venue"], "polymarket")

    def test_identifies_the_row_for_idempotent_upserts(self):
        row = self.row()
        self.assertEqual(row["ticker"], "will-there-be-no-change-in-fed-interest-rates-after-the-october-2026-meeting-20260617190324031")
        self.assertEqual(row["series_ticker"], EVENT["slug"])

    def test_market_without_a_price_is_rejected(self):
        market = dict(self.market, lastTradePrice=None, bestBid=None, bestAsk=None)
        with self.assertRaises(ValueError):
            fetch_venue_b.row_from(market, EVENT)


class OpenFilterTest(unittest.TestCase):
    """An open event can still contain resolved markets."""

    def test_live_market_is_kept(self):
        self.assertTrue(fetch_venue_b.is_open({"closed": False, "active": True}))

    def test_closed_market_is_dropped(self):
        self.assertFalse(fetch_venue_b.is_open({"closed": True, "active": True}))

    def test_inactive_market_is_dropped(self):
        self.assertFalse(fetch_venue_b.is_open({"closed": False, "active": False}))


class EventLimitTest(unittest.TestCase):
    """The scan must bound itself even if the API grows a paging quirk."""

    def test_limit_is_applied_client_side(self):
        original = fetch_venue_b.get_json
        fetch_venue_b.get_json = lambda url, params=None, max_age=0: [
            {"slug": f"event-{i}"} for i in range(500)
        ]
        self.addCleanup(lambda: setattr(fetch_venue_b, "get_json", original))

        self.assertEqual(len(fetch_venue_b.fetch_economic_events(50, max_age=0)), 50)
        self.assertEqual(len(fetch_venue_b.fetch_economic_events(7, max_age=0)), 7)

    def test_non_list_payload_yields_no_events(self):
        original = fetch_venue_b.get_json
        fetch_venue_b.get_json = lambda url, params=None, max_age=0: {"error": "nope"}
        self.addCleanup(lambda: setattr(fetch_venue_b, "get_json", original))
        self.assertEqual(fetch_venue_b.fetch_economic_events(10, max_age=0), [])


if __name__ == "__main__":
    unittest.main()
