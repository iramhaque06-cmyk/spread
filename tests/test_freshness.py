"""Offline tests for freshness.py: the 24 hour rule, offline and fixture-free.

The interesting cases are the ones no live run produces reliably: a price
exactly on the boundary, a market with no trade evidence at all, and two
venues whose evidence contradicts itself. Those decide whether the page ever
compares a price it should not have.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timezone

import freshness
from venues import Quote

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def quote(**fields) -> Quote:
    """A minimal Kalshi-shaped quote, overridden per test."""
    base = dict(
        venue="kalshi",
        market_id="KXTEST",
        title="test market",
        price_cents=50.0,
        price_kind="last",
        volume=1000,
        url="https://example.test/market",
        last_updated="2026-10-06T11:00:00+00:00",
        status="active",
    )
    base.update(fields)
    return Quote(**base)


class TimestampEvidenceTest(unittest.TestCase):
    """A real timestamp, when a venue publishes one, decides the verdict."""

    def test_a_fresh_price_is_fresh(self):
        verdict = freshness.assess(
            quote(price_as_of="2026-10-06T11:00:00+00:00"), now=NOW
        )
        self.assertTrue(verdict.fresh)
        self.assertAlmostEqual(verdict.age_hours, 1.0, places=3)
        self.assertIn("price last moved 1.0 h ago", verdict.basis)
        self.assertEqual(verdict.reason, "")

    def test_exactly_24_hours_is_still_fresh(self):
        verdict = freshness.assess(
            quote(price_as_of="2026-10-05T12:00:00+00:00"), now=NOW
        )
        self.assertTrue(verdict.fresh)
        self.assertAlmostEqual(verdict.age_hours, 24.0, places=3)

    def test_just_past_24_hours_is_stale(self):
        verdict = freshness.assess(
            quote(price_as_of="2026-10-05T11:59:00+00:00"), now=NOW
        )
        self.assertFalse(verdict.fresh)
        self.assertIn("older than 24 hours", verdict.reason)
        self.assertIn("24.0 h", verdict.reason)

    def test_a_timestamp_beats_traded_volume(self):
        # Contradictory evidence: the price last moved 30 h ago but a contract
        # traded in the window. The direct measurement wins, so it is stale.
        verdict = freshness.assess(
            quote(
                price_as_of="2026-10-05T06:00:00+00:00",
                price_as_of_kind="last_price_change",
                traded_24h=True,
                volume_24h=10,
            ),
            now=NOW,
        )
        self.assertFalse(verdict.fresh)
        self.assertAlmostEqual(verdict.age_hours, 30.0, places=3)

    def test_a_missing_zone_is_read_as_utc(self):
        verdict = freshness.assess(quote(price_as_of="2026-10-06T11:00:00"), now=NOW)
        self.assertTrue(verdict.fresh)
        self.assertAlmostEqual(verdict.age_hours, 1.0, places=3)

    def test_an_unreadable_timestamp_is_stale(self):
        verdict = freshness.assess(quote(price_as_of="not a timestamp"), now=NOW)
        self.assertFalse(verdict.fresh)
        self.assertIsNone(verdict.age_hours)
        self.assertIn("unreadable", verdict.basis)
        self.assertEqual(verdict.reason, "timestamp could not be parsed")


class VolumeEvidenceTest(unittest.TestCase):
    """Kalshi publishes no trade clock, only trailing 24 hour volume."""

    def test_traded_today_is_fresh_and_quotes_its_evidence(self):
        verdict = freshness.assess(
            quote(traded_24h=True, volume_24h=109_329), now=NOW
        )
        self.assertTrue(verdict.fresh)
        self.assertEqual(
            verdict.basis, "traded in the last 24 hours (109,329 contracts)"
        )

    def test_no_trade_in_24_hours_is_stale(self):
        verdict = freshness.assess(quote(traded_24h=False, volume_24h=0), now=NOW)
        self.assertFalse(verdict.fresh)
        self.assertEqual(verdict.reason, "no trade in the last 24 hours")

    def test_volume_without_a_boolean_still_counts(self):
        verdict = freshness.assess(quote(traded_24h=True, volume_24h=None), now=NOW)
        self.assertTrue(verdict.fresh)
        self.assertIn("some contracts", verdict.basis)


class NoEvidenceTest(unittest.TestCase):
    """A price that cannot be shown fresh is stale, never assumed fresh."""

    def test_no_evidence_at_all_is_stale(self):
        verdict = freshness.assess(quote(), now=NOW)
        self.assertFalse(verdict.fresh)
        self.assertIsNone(verdict.age_hours)
        self.assertEqual(verdict.reason, "cannot be shown to be fresher than 24 hours")


class RowAndFormattingTest(unittest.TestCase):
    """The same rule serves Quotes, stored database rows and the page."""

    def test_database_rows_work_like_quotes(self):
        row = {
            "price_as_of": None,
            "price_as_of_kind": "none",
            "traded_24h": True,
            "volume_24h": 10,
        }
        self.assertTrue(freshness.assess(row, now=NOW).fresh)
        row["traded_24h"] = False
        self.assertFalse(freshness.assess(row, now=NOW).fresh)

    def test_a_quote_without_any_evidence_is_stale(self):
        self.assertFalse(freshness.assess(quote(), now=NOW).fresh)

    def test_as_utc_defaults_to_utc_and_converts(self):
        naive = freshness.as_utc(datetime(2026, 10, 6, 12, 0, 0))
        self.assertEqual(naive.tzinfo, timezone.utc)
        self.assertIsNone(freshness.as_utc(None))

    def test_parse_iso_reads_z_and_offsets(self):
        self.assertEqual(
            freshness.parse_iso("2026-10-06T12:00:00Z"),
            datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_human_age_switches_from_minutes_to_hours(self):
        self.assertEqual(freshness.human_age(0.5), "30 min")
        self.assertEqual(freshness.human_age(5), "5.0 h")

    def test_replace_keeps_a_quote_usable(self):
        # dataclasses.replace is how the tests and callers tweak one field.
        stale = replace(quote(), traded_24h=False, volume_24h=0)
        self.assertFalse(freshness.assess(stale, now=NOW).fresh)


if __name__ == "__main__":
    unittest.main()
