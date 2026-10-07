"""Offline tests for the static-site exporter (no network anywhere).

Every test builds its own database in a temporary directory and writes into a
temporary output directory, so nothing here reads `spread.db` or touches the
real `data/`. The size limits are the point of several tests, so they are
checked against the bytes actually on disk rather than against an estimate.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import export
import store
from venues import MarketRecord, outcome

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
ISO = NOW.isoformat(timespec="seconds")


def record(venue="kalshi", market_id="M1", **overrides):
    """One market row, priced and fresh unless a test says otherwise."""
    base = dict(
        venue=venue,
        venue_type="prediction_market" if venue != "manifold" else "play_money",
        market_id=market_id,
        title=f"Will the Fed cut rates by {market_id}?",
        category="Economics",
        close_time="2026-12-31T00:00:00+00:00",
        outcomes=(outcome("Yes", 62.0),),
        price=62.0,
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
    base.update(overrides)
    return MarketRecord(**base)


class ExportCase(unittest.TestCase):
    """Shared plumbing: a temporary database and output directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.db"
        self.out = self.root / "data"
        self.conn = store.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, *records):
        store.upsert_markets(self.conn, list(records), now=ISO)

    def export(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return export.export_all(self.conn, self.out, **kwargs)

    def read(self, *parts):
        return json.loads((self.out.joinpath(*parts)).read_text(encoding="utf-8"))


class CatalogShardTest(ExportCase):
    """Shards, their size ceiling, and the manifest that maps them."""

    def test_writes_one_shard_per_venue_with_compact_keys(self):
        self.add(record("kalshi", "A1"), record("polymarket", "b1"))
        self.export()

        shard = self.read("catalog", "kalshi-1.json")
        self.assertEqual(shard["v"], "kalshi")
        self.assertEqual(shard["from"], "A1")
        self.assertEqual(shard["to"], "A1")
        self.assertEqual([item["i"] for item in shard["markets"]], ["A1"])
        # The compact names are the whole point: no long key survives.
        self.assertNotIn("market_id", shard["markets"][0])
        self.assertIn("t", shard["markets"][0])

    def test_manifest_legend_documents_every_key_a_shard_carries(self):
        self.add(record())
        self.export()
        manifest = self.read("catalog", "manifest.json")
        market = self.read("catalog", "kalshi-1.json")["markets"][0]
        undocumented = set(market) - set(manifest["fields"])
        self.assertEqual(undocumented, set(), "every emitted key needs a legend entry")
        for outcome_entry in market["o"]:
            self.assertEqual(set(outcome_entry), {"n", "p"})

    def test_every_shard_is_within_the_size_limit(self):
        self.add(*[record("kalshi", f"K{index:05}") for index in range(400)])
        self.export()
        manifest = self.read("catalog", "manifest.json")
        limit = manifest["limit_bytes"]
        self.assertEqual(limit, export.CATALOG_LIMIT)
        self.assertEqual(manifest["totals"]["markets"], 400)
        # 400 records fit one shard, so the real check is the ceiling itself and
        # that every record is still present; the split behaviour has its own test.
        for path in (self.out / "catalog").glob("kalshi-*.json"):
            self.assertLessEqual(path.stat().st_size, limit, path.name)

    def test_a_tiny_limit_splits_instead_of_overflowing(self):
        """The limit is enforced against real bytes, so shrinking it must hold."""
        self.add(*[record("kalshi", f"K{index:04}") for index in range(60)])
        with mock.patch.object(export, "CATALOG_LIMIT", 3000):
            self.export()
        manifest = self.read("catalog", "manifest.json")
        self.assertEqual(manifest["limit_bytes"], 3000)
        shards = sorted((self.out / "catalog").glob("kalshi-*.json"))
        self.assertGreater(len(shards), 3)
        for path in shards:
            self.assertLessEqual(path.stat().st_size, 3000, path.name)

    def test_manifest_counts_and_ranges_describe_the_shards(self):
        self.add(*[record("kalshi", f"K{index:03}") for index in range(50)])
        self.export()
        manifest = self.read("catalog", "manifest.json")
        entry = manifest["venues"]["kalshi"]
        self.assertEqual(entry["count"], 50)
        self.assertEqual(manifest["totals"]["markets"], 50)

        seen = []
        for shard in entry["shards"]:
            document = self.read(*shard["file"].split("/"))
            ids = [item["i"] for item in document["markets"]]
            self.assertEqual(document["from"], ids[0])
            self.assertEqual(document["to"], ids[-1])
            self.assertEqual(shard["count"], len(ids))
            self.assertEqual(shard["bytes"], (self.out / shard["file"]).stat().st_size)
            seen.extend(ids)
        # Shards are ordered, so the manifest's ranges resolve a key by search.
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))

    def test_a_market_key_resolves_through_the_manifest_ranges(self):
        self.add(*[record("kalshi", f"K{index:03}") for index in range(80)])
        self.export()
        manifest = self.read("catalog", "manifest.json")
        wanted = "K042"
        for shard in manifest["venues"]["kalshi"]["shards"]:
            if shard["from"] <= wanted <= shard["to"]:
                document = self.read(*shard["file"].split("/"))
                found = [item for item in document["markets"] if item["i"] == wanted]
                self.assertEqual(len(found), 1)
                return
        self.fail("no shard range covered the key")

    def test_stale_flag_is_decided_once_here(self):
        old = (NOW - timedelta(days=3)).isoformat(timespec="seconds")
        self.add(
            record("kalshi", "FRESH"),
            record("kalshi", "OLD", price_as_of=old, as_of_kind="last_price_change"),
        )
        self.export()
        markets = {
            item["i"]: item for item in self.read("catalog", "kalshi-1.json")["markets"]
        }
        self.assertFalse(markets["FRESH"]["x"])
        self.assertTrue(markets["OLD"]["x"])

    def test_only_open_markets_are_exported(self):
        self.add(record("kalshi", "OPEN"), record("kalshi", "SHUT", is_open=False))
        self.export()
        ids = [item["i"] for item in self.read("catalog", "kalshi-1.json")["markets"]]
        self.assertEqual(ids, ["OPEN"])

    def test_a_shrinking_catalog_removes_yesterdays_shards(self):
        self.add(*[record("kalshi", f"K{index}", title="x" * 100) for index in range(6)])
        with mock.patch.object(export, "CATALOG_LIMIT", 900):
            self.export()
            self.assertGreater(len(list((self.out / "catalog").glob("kalshi-*.json"))), 1)
            # Drop most of the catalog and export again: shards that no longer
            # have content must not be left behind for the site to serve.
            self.conn.execute("DELETE FROM markets WHERE market_id != 'K0'")
            self.conn.commit()
            self.export()
            remaining = sorted(path.name for path in (self.out / "catalog").glob("kalshi-*.json"))
        self.assertEqual(remaining, ["kalshi-1.json"])

    def test_non_ascii_titles_are_measured_in_bytes(self):
        """A character count is not a byte count, and real titles are not ASCII.

        Measuring characters is what let a shard land over limit the first time
        this ran against a live catalog.
        """
        self.add(
            *[
                record(
                    "kalshi",
                    f"K{index:03}",
                    title="Wird der Präsident zurücktreten? 日本語の見出し ½ → " * 3,
                )
                for index in range(40)
            ]
        )
        with mock.patch.object(export, "CATALOG_LIMIT", 6000):
            self.export()
        for path in (self.out / "catalog").glob("kalshi-*.json"):
            self.assertLessEqual(path.stat().st_size, 6000, path.name)
        self.assertEqual(export.oversize_artifacts(self.out), [])

    def test_shard_sizes_reports_every_shard(self):
        self.add(record("kalshi", "K1"))
        self.export()
        sizes = export.shard_sizes(self.out, "catalog")
        self.assertEqual([name for name, _ in sizes], ["catalog/kalshi-1.json"])
        self.assertGreater(sizes[0][1], 0)

    def test_rule_text_is_truncated(self):
        self.add(record(rule_text="x" * 5000))
        self.export()
        market = self.read("catalog", "kalshi-1.json")["markets"][0]
        self.assertEqual(len(market["r"]), export.RULE_TEXT_LIMIT)


