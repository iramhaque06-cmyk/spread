"""Offline tests for render.py: labels, gaps, links, notices, missing sides."""

import json
import tempfile
import unittest
from pathlib import Path

import mapping
import render
import store
from venues import kalshi, polymarket

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_quotes():
    kalshi_quote = kalshi.parse_market(
        load("kalshi_market_KXFEDDECISION-26OCT-H0.json"),
        series=load("kalshi_series_KXFEDDECISION.json")["series"],
    )
    polymarket_quote = polymarket.parse_market(load("polymarket_market_2589812.json"))
    return kalshi_quote, polymarket_quote


class RenderPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "spread.db")
        self.addCleanup(self.conn.close)
        store.upsert_event(self.conn, "fed-2026-10", "Fed decision, October 2026 meeting")

    def render(self) -> str:
        return render.render_page(self.conn)

    def test_prices_cents_source_labels_and_volumes(self):
        kalshi_quote, polymarket_quote = fixture_quotes()
        for quote in (kalshi_quote, polymarket_quote):
            store.upsert_observation(self.conn, "fed-2026-10", quote)
        page = self.render()

        # Both prices in cents, each next to its own source label.
        self.assertIn('<span class="source">Kalshi</span>', page)
        self.assertIn('<span class="source">Polymarket</span>', page)
        self.assertIn('<span class="price">80.0¢</span>', page)
        self.assertIn('<span class="price">81.0¢</span>', page)
        # Volumes labelled per venue.
        self.assertIn("Kalshi volume: 3,400,660", page)
        self.assertIn("Polymarket volume: 7,586,201", page)

    def test_gap_comes_from_the_shared_math(self):
        kalshi_quote, polymarket_quote = fixture_quotes()
        for quote in (kalshi_quote, polymarket_quote):
            store.upsert_observation(self.conn, "fed-2026-10", quote)
        page = self.render()

        expected = mapping.spread_between(
            kalshi_quote.price_cents, polymarket_quote.price_cents
        )
        self.assertEqual(expected, -1.0)
        self.assertIn(f"{expected:+.1f} points", page)
        self.assertIn("Kalshi − Polymarket", page)

    def test_links_and_last_updated_for_each_market(self):
        kalshi_quote, polymarket_quote = fixture_quotes()
        for quote in (kalshi_quote, polymarket_quote):
            store.upsert_observation(self.conn, "fed-2026-10", quote)
        page = self.render()

        self.assertIn(f'href="{kalshi_quote.url}"', page)
        self.assertIn(f'href="{polymarket_quote.url}"', page)
        self.assertIn("Kalshi updated: 2026-09-30T18:52:10.739024Z", page)
        self.assertIn("Polymarket updated: 2026-10-06T00:26:18.513158Z", page)

    def test_event_title_is_shown(self):
        store.upsert_observation(self.conn, "fed-2026-10", fixture_quotes()[0])
        self.assertIn("Fed decision, October 2026 meeting", self.render())

    def test_required_notices(self):
        page = self.render()
        for notice in (render.CAVEAT, render.INFORMATIONAL, render.ADULT_NOTICE):
            self.assertIn(notice, page)
        # The caveats SPEC asks for, in the wording a reader will see.
        lowered = page.lower()
        self.assertIn("fees", lowered)
        self.assertIn("liquidity", lowered)
        self.assertIn("resolution rules", lowered)
        self.assertIn("informational only", lowered)
        self.assertIn("financial advice", lowered)
        self.assertIn("18+", lowered)

    def test_missing_side_shows_as_missing_not_zero(self):
        store.upsert_observation(self.conn, "fed-2026-10", fixture_quotes()[0])
        page = self.render()

        self.assertIn("No data for this venue", page)
        self.assertIn("missing Polymarket", page)
        self.assertNotIn("points", page)  # no fabricated gap
        self.assertNotIn("0.0 points", page)

    def test_hostile_text_is_escaped(self):
        store.upsert_event(self.conn, "fed-2026-10", "<script>alert(1)</script> event")
        store.upsert_observation(self.conn, "fed-2026-10", fixture_quotes()[0])
        page = self.render()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_price_kind_is_explained(self):
        store.upsert_observation(self.conn, "fed-2026-10", fixture_quotes()[0])
        self.assertIn("last trade", self.render())

    def test_write_page_creates_the_file(self):
        store.upsert_observation(self.conn, "fed-2026-10", fixture_quotes()[0])
        path = Path(self.tmp.name) / "index.html"
        written = render.write_page(self.conn, path)
        self.assertEqual(written, path)
        self.assertTrue(path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>"))


if __name__ == "__main__":
    unittest.main()
