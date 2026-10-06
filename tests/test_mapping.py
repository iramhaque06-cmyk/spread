"""Offline tests for the curated mapping, pairing, spread math, and suggestions."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import mapping
from venues import Quote, kalshi, polymarket

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_quotes():
    """The paired quotes for the Fed event, straight from the fixtures."""
    kalshi_quote = kalshi.parse_market(
        load("kalshi_market_KXFEDDECISION-26OCT-H0.json"),
        series=load("kalshi_series_KXFEDDECISION.json")["series"],
    )
    polymarket_quote = polymarket.parse_market(load("polymarket_market_2589812.json"))
    return kalshi_quote, polymarket_quote


def bare_quote(venue: str, price_cents: float) -> Quote:
    return Quote(
        venue=venue,
        market_id=f"{venue}-id",
        title="a market",
        price_cents=price_cents,
        price_kind="last",
        volume=1,
        url="https://example.invalid/market",
        last_updated="2026-10-06T00:00:00Z",
        status="active",
    )


class MappingFileTest(unittest.TestCase):
    """The committed mapping must stay complete and well-formed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mapping.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return self.path

    def test_seeds_fed_cpi_and_jobs_report(self):
        entries = mapping.load()
        self.assertEqual(
            [entry["event_key"] for entry in entries],
            ["fed-2026-10", "cpi-2026-09", "payrolls-2026-10"],
        )
        for entry in entries:
            self.assertTrue(entry["kalshi"], f"{entry['event_key']} has no Kalshi side")
            self.assertTrue(
                entry["polymarket"], f"{entry['event_key']} has no Polymarket side"
            )

    def test_rejects_a_missing_title(self):
        with self.assertRaises(ValueError):
            mapping.load(self.write([{"event_key": "x", "kalshi": "TICKER"}]))

    def test_rejects_an_entry_with_no_venue_market(self):
        with self.assertRaises(ValueError):
            mapping.load(self.write([{"event_key": "x", "title": "X"}]))

    def test_rejects_unknown_fields(self):
        entry = {"event_key": "x", "title": "X", "kalshi": "TICKER", "polymarket": "s"}
        with self.assertRaises(ValueError):
            mapping.load(self.write([dict(entry, kalshii="typo")]))

    def test_rejects_a_non_array_file(self):
        with self.assertRaises(ValueError):
            mapping.load(self.write({"event_key": "x"}))


class SpreadMathTest(unittest.TestCase):
    """Spread = Kalshi cents - Polymarket cents, or nothing at all."""

    def test_kalshi_below_polymarket_is_negative(self):
        self.assertEqual(mapping.spread_between(80.0, 81.0), -1.0)

    def test_kalshi_above_polymarket_is_positive(self):
        self.assertEqual(mapping.spread_between(85.5, 80.0), 5.5)

    def test_result_is_rounded_to_points(self):
        self.assertEqual(mapping.spread_between(80.049, 80.0), 0.05)

    def test_missing_either_side_gives_no_spread(self):
        self.assertIsNone(mapping.spread_between(None, 81.0))
        self.assertIsNone(mapping.spread_between(80.0, None))
        self.assertIsNone(mapping.spread_between(None, None))