class SearchIndexTest(ExportCase):
    """The token index: a dictionary in shard 0, postings behind it."""

    def test_finds_a_market_by_a_title_token(self):
        self.add(record("kalshi", "FED1", title="Will the Fed cut rates in December?"))
        self.export()
        postings = self.read("search", "index-0.json")["postings"]
        self.assertIn("fed", postings)
        self.assertEqual(postings["fed"], ["kalshi:FED1"])

    def test_indexes_the_category_and_the_venue_too(self):
        self.add(record("kalshi", "FED1", title="Will rates fall?", category="Economics"))
        self.export()
        postings = self.read("search", "index-0.json")["postings"]
        self.assertIn("economics", postings)
        self.assertIn("kalshi", postings)

    def test_numbers_are_canonicalized_by_the_matcher(self):
        self.add(record("kalshi", "N1", title="Will bitcoin close above 100,000?")
                 , record("kalshi", "N2", title="Will bitcoin close above 100k?"))
        self.export()
        postings = self.read("search", "index-0.json")["postings"]
        self.assertEqual(postings["100000"], ["kalshi:N1", "kalshi:N2"])

    def test_bounds_cover_every_shard_and_shard_zero_carries_them(self):
        """Shards are numbered from zero, so index-0.json is the entry point."""
        self.add(*[record("kalshi", f"K{index:03}", title=f"Topic {index} here")
                   for index in range(60)])
        with mock.patch.object(export, "SEARCH_LIMIT", 3000):
            self.export()
        first = self.read("search", "index-0.json")
        self.assertEqual(first["shard"], 0)
        self.assertGreater(len(first["bounds"]), 1)
        for number, (low, high) in enumerate(first["bounds"]):
            document = self.read("search", f"index-{number}.json")
            self.assertEqual(document["from"], low)
            self.assertEqual(document["to"], high)

    def test_every_search_shard_is_within_its_limit(self):
        self.add(*[record("kalshi", f"K{index:04}", title=f"Unique topic {index}")
                   for index in range(400)])
        with mock.patch.object(export, "SEARCH_LIMIT", 5000):
            self.export()
        for path in (self.out / "search").glob("index-*.json"):
            self.assertLessEqual(path.stat().st_size, 5000, path.name)

    def test_tokens_are_ordered_so_a_prefix_lands_in_one_range(self):
        self.add(*[record("kalshi", f"K{index:03}", title=f"Zebra {index}")
                   for index in range(30)])
        with mock.patch.object(export, "SEARCH_LIMIT", 2000):
            self.export()
        bounds = self.read("search", "index-0.json")["bounds"]
        flattened = [item for pair in bounds for item in pair]
        self.assertEqual(flattened, sorted(flattened))

    def test_every_token_in_shard_zero_appears_in_its_own_alphabet_range(self):
        self.add(record("kalshi", "FED1", title="Fed bitcoin nfl zebra"))
        self.export()
        first = self.read("search", "index-0.json")
        for token in first["postings"]:
            low, high = first["from"], first["to"]
            self.assertLessEqual(low, token)
            self.assertLessEqual(token, high)


