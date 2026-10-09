"""Candidate selection: which pairs are even compared, and how stably.

tests/test_matching.py covers the scorer; this file covers the pass in front of
it. That pass had three failures worth pinning shut:

* a **positional cut** - only the first 40 postings of a token were used, and
  records arrive sorted by market_id, so the real counterpart was often never
  compared at all;
* a **document-frequency ceiling** - tokens in more than 300 markets were
  dropped entirely, which is exactly the Fed / Bitcoin / Trump tokens that name
  the biggest events;
* **set iteration**, which let ``PYTHONHASHSEED`` decide what got compared.
"""

import hashlib
import json
import os
import random
import subprocess
import sys
import unittest
from pathlib import Path

import matching

ROOT = Path(__file__).resolve().parents[1]


def record(venue, market_id, title, close_time="2026-12-01T00:00:00Z", **extra):
    """One market row, in the shape both the adapters and the store use."""
    base = {
        "venue": venue,
        "venue_type": "prediction_market",
        "market_id": market_id,
        "title": title,
        "close_time": close_time,
    }
    base.update(extra)
    return base


# A small mixed catalog: two confident pairs, one possible (number) pair, one
# pair the date gate must reject, and a combo that must never take part.
FIXTURE = [
    record("kalshi", "K-10", "Will the Fed cut rates in December 2026?"),
    record("polymarket", "P-3", "Will the Fed cut rates in December 2026?"),
    record("kalshi", "K-2", "Will bitcoin close above 100000 dollars in 2026?"),
    record("polymarket", "P-9", "Will bitcoin close above 100000 dollars in 2026?"),
    record(
        "manifold",
        "M-1",
        "Will bitcoin close above 100000 dollars in 2026?",
        venue_type="play_money",
    ),
    record("kalshi", "K-7", "Will the Lakers win the 2027 championship?"),
    record(
        "polymarket",
        "P-1",
        "Will the Lakers win the 2027 championship?",
        close_time="2027-06-01T00:00:00Z",
    ),
    record("kalshi", "K-4", "Will CPI rise more than 0.7 percent in September 2026?"),
    record("polymarket", "P-4", "Will CPI rise more than 0.8 percent in September 2026?"),
    record(
        "kalshi",
        "K-99",
        "Parlay: Fed cut and bitcoin above 100000",
        is_combo=1,
    ),
]


def named_pairs(records) -> list:
    """The candidate pairs of an already-canonical list, named, sorted."""
    pairs, _ = matching.candidate_pairs(records)
    return sorted(
        tuple(
            sorted((matching.record_key(records[i]), matching.record_key(records[j])))
        )
        for i, j in pairs
    )


def event_digest(records) -> list:
    """The events of a list, named, in a form two runs must agree on."""
    return sorted(
        (
            match.event_key,
            tuple(sorted((member[0], member[1]) for member in match.members)),
        )
        for match in matching.match_markets(records)
    )


class DeterminismTest(unittest.TestCase):
    """The feed order is not information: it must not change the answer."""

    def test_a_shuffled_feed_gives_the_same_candidates_and_events(self):
        shuffled = list(FIXTURE)
        random.Random(3).shuffle(shuffled)

        self.assertEqual(
            named_pairs(matching.canonical_records(FIXTURE)),
            named_pairs(matching.canonical_records(shuffled)),
        )
        self.assertEqual(event_digest(FIXTURE), event_digest(shuffled))


# The fixture, handed to a fresh interpreter so PYTHONHASHSEED can be set. It
# prints two digests per run: the fixture as written, and the same records
# shuffled with a fixed seed.
HASH_SEED_DRIVER = """
import hashlib
import json
import random
import sys

sys.path.insert(0, __ROOT__)
import matching

FIXTURE = __FIXTURE__


def digest(records):
    ordered = matching.canonical_records(records)
    pairs, _ = matching.candidate_pairs(ordered)
    named = sorted(
        sorted((matching.record_key(ordered[i]), matching.record_key(ordered[j])))
        for i, j in pairs
    )
    events = sorted(
        (match.event_key, tuple(sorted((m[0], m[1]) for m in match.members)))
        for match in matching.match_markets(records)
    )
    return hashlib.sha1(json.dumps([named, events], sort_keys=True).encode()).hexdigest()


shuffled = list(FIXTURE)
random.Random(7).shuffle(shuffled)
print("as-is", digest(FIXTURE))
print("shuffled", digest(shuffled))
"""


class HashSeedTest(unittest.TestCase):
    """A set of strings is not an order, and the interpreter must not supply one."""

    def digests(self, seed: str) -> list[str]:
        script = HASH_SEED_DRIVER.replace("__ROOT__", repr(str(ROOT))).replace(
            "__FIXTURE__", repr(FIXTURE)
        )
        done = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=dict(os.environ, PYTHONHASHSEED=seed),
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return [line.split()[1] for line in done.stdout.strip().splitlines()]

    def test_identical_candidates_and_events_under_three_hash_seeds(self):
        runs = {seed: self.digests(seed) for seed in ("0", "1", "12345")}
        for seed, digests in runs.items():
            self.assertEqual(len(set(digests)), 1, (seed, digests))
        self.assertEqual(
            len({digest for digests in runs.values() for digest in digests}),
            1,
            runs,
        )


