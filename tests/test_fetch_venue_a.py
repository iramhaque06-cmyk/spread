"""Offline test for fetch_venue_a's row mapping (committed Kalshi fixture)."""

import json
import unittest
from pathlib import Path

import fetch_venue_a

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RowFromTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.market = load("kalshi_market_KXFEDDECISION-26OCT-H0.json")["market"]
        cls.series = load("kalshi_series_KXFEDDECISION.json")["series"]

    def row(self):
        return fetch_venue_a.row_from(self.market, self.series)

    def test_stores_the_five_required_fields(self):
        row = self.row()
        self.assertEqual(
            row["title"],
            "Will the Federal Reserve Hike rates by 0bps at their October 2026 meeting?",
        )
        self.assertEqual(row["price_cents"], 80.0)
        self.assertEqual(row["volume"], 3_400_660)
        self.assertEqual(row["resolution_date"], "2026-10-28T17:59:00Z")
        self.assertEqual(
            row["url"],
            "https://kalshi.com/markets/kxfeddecision/fed-meeting/kxfeddecision-26oct",
        )

    def test_identifies_the_row_for_idempotent_upserts(self):
        row = self.row()
        self.assertEqual(row["ticker"], "KXFEDDECISION-26OCT-H0")
        self.assertEqual(row["series_ticker"], "KXFEDDECISION")
        self.assertTrue(row["Fetched_at".lower()].startswith("20"))

    def test_resolution_date_falls_back_to_expiration(self):
        market = dict(self.market, close_time=None)
        row = fetch_venue_a.row_from(market, self.series)
        self.assertEqual(row["resolution_date"], market["expiration_time"])

    def test_market_without_a_price_is_rejected(self):
        market = dict(
            self.market,
            last_price_dollars=None,
            yes_bid_dollars=None,
            yes_ask_dollars=None,
        )
        with self.assertRaises(ValueError):
            fetch_venue_a.row_from(market, self.series)


class SeriesLimitTest(unittest.TestCase):
    """Kalshi ignores `limit` on /series, so the scan must bound itself."""

    def test_limit_is_applied_client_side(self):
        original = fetch_venue_a.get_json
        fetch_venue_a.get_json = lambda url, params=None, max_age=0: {
            "series": [{"ticker": f"KX{i}"} for i in range(894)]
        }
        self.addCleanup(lambda: setattr(fetch_venue_a, "get_json", original))

        self.assertEqual(len(fetch_venue_a.fetch_economic_series(20, max_age=0)), 20)
        self.assertEqual(len(fetch_venue_a.fetch_economic_series(3, max_age=0)), 3)


if __name__ == "__main__":
    unittest.main()