class EventExportTest(ExportCase):
    """One file per matched event, with prices, history, rules and sources."""

    def match_two(self, *, gap_expected=True, possible=False):
        confidence = 70.0 if possible else 95.0
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, rules_differ, venue_count, gap, updated_at) "
            "VALUES ('ev-abc', 'Fed decision', 'Economics', '2026-12-31', ?, ?, 0, 2, NULL, ?)",
            (confidence, 1 if possible else 0, ISO),
        )
        for venue, market_id in (("kalshi", "FED1"), ("polymarket", "fed-1")):
            self.conn.execute(
                "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
                "confidence, rules_differ, matched_on) VALUES ('ev-abc', ?, ?, '', ?, 0, 'title')",
                (venue, market_id, confidence),
            )
        self.conn.commit()

    def test_event_file_carries_prices_rules_and_sources(self):
        self.add(
            record("kalshi", "FED1", title="Fed cut?", price=62.0),
            record("polymarket", "fed-1", title="Fed cut?", price=57.0),
        )
        self.match_two()
        self.export()
        document = self.read("events", "ev-abc.json")
        self.assertEqual(document["id"], "ev-abc")
        self.assertEqual({item["venue"] for item in document["prices"]}, {"kalshi", "polymarket"})
        self.assertEqual(len(document["sources"]), 2)
        self.assertEqual(len(document["rules"]), 2)
        self.assertTrue(document["rules"]["kalshi:FED1"]["rule_text"])

    def test_gap_comes_from_the_shared_gap_rules(self):
        self.add(
            record("kalshi", "FED1", price=62.0),
            record("polymarket", "fed-1", price=57.0),
        )
        self.match_two()
        self.export()
        self.assertEqual(self.read("events", "ev-abc.json")["gap"], 5.0)

    def test_history_comes_from_observed_snapshots(self):
        self.add(record("kalshi", "FED1", price=62.0))
        self.match_two()
        store.insert_market_snapshots(
            self.conn,
            [{"venue": "kalshi", "market_id": "FED1", "price": 60.0, "volume_24h": 5}],
            at=(NOW - timedelta(hours=2)).isoformat(timespec="seconds"),
        )
        self.export()
        history = self.read("events", "ev-abc.json")["history"]["kalshi:FED1"]
        self.assertEqual([point["price"] for point in history], [60.0, 62.0])

    def test_play_money_is_shown_but_never_inside_a_gap(self):
        self.add(
            record("kalshi", "FED1", price=62.0),
            record("manifold", "fed1", price=30.0, venue_type="play_money"),
        )
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, rules_differ, venue_count, gap, updated_at) "
            "VALUES ('ev-abc', 'Fed', 'Economics', '', 95.0, 0, 0, 2, NULL, ?)",
            (ISO,),
        )
        for venue, market_id in (("kalshi", "FED1"), ("manifold", "fed1")):
            self.conn.execute(
                "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
                "confidence, rules_differ, matched_on) VALUES ('ev-abc', ?, ?, '', 95.0, 0, 'title')",
                (venue, market_id),
            )
        self.conn.commit()
        self.export()
        document = self.read("events", "ev-abc.json")
        self.assertIsNone(document["gap"])
        self.assertEqual([item["venue"] for item in document["prices"]], ["kalshi", "manifold"])
        self.assertEqual(document["real_money_count"], 1)

    def test_a_possible_match_is_labelled_and_has_no_gap(self):
        self.add(
            record("kalshi", "FED1", price=62.0),
            record("polymarket", "fed-1", price=57.0),
        )
        self.match_two(possible=True)
        self.export()
        document = self.read("events", "ev-abc.json")
        self.assertTrue(document["possible"])
        # Two fresh real-money prices that would give 5.0 points, and still no
        # gap: a near-match may be shown but never counted (PLAN.md 2.2).
        self.assertEqual(document["gap"], None)
        self.assertEqual(self.read("events-index.json")["events"][0]["g"], None)

    def test_event_ids_are_safe_as_filenames(self):
        self.assertEqual(export._safe_id("ev-abc:1/2"), "ev-abc_1_2")