class RankedCandidateTest(unittest.TestCase):
    """Candidates are ranked by weight, not cut by position."""

    def test_the_counterpart_is_found_when_its_id_sorts_last(self):
        """500 near-identical bitcoin titles, the true partner last by name.

        Every decoy here shares a wall of common tokens with the target, and
        the target market sorts first in its venue while its counterpart sorts
        last in its own. A positional cut hands the first market 40
        alphabetically early decoys and never reaches the partner.
        """
        decoys = []
        for index in range(500):
            venue = "kalshi" if index % 2 == 0 else "polymarket"
            prefix = "K" if index % 2 == 0 else "P"
            decoys.append(
                record(
                    venue,
                    f"{prefix}-{index // 2:04d}",
                    f"Will bitcoin close above {90000 + 10 * index} dollars "
                    f"on December 15 2026?",
                    close_time="2026-12-15T00:00:00Z",
                )
            )
        focus = record(
            "kalshi",
            "A-FOCUS",
            "Will bitcoin close above 100000 dollars on December 31 2026?",
            close_time="2026-12-31T00:00:00Z",
        )
        partner = record(
            "polymarket",
            "ZZZ-PARTNER",
            "Will bitcoin close above 100000 dollars on December 31 2026?",
            close_time="2026-12-31T00:00:00Z",
        )
        records = matching.canonical_records(decoys + [focus, partner])

        with self.assertLogs("matching", level="WARNING") as logged:
            pairs, stats = matching.candidate_pairs(records)
            matches = matching.match_markets(decoys + [focus, partner])

        self.assertGreater(stats["capped_buckets"], 0)
        self.assertTrue(any("capped" in line for line in logged.output), logged.output)

        found = {
            frozenset((matching.record_key(records[i]), matching.record_key(records[j])))
            for i, j in pairs
        }
        together = frozenset({("kalshi", "A-FOCUS"), ("polymarket", "ZZZ-PARTNER")})
        self.assertIn(together, found)

        events = [
            match
            for match in matches
            if together <= {(member[0], member[1]) for member in match.members}
        ]
        self.assertEqual(len(events), 1, [m.event_key for m in matches])

    def test_k_is_ten_and_every_market_keeps_at_most_that_many(self):
        self.assertEqual(matching.CANDIDATES_PER_MARKET, 10)
        records = matching.canonical_records(
            [
                record("kalshi", "K-1", "Will the Fed cut rates in December 2026?"),
                record("polymarket", "P-1", "Will the Fed cut rates in December 2026?"),
            ]
        )
        pairs, _ = matching.candidate_pairs(records)
        for position in range(len(records)):
            kept = sum(1 for first, second in pairs if position in (first, second))
            self.assertLessEqual(kept, matching.CANDIDATES_PER_MARKET)

    def test_the_heaviest_candidate_survives_a_wall_of_ties(self):
        """A rare shared token beats forty candidates that share only common ones.

        Forty markets share "trump", "september" and "2026" with the focus
        market and nothing else; one shares its whole title. Ranking keeps the
        whole title, which is the counterpart, inside a list of ten.
        """
        records = [record("kalshi", "FOCUS", "Trump approval rating September 2026")]
        for index in range(40):
            records.append(
                record(
                    "polymarket",
                    f"P-{index:03d}",
                    "Trump rally speech September 2026",
                )
            )
        records.append(
            record("polymarket", "PARTNER", "Trump approval rating September 2026")
        )
        ordered = matching.canonical_records(records)
        pairs, _ = matching.candidate_pairs(ordered)

        found = {
            frozenset((matching.record_key(ordered[i]), matching.record_key(ordered[j])))
            for i, j in pairs
        }
        self.assertIn(
            frozenset({("kalshi", "FOCUS"), ("polymarket", "PARTNER")}), found
        )


