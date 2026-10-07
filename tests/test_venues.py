"""Fixture tests for the venue adapters (no network: tests/fixtures/*.json)."""

import copy
import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

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

    def test_no_trade_time_is_claimed(self):
        # Kalshi publishes none, so the adapter must not invent one: the page
        # is told exactly that instead of being handed updated_time.
        self.assertIsNone(self.quote.price_as_of)
        self.assertEqual(self.quote.price_as_of_kind, "none")

    def test_trailing_24_hour_volume_dates_the_price(self):
        self.assertEqual(self.quote.volume_24h, 110_304)
        self.assertTrue(self.quote.traded_24h)

    def test_volume_24h_missing_leaves_the_evidence_unknown(self):
        market = copy.deepcopy(self.body["market"])
        market["volume_24h_fp"] = None
        quote = kalshi.parse_market(market, series=self.series)
        self.assertIsNone(quote.volume_24h)
        self.assertIsNone(quote.traded_24h)

    def test_fetch_time_is_carried_for_provenance(self):
        quote = kalshi.parse_market(
            self.body, series=self.series, fetched_at="2026-10-06T05:40:00+00:00"
        )
        self.assertEqual(quote.fetched_at, "2026-10-06T05:40:00+00:00")
        # Parsing itself stays deterministic, so two parses stay comparable.
        self.assertEqual(kalshi.parse_market(self.body, series=self.series).fetched_at, "")

    def test_resolution_rules_are_kept_verbatim(self):
        self.assertIn("Federal Reserve", self.quote.rules_text)
        self.assertIn("mutually exclusive", self.quote.rules_text)


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

    def test_the_bare_parse_claims_no_price_timestamp(self):
        # Gamma's updatedAt is a batch stamp, so a bare parse must not pass it
        # off as this market's last trade: fetch_quote supplies the real one.
        self.assertIsNone(self.quote.price_as_of)
        self.assertEqual(self.quote.price_as_of_kind, "none")

    def test_a_supplied_price_timestamp_is_carried_through(self):
        quote = polymarket.parse_market(
            self.body,
            price_as_of="2026-10-06T05:35:14+00:00",
            price_as_of_kind="last_price_change",
        )
        self.assertEqual(quote.price_as_of, "2026-10-06T05:35:14+00:00")
        self.assertEqual(quote.price_as_of_kind, "last_price_change")

    def test_trailing_24_hour_volume_is_captured(self):
        self.assertEqual(self.quote.volume_24h, 353_836)
        self.assertTrue(self.quote.traded_24h)

    def test_resolution_rules_come_from_the_description(self):
        self.assertIn("upper bound", self.quote.rules_text)
        self.assertIn("Federal Open Market Committee", self.quote.rules_text)

    def test_the_yes_token_id_is_the_first_clob_token(self):
        self.assertEqual(
            polymarket.yes_token_id(self.body[0]),
            "111061902544814266207267295505639408607400625795891618462682726460921782993748",
        )

    def test_a_market_without_tokens_has_no_token_id(self):
        market = copy.deepcopy(self.body[0])
        market["clobTokenIds"] = "[]"
        self.assertIsNone(polymarket.yes_token_id(market))
        market["clobTokenIds"] = "not json"
        self.assertIsNone(polymarket.yes_token_id(market))


class ClobPriceHistoryTest(unittest.TestCase):
    """The one public source that dates a Polymarket price, stubbed offline."""

    def sample(self) -> dict:
        return load("polymarket_clob_history_2589812.json")

    def test_last_price_change_reads_the_final_history_point(self):
        body = self.sample()
        expected = datetime.fromtimestamp(
            body["history"][-1]["t"], timezone.utc
        ).isoformat(timespec="seconds")
        with mock.patch.object(polymarket, "get_json", return_value=body) as get:
            stamp = polymarket.last_price_change("token-id")
        self.assertEqual(stamp, expected)
        self.assertTrue(stamp.startswith("2026-10-06T"), stamp)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["interval"], "1d")

    def test_an_empty_day_falls_back_to_the_full_history(self):
        points = self.sample()["history"]
        responses = [{"history": []}, {"history": points}]
        with mock.patch.object(
            polymarket, "get_json", side_effect=responses
        ) as get:
            stamp = polymarket.last_price_change("token-id")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"]["interval"], "max")
        self.assertEqual(
            stamp,
            datetime.fromtimestamp(
                points[-1]["t"], timezone.utc
            ).isoformat(timespec="seconds"),
        )

    def test_no_history_at_all_reports_nothing(self):
        with mock.patch.object(
            polymarket, "get_json", return_value={"history": []}
        ):
            self.assertIsNone(polymarket.last_price_change("token-id"))

    def test_a_broken_clob_falls_back_to_the_labelled_gamma_stamp(self):
        market = load("polymarket_market_2589812.json")

        def fake(url, params=None, *, max_age=None):
            if "prices-history" in url:
                raise requests.RequestException("clob down")
            return market

        stderr = io.StringIO()
        with mock.patch.object(polymarket, "get_json", side_effect=fake):
            with mock.patch("sys.stderr", stderr):
                quote = polymarket.fetch_quote("some-slug")
        # Gamma's stamp is used, but labelled as the batch stamp it is.
        self.assertEqual(quote.price_as_of, "2026-10-06T00:26:18.513158Z")
        self.assertEqual(quote.price_as_of_kind, "batch_stamp")
        self.assertIn("CLOB price history unavailable", stderr.getvalue())

    def test_a_quote_without_tokens_keeps_the_gamma_stamp(self):
        market = copy.deepcopy(load("polymarket_market_2589812.json"))
        market[0]["clobTokenIds"] = "[]"
        with mock.patch.object(
            polymarket, "get_json", return_value=market
        ) as get:
            quote = polymarket.fetch_quote("some-slug")
        self.assertEqual(quote.price_as_of_kind, "batch_stamp")
        self.assertEqual(get.call_count, 1)  # no CLOB call was possible


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