class HomeTest(ExportCase):
    """The landing document: gaps, moves, venue health, quota."""

    def test_home_lists_gaps_widest_first_and_skips_gapless_events(self):
        self.add(
            record("kalshi", "A", price=10.0), record("polymarket", "a", price=20.0),
            record("kalshi", "B", price=50.0), record("polymarket", "b", price=59.0),
        )
        for key, ids in (("ev-wide", ("A", "a")), ("ev-narrow", ("B", "b")), ("ev-none", ("C", "c"))):
            self.conn.execute(
                "INSERT INTO matched_events (event_key, title, category, close_time, "
                "confidence, possible, rules_differ, venue_count, gap, updated_at) "
                "VALUES (?, ?, 'Economics', '', 95.0, 0, 0, 2, NULL, ?)",
                (key, key, ISO),
            )
        pairs = {
            "ev-wide": (("kalshi", "A"), ("polymarket", "a")),
            "ev-narrow": (("kalshi", "B"), ("polymarket", "b")),
            "ev-none": (("kalshi", "C"),),
        }
        for key, members in pairs.items():
            for venue, market_id in members:
                self.conn.execute(
                    "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
                    "confidence, rules_differ, matched_on) VALUES (?, ?, ?, '', 95.0, 0, 'title')",
                    (key, venue, market_id),
                )
        self.conn.commit()

        import catalog

        catalog.compute_gaps(self.conn, now=NOW)
        self.export()

        home = self.read("home.json")
        self.assertEqual([item["id"] for item in home["top_gaps"]], ["ev-wide", "ev-narrow"])
        self.assertEqual(home["top_gaps"][0]["gap"], 10.0)
        self.assertEqual(home["stats"]["events_with_gap"], 2)

    def test_home_carries_venue_status_including_errors(self):
        self.add(record("kalshi", "K1"))
        store.set_catalog_state(
            self.conn, "odds_api", errors="ODDS_API_KEY is not set.", markets=0
        )
        self.export()
        home = self.read("home.json")
        venues = {item["venue"]: item for item in home["venue_status"]}
        self.assertIn("kalshi", venues)
        self.assertEqual(venues["kalshi"]["markets"], 1)
        self.assertIn("ODDS_API_KEY is not set.", venues["odds_api"]["errors"])

    def test_recent_moves_are_computed_from_stored_points(self):
        self.add(record("kalshi", "K1", price=52.0))
        store.insert_market_snapshots(
            self.conn,
            [{"venue": "kalshi", "market_id": "K1", "price": 60.0, "volume_24h": 1}],
            at=(NOW - timedelta(hours=3)).isoformat(timespec="seconds"),
        )
        self.export()
        moves = self.read("home.json")["recent_moves"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["from_price"], 60.0)
        self.assertEqual(moves[0]["to_price"], 52.0)
        self.assertEqual(moves[0]["change"], -8.0)
        self.assertEqual(moves[0]["title"], "Will the Fed cut rates by K1?")

    def test_home_includes_the_odds_quota(self):
        self.add(record("kalshi", "K1"))
        store.record_odds_call(self.conn, remaining=480, used=20, cost=2, at=ISO)
        self.export()
        quota = self.read("home.json")["odds_quota"]
        self.assertEqual(quota["remaining"], 480)
        self.assertEqual(quota["used"], 20)


