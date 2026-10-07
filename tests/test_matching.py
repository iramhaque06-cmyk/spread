"""Offline tests for cross-venue matching and the gap rules."""

import unittest

import gaps
import matching
from venues import MarketRecord

ALIASES = {
    "new york knicks": ("knicks",),
    "boston celtics": ("celtics",),
    "los angeles lakers": ("lakers",),
}


def market(venue, market_id, title, **overrides):
    base = dict(
        venue=venue,
        venue_type="play_money" if venue == "manifold" else "prediction_market",
        market_id=market_id,
        title=title,
        close_time="2026-11-01T00:00:00Z",
        price=40.0,
        volume_24h=10,
    )
    base.update(overrides)
    return MarketRecord(**base)


class NormalizeTest(unittest.TestCase):
    """Two venues word the same threshold two ways; one form must remain."""

    def test_thousands_are_canonical(self):
        self.assertEqual(matching.normalize("Add 100,000 jobs"), "add 100000 jobs")
        self.assertEqual(matching.normalize("Add 100k jobs"), "add 100000 jobs")

    def test_percentages_keep_their_value(self):
        self.assertIn("0.7 percent", matching.normalize("CPI up more than 0.7%"))

    def test_tokens_drop_function_words_only(self):
        found = matching.tokens(matching.normalize("Will there be a hike above 0.7%"))
        self.assertIn("above", found)  # direction is meaningful, kept
        self.assertNotIn("will", found)
        self.assertNotIn("there", found)

    def test_numbers_are_extracted_for_the_entity_check(self):
        self.assertEqual(matching.numbers("cpi 0.8 percent in 2026"), {"0.8", "2026"})


