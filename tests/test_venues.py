"""Fixture tests for the venue adapters (no network: tests/fixtures/*.json)."""

import copy
import json
import unittest
from pathlib import Path

from venues import kalshi, polymarket

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ParseMarketTest(unittest.TestCase):
    """Parse a real Fed-decision market captured from Kalshi's public API."""

    @classmethod
    def setUpClass(cls):
        cls.body = load("kalshi_market_KXFEDDECISION-26OCT-H0.json")
        cls.series = load("kalshi_series_KXFEDDECISION.json")["series"]
        cls.quote = kalshi.parse_market(cls.body, series=cls.series)

    def test_venue_and_ids(self):
        self.assertEqual(self.quote.venue, "kalshi")
        self.assertEqual(self.quote.market_id, "KXFEDDECISION-26OCT-H0")
        self.assertEqual(
            self.quote.title,
            "Will the Federal Reserve Hike rates by 0bps at their October 2026 meeting?",
        )
        self.assertEqual(self.quote.status, "active")

    def test_price_is_last_trade_in_cents(self):
        # Fixture holds last_price_dollars "0.8000" -> 80 cents.
        self.assertEqual(self.quote.price_cents, 80.0)
        self.assertEqual(self.quote.price_kind, "last")

    def test_price_falls_back_to_bid_ask_midpoint(self):
        market = copy.deepcopy(self.body["market"])
        market["last_price_dollars"] = None
        market["yes_bid_dollars"] = "0.7900"
        market["yes_ask_dollars"] = "0.8100"
        quote = kalshi.parse_market(market, series=self.series)
        self.assertEqual(quote.price_cents, 80.0)
        self.assertEqual(quote.price_kind, "mid")

    def test_price_fails_loudly_without_any_quote(self):
        market = copy.deepcopy(self.body["market"])
        for field in ("last_price_dollars", "yes_bid_dollars", "yes_ask_dollars"):
            market[field] = None
        with self.assertRaises(ValueError):
            kalshi.parse_market(market, series=self.series)

    def test_volume_is_an_integer(self):
        self.assertEqual(self.quote.volume, 3_400_660)

    def test_link_is_the_canonical_kalshi_page(self):
        self.assertEqual(
            self.quote.url,
            "https://kalshi.com/markets/kxfeddecision/fed-meeting/kxfeddecision-26oct",
        )

    def test_last_updated_is_the_venue_timestamp(self):
        self.assertEqual(self.quote.last_updated, "2026-09-30T18:52:10.739024Z")

    def test_accepts_bare_market_object(self):
        quote = kalshi.parse_market(self.body["market"], series=self.series)
        self.assertEqual(quote, self.quote)


class PolymarketParseTest(unittest.TestCase):
    """Parse the real Fed 'no change' market captured from Gamma."""

    @classmethod
    def setUpClass(cls):
        cls.body = load("polymarket_market_2589812.json")
        cls.quote = polymarket.parse_market(cls.body)

    def test_venue_and_ids(self):
        self.assertEqual(self.quote.venue, "polymarket")
        self.assertEqual(
            self.quote.market_id,
            "will-there-be-no-change-in-fed-interest-rates-after-the-october-"
            "2026-meeting-20260617190324031",
        )
        self.assertEqual(
            self.quote.title,
            "Will there be no change in Fed interest rates after the October 2026 meeting?",
        )
        self.assertEqual(self.quote.status, "active")

    def test_price_is_last_trade_in_cents(self):
        # Gamma's 0-1 lastTradePrice 0.81 -> 81 cents.
        self.assertEqual(self.quote.price_cents, 81.0)
        self.assertEqual(self.quote.price_kind, "last")

    def test_price_falls_back_to_bid_ask_midpoint(self):
        market = copy.deepcopy(self.body[0])
        market["lastTradePrice"] = None  # bestBid 0.80 / bestAsk 0.81
        quote = polymarket.parse_market(market)
        self.assertEqual(quote.price_cents, 80.5)
        self.assertEqual(quote.price_kind, "mid")

    def test_price_fails_loudly_without_any_quote(self):
        market = copy.deepcopy(self.body[0])
        for field in ("lastTradePrice", "bestBid", "bestAsk"):
            market[field] = None
        with self.assertRaises(ValueError):
            polymarket.parse_market(market)

    def test_volume_is_an_integer(self):
        self.assertEqual(self.quote.volume, 7_586_201)

    def test_link_is_the_canonical_polymarket_page(self):
        self.assertEqual(
            self.quote.url,
            "https://polymarket.com/event/fed-decision-in-october-20260617190323537/"
            "will-there-be-no-change-in-fed-interest-rates-after-the-october-"
            "2026-meeting-20260617190324031",
        )

    def test_link_falls_back_without_the_parent_event(self):
        market = copy.deepcopy(self.body[0])
        market["events"] = []
        quote = polymarket.parse_market(market)
        self.assertEqual(quote.url, f"https://polymarket.com/market/{market['slug']}")

    def test_last_updated_is_the_venue_timestamp(self):
        self.assertEqual(self.quote.last_updated, "2026-10-06T00:26:18.513158Z")

    def test_accepts_bare_market_object(self):
        self.assertEqual(polymarket.parse_market(self.body[0]), self.quote)

    def test_rejects_an_empty_response(self):
        with self.assertRaises(ValueError):
            polymarket.parse_market([])


class SlugifyTest(unittest.TestCase):
    """The URL slug must match Kalshi's own pages (checked vs their sitemap)."""

    def test_fed_meeting(self):
        self.assertEqual(kalshi.slugify("Fed meeting"), "fed-meeting")

    def test_punctuation_and_case(self):
        self.assertEqual(kalshi.slugify(" Jobs  Numbers! "), "jobs-numbers")

    def test_numbers_are_kept(self):
        self.assertEqual(kalshi.slugify("CPI core"), "cpi-core")


if __name__ == "__main__":
    unittest.main()