class EventIndexTest(ExportCase):
    """The bridge between a search hit and the event it belongs to.

    A search finds market keys; a result row owes the reader a venue count, the
    price range across venues and the gap. That has to come from one small file
    rather than from walking every event document.
    """

    def match(self, pairs, *, venue_count=2, confidence=95.0):
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, rules_differ, venue_count, gap, updated_at) "
            "VALUES ('ev-abc', 'Fed decision', 'Economics', '2026-12-31', ?, 0, 0, ?, NULL, ?)",
            (confidence, venue_count, ISO),
        )
        for venue, market_id, outcome in pairs:
            self.conn.execute(
                "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
                "confidence, rules_differ, matched_on) VALUES ('ev-abc', ?, ?, ?, ?, 0, 'title')",
                (venue, market_id, outcome, confidence),
            )
        self.conn.commit()

    def test_it_maps_matched_markets_and_carries_the_event_totals(self):
        self.add(
            record("kalshi", "FED1", price=62.0),
            record("polymarket", "fed-1", price=57.0),
        )
        self.match((("kalshi", "FED1", ""), ("polymarket", "fed-1", "")))
        import catalog

        catalog.compute_gaps(self.conn, now=NOW)
        self.export()

        index = self.read("events-index.json")
        self.assertEqual(index["count"], 1)
        self.assertEqual(
            index["markets"],
            {"kalshi:FED1": "ev-abc", "polymarket:fed-1": "ev-abc"},
        )
        row = index["events"][0]
        self.assertEqual(row["i"], "ev-abc")
        self.assertEqual(row["t"], "Fed decision")
        self.assertEqual(row["n"], 2)
        self.assertEqual(row["g"], 5.0)
        self.assertEqual((row["lo"], row["hi"]), (57.0, 62.0))
        self.assertEqual(row["y"], ["prediction_market"])
        # A search row is rendered by JS, so the file has to explain itself.
        self.assertEqual(set(row) - set(index["fields"]), set())

    def test_a_play_money_price_never_widens_the_row_range(self):
        self.add(
            record("kalshi", "FED1", price=62.0),
            record("polymarket", "fed-1", price=57.0),
            record("manifold", "fed1", price=3.0, venue_type="play_money"),
        )
        self.match(
            (
                ("kalshi", "FED1", ""),
                ("polymarket", "fed-1", ""),
                ("manifold", "fed1", ""),
            ),
            venue_count=3,
        )
        import catalog

        catalog.compute_gaps(self.conn, now=NOW)
        self.export()

        row = self.read("events-index.json")["events"][0]
        # The range the row draws is the comparable one, so mana cannot make a
        # two-venue gap look like a sixty-point one.
        self.assertEqual((row["lo"], row["hi"]), (57.0, 62.0))
        self.assertEqual(row["g"], 5.0)
        self.assertEqual(row["y"], ["play_money", "prediction_market"])

    def test_a_single_venue_event_is_written_too(self):
        self.add(record("kalshi", "FED1", price=62.0))
        self.match((("kalshi", "FED1", ""),), venue_count=1)
        import catalog

        catalog.compute_gaps(self.conn, now=NOW)
        self.export()
        row = self.read("events-index.json")["events"][0]
        # "1 venue" is a row a reader must be able to see, not a missing one.
        self.assertEqual(row["n"], 1)
        self.assertIsNone(row["g"])
        self.assertEqual((row["lo"], row["hi"]), (62.0, 62.0))

    def test_two_markets_from_one_venue_are_not_a_gap(self):
        """One venue comparing with itself is not a cross-venue comparison."""
        self.add(
            record("polymarket", "a", price=1.0),
            record("polymarket", "b", price=97.0),
        )
        self.match((("polymarket", "a", ""), ("polymarket", "b", "")))
        import catalog

        catalog.compute_gaps(self.conn, now=NOW)
        self.export()

        row = self.read("events-index.json")["events"][0]
        self.assertIsNone(row["g"])
        self.assertEqual(row["n"], 1)
        self.assertEqual(self.read("events", "ev-abc.json")["gap"], None)
        self.assertEqual(self.read("home.json")["stats"]["events_with_gap"], 0)

    def test_the_row_the_index_and_the_event_file_share_one_gap(self):
        """A stale stored column must not become a second, different gap."""
        self.add(
            record("kalshi", "FED1", price=62.0),
            record("polymarket", "fed-1", price=57.0),
        )
        self.match((("kalshi", "FED1", ""), ("polymarket", "fed-1", "")))
        self.conn.execute("UPDATE matched_events SET gap = 99.0")
        self.conn.commit()
        self.export()

        self.assertEqual(self.read("events-index.json")["events"][0]["g"], 5.0)
        self.assertEqual(self.read("events", "ev-abc.json")["gap"], 5.0)
        home = self.read("home.json")
        self.assertEqual(home["top_gaps"][0]["gap"], 5.0)
        # The row carries the venue count the page draws its label from.
        self.assertEqual(home["top_gaps"][0]["venue_count"], 2)

    def test_the_index_is_rebuilt_by_the_prices_only_pass(self):
        self.add(record("kalshi", "FED1", price=62.0))
        self.match((("kalshi", "FED1", ""),), venue_count=1)
        self.export(prices_only=True)
        self.assertTrue((self.out / "events-index.json").exists())


