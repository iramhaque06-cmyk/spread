"""Offline tests for the rendered page: stale prices, excluded gaps, ordering.

Everything here runs against a temporary database built from synthetic
quotes, so a stale price can be produced on demand - the live venue data
happens to be fresh, and a rule that only ever shows green proves nothing.
"""

import html
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import mapping
import render
import store
from venues import Quote

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

KALSHI_RULE = "Yes if the test number is above 1."
POLYMARKET_RULE = "Yes if the test number is at least 1."


def quote(venue: str, price_cents: float, **fields) -> Quote:
    base = dict(
        venue=venue,
        market_id=f"{venue}-market",
        title=f"{venue} market",
        price_cents=price_cents,
        price_kind="last",
        volume=1_000,
        url=f"https://example.test/{venue}",
        last_updated="2026-09-30T18:52:10Z",
        status="active",
        price_as_of="2026-10-06T11:00:00+00:00",
        price_as_of_kind="last_price_change",
        traded_24h=True,
        volume_24h=109_329,
        fetched_at="2026-10-06T11:59:00+00:00",
        rules_text=f"{venue} rule text, verbatim from the API",
    )
    base.update(fields)
    return Quote(**base)


class PageTestCase(unittest.TestCase):
    """A database with fresh, stale, missing and rule-flagged events."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(Path(tmp.name) / "spread.db")
        self.addCleanup(self.conn.close)

        self.add_event(
            "big-gap",
            kalshi=quote("kalshi", 70.0),
            polymarket=quote("polymarket", 50.0),
            rules_differ=True,
        )
        self.add_event(
            "small-gap",
            kalshi=quote("kalshi", 82.0),
            polymarket=quote("polymarket", 80.0),
            rules_differ=False,
        )
        self.add_event(
            "missing-poly",
            kalshi=quote("kalshi", 60.0),
            polymarket=None,
        )
        self.add_event(
            "stale-kalshi",
            kalshi=quote(
                "kalshi",
                25.0,
                price_as_of=None,
                price_as_of_kind="none",
                traded_24h=False,
                volume_24h=0,
            ),
            polymarket=quote("polymarket", 31.0),
        )

    def add_event(self, event_key, *, kalshi=None, polymarket=None, rules_differ=False):
        store.upsert_event(
            self.conn,
            event_key,
            event_key.replace("-", " ").title(),
            kalshi_rule=KALSHI_RULE,
            polymarket_rule=POLYMARKET_RULE,
            rules_differ=rules_differ,
            rules_note="why they differ" if rules_differ else "",
        )
        for item in (kalshi, polymarket):
            if item is not None:
                store.upsert_observation(self.conn, event_key, item)

    def page(self) -> str:
        return render.render_page(self.conn, now=NOW)

    def row(self, page: str, event_key: str) -> str:
        for line in page.splitlines():
            if f'data-key="{event_key}"' in line:
                return line
        raise AssertionError(f"no row for {event_key}")

    def order(self, page: str) -> list[str]:
        return [
            line.split('data-key="')[1].split('"')[0]
            for line in page.splitlines()
            if "data-key=" in line
        ]


class StaleRenderingTest(PageTestCase):
    def test_a_stale_price_is_greyed_out_and_tagged(self):
        page = self.page()
        self.assertIn('class="venue stale"', page)
        self.assertIn('<span class="stale-tag">stale</span>', page)
        # The evidence travels with the verdict, so a reader can check it.
        self.assertIn("no contract traded in the last 24 hours", page)
        # Only the stale Kalshi cell is greyed; the fresh side is untouched.
        self.assertEqual(page.count('class="venue stale"'), 1)

    def test_a_stale_side_is_excluded_from_the_gap(self):
        stale_row = self.row(self.page(), "stale-kalshi")
        self.assertIn("excluded — stale price", stale_row)
        self.assertIn("left out of the gap sort", stale_row)
        self.assertIn("Kalshi: no trade in the last 24 hours", stale_row)
        self.assertNotIn("points", stale_row)
        # The fresh pair next door still gets its number.
        self.assertIn("+20.0 points", self.row(self.page(), "big-gap"))

    def test_the_stale_row_is_marked_for_the_sort_script(self):
        self.assertIn('class="row-stale"', self.row(self.page(), "stale-kalshi"))
        self.assertIn('data-gap=""', self.row(self.page(), "stale-kalshi"))

    def test_fresh_prices_report_their_age_and_source(self):
        fresh_row = self.row(self.page(), "big-gap")
        self.assertIn("price last moved 1.0 h ago", fresh_row)
        self.assertIn("traded 24h: 109,329", fresh_row)
        self.assertNotIn("stale-tag", fresh_row)


class OrderingTest(PageTestCase):
    def test_fresh_pairs_rank_by_gap_size_biggest_first(self):
        self.assertEqual(
            self.order(self.page()),
            ["big-gap", "small-gap", "missing-poly", "stale-kalshi"],
        )

    def test_ranked_rows_carry_their_gap_for_the_sort_script(self):
        page = self.page()
        self.assertIn('data-gap="20.00"', self.row(page, "big-gap"))
        self.assertIn('data-gap="2.00"', self.row(page, "small-gap"))

    def test_the_sort_button_starts_hidden_without_scripting(self):
        page = self.page()
        # Server order is already correct, so the button only appears with JS.
        self.assertIn("button.sort[hidden]", page)
        self.assertIn("hidden>sort ▼</button>", page)

    def test_pinned_rows_cannot_be_sorted_into_the_ranking(self):
        page = self.page()
        # The script ranks only rows with a numeric data-gap and re-appends
        # the rest afterwards, so a stale row can never rise into the ranking.
        self.assertIn("ranked.concat(pinned).forEach", page)
        self.assertIn("gap !== \"\" && isFinite(Number(gap))", page)


class RulesColumnTest(PageTestCase):
    def test_each_venue_gets_a_one_line_rule_summary(self):
        row = self.row(self.page(), "big-gap")
        self.assertIn(KALSHI_RULE, row)
        self.assertIn(POLYMARKET_RULE, row)

    def test_differing_rules_are_flagged_with_the_reason(self):
        row = self.row(self.page(), "big-gap")
        self.assertIn("resolution rules differ", row)
        self.assertIn("why they differ", row)

    def test_matching_rules_say_so(self):
        row = self.row(self.page(), "small-gap")
        self.assertIn("same resolution rule", row)
        self.assertNotIn("resolution rules differ", row)

    def test_the_live_rule_text_is_shown_verbatim_behind_a_disclosure(self):
        row = self.row(self.page(), "big-gap")
        self.assertIn("<details>", row)
        self.assertIn("kalshi rule text, verbatim from the API", row)
        self.assertIn("polymarket rule text, verbatim from the API", row)

    def test_hostile_rule_text_is_escaped(self):
        store.upsert_observation(
            self.conn,
            "big-gap",
            quote("kalshi", 70.0, rules_text="<script>alert('rules')</script>"),
        )
        page = self.page()
        self.assertNotIn("<script>alert('rules')</script>", page)
        self.assertIn("&lt;script&gt;alert(&#x27;rules&#x27;)&lt;/script&gt;", page)

    def test_an_event_without_curated_rules_says_so(self):
        store.upsert_event(self.conn, "no-rules", "No Rules")
        store.upsert_observation(self.conn, "no-rules", quote("kalshi", 55.0))
        self.assertIn(
            "No curated rule summary for this event", self.row(self.page(), "no-rules")
        )


class TimestampLabellingTest(PageTestCase):
    def test_each_venue_stamp_is_named_for_what_it_is(self):
        page = self.page()
        self.assertIn(
            "Kalshi updated: 2026-09-30T18:52:10Z "
            "(when Kalshi last changed this market record, not a trade time)",
            page,
        )
        self.assertIn(
            "Polymarket updated: 2026-09-30T18:52:10Z "
            "(Gamma batch stamp, shared by many markets, not a trade time)",
            page,
        )

    def test_the_price_timestamp_is_shown_separately_from_the_record_stamp(self):
        row = self.row(self.page(), "big-gap")
        self.assertIn("price as of: 2026-10-06T11:00:00+00:00", row)
        self.assertIn("from the venue", row)

    def test_a_venue_with_no_trade_time_says_so(self):
        row = self.row(self.page(), "stale-kalshi")
        self.assertIn("Kalshi price as of: —", row)
        self.assertIn("no trade time published by this venue", row)

    def test_the_fetch_time_is_shown(self):
        page = self.page()
        self.assertIn("Kalshi fetched: 2026-10-06T11:59:00+00:00", page)
        self.assertIn("Live fetch: 2026-10-06T11:59:00+00:00 (1 min ago).", page)


class ProvenanceTest(PageTestCase):
    def test_the_page_states_that_it_is_live_and_not_fixture_data(self):
        page = self.page()
        self.assertIn(html.escape(render.PROVENANCE), page)
        self.assertIn("live API calls only", page)
        self.assertIn("No sample or fixture data appears on this page", page)
        self.assertIn("used only by the offline unit tests", page)

    def test_the_page_explains_the_24_hour_rule(self):
        page = self.page()
        self.assertIn(html.escape(render.FRESHNESS_NOTE), page)
        self.assertIn("a price older than 24 hours is greyed out", page)
        self.assertIn("Kalshi publishes none at all", page)

    def test_an_empty_database_still_renders(self):
        empty = store.connect(Path(tempfile.mkdtemp()) / "empty.db")
        self.addCleanup(empty.close)
        page = render.render_page(empty, now=NOW)
        self.assertIn(render.CAVEAT, page)
        self.assertIn("no observations yet", page)


class ColumnHeaderTest(PageTestCase):
    def test_the_rules_column_is_labelled(self):
        self.assertIn("Resolution rules", self.page())

    def test_the_gap_sort_is_the_documented_one_way_to_order(self):
        page = self.page()
        self.assertIn('id="sort-gap"', page)
        self.assertIn('aria-sort="descending"', page)

    def test_the_gap_is_still_rendered_by_the_shared_math(self):
        row = self.row(self.page(), "big-gap")
        expected = mapping.spread_between(70.0, 50.0)
        self.assertEqual(expected, 20.0)
        self.assertIn(f"{expected:+.1f} points", row)
        self.assertIn("Kalshi − Polymarket", row)


if __name__ == "__main__":
    unittest.main()
