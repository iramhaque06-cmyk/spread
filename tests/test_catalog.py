"""Offline tests for the multi-venue catalog layer (no network anywhere)."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import envfile
import store
from venues import MarketRecord, kalshi, manifold, odds_api, polymarket

# Every timestamp a test writes is this instant, never the wall clock.
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


class EnvFileTest(unittest.TestCase):
    """Secrets live in .env; nothing here may ever echo a value."""

    def test_parses_quotes_comments_and_export(self):
        values = envfile.parse(
            '# a comment\nODDS_API_KEY="abc123"\nexport OTHER=plain\n\nBROKEN\n'
        )
        self.assertEqual(values, {"ODDS_API_KEY": "abc123", "OTHER": "plain"})

    def test_load_does_not_override_the_real_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("ODDS_API_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"ODDS_API_KEY": "from-env"}):
                envfile.load(path)
                self.assertEqual(envfile.get("ODDS_API_KEY"), "from-env")

    def test_missing_key_raises_without_naming_a_value(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(KeyError) as caught:
                envfile.get("ODDS_API_KEY", path=Path("does-not-exist.env"))
        self.assertIn("ODDS_API_KEY", str(caught.exception))

    def test_redact_hides_url_and_environment_secrets(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": "supersecretkey"}):
            text = envfile.redact(
                "GET https://api.the-odds-api.com/v4/sports?apiKey=supersecretkey"
            )
        self.assertNotIn("supersecretkey", text)
        self.assertIn("apiKey=***", text)


class KalshiCatalogTest(unittest.TestCase):
    """Pagination, tolerant pricing, and the series join that names a market."""

    SERIES = {"KXFEDDECISION": {"ticker": "KXFEDDECISION", "title": "Fed meeting",
                                "category": "Economics"}}
    EVENTS = {"KXFEDDECISION-26OCT": {"event_ticker": "KXFEDDECISION-26OCT",
                                      "series_ticker": "KXFEDDECISION",
                                      "category": "Economics"}}

    def market(self, **overrides):
        base = {
            "ticker": "KXFEDDECISION-26OCT-H0",
            "event_ticker": "KXFEDDECISION-26OCT",
            "title": "Will the Fed hike by 0bps?",
            "status": "active",
            "close_time": "2026-10-28T18:00:00Z",
            "last_price_dollars": "0.8000",
            "volume_fp": "3400660.00",
            "volume_24h_fp": "110304.00",
            "updated_time": "2026-09-30T18:52:10Z",
            "rules_primary": "Federal Reserve decides.",
            "rules_secondary": "Mutually exclusive.",
        }
        base.update(overrides)
        return base

    def test_catalog_row_carries_series_category_and_canonical_url(self):
        record = kalshi.parse_open_market(
            self.market(), series=self.SERIES, events=self.EVENTS
        )
        self.assertEqual(record.venue, "kalshi")
        self.assertEqual(record.venue_type, "prediction_market")
        self.assertEqual(record.category, "Economics")
        self.assertEqual(record.price, 80.0)
        self.assertEqual(record.price_kind, "last")
        self.assertEqual(record.volume, 3_400_660)
        self.assertEqual(record.volume_24h, 110_304)
        self.assertTrue(record.traded_24h)
        self.assertEqual(
            record.url,
            "https://kalshi.com/markets/kxfeddecision/fed-meeting/kxfeddecision-26oct",
        )
        self.assertEqual(record.event_ref, "KXFEDDECISION-26OCT")
        self.assertIn("Federal Reserve", record.rule_text)

    def test_a_market_without_a_quote_is_kept_with_no_price(self):
        record = kalshi.parse_open_market(
            self.market(last_price_dollars=None, yes_bid_dollars=None,
                        yes_ask_dollars=None),
            series=self.SERIES,
            events=self.EVENTS,
        )
        self.assertIsNone(record.price)
        self.assertEqual(record.price_kind, "")
        self.assertEqual(record.market_id, "KXFEDDECISION-26OCT-H0")

    def test_a_row_without_a_ticker_is_refused(self):
        self.assertIsNone(kalshi.parse_open_market({"title": "anonymous"}))

    def test_no_trade_time_is_ever_invented(self):
        record = kalshi.parse_open_market(
            self.market(), series=self.SERIES, events=self.EVENTS
        )
        self.assertIsNone(record.price_as_of)
        self.assertEqual(record.as_of_kind, "none")

    def test_the_listing_excludes_parlays_and_follows_the_cursor(self):
        pages = [
            {"markets": [self.market(ticker="A-1")], "cursor": "next"},
            {"markets": [self.market(ticker="B-1")], "cursor": ""},
        ]
        calls = []

        def fake(url, params=None, *, max_age=None, **kwargs):
            calls.append((url, params))
            if url.endswith("/series"):
                return {"series": list(self.SERIES.values())}
            if url.endswith("/events"):
                return {"events": list(self.EVENTS.values()), "cursor": ""}
            return pages.pop(0)

        with mock.patch.object(kalshi, "get_json", side_effect=fake):
            records = list(kalshi.list_open_markets())

        self.assertEqual([r.market_id for r in records], ["A-1", "B-1"])
        market_calls = [p for u, p in calls if u.endswith("/markets")]
        self.assertEqual(market_calls[0]["mve_filter"], "exclude")
        self.assertEqual(market_calls[0]["status"], "open")
        self.assertNotIn("cursor", market_calls[0])
        self.assertEqual(market_calls[1]["cursor"], "next")

    def test_prices_are_fetched_in_batches_of_one_hundred(self):
        requested = []

        def fake(url, params=None, *, max_age=None, **kwargs):
            requested.append(params["tickers"])
            return {"markets": []}

        with mock.patch.object(kalshi, "get_json", side_effect=fake):
            kalshi.get_prices([f"T{i}" for i in range(250)], max_age=0)

        self.assertEqual([len(chunk.split(",")) for chunk in requested], [100, 100, 50])


class PolymarketCatalogTest(unittest.TestCase):
    MARKET = {
        "slug": "will-x-happen",
        "question": "Will X happen?",
        "endDate": "2026-11-03T00:00:00Z",
        "lastTradePrice": 0.42,
        "volumeNum": 1000,
        "volume24hr": 0,
        "active": True,
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.42", "0.58"]',
        "clobTokenIds": '["111", "222"]',
        "events": [{"slug": "parent-event", "category": "Politics"}],
    }

    def test_catalog_row_reads_outcomes_and_the_parent_event(self):
        record = polymarket.parse_open_market(self.MARKET)
        self.assertEqual(record.price, 42.0)
        self.assertEqual(record.price_kind, "last")
        self.assertEqual(record.category, "Politics")
        self.assertEqual(
            [item["name"] for item in record.outcomes], ["Yes", "No"]
        )
        self.assertEqual(record.outcomes[1]["price"], 58.0)
        self.assertEqual(
            record.url, "https://polymarket.com/event/parent-event/will-x-happen"
        )

    def test_a_quiet_market_is_marked_not_traded_today(self):
        record = polymarket.parse_open_market(self.MARKET)
        self.assertEqual(record.volume_24h, 0)
        self.assertFalse(record.traded_24h)

    def test_the_catalog_walks_the_keyset_cursor(self):
        pages = [
            {"markets": [self.MARKET], "next_cursor": "abc"},
            {"markets": [dict(self.MARKET, slug="second")], "next_cursor": ""},
        ]
        params_seen = []

        def fake(url, params=None, *, max_age=None, **kwargs):
            params_seen.append(params)
            return pages.pop(0)

        with mock.patch.object(polymarket, "get_json", side_effect=fake):
            records = list(polymarket.list_open_markets())

        self.assertEqual([r.market_id for r in records], ["will-x-happen", "second"])
        self.assertEqual(params_seen[0]["closed"], "false")
        self.assertNotIn("after_cursor", params_seen[0])
        # The request parameter is after_cursor; next_cursor is the *response*
        # field. Sending next_cursor back is silently ignored by Gamma and
        # repeats page one, which is a trap this test now pins shut.
        self.assertEqual(params_seen[1]["after_cursor"], "abc")
        self.assertNotIn("next_cursor", params_seen[1])

    def test_a_cursor_that_does_not_move_cannot_loop_forever(self):
        def fake(url, params=None, *, max_age=None, **kwargs):
            return {"markets": [self.MARKET], "next_cursor": "stuck"}

        with mock.patch.object(polymarket, "get_json", side_effect=fake) as get:
            records = list(polymarket.list_open_markets())
        # Page one, then the identical page once more, then it stops. The
        # repeated row is harmless - the store is keyed by (venue, market_id) -
        # and the endless loop it would otherwise become is not.
        self.assertEqual(get.call_count, 2)
        self.assertEqual({r.market_id for r in records}, {"will-x-happen"})

    def test_get_prices_asks_for_one_market_at_a_time(self):
        seen = []

        def fake(url, params=None, *, max_age=None, **kwargs):
            seen.append(params)
            return [self.MARKET]

        with mock.patch.object(polymarket, "get_json", side_effect=fake):
            prices = polymarket.get_prices(["will-x-happen"])
        # Stored ids are slugs, and Gamma answers 422 for a slug sent as id.
        self.assertEqual(seen, [{"slug": "will-x-happen"}])
        self.assertEqual(prices["will-x-happen"].price, 42.0)


class ManifoldCatalogTest(unittest.TestCase):
    MARKET = {
        "id": "abc123",
        "question": "Will it rain tomorrow?",
        "probability": 0.31,
        "volume": 500.0,
        "volume24Hours": 12.0,
        "closeTime": 1796111940000,
        "lastBetTime": 1760000000000,
        "lastUpdatedTime": 1760000000000,
        "url": "https://manifold.markets/someone/will-it-rain",
        "isResolved": False,
        "groupSlugs": ["weather"],
    }

    def test_a_play_money_market_is_labelled_as_one(self):
        record = manifold.parse_market(self.MARKET)
        self.assertEqual(record.venue, "manifold")
        self.assertEqual(record.venue_type, "play_money")
        self.assertEqual(record.price, 31.0)
        self.assertEqual(record.price_kind, "prob")
        self.assertEqual(record.category, "weather")

    def test_the_last_bet_time_dates_the_price(self):
        record = manifold.parse_market(self.MARKET)
        self.assertEqual(record.as_of_kind, "last_trade")
        self.assertTrue(record.price_as_of.startswith("2025-10-09"))

    def test_a_market_with_no_bet_has_no_price_time(self):
        record = manifold.parse_market(dict(self.MARKET, lastBetTime=None))
        self.assertIsNone(record.price_as_of)
        self.assertEqual(record.as_of_kind, "none")

    def test_millisecond_timestamps_become_iso(self):
        self.assertEqual(manifold._iso_ms(0), "")
        self.assertTrue(manifold.parse_market(self.MARKET).close_time.endswith("+00:00"))

    def test_an_unrepresentable_timestamp_is_ignored_not_raised(self):
        # Real Manifold markets carry sentinel close times; Windows raises
        # OSError on those, which must not sink a 1,000-row page.
        for value in (10**18, -1, "not a number", None):
            self.assertEqual(manifold._iso_ms(value), "")
        record = manifold.parse_market(dict(self.MARKET, closeTime=10**18))
        self.assertEqual(record.close_time, "")
        self.assertEqual(record.market_id, "abc123")

    def test_batch_prices_use_repeated_ids(self):
        seen = []

        def fake(url, params=None, *, max_age=None, **kwargs):
            seen.append((url, params))
            if url.endswith("market-probs"):
                return {"abc123": {"prob": 0.62}, "def456": {"prob": 0.31}}
            return self.MARKET

        with mock.patch.object(manifold, "get_json", side_effect=fake):
            prices = manifold.get_prices(["abc123", "def456"], detailed=False)
        self.assertTrue(seen[0][0].endswith("market-probs"))
        # requests repeats a list parameter: ids=a&ids=b, which JSON-encodes as
        # an array - the only shape Manifold's validator accepts.
        self.assertEqual(seen[0][1], {"ids": ["abc123", "def456"]})
        self.assertEqual(prices["abc123"].price, 62.0)

    def test_a_lone_id_skips_the_batch_endpoint(self):
        """One id arrives as a bare string, which market-probs rejects with 400.

        Measured live on 2026-10-07: ``?ids=abc`` answers "Expected array,
        received string". A single id therefore reads the market directly.
        """
        seen = []

        def fake(url, params=None, *, max_age=None, **kwargs):
            seen.append((url, params))
            return self.MARKET

        with mock.patch.object(manifold, "get_json", side_effect=fake):
            prices = manifold.get_prices(["abc123"], detailed=False)
        self.assertEqual(len(seen), 1)
        self.assertIn("/v0/market/abc123", seen[0][0])
        self.assertNotIn("market-probs", seen[0][0])
        expected = manifold.parse_market(self.MARKET).price
        self.assertEqual(prices["abc123"].price, expected)

    def test_detailed_prices_read_one_market_each(self):
        with mock.patch.object(
            manifold, "get_json", return_value=self.MARKET
        ) as get:
            prices = manifold.get_prices(["abc123"], detailed=True)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(prices["abc123"].as_of_kind, "last_trade")


class OddsApiTest(unittest.TestCase):
    EVENT = {
        "id": "evt1",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": "2026-10-20T00:00:00Z",
        "home_team": "Boston Celtics",
        "away_team": "New York Knicks",
    }
    BOOK = {
        "key": "fanduel",
        "markets": [
            {
                "key": "h2h",
                "last_update": "2026-10-07T04:00:00Z",
                "outcomes": [
                    {"name": "New York Knicks", "price": 2.50},
                    {"name": "Boston Celtics", "price": 1.60},
                ],
            }
        ],
    }

    def test_devig_normalizes_the_book_to_one_hundred_points(self):
        outcomes = odds_api.devig([("A", 2.50), ("B", 1.60)])
        total = sum(item["price"] for item in outcomes)
        self.assertAlmostEqual(total, 100.0, places=1)
        self.assertEqual(outcomes[0]["price_raw"], 2.50)
        # 1/2.50 = 0.40, 1/1.60 = 0.625, sum 1.025 -> ~39.02 / ~60.98
        self.assertAlmostEqual(outcomes[0]["price"], 39.02, places=2)
        self.assertAlmostEqual(outcomes[1]["price"], 60.98, places=2)

    def test_devig_removes_the_vig_rather_than_keeping_it(self):
        outcomes = odds_api.devig([("A", 1.91), ("B", 1.91)])
        self.assertAlmostEqual(outcomes[0]["price"], 50.0, places=1)

    def test_an_unpriceable_outcome_is_dropped_not_guessed(self):
        self.assertEqual(odds_api.devig([("A", 1.0), ("B", None)]), ())

    def test_the_catalog_row_costs_nothing_and_carries_no_price(self):
        record = odds_api.parse_event(
            dict(self.EVENT, bookmakers=[self.BOOK]), sport="basketball_nba"
        )
        self.assertEqual(record.venue_type, "sportsbook")
        self.assertEqual(record.market_id, "basketball_nba:evt1")
        self.assertEqual(record.title, "New York Knicks @ Boston Celtics")
        self.assertIsNone(record.price)
        self.assertEqual(record.outcomes, ())

    def test_a_priced_row_keeps_raw_and_devigged_values(self):
        record = odds_api.parse_event(
            dict(self.EVENT, bookmakers=[self.BOOK]),
            sport="basketball_nba",
            priced=True,
        )
        self.assertEqual(record.price_kind, "implied_devig")
        names = [item["name"] for item in record.outcomes]
        self.assertEqual(names, ["New York Knicks", "Boston Celtics"])
        self.assertEqual(record.outcomes[0]["price_raw"], 2.50)
        self.assertEqual(record.price_as_of, "2026-10-07T04:00:00Z")
        self.assertEqual(record.as_of_kind, "last_price_change")

    def test_the_quota_guard_refuses_to_spend_the_last_fifth(self):
        with mock.patch.dict(odds_api.LAST_QUOTA, {"remaining": 400}):
            self.assertTrue(odds_api.has_budget())
        with mock.patch.dict(odds_api.LAST_QUOTA, {"remaining": 100}):
            self.assertFalse(odds_api.has_budget())
        with mock.patch.dict(odds_api.LAST_QUOTA, {"remaining": 101}):
            self.assertTrue(odds_api.has_budget())

    def test_quota_headers_are_recorded(self):
        odds_api._record_quota(
            {
                "x-requests-remaining": "499",
                "x-requests-used": "1",
                "x-requests-last": "1",
            }
        )
        self.assertEqual(odds_api.LAST_QUOTA["remaining"], 499)
        self.assertEqual(odds_api.LAST_QUOTA["used"], 1)
        self.assertTrue(odds_api.LAST_QUOTA["at"])

    def test_the_key_never_reaches_an_error_message(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": "topsecretvalue"}):
            with self.assertRaises(KeyError):
                envfile.get("METACULUS_TOKEN", path=Path("nope.env"))
            self.assertNotIn(
                "topsecretvalue",
                envfile.redact("https://api.the-odds-api.com/?apiKey=topsecretvalue"),
            )


class StoreCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(Path(self.tmp.name) / "spread.db")
        self.addCleanup(self.conn.close)

    def record(self, market_id="M1", **overrides):
        base = dict(
            venue="kalshi",
            venue_type="prediction_market",
            market_id=market_id,
            title="Will the thing happen?",
            category="Economics",
            close_time="2026-11-01T00:00:00Z",
            outcomes=({"name": "Yes", "price": 40.0},),
            price=40.0,
            price_kind="last",
            volume=100,
            volume_24h=5,
            updated_at="2026-10-07T00:00:00Z",
            url="https://kalshi.com/x",
            rule_text="rules",
            status="active",
            fetched_at="2026-10-07T00:00:00Z",
        )
        base.update(overrides)
        return MarketRecord(**base)

    def test_a_catalog_row_round_trips(self):
        store.upsert_markets(self.conn, [self.record()])
        [row] = store.read_markets(self.conn)
        self.assertEqual(row["price"], 40.0)
        self.assertEqual(row["outcomes"][0]["name"], "Yes")
        self.assertTrue(row["traded_24h"])
        self.assertEqual(row["first_seen_at"], row["last_seen_at"])

    def test_a_price_only_row_cannot_blank_the_catalog_fields(self):
        store.upsert_markets(self.conn, [self.record()])
        store.upsert_markets(
            self.conn,
            [
                MarketRecord(
                    venue="kalshi",
                    venue_type="prediction_market",
                    market_id="M1",
                    price=55.0,
                    price_kind="last",
                    volume_24h=9,
                    fetched_at="2026-10-07T00:30:00Z",
                )
            ],
        )
        [row] = store.read_markets(self.conn)
        self.assertEqual(row["price"], 55.0)
        self.assertEqual(row["title"], "Will the thing happen?")
        self.assertEqual(row["category"], "Economics")
        self.assertEqual(row["url"], "https://kalshi.com/x")
        self.assertEqual(row["rule_text"], "rules")

    def test_top_by_volume_orders_the_refresh_set(self):
        store.upsert_markets(
            self.conn,
            [
                self.record("small", volume=10),
                self.record("big", volume=9_000),
                self.record("none", volume=None),
            ],
        )
        order = [row["market_id"] for row in store.top_markets_by_volume(self.conn, 2)]
        self.assertEqual(order, ["big", "small"])

    def test_a_market_missing_from_a_full_pass_is_closed_not_deleted(self):
        ids = ["gone", "stays", "a", "b", "c"]
        store.upsert_markets(self.conn, [self.record(market_id) for market_id in ids])
        # A full pass that listed 4 of the 5 open markets: one really went away.
        closed = store.mark_missing_closed(self.conn, "kalshi", ids[1:])
        self.assertEqual(closed, 1)
        rows = {
            row["market_id"]: row
            for row in store.read_markets(self.conn, open_only=False)
        }
        self.assertFalse(rows["gone"]["is_open"])
        for market_id in ids[1:]:
            self.assertTrue(rows[market_id]["is_open"])
        # The open-only read the site uses no longer lists it...
        self.assertEqual(
            [row["market_id"] for row in store.read_markets(self.conn)],
            ["a", "b", "c", "stays"],
        )
        # ...but nothing was deleted.
        self.assertEqual(len(store.read_markets(self.conn, open_only=False)), 5)

    def test_a_bounded_listing_never_closes_anything(self):
        """A --limit run sees a slice by construction, so absence proves nothing."""
        store.upsert_markets(self.conn, [self.record("gone"), self.record("stays")])
        self.assertEqual(
            store.mark_missing_closed(self.conn, "kalshi", ["stays"], limit=500), 0
        )
        rows = {
            row["market_id"]: row
            for row in store.read_markets(self.conn, open_only=False)
        }
        self.assertTrue(rows["gone"]["is_open"])

    def test_a_listing_that_failed_never_closes_anything(self):
        store.upsert_markets(self.conn, [self.record("gone"), self.record("stays")])
        self.assertEqual(
            store.mark_missing_closed(self.conn, "kalshi", ["stays"], error="timed out"),
            0,
        )
        rows = {
            row["market_id"]: row
            for row in store.read_markets(self.conn, open_only=False)
        }
        self.assertTrue(rows["gone"]["is_open"])

    def test_a_listing_under_the_floor_never_closes_anything(self):
        """A truncated or half-parsed listing looks like a mass disappearance.

        Four of these eight markets are "missing" from the listing below. An
        ungated closing pass would resolve half the venue on the strength of a
        response that only proves the venue answered.
        """
        ids = ["a", "b", "c", "d", "gone-1", "gone-2", "gone-3", "gone-4"]
        store.upsert_markets(self.conn, [self.record(market_id) for market_id in ids])
        self.assertEqual(store.mark_missing_closed(self.conn, "kalshi", ids[:4]), 0)
        self.assertEqual(
            {row["market_id"] for row in store.read_markets(self.conn)}, set(ids)
        )

    def test_closing_a_market_never_rewrites_its_last_seen_at(self):
        """Closing is not an observation, so it must not stamp one.

        ``last_seen_at`` records when the venue actually listed the market.
        Writing the closing time there would claim a sighting that never
        happened and erase when it was last really seen.
        """
        ids = ["gone", "stays", "a", "b", "c"]
        store.upsert_markets(
            self.conn,
            [self.record(market_id) for market_id in ids],
            now="2026-10-07T00:00:00+00:00",
        )
        before = {
            row["market_id"]: row["last_seen_at"]
            for row in store.read_markets(self.conn, open_only=False)
        }
        self.assertEqual(store.mark_missing_closed(self.conn, "kalshi", ids[1:]), 1)
        after = {
            row["market_id"]: row
            for row in store.read_markets(self.conn, open_only=False)
        }
        self.assertFalse(after["gone"]["is_open"])
        self.assertEqual(after["gone"]["last_seen_at"], before["gone"])
        for market_id in ids[1:]:
            self.assertEqual(after[market_id]["last_seen_at"], before[market_id])

    def test_the_close_guard_says_why_it_refused(self):
        store.upsert_markets(self.conn, [self.record("a"), self.record("b")])
        self.assertIn(
            "bounded", store.close_refusal_reason(self.conn, "kalshi", 0, limit=10)
        )
        self.assertIn(
            "error", store.close_refusal_reason(self.conn, "kalshi", 0, error="boom")
        )
        self.assertIn("under 80%", store.close_refusal_reason(self.conn, "kalshi", 1))
        self.assertEqual(store.close_refusal_reason(self.conn, "kalshi", 2), "")
        # A venue with nothing stored has nothing to lose either way.
        self.assertEqual(store.close_refusal_reason(self.conn, "manifold", 0), "")

    def test_sync_catalog_closes_nothing_on_a_bounded_or_failed_listing(self):
        """The catalog loop must not close what a partial listing left out."""
        import catalog

        store.upsert_markets(self.conn, [self.record("gone"), self.record("stays")])

        def bounded(limit=None):
            return [self.record("stays")]

        with mock.patch.dict(catalog.CATALOG_VENUES, {"kalshi": bounded}):
            report = catalog.sync_catalog(
                self.conn, venues_wanted=["kalshi"], limit=1, now=NOW
            )
        self.assertEqual(report["kalshi"]["closed"], 0)
        self.assertIn("bounded", report["kalshi"]["refused"])
        self.assertEqual(
            {row["market_id"] for row in store.read_markets(self.conn)},
            {"gone", "stays"},
        )

        def broken(limit=None):
            raise RuntimeError("kalshi said no")

        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.dict(catalog.CATALOG_VENUES, {"kalshi": broken}):
                report = catalog.sync_catalog(
                    self.conn, venues_wanted=["kalshi"], now=NOW
                )
        self.assertEqual(report["kalshi"]["closed"], 0)
        self.assertIn("kalshi said no", report["kalshi"]["errors"])
        self.assertEqual(
            {row["market_id"] for row in store.read_markets(self.conn)},
            {"gone", "stays"},
        )

    def test_counts_report_per_venue(self):
        store.upsert_markets(
            self.conn,
            [
                self.record("a"),
                self.record("b", price=None),
                self.record("na", venue="manifold", venue_type="play_money"),
            ],
        )
        counts = store.market_counts(self.conn)
        self.assertEqual(counts["kalshi"]["total"], 2)
        self.assertEqual(counts["kalshi"]["priced"], 1)
        self.assertEqual(counts["manifold"]["open"], 1)

    def test_matching_is_replaced_wholesale(self):
        events = [
            {
                "event_key": "ev-1",
                "title": "An event",
                "category": "Economics",
                "close_time": "2026-11-01T00:00:00Z",
                "confidence": 92.0,
                "possible": 0,
                "rules_differ": 0,
                "venue_count": 2,
                "gap": 3.5,
                "updated_at": "2026-10-07T00:00:00Z",
            }
        ]
        members = [
            {"event_key": "ev-1", "venue": "kalshi", "market_id": "M1", "outcome": "",
             "confidence": 92.0, "rules_differ": 0, "matched_on": "title"},
            {"event_key": "ev-1", "venue": "polymarket", "market_id": "P1", "outcome": "",
             "confidence": 92.0, "rules_differ": 0, "matched_on": "title"},
        ]
        store.replace_matching(self.conn, events, members)
        self.assertEqual(len(store.read_matched_events(self.conn)), 1)
        self.assertEqual(len(store.read_members(self.conn, "ev-1")), 2)

        store.replace_matching(self.conn, [], [])
        self.assertEqual(store.read_matched_events(self.conn), [])

    def test_possible_matches_can_be_excluded_from_a_read(self):
        rows = [
            {"event_key": f"ev-{i}", "title": "t", "category": "", "close_time": "",
             "confidence": score, "possible": possible, "rules_differ": 0,
             "venue_count": 2, "gap": None, "updated_at": ""}
            for i, (score, possible) in enumerate([(90.0, 0), (72.0, 1)])
        ]
        store.replace_matching(self.conn, rows, [])
        self.assertEqual(len(store.read_matched_events(self.conn)), 2)
        self.assertEqual(
            len(store.read_matched_events(self.conn, include_possible=False)), 1
        )

    def test_the_odds_quota_is_remembered_and_resets_monthly(self):
        store.record_odds_call(
            self.conn, remaining=499, used=1, cost=1, at="2026-10-07T00:00:00+00:00"
        )
        quota = store.read_odds_quota(self.conn)
        self.assertEqual(quota["remaining"], 499)
        self.assertEqual(quota["calls"], 1)
        store.record_odds_call(
            self.conn, remaining=498, used=2, cost=1, at="2026-10-07T01:00:00+00:00"
        )
        self.assertEqual(store.read_odds_quota(self.conn)["calls"], 2)
        store.record_odds_call(
            self.conn, remaining=500, used=0, cost=1, at="2026-11-01T00:00:00+00:00"
        )
        self.assertEqual(store.read_odds_quota(self.conn)["calls"], 1)

    def test_catalog_state_records_a_run_without_losing_the_last_good_one(self):
        store.set_catalog_state(
            self.conn, "kalshi", last_catalog_ok_at="2026-10-07T00:00:00Z", markets=10
        )
        store.set_catalog_state(self.conn, "kalshi", errors="timed out")
        [state] = store.read_catalog_state(self.conn)
        self.assertEqual(state["last_catalog_ok_at"], "2026-10-07T00:00:00Z")
        self.assertEqual(state["markets"], 10)
        self.assertEqual(state["errors"], "timed out")

    def test_an_unknown_state_field_is_refused(self):
        with self.assertRaises(ValueError):
            store.set_catalog_state(self.conn, "kalshi", nonsense=1)


if __name__ == "__main__":
    unittest.main()