class PairingTest(unittest.TestCase):
    def test_pairs_the_fixture_quotes(self):
        kalshi_quote, polymarket_quote = fixture_quotes()
        entry = mapping.load()[0]
        event = mapping.pair(
            entry, {"kalshi": kalshi_quote, "polymarket": polymarket_quote}
        )

        self.assertEqual(event.event_key, "fed-2026-10")
        self.assertEqual(event.missing, ())
        self.assertEqual(event.spread_points, kalshi_quote.price_cents - polymarket_quote.price_cents)

    def test_one_side_missing_means_no_spread(self):
        kalshi_quote, _ = fixture_quotes()
        event = mapping.pair(
            {"event_key": "x", "title": "X"}, {"kalshi": kalshi_quote}
        )
        self.assertEqual(event.missing, ("polymarket",))
        self.assertIsNone(event.spread_points)

    def test_fetch_pairs_drives_the_existing_adapters(self):
        kalshi_quote, polymarket_quote = fixture_quotes()
        calls = []

        def fake_kalshi(market_id, *, max_age):
            calls.append(("kalshi", market_id))
            return kalshi_quote

        def fake_polymarket(market_id, *, max_age):
            calls.append(("polymarket", market_id))
            return polymarket_quote

        original = mapping.FETCHERS
        mapping.FETCHERS = {"kalshi": fake_kalshi, "polymarket": fake_polymarket}
        self.addCleanup(lambda: setattr(mapping, "FETCHERS", original))

        entries = [{"event_key": "x", "title": "X", "kalshi": "TICK", "polymarket": "slug"}]
        [event] = mapping.fetch_pairs(entries, max_age=0)

        self.assertEqual(calls, [("kalshi", "TICK"), ("polymarket", "slug")])
        self.assertEqual(event.spread_points, -1.0)
        self.assertEqual(event.missing, ())

    def test_a_failing_venue_leaves_that_side_missing(self):
        _, polymarket_quote = fixture_quotes()

        def explode(market_id, *, max_age):
            raise ValueError("venue unreachable")

        original = mapping.FETCHERS
        mapping.FETCHERS = {
            "kalshi": explode,
            "polymarket": lambda market_id, *, max_age: polymarket_quote,
        }
        self.addCleanup(lambda: setattr(mapping, "FETCHERS", original))

        entries = [{"event_key": "x", "title": "X", "kalshi": "TICK", "polymarket": "slug"}]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            [event] = mapping.fetch_pairs(entries, max_age=0)

        self.assertEqual(event.missing, ("kalshi",))
        self.assertIsNone(event.spread_points)
        self.assertIn("venue unreachable", stderr.getvalue())

    def test_an_unmapped_side_is_not_fetched(self):
        _, polymarket_quote = fixture_quotes()
        calls = []
        original = mapping.FETCHERS
        mapping.FETCHERS = {
            "kalshi": lambda market_id, *, max_age: calls.append(market_id),
            "polymarket": lambda market_id, *, max_age: (
                calls.append(market_id) or polymarket_quote
            ),
        }
        self.addCleanup(lambda: setattr(mapping, "FETCHERS", original))

        with redirect_stderr(io.StringIO()):
            [event] = mapping.fetch_pairs(
                [{"event_key": "x", "title": "X", "polymarket": "slug"}], max_age=0
            )

        self.assertEqual(event.missing, ("kalshi",))
        self.assertEqual(len(calls), 1)


class SuggestTest(unittest.TestCase):
    """rapidfuzz only ever suggests; the file stays curated."""

    CANDIDATES = [
        "Will the US add at least 100k jobs in October?",
        "Will monthly inflation increase by 0.8% or more in September?",
        "Will there be no change in Fed interest rates after the October 2026 meeting?",
    ]

    def test_ranks_the_closest_market_first(self):
        results = mapping.suggest(
            "Will CPI rise more than 0.7% in September 2026?", self.CANDIDATES
        )
        self.assertTrue(results)
        self.assertTrue(
            results[0][0].startswith("Will monthly inflation increase by 0.8%"),
            f"unexpected best match: {results[0]}",
        )

    def test_ranks_the_jobs_market_for_a_jobs_question(self):
        results = mapping.suggest(
            "Will above 100000 jobs be added in October 2026?", self.CANDIDATES
        )
        self.assertTrue(results)
        self.assertTrue(
            results[0][0].startswith("Will the US add at least 100k"),
            f"unexpected best match: {results[0]}",
        )

    def test_scores_descend(self):
        results = mapping.suggest(
            "Will CPI rise more than 0.7% in September 2026?", self.CANDIDATES
        )
        scores = [score for _, score in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_candidates_means_no_suggestions(self):
        self.assertEqual(mapping.suggest("anything", []), [])


if __name__ == "__main__":
    unittest.main()
