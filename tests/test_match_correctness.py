"""The hard checks: what must never be stored as the same event.

Every case here was a real failure on the real catalog - an Iowa governor race
merged with a California one, "before 2027" merged with "after 2027", and
different thresholds stored as near-matches. Each one is written as the
smallest catalog that shows it, and the clock is never involved.

The rules under test:

* the numbers each market bets on must be the same set, with years and dates
  excluded (:func:`matching.strikes`);
* the names in the two titles must overlap (:func:`matching.entities`);
* the directions must agree, with one exception: above/below the same strike on
  the same day is one event read from its two sides, and the up side is stored
  inverted (:func:`matching.directions`);
* close dates must agree within ``DATE_WINDOW_DAYS`` (3), or 1 for sports;
* one event holds at most one market per venue.
"""

import unittest
from datetime import datetime, timezone

from rapidfuzz import fuzz

import gaps
import matching

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

ALIASES = {
    "new york knicks": ("knicks",),
    "boston celtics": ("celtics",),
    "los angeles lakers": ("lakers",),
}


def market(venue, market_id, title, close_time="2026-11-03T00:00:00Z", **overrides):
    """One market row, in the shape both the adapters and the store use."""
    base = {
        "venue": venue,
        "venue_type": "play_money" if venue == "manifold" else "prediction_market",
        "market_id": market_id,
        "title": title,
        "close_time": close_time,
        "price": 40.0,
        "volume_24h": 10,
    }
    base.update(overrides)
    return base


def titles_of(records):
    return {matching._title(item) for item in records}