class CommonTokenBlockingTest(unittest.TestCase):
    """A common token still links markets - but only inside one window."""

    def test_a_common_token_links_markets_in_the_same_window(self):
        """2,000 "fed" markets, and the partner shares only "fed" plus a date.

        "fed" is the token the old ceiling threw away, which is why the broad
        Fed markets never matched anything. Here it is far too common to link
        the catalog, so it is blocked by close-date window and venue type
        instead - and the one pair inside the same window is still found.
        """
        decoys = []
        for index in range(2000):
            day = 1 + (index % 28)
            venue = "kalshi" if index % 2 == 0 else "polymarket"
            decoys.append(
                record(
                    venue,
                    f"D-{index:04d}",
                    f"Fed decision on December {day} 2026",
                    close_time=f"2026-12-{day:02d}T00:00:00Z",
                )
            )
        focus = record(
            "kalshi", "TARGET-K", "Fed decision March 2026", close_time="2026-03-18T00:00:00Z"
        )
        partner = record(
            "polymarket", "TARGET-P", "Fed vote March 2026", close_time="2026-03-18T00:00:00Z"
        )
        records = matching.canonical_records(decoys + [focus, partner])

        with self.assertLogs("matching", level="WARNING") as logged:
            pairs, stats = matching.candidate_pairs(records)
            matches = matching.match_markets(decoys + [focus, partner])

        self.assertGreater(stats["common_tokens"], 0)
        self.assertTrue(any("capped" in line for line in logged.output), logged.output)

        found = {
            frozenset((matching.record_key(records[i]), matching.record_key(records[j])))
            for i, j in pairs
        }
        together = frozenset({("kalshi", "TARGET-K"), ("polymarket", "TARGET-P")})
        self.assertIn(together, found)

        events = [
            match
            for match in matches
            if together <= {(member[0], member[1]) for member in match.members}
        ]
        self.assertEqual(len(events), 1)
        # The December decoys are in their own windows, so nothing else joins.
        self.assertEqual(len(events[0].members), 2)

    def test_a_common_token_never_links_markets_in_different_windows(self):
        """400 markets, one repeated title, four close dates 60 days apart.

        Nothing here is rare: "trump", "approval" and "rating" are in all 400
        markets, so the only way two of them can be candidates is the same
        close-date window - and every cross-window pair must be absent.
        """
        windows = ("2026-03-01", "2026-05-01", "2026-07-01", "2026-09-01")
        records = []
        window_of = {}
        for window, close in enumerate(windows):
            for index in range(100):
                venue = "kalshi" if index % 2 == 0 else "polymarket"
                item = record(
                    venue,
                    f"W{window}-{index:03d}",
                    "Trump approval rating",
                    close_time=f"{close}T00:00:00Z",
                )
                records.append(item)
                window_of[matching.record_key(item)] = window

        ordered = matching.canonical_records(records)
        with self.assertLogs("matching", level="WARNING"):
            pairs, stats = matching.candidate_pairs(ordered)

        self.assertGreater(len(pairs), 0)  # the windows are compared inside
        self.assertGreater(stats["capped_buckets"], 0)
        crossing = [
            (matching.record_key(ordered[i]), matching.record_key(ordered[j]))
            for i, j in pairs
            if window_of[matching.record_key(ordered[i])]
            != window_of[matching.record_key(ordered[j])]
        ]
        self.assertEqual(crossing, [])

    def test_a_market_without_a_close_date_takes_no_common_token_links(self):
        """No date is no evidence that two markets belong to the same window."""
        records = [record("kalshi", "K-1", "Trump approval rating", close_time="")]
        for index in range(400):
            records.append(
                record(
                    "polymarket",
                    f"P-{index:03d}",
                    "Trump approval rating",
                    close_time="",
                )
            )
        ordered = matching.canonical_records(records)
        pairs, stats = matching.candidate_pairs(ordered)
        self.assertEqual(pairs, {})
        self.assertEqual(stats["common_tokens"], 0)


class ComboTest(unittest.TestCase):
    """A parlay bundles other markets; it is never a question of its own."""

    def test_a_combo_market_is_never_a_candidate_or_an_event(self):
        records = [
            record("kalshi", "K-1", "Will the Fed cut rates in December 2026?"),
            record("polymarket", "P-1", "Will the Fed cut rates in December 2026?"),
            record(
                "polymarket",
                "P-COMBO",
                "Will the Fed cut rates in December 2026?",
                is_combo=1,
            ),
        ]
        ordered = matching.canonical_records(records)
        self.assertEqual(
            [matching.record_key(item) for item in ordered],
            [("kalshi", "K-1"), ("polymarket", "P-1")],
        )

        pairs, stats = matching.candidate_pairs(ordered)
        self.assertEqual(stats["records"], 2)
        self.assertEqual(len(pairs), 1)

        members = {
            member[1]
            for match in matching.match_markets(records)
            for member in match.members
        }
        self.assertEqual(members, {"K-1", "P-1"})

    def test_the_combo_flag_is_read_the_way_venues_store_it(self):
        self.assertTrue(matching._is_combo({"is_combo": 1}))
        self.assertTrue(matching._is_combo({"is_combo": True}))
        self.assertTrue(matching._is_combo({"is_combo": "true"}))
        self.assertFalse(matching._is_combo({"is_combo": 0}))
        self.assertFalse(matching._is_combo({"is_combo": "0"}))
        self.assertFalse(matching._is_combo({"is_combo": None}))
        self.assertFalse(matching._is_combo({}))


if __name__ == "__main__":
    unittest.main()
