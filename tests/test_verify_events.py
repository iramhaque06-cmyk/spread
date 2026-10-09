"""Offline tests for verify_events.py, the checker that fails loudly.

Every test builds its own database, drives the real matching pass through
:func:`catalog.run_matching` where the point is what gets stored, and checks
the checker's own verdict. `now=` is always passed: nothing here reads a clock.
"""

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import catalog
import store
import verify_events
from venues import MarketRecord, outcome

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
ISO = NOW.isoformat(timespec="seconds")


def record(venue, market_id, title, *, price=62.0, venue_type="prediction_market"):
    """One priced, fresh market row, as a venue adapter would hand it over."""
    return MarketRecord(
        venue=venue,
        venue_type=venue_type,
        market_id=market_id,
        title=title,
        close_time="2026-12-31T00:00:00+00:00",
        outcomes=(outcome("Yes", price),),
        price=price,
        price_kind="last",
        volume=1000,
        volume_24h=10,
        updated_at=ISO,
        price_as_of=ISO,
        as_of_kind="last_price_change",
        url=f"https://example.test/{market_id}",
        rule_text="Resolves per the official release.",
        status="active",
        fetched_at=ISO,
    )


class VerifyEventsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, *records):
        store.upsert_markets(self.conn, list(records), now=ISO)


class CleanCatalogTest(VerifyEventsCase):
    """A matched catalog that breaks no rule reports nothing."""

    def test_a_two_venue_match_passes_every_check(self):
        self.add(
            record("kalshi", "K1", "Will bitcoin close above 100000 dollars in 2026?"),
            record("polymarket", "P1", "Will bitcoin close above 100000 dollars in 2026?"),
        )
        catalog.run_matching(self.conn, now=NOW)

        self.assertEqual(verify_events.problems(self.conn, now=NOW), [])
        stream = io.StringIO()
        events, members, found = verify_events.report(self.conn, now=NOW, stream=stream)
        self.assertEqual((events, members, found), (1, 2, 0))
        self.assertIn("no problems", stream.getvalue())

    def test_a_play_money_venue_attaches_without_being_published(self):
        """Mana is shown beside a real pair, and never counts toward the two."""
        self.add(
            record("kalshi", "K1", "Will the Fed cut rates in December 2026?"),
            record("polymarket", "P1", "Will the Fed cut rates in December 2026?"),
            record(
                "manifold",
                "M1",
                "Will the Fed cut rates in December 2026?",
                venue_type="play_money",
            ),
        )
        catalog.run_matching(self.conn, now=NOW)

        [event] = store.read_matched_events(self.conn)
        self.assertTrue(event["published"])
        self.assertEqual(event["venue_count"], 3)
        self.assertEqual(verify_events.problems(self.conn, now=NOW), [])

    def test_a_single_real_money_venue_is_not_published(self):
        self.add(
            record("kalshi", "K1", "Will the Fed cut rates in December 2026?"),
            record(
                "manifold",
                "M1",
                "Will the Fed cut rates in December 2026?",
                venue_type="play_money",
            ),
        )
        catalog.run_matching(self.conn, now=NOW)

        [event] = store.read_matched_events(self.conn)
        self.assertFalse(event["published"])
        self.assertFalse(verify_events.problems(self.conn, now=NOW))

    def test_an_inverted_pair_keeps_its_side_through_the_store(self):
        self.add(
            record("kalshi", "K-A", "Will bitcoin close above 100000 dollars on December 31, 2026?"),
            record("polymarket", "P-B", "Will bitcoin close below 100000 dollars on December 31, 2026?"),
        )
        catalog.run_matching(self.conn, now=NOW)

        [event] = store.read_matched_events(self.conn)
        self.assertTrue(event["rules_differ"])
        flipped = {
            member["venue"]: member["inverted"]
            for member in store.read_members(self.conn, event["event_key"])
        }
        self.assertEqual(flipped, {"kalshi": 1, "polymarket": 0})
        self.assertEqual(verify_events.problems(self.conn, now=NOW), [])