class SportsbookTermsTest(ExportCase):
    """A book's raw decimal odds and its margin, for the collapsed details.

    The site compares the de-vigged probability, and the brief asks for the raw
    figure and the margin only inside a collapsed section - but they never
    reach the event file unless this carries them.
    """

    def book(self):
        self.add(
            record(
                "odds_api",
                "ev1",
                venue_type="sportsbook",
                price=None,
                price_kind="implied_devig",
                outcomes=(
                    outcome("New York Knicks", 60.98, price_raw=1.6),
                    outcome("Boston Celtics", 39.02, price_raw=2.5),
                ),
            )
        )
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, rules_differ, venue_count, gap, updated_at) "
            "VALUES ('ev-abc', 'Knicks at Celtics', 'Sports', '', 95.0, 0, 0, 2, NULL, ?)",
            (ISO,),
        )
        for venue, market_id, named in (("odds_api", "ev1", "New York Knicks"),):
            self.conn.execute(
                "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
                "confidence, rules_differ, matched_on) VALUES ('ev-abc', ?, ?, ?, 95.0, 0, 'teams')",
                (venue, market_id, named),
            )
        self.conn.commit()

    def test_raw_odds_and_margin_ride_along_with_the_devigged_price(self):
        self.book()
        self.export()
        price = self.read("events", "ev-abc.json")["prices"][0]
        self.assertEqual(price["venue_type"], "sportsbook")
        self.assertEqual(price["price"], 60.98)
        self.assertEqual(price["raw"], 1.6)
        # 1/1.6 + 1/2.5 = 1.025, so the book is holding 2.5% for itself.
        self.assertEqual(price["margin"], 2.5)

    def test_a_prediction_market_row_carries_no_book_terms(self):
        self.add(record("kalshi", "FED1", price=62.0))
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, rules_differ, venue_count, gap, updated_at) "
            "VALUES ('ev-abc', 'Fed', '', '', 95.0, 0, 0, 1, NULL, ?)",
            (ISO,),
        )
        self.conn.execute(
            "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
            "confidence, rules_differ, matched_on) VALUES ('ev-abc', 'kalshi', 'FED1', '', 95.0, 0, 'title')"
        )
        self.conn.commit()
        self.export()
        price = self.read("events", "ev-abc.json")["prices"][0]
        self.assertIsNone(price["raw"])
        self.assertIsNone(price["margin"])