class TitleMatchingTest(unittest.TestCase):
    def test_the_same_question_on_two_venues_is_a_confident_match(self):
        records = [
            market("kalshi", "K1", "Will there be no change in Fed rates in October 2026?"),
            market("polymarket", "P1", "Will there be no change in Fed rates in October 2026?"),
        ]
        [found] = matching.match_markets(records, aliases=ALIASES)
        self.assertGreaterEqual(found.confidence, 85)
        self.assertFalse(found.possible)
        self.assertFalse(found.rules_differ)
        self.assertEqual(len(found.members), 2)
        self.assertEqual({m[0] for m in found.members}, {"kalshi", "polymarket"})

    def test_different_thresholds_are_possible_and_flagged_not_counted(self):
        records = [
            market("kalshi", "K1", "Will CPI rise more than 0.7% in September 2026?"),
            market("polymarket", "P1", "Will CPI rise more than 0.8% in September 2026?"),
        ]
        [found] = matching.match_markets(
            records, aliases=ALIASES, threshold=85.0, floor=50.0
        )
        self.assertTrue(found.possible)
        self.assertTrue(found.rules_differ)
        self.assertLess(found.confidence, 85)

    def test_a_close_date_far_away_breaks_the_match(self):
        records = [
            market("kalshi", "K1", "Will X happen?", close_time="2026-11-01T00:00:00Z"),
            market("polymarket", "P1", "Will X happen?", close_time="2027-03-01T00:00:00Z"),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_two_markets_on_the_same_venue_are_never_a_pair(self):
        records = [
            market("kalshi", "K1", "Will X happen?"),
            market("kalshi", "K2", "Will X happen?"),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_unrelated_titles_do_not_match(self):
        records = [
            market("kalshi", "K1", "Will the Lakers win the 2027 championship?"),
            market("manifold", "M1", "Will Bitcoin close above 200000 in 2027?"),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_the_event_key_is_stable_for_the_same_members(self):
        records = [
            market("kalshi", "K1", "Will X happen in November 2026?"),
            market("polymarket", "P1", "Will X happen in November 2026?"),
        ]
        first = matching.match_markets(records, aliases=ALIASES)[0].event_key
        again = matching.match_markets(list(reversed(records)), aliases=ALIASES)[0].event_key
        self.assertEqual(first, again)


class SportsMatchingTest(unittest.TestCase):
    def test_a_team_market_matches_the_moneyline_on_the_team(self):
        records = [
            market(
                "odds_api",
                "basketball_nba:evt1",
                "New York Knicks @ Boston Celtics",
                venue_type="sportsbook",
                close_time="2026-10-20T00:00:00Z",
                price=None,
            ),
            market(
                "kalshi",
                "K1",
                "Will the New York Knicks win on October 20, 2026?",
                close_time="2026-10-20T01:00:00Z",
            ),
        ]
        found = [
            match
            for match in matching.match_markets(records, aliases=ALIASES)
            if match.matched_on == "teams"
        ]
        self.assertEqual(len(found), 1)
        match = found[0]
        self.assertFalse(match.possible)
        outcome = {m[0]: m[2] for m in match.members}
        self.assertEqual(outcome["odds_api"], "New York Knicks")
        self.assertEqual(outcome["kalshi"], "")

    def test_a_game_more_than_a_day_away_is_not_matched(self):
        records = [
            market(
                "odds_api",
                "basketball_nba:evt1",
                "New York Knicks @ Boston Celtics",
                venue_type="sportsbook",
                close_time="2026-10-20T00:00:00Z",
            ),
            market(
                "kalshi",
                "K1",
                "Will the Knicks win on October 25, 2026?",
                close_time="2026-10-25T00:00:00Z",
            ),
        ]
        self.assertEqual(
            [m for m in matching.match_markets(records, aliases=ALIASES)
             if m.matched_on == "teams"],
            [],
        )

    def test_a_non_winner_market_is_not_matched_to_a_game(self):
        records = [
            market(
                "odds_api",
                "basketball_nba:evt1",
                "New York Knicks @ Boston Celtics",
                venue_type="sportsbook",
                close_time="2026-10-20T00:00:00Z",
            ),
            market(
                "kalshi",
                "K1",
                "Will the Knicks score 120 points on October 20, 2026?",
                close_time="2026-10-20T00:00:00Z",
            ),
        ]
        self.assertEqual(
            [m for m in matching.match_markets(records, aliases=ALIASES)
             if m.matched_on == "teams"],
            [],
        )


class GapTest(unittest.TestCase):
    def row(self, venue, venue_type, price, **overrides):
        base = {
            "venue": venue,
            "venue_type": venue_type,
            "market_id": f"{venue}-1",
            "price": price,
            "outcomes": [{"name": "Yes", "price": price}],
            "price_as_of": "2026-10-07T00:00:00+00:00",
            "as_of_kind": "last_price_change",
            "traded_24h": True,
            "volume_24h": 5,
        }
        base.update(overrides)
        return base

    def test_the_gap_is_max_minus_min_of_real_money_prices(self):
        points = gaps.event_prices(
            [{"venue": "kalshi", "market_id": "kalshi-1", "outcome": ""},
             {"venue": "polymarket", "market_id": "polymarket-1", "outcome": ""}],
            {
                ("kalshi", "kalshi-1"): self.row("kalshi", "prediction_market", 79.0),
                ("polymarket", "polymarket-1"): self.row(
                    "polymarket", "prediction_market", 84.0, market_id="polymarket-1"
                ),
            },
        )
        self.assertEqual(gaps.gap(points), 5.0)

    def test_a_venue_quoting_two_markets_is_not_a_gap_in_itself(self):
        """Two prices from one venue are two questions, not a disagreement.

        The rule has always asked for two venues; comparing a venue's own
        markets with each other turned a mis-matched group of nine Polymarket
        governor races into a 98 point "gap".
        """
        points = gaps.event_prices(
            [{"venue": "polymarket", "market_id": "a", "outcome": ""},
             {"venue": "polymarket", "market_id": "b", "outcome": ""}],
            {
                ("polymarket", "a"): self.row(
                    "polymarket", "prediction_market", 1.0, market_id="a"
                ),
                ("polymarket", "b"): self.row(
                    "polymarket", "prediction_market", 97.0, market_id="b"
                ),
            },
        )
        self.assertEqual(len(points), 2)
        self.assertIsNone(gaps.gap(points))

    def test_a_second_venue_makes_it_a_gap_again(self):
        points = gaps.event_prices(
            [{"venue": "kalshi", "market_id": "a", "outcome": ""},
             {"venue": "polymarket", "market_id": "b", "outcome": ""},
             {"venue": "polymarket", "market_id": "c", "outcome": ""}],
            {
                ("kalshi", "a"): self.row(
                    "kalshi", "prediction_market", 10.0, market_id="a"
                ),
                ("polymarket", "b"): self.row(
                    "polymarket", "prediction_market", 20.0, market_id="b"
                ),
                ("polymarket", "c"): self.row(
                    "polymarket", "prediction_market", 30.0, market_id="c"
                ),
            },
        )
        self.assertEqual(gaps.gap(points), 20.0)

    def test_play_money_never_enters_a_gap(self):
        points = gaps.event_prices(
            [{"venue": "kalshi", "market_id": "kalshi-1", "outcome": ""},
             {"venue": "manifold", "market_id": "manifold-1", "outcome": ""}],
            {
                ("kalshi", "kalshi-1"): self.row("kalshi", "prediction_market", 40.0),
                ("manifold", "manifold-1"): self.row(
                    "manifold", "play_money", 90.0, market_id="manifold-1"
                ),
            },
        )
        self.assertIsNone(gaps.gap(points))  # one real-money venue is not a gap
        self.assertEqual(sum(1 for p in points if p["real_money"]), 1)

    def test_a_stale_price_is_excluded_from_the_gap(self):
        points = gaps.event_prices(
            [{"venue": "kalshi", "market_id": "kalshi-1", "outcome": ""},
             {"venue": "polymarket", "market_id": "polymarket-1", "outcome": ""}],
            {
                ("kalshi", "kalshi-1"): self.row(
                    "kalshi", "prediction_market", 10.0,
                    price_as_of=None, as_of_kind="none",
                    traded_24h=False, volume_24h=0,
                ),
                ("polymarket", "polymarket-1"): self.row(
                    "polymarket", "prediction_market", 60.0, market_id="polymarket-1"
                ),
            },
        )
        self.assertIsNone(gaps.gap(points))

    def test_a_sportsbook_outcome_uses_its_own_de_vigged_price(self):
        row = self.row("odds_api", "sportsbook", None, market_id="odds_api-1")
        row["outcomes"] = [
            {"name": "New York Knicks", "price": 39.02, "price_raw": 2.5},
            {"name": "Boston Celtics", "price": 60.98, "price_raw": 1.6},
        ]
        self.assertEqual(gaps.member_price(row, "Boston Celtics"), 60.98)
        self.assertIsNone(gaps.member_price(row, "Nobody"))

    def test_a_market_missing_from_the_catalog_is_skipped(self):
        points = gaps.event_prices(
            [{"venue": "kalshi", "market_id": "gone", "outcome": ""}], {}
        )
        self.assertEqual(points, [])

    def test_the_summary_reports_what_the_gap_was_built_from(self):
        points = gaps.event_prices(
            [{"venue": "kalshi", "market_id": "kalshi-1", "outcome": ""},
             {"venue": "polymarket", "market_id": "polymarket-1", "outcome": ""}],
            {
                ("kalshi", "kalshi-1"): self.row("kalshi", "prediction_market", 20.0),
                ("polymarket", "polymarket-1"): self.row(
                    "polymarket", "prediction_market", 25.0, market_id="polymarket-1"
                ),
            },
        )
        summary = gaps.summarise(points)
        self.assertEqual(summary["gap"], 5.0)
        self.assertEqual(summary["real_money"], 2)
        self.assertEqual(summary["venues"], ["kalshi", "polymarket"])


if __name__ == "__main__":
    unittest.main()