class BrokenCatalogTest(VerifyEventsCase):
    """Every rule the checker promises to catch, caught."""

    def store_event(self, *members, published=1, possible=0):
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, published, rules_differ, venue_count, gap, "
            "updated_at) VALUES ('ev-x', 'An event', '', '', 95.0, ?, ?, 0, ?, NULL, ?)",
            (possible, published, len(members), ISO),
        )
        for venue, market_id, inverted in members:
            self.conn.execute(
                "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
                "confidence, rules_differ, matched_on, inverted) "
                "VALUES ('ev-x', ?, ?, '', 95.0, 0, 'title', ?)",
                (venue, market_id, inverted),
            )
        self.conn.commit()

    def test_a_repeated_venue_fails(self):
        self.add(
            record("kalshi", "K1", "Will the Fed cut rates in December 2026?", price=60.0),
            record("kalshi", "K2", "Will the Fed cut rates in December 2026?", price=61.0),
            record("polymarket", "P1", "Will the Fed cut rates in December 2026?", price=55.0),
        )
        self.store_event(("kalshi", "K1", 0), ("kalshi", "K2", 0), ("polymarket", "P1", 0))

        found = verify_events.problems(self.conn, now=NOW)
        self.assertEqual(len(found), 1)
        self.assertIn("kalshi appears 2 times", found[0])

    def test_published_without_two_fresh_real_money_venues_fails(self):
        self.add(
            record("kalshi", "K1", "Will the Fed cut rates in December 2026?", price=60.0),
            record(
                "manifold",
                "M1",
                "Will the Fed cut rates in December 2026?",
                price=50.0,
                venue_type="play_money",
            ),
        )
        self.store_event(("kalshi", "K1", 0), ("manifold", "M1", 0))

        found = verify_events.problems(self.conn, now=NOW)
        self.assertEqual(len(found), 1)
        self.assertIn("published with 1 fresh real-money venue", found[0])

    def test_published_while_possible_fails(self):
        self.add(
            record("kalshi", "K1", "Will the Fed cut rates in December 2026?", price=60.0),
            record("polymarket", "P1", "Will the Fed cut rates in December 2026?", price=55.0),
        )
        self.store_event(("kalshi", "K1", 0), ("polymarket", "P1", 0), possible=1)

        found = verify_events.problems(self.conn, now=NOW)
        self.assertTrue(
            any("published while still a possible match" in line for line in found),
            found,
        )

    def test_members_stating_different_numbers_fail(self):
        self.add(
            record("kalshi", "K1", "Will bitcoin close above 100000 in 2026?", price=60.0),
            record("polymarket", "P1", "Will bitcoin close above 120000 in 2026?", price=55.0),
        )
        self.store_event(("kalshi", "K1", 0), ("polymarket", "P1", 0))

        found = verify_events.problems(self.conn, now=NOW)
        numbers = [line for line in found if "states" in line]
        self.assertEqual(len(numbers), 1, found)
        self.assertIn("100000", numbers[0])
        self.assertIn("120000", numbers[0])

    def test_a_member_stating_no_numbers_fails(self):
        self.add(
            record("kalshi", "K1", "Will bitcoin close above 100000 in 2026?", price=60.0),
            record("polymarket", "P1", "Will bitcoin close above the record in 2026?", price=55.0),
        )
        self.store_event(("kalshi", "K1", 0), ("polymarket", "P1", 0))

        found = verify_events.problems(self.conn, now=NOW)
        self.assertTrue(any("no numbers" in line for line in found), found)

    def test_an_event_of_one_market_fails(self):
        self.add(record("kalshi", "K1", "Will the Fed cut rates in December 2026?", price=60.0))
        self.store_event(("kalshi", "K1", 0))

        found = verify_events.problems(self.conn, now=NOW)
        self.assertTrue(any("needs two markets" in line for line in found), found)


if __name__ == "__main__":
    unittest.main()