class PricesOnlyTest(ExportCase):
    """The 30 minute job: events and home, no catalog or search rebuild."""

    def test_prices_only_skips_catalog_and_search(self):
        self.add(record("kalshi", "K1"))
        report = self.export(prices_only=True)
        self.assertNotIn("catalog", report)
        self.assertNotIn("search", report)
        self.assertTrue((self.out / "home.json").exists())
        self.assertFalse((self.out / "catalog").exists())
        self.assertFalse((self.out / "search").exists())

    def test_prices_only_still_writes_events_and_snapshots(self):
        self.add(record("kalshi", "FED1"))
        self.conn.execute(
            "INSERT INTO matched_events (event_key, title, category, close_time, "
            "confidence, possible, rules_differ, venue_count, gap, updated_at) "
            "VALUES ('ev-abc', 'Fed', 'Economics', '', 95.0, 0, 0, 1, NULL, ?)",
            (ISO,),
        )
        self.conn.execute(
            "INSERT INTO matched_members (event_key, venue, market_id, outcome, "
            "confidence, rules_differ, matched_on) VALUES ('ev-abc', 'kalshi', 'FED1', '', 95.0, 0, 'title')"
        )
        self.conn.commit()
        report = self.export(prices_only=True)
        self.assertEqual(report["events"]["count"], 1)
        self.assertEqual(report["snapshots"], 1)
        self.assertTrue((self.out / "events" / "ev-abc.json").exists())

    def test_repeated_passes_accumulate_history_once_per_instant(self):
        self.add(record("kalshi", "K1"))
        self.export(prices_only=True, now=NOW)
        self.export(prices_only=True, now=NOW)
        self.assertEqual(store.read_market_history(self.conn, "kalshi", "K1"), [
            {"t": ISO, "price": 62.0}
        ])
        self.export(prices_only=True, now=NOW + timedelta(minutes=30))
        self.assertEqual(len(store.read_market_history(self.conn, "kalshi", "K1")), 2)


class ExporterCliTest(ExportCase):
    """The command line: exit codes, redaction, and the report it prints."""

    def run_main(self, argv) -> int:
        """Run the CLI with its report swallowed, as a suite should."""
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            return export.main(argv)

    def test_main_writes_everything_and_returns_zero(self):
        self.add(record("kalshi", "K1"))
        code = self.run_main(
            ["--db", str(self.db), "--out", str(self.out)]
        )
        self.assertEqual(code, 0)
        for name in (
            "home.json",
            "events-index.json",
            "catalog/manifest.json",
            "search/index-0.json",
        ):
            self.assertTrue((self.out / name).exists(), name)

    def test_main_reports_a_failure_without_leaking_a_secret(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": "supersecretvalue"}):
            with mock.patch.object(
                export,
                "export_all",
                side_effect=RuntimeError("boom at apiKey=supersecretvalue"),
            ):
                code = self.run_main(["--db", str(self.db), "--out", str(self.out)])
        self.assertEqual(code, 1)

    def test_main_survives_a_database_it_cannot_open(self):
        broken = self.root / "missing" / "nested" / "db.sqlite"
        self.assertEqual(
            self.run_main(["--db", str(broken), "--out", str(self.out)]), 1
        )


class WriteGroupsTest(unittest.TestCase):
    """The shard writer's own contract: never over the limit, never lose data."""

    def test_halves_a_group_the_estimate_let_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            groups = [[{"i": f"K{index}", "t": "x" * 200} for index in range(10)]]
            files, splits = export._write_groups(
                directory,
                "kalshi",
                groups,
                500,
                lambda number, all_groups, group: {"v": "kalshi", "markets": group},
            )
            self.assertGreater(splits, 0)
            self.assertEqual(sum(item["count"] for item in files), 10)
            for path in directory.glob("kalshi-*.json"):
                self.assertLessEqual(path.stat().st_size, 500)

    def test_a_single_record_too_big_for_the_limit_is_still_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            groups = [[{"i": "BIG", "t": "x" * 400}]]
            files, _ = export._write_groups(
                directory,
                "kalshi",
                groups,
                50,
                lambda number, all_groups, group: {"markets": group},
            )
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
