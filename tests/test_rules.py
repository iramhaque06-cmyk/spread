"""Offline tests for mapping.py's rule summaries and stale-aware gap.

Two claims are checked here. First, that the curated mapping cannot be
half-filled: an entry that names a venue must say what that venue's rule is,
or loading fails instead of the page quietly showing nothing. Second, that
the curated wording is checked against the rule text the venues are actually
serving, so a rule change surfaces as a warning rather than as a stale
assertion.
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import mapping
from venues import Quote

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def entry(**fields) -> dict:
    base = {
        "event_key": "test-event",
        "title": "Test event",
        "kalshi": "KXTEST-1",
        "polymarket": "test-slug",
        "kalshi_rule": "Yes if the test number is above 1.",
        "polymarket_rule": "Yes if the test number is at least 1.",
        "rules_differ": True,
    }
    base.update(fields)
    return base


def quote(venue: str, **fields) -> Quote:
    base = dict(
        venue=venue,
        market_id=f"{venue}-market",
        title=f"{venue} market",
        price_cents=50.0,
        price_kind="last",
        volume=10,
        url="https://example.test/market",
        last_updated="2026-10-06T11:00:00+00:00",
        status="active",
        price_as_of="2026-10-06T11:00:00+00:00",
        price_as_of_kind="last_price_change",
        traded_24h=True,
        volume_24h=25,
        fetched_at="2026-10-06T12:00:00+00:00",
        rules_text="",
    )
    base.update(fields)
    return Quote(**base)


class LoadValidationTest(unittest.TestCase):
    def load(self, entries: list) -> list:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mapping.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return mapping.load(path)

    def test_a_complete_entry_loads(self):
        self.assertEqual(len(self.load([entry()])), 1)

    def test_a_mapped_venue_without_a_rule_one_liner_is_rejected(self):
        for venue in mapping.VENUES:
            with self.subTest(venue=venue):
                with self.assertRaises(ValueError) as caught:
                    self.load([entry(**{f"{venue}_rule": ""})])
                self.assertIn(f"{venue}_rule", str(caught.exception))

    def test_pairing_both_venues_without_a_differ_flag_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.load([entry(rules_differ=None)])
        self.assertIn("rules_differ", str(caught.exception))

    def test_a_single_venue_entry_needs_no_differ_flag(self):
        loaded = self.load([entry(polymarket=None, rules_differ=None)])
        self.assertEqual(loaded[0]["event_key"], "test-event")


class ShippedMappingTest(unittest.TestCase):
    """The mapping.json in the repository must satisfy its own rules."""

    def test_every_entry_is_fully_curated(self):
        entries = mapping.load()
        self.assertTrue(entries)
        for item in entries:
            key = item["event_key"]
            with self.subTest(event=key):
                self.assertTrue(item["kalshi_rule"], f"{key}: no Kalshi rule line")
                self.assertTrue(
                    item["polymarket_rule"], f"{key}: no Polymarket rule line"
                )
                self.assertIsInstance(item["rules_differ"], bool)
                if not item["rules_differ"]:
                    self.assertTrue(
                        item.get("rules_note"),
                        f"{key}: claims matching rules with no explanation",
                    )

    def test_flagged_pairs_explain_why(self):
        for item in mapping.load():
            if item["rules_differ"]:
                with self.subTest(event=item["event_key"]):
                    self.assertTrue(item.get("rules_note"))
                    self.assertNotEqual(
                        item["kalshi_rule"], item["polymarket_rule"]
                    )


class RuleDriftTest(unittest.TestCase):
    def test_no_warning_when_the_live_rules_still_mention_the_terms(self):
        item = entry(kalshi_rule_terms=["above 1"], polymarket_rule_terms=["at least 1"])
        quotes = {
            "kalshi": quote("kalshi", rules_text="Yes if the number is above 1."),
            "polymarket": quote(
                "polymarket", rules_text="Yes if the number is at least 1."
            ),
        }
        self.assertEqual(mapping.rule_drift(item, quotes), [])

    def test_a_changed_threshold_is_reported(self):
        item = entry(
            kalshi_rule_terms=["above 1"],
            polymarket_rule_terms=["at least 1"],
        )
        quotes = {
            "kalshi": quote("kalshi", rules_text="Yes if the number is above 2."),
            "polymarket": quote(
                "polymarket", rules_text="Yes if the number is at least 1."
            ),
        }
        notes = mapping.rule_drift(item, quotes)
        self.assertEqual(len(notes), 1)
        self.assertIn("kalshi: live rules no longer mention 'above 1'", notes[0])

    def test_missing_live_text_is_reported_not_ignored(self):
        item = entry(kalshi_rule_terms=["above 1"])
        quotes = {
            "kalshi": quote("kalshi", rules_text=""),
            "polymarket": quote("polymarket", rules_text="x"),
        }
        self.assertEqual(
            mapping.rule_drift(item, quotes),
            [
                "kalshi: no live rule text available to check against",
                "polymarket: rule wording is not machine-checked",
            ],
        )

    def test_entries_without_terms_are_marked_unchecked(self):
        quotes = {
            "kalshi": quote("kalshi", rules_text="something"),
            "polymarket": quote("polymarket", rules_text="something"),
        }
        self.assertEqual(
            mapping.rule_drift(entry(), quotes),
            [
                "kalshi: rule wording is not machine-checked",
                "polymarket: rule wording is not machine-checked",
            ],
        )

    def test_an_unmapped_venue_is_not_checked(self):
        item = entry(event_key="only-poly", kalshi=None)
        notes = mapping.rule_drift(
            item, {"polymarket": quote("polymarket", rules_text="x")}
        )
        self.assertEqual(notes, ["polymarket: rule wording is not machine-checked"])


class GapForPairTest(unittest.TestCase):
    def pair(self, kalshi_quote, polymarket_quote) -> mapping.EventPair:
        return mapping.pair(entry(), {"kalshi": kalshi_quote, "polymarket": polymarket_quote})

    def test_fresh_prices_produce_the_spread(self):
        event = self.pair(quote("kalshi", price_cents=80.0), quote("polymarket", price_cents=81.0))
        self.assertEqual(mapping.gap_for_pair(event, now=NOW), -1.0)

    def test_a_stale_side_excludes_the_gap(self):
        event = self.pair(
            quote("kalshi", price_cents=80.0, price_as_of="2026-10-01T00:00:00+00:00"),
            quote("polymarket", price_cents=81.0),
        )
        self.assertIsNone(mapping.gap_for_pair(event, now=NOW))
        # The raw arithmetic is untouched: only comparability changed.
        self.assertEqual(event.spread_points, -1.0)

    def test_a_missing_side_excludes_the_gap(self):
        event = self.pair(quote("kalshi", price_cents=80.0), None)
        self.assertIsNone(mapping.gap_for_pair(event, now=NOW))

    def test_pair_records_rule_drift_notes(self):
        event = mapping.pair(
            entry(kalshi_rule_terms=["0.7%"], polymarket=None, rules_differ=None),
            {"kalshi": quote("kalshi", rules_text="totally different rules")},
        )
        self.assertEqual(len(event.rule_notes), 1)
        self.assertIn("0.7%", event.rule_notes[0])


if __name__ == "__main__":
    unittest.main()