class DifferentThingTest(unittest.TestCase):
    """Two markets that share a subject are not two markets about one event."""

    def test_an_iowa_governor_race_is_not_a_california_governor_race(self):
        records = [
            market("manifold", "M-IA", "Will Zach Lahn win Iowa governor 2026 election?"),
            market(
                "polymarket",
                "P-CA1",
                "Will Kamala Harris win the California Governor Election in 2026?",
            ),
            market(
                "polymarket",
                "P-CA2",
                "Will Katie Porter win the California Governor Election in 2026?",
            ),
            market(
                "polymarket",
                "P-CA3",
                "Will Chad Bianco win the California Governor Election in 2026?",
            ),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_the_same_two_venues_written_as_one_market_are_still_refused(self):
        """The reported case, phrased the way it arrived: one Polymarket market."""
        records = [
            market("manifold", "M-IA", "Will Zach Lahn win Iowa governor 2026 election?"),
            market(
                "polymarket",
                "P-CA",
                "Will Kamala Harris/Katie Porter/Chad Bianco win the California "
                "Governor Election in 2026?",
            ),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_before_and_after_the_same_year_are_not_one_event(self):
        records = [
            market("kalshi", "K-BEFORE", "Will the Fed cut rates before 2027?"),
            market("polymarket", "P-AFTER", "Will the Fed cut rates after 2027?"),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_different_thresholds_are_not_the_same_bet(self):
        records = [
            market("kalshi", "K-1600", "Will the S and P 500 close at 1600 or higher in 2026?"),
            market(
                "polymarket",
                "P-2000",
                "Will the S and P 500 close at 2000 or higher in 2026?",
            ),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_sharing_only_a_year_earns_no_score_bonus(self):
        """The year is in every title, so agreeing on it proves nothing.

        Two markets that share nothing but the year must score exactly what the
        fuzzy comparison says - no bonus for a number that distinguishes
        nothing.
        """
        left = "will the fed cut rates in 2026"
        right = "will the federal reserve cut rates during 2026"
        records = [
            market("kalshi", "K-1", "Will the Fed cut rates in 2026?"),
            market("polymarket", "P-1", "Will the Federal Reserve cut rates during 2026?"),
        ]
        [event] = matching.match_markets(records, aliases=ALIASES)
        self.assertEqual(event.confidence, round(float(fuzz.token_set_ratio(left, right)), 2))


class InverseTest(unittest.TestCase):
    """Above and below the same strike on the same day: one event, two sides."""

    def test_above_and_below_the_same_strike_match_with_the_up_side_inverted(self):
        records = [
            market(
                "kalshi",
                "K-ABOVE",
                "Will bitcoin close above 100000 dollars on December 31, 2026?",
            ),
            market(
                "polymarket",
                "P-BELOW",
                "Will bitcoin close below 100000 dollars on December 31, 2026?",
            ),
        ]
        [event] = matching.match_markets(records, aliases=ALIASES)
        self.assertEqual(
            {member[0]: member[6] for member in event.members},
            {"kalshi": 1, "polymarket": 0},
        )
        self.assertTrue(event.rules_differ)  # opposite sides, one question

    def test_a_clean_inverse_elsewhere_is_still_refused(self):
        """Same strikes, one up one down, but a different day is not the same bet."""
        records = [
            market(
                "kalshi",
                "K-ABOVE",
                "Will bitcoin close above 100000 dollars on December 31, 2026?",
                close_time="2026-12-31T00:00:00Z",
            ),
            market(
                "polymarket",
                "P-BELOW",
                "Will bitcoin close below 100000 dollars on January 15, 2027?",
                close_time="2027-01-15T00:00:00Z",
            ),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_the_gap_reads_the_inverted_side_as_one_hundred_minus_price(self):
        """An inverted member's price is the other side of its own question."""
        members = [
            {"venue": "kalshi", "market_id": "K-ABOVE", "outcome": "", "inverted": 1},
            {"venue": "polymarket", "market_id": "P-BELOW", "outcome": ""},
        ]
        row = {
            "venue": "kalshi",
            "venue_type": "prediction_market",
            "price": 62.0,
            "outcomes": [],
            "price_as_of": "2026-10-07T11:00:00+00:00",
            "as_of_kind": "last_price_change",
            "traded_24h": True,
            "volume_24h": 5,
        }
        other = dict(row, venue="polymarket", price=45.0)
        points = gaps.event_prices(
            members,
            {
                ("kalshi", "K-ABOVE"): row,
                ("polymarket", "P-BELOW"): other,
            },
            now=NOW,
        )
        self.assertEqual([point["price"] for point in points], [38.0, 45.0])
        self.assertEqual(gaps.gap(points), 7.0)


class WindowTest(unittest.TestCase):
    """Close dates are part of the question, and the window is three days."""

    def test_the_windows_are_three_days_and_one_for_sports(self):
        self.assertEqual(matching.DATE_WINDOW_DAYS, 3)
        self.assertEqual(matching.SPORTS_DATE_WINDOW_DAYS, 1)

    def test_a_four_day_gap_is_two_different_questions(self):
        records = [
            market("kalshi", "K-1", "Will the Fed cut rates in November 2026?",
                   close_time="2026-11-03T00:00:00Z"),
            market("polymarket", "P-1", "Will the Fed cut rates in November 2026?",
                   close_time="2026-11-07T00:00:00Z"),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])

    def test_a_two_day_gap_is_the_same_question(self):
        records = [
            market("kalshi", "K-1", "Will the Fed cut rates in November 2026?",
                   close_time="2026-11-03T00:00:00Z"),
            market("polymarket", "P-1", "Will the Fed cut rates in November 2026?",
                   close_time="2026-11-05T00:00:00Z"),
        ]
        self.assertEqual(len(matching.match_markets(records, aliases=ALIASES)), 1)


class OneVenuePerEventTest(unittest.TestCase):
    """Two markets of one venue in an event would compare a venue with itself."""

    def test_only_the_best_connected_market_of_a_venue_stays(self):
        records = [
            market("kalshi", "K-A", "Will bitcoin close above 100000 dollars in 2026?"),
            market("kalshi", "K-B", "Will bitcoin close above 100000 dollars in 2026?"),
            market("polymarket", "P-1", "Will bitcoin close above 100000 dollars in 2026?"),
        ]
        matches = matching.match_markets(records, aliases=ALIASES)
        [event] = matches
        venues = [member[0] for member in event.members]
        self.assertEqual(sorted(venues), ["kalshi", "polymarket"])
        self.assertEqual(len(set(venues)), len(venues))
        self.assertEqual(
            [member[1] for member in event.members], ["K-A", "P-1"]
        )  # the first in canonical order wins the tie

    def test_an_event_never_names_a_venue_twice(self):
        records = [
            market("kalshi", "K-A", "Will the Fed cut rates in December 2026?"),
            market("kalshi", "K-B", "Will the Fed cut rates in December 2026?"),
            market("kalshi", "K-C", "Will the Fed cut rates in December 2026?"),
            market("polymarket", "P-1", "Will the Fed cut rates in December 2026?"),
            market("manifold", "M-1", "Will the Fed cut rates in December 2026?"),
        ]
        for event in matching.match_markets(records, aliases=ALIASES):
            venues = [member[0] for member in event.members]
            self.assertEqual(len(set(venues)), len(venues), venues)


class SportsEvidenceTest(unittest.TestCase):
    """A sports match scores the evidence it has, not a constant."""

    def game(self, title, close_time):
        return market(
            "odds_api",
            "basketball_nba:evt1",
            title,
            close_time=close_time,
            venue_type="sportsbook",
            price=None,
        )

    def test_a_named_team_on_a_known_day_is_confident(self):
        records = [
            self.game("New York Knicks @ Boston Celtics", "2026-10-20T00:00:00Z"),
            market(
                "kalshi",
                "K-1",
                "Will the New York Knicks win on October 20, 2026?",
                close_time="2026-10-20T01:00:00Z",
            ),
        ]
        [event] = matching.match_markets(records, aliases=ALIASES)
        self.assertFalse(event.possible)
        self.assertEqual(
            event.confidence,
            matching.SPORTS_TEAM_POINTS
            + matching.SPORTS_DAY_POINTS
            + matching.SPORTS_MARKET_POINTS,
        )

    def test_a_market_with_no_day_is_possible_not_confident(self):
        records = [
            self.game("New York Knicks @ Boston Celtics", ""),
            market(
                "kalshi",
                "K-1",
                "Will the New York Knicks win on October 20, 2026?",
                close_time="2026-10-20T01:00:00Z",
            ),
        ]
        [event] = matching.match_markets(records, aliases=ALIASES)
        self.assertTrue(event.possible)
        self.assertEqual(
            event.confidence,
            matching.SPORTS_TEAM_POINTS + matching.SPORTS_MARKET_POINTS,
        )

    def test_a_spread_is_not_the_moneyline(self):
        records = [
            self.game("New York Knicks @ Boston Celtics", "2026-10-20T00:00:00Z"),
            market(
                "kalshi",
                "K-1",
                "Will the Knicks win by 5 or more points on October 20, 2026?",
                close_time="2026-10-20T00:00:00Z",
            ),
        ]
        self.assertEqual(matching.match_markets(records, aliases=ALIASES), [])


class ExtractorTest(unittest.TestCase):
    """The three extractors, stated plainly."""

    def test_strikes_drop_years_dates_and_times_and_keep_units(self):
        self.assertEqual(matching.strikes("Will bitcoin close above 100000 in 2026"),
                         frozenset({"100000"}))
        self.assertEqual(matching.strikes("cpi up more than 0.7 percent in 2026"),
                         frozenset({"0.7 percent"}))
        self.assertEqual(matching.strikes("the market on October 7, 2026 at 1pm EDT"),
                         frozenset())
        self.assertEqual(matching.strikes("100000 dollars"), frozenset({"100000 dollars"}))

    def test_strikes_from_two_spellings_of_one_number_agree(self):
        self.assertFalse(matching.strikes_disagree("above $95,750", "above 95750 dollars"))
        self.assertFalse(matching.strikes_disagree("above 100000", "above 100000 dollars"))
        self.assertTrue(matching.strikes_disagree("0.7 percent", "0.7 points"))
        self.assertTrue(matching.strikes_disagree("above 1600", "above 2000"))

    def test_entities_are_names_not_subjects(self):
        self.assertEqual(
            matching.entities("Will Zach Lahn win Iowa governor 2026 election?", ALIASES),
            frozenset({"name:zach", "name:lahn", "name:iowa"}),
        )
        self.assertEqual(
            matching.entities("Will the Fed cut rates?", ALIASES),
            frozenset({"name:federal-reserve"}),
        )
        self.assertEqual(
            matching.entities("Will the Federal Reserve cut rates?", ALIASES),
            frozenset({"name:federal-reserve"}),
        )
        self.assertEqual(
            matching.entities("Will BTC close above 100000?", ALIASES),
            frozenset({"name:bitcoin"}),
        )

    def test_directions_name_each_family_once(self):
        self.assertEqual(matching.directions("above 140"), {"threshold": "up"})
        self.assertEqual(
            matching.directions("Will the Fed cut rates after 2027?"),
            {"time": "down", "change": "down"},
        )
        self.assertEqual(matching.directions("rise or fall"), {"change": "mixed"})
        self.assertEqual(matching.directions("Will the Lakers win?"), {"outcome": "up"})

    def test_directions_disagree_only_when_both_sides_commit(self):
        self.assertEqual(matching.directions_disagree("above 140", "above 150"), (False, False))
        self.assertEqual(matching.directions_disagree("above 140", "below 140"), (True, True))
        self.assertEqual(
            matching.directions_disagree("cut rates before 2027", "cut rates after 2027"),
            (True, False),
        )


if __name__ == "__main__":
    unittest.main()
