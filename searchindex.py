"""Read the exported search index exactly the way the site has to.

:mod:`export` writes the index; this reads it back, so the contract between
them is exercised by code rather than described in a comment. It is also what
:mod:`test_smoke` searches with, which is why a smoke test can prove the
artifacts work rather than only that they exist.

The contract, in the order a client uses it:

1. Fetch ``search/index-1.json``. It is the only file loaded up front, and it
   carries the dictionary: ``tokens`` maps a token to the shard (or shards)
   holding its postings, and ``bounds`` lists each shard's alphabet range.
2. To search, normalize the typed text, then fetch the shards named for its
   tokens and intersect the posting lists. Nothing else is loaded.
3. A posting is a market key, ``"<venue>:<market_id>"``. To show it, resolve
   the key against ``catalog/manifest.json``: each venue's shards carry a
   ``from``/``to`` market-id range, so one binary search names the file, and
   one fetch returns the market.

Only shards that a query actually needs are read, which is what keeps the
first keystroke cheap on a catalog of a hundred thousand markets.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from pathlib import Path

import matching
from venues import REAL_MONEY_TYPES, VENUE_TYPES


class IndexError_(Exception):
    """Raised when the exported files are missing or inconsistent."""


class Index:
    """The exported index, loaded lazily, one shard at a time."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.first = self._read("search", "index-1.json")
        self.tokens = self.first.get("tokens") or {}
        self.bounds = self.first.get("bounds") or []
        self.vocab = self.first.get("vocab", len(self.tokens))
        self.markets = self.first.get("markets", 0)
        self._loaded = {"search/index-1.json"}
        self._postings = {1: self.first.get("postings") or {}}
        self.manifest = self._read("catalog", "manifest.json")
        self._catalog: dict[str, dict] = {}

    # -- loading ---------------------------------------------------------- #

    def _read(self, *parts) -> dict:
        path = self.data_dir.joinpath(*parts)
        if not path.exists():
            raise IndexError_(f"missing exported file: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def shards_loaded(self) -> int:
        """How many files this reader has actually fetched."""
        return len(self._loaded)

    def ensure(self, number: int) -> None:
        """Fetch one postings shard, once."""
        if number in self._postings:
            return
        self._postings[number] = self._read("search", f"index-{number}.json").get(
            "postings"
        ) or {}
        self._loaded.add(f"search/index-{number}.json")

    def shards_for(self, token: str) -> list[int]:
        """The shard(s) holding a token: the dictionary first, bounds as backup."""
        found = self.tokens.get(token)
        if isinstance(found, int):
            return [found]
        if isinstance(found, list):
            return list(found)
        # Not in the dictionary: fall back to the alphabet range, so a token
        # that only appears as part of a longer one still resolves.
        return [number for number, (low, high) in enumerate(self.bounds, start=1)
                if low <= token <= high]

    # -- searching -------------------------------------------------------- #

    def look(self, token: str) -> list[str]:
        """Every market key whose indexed text contains this token."""
        keys: list[str] = []
        for number in self.shards_for(token):
            self.ensure(number)
            keys.extend(self._postings[number].get(token, []))
        return keys

    def matches(self, text: str) -> dict[str, int]:
        """Every candidate market for a query, with how many of its tokens hit.

        All tokens must match - that is what makes "fed cut" narrower than
        "fed". When nothing matches all of them (a half-typed word, or words
        that never co-occur) it falls back to any-token, so the first
        keystrokes still answer with something real instead of nothing.
        """
        tokens = sorted(
            matching.tokens(matching.normalize(text or "")), key=len, reverse=True
        )
        if not tokens:
            return {}
        hits: dict[str, int] = {}
        for token in tokens:
            for key in self.look(token):
                hits[key] = hits.get(key, 0) + 1
        wanted = len(tokens)
        strict = {key: count for key, count in hits.items() if count == wanted}
        return strict or hits

    def count(self, text: str) -> int:
        """How many markets match, without resolving any of them."""
        return len(self.matches(text))

    def query(self, text: str, limit: int = 10) -> list[dict]:
        """The best matches for a query, real money ahead of play money.

        Ranking is deliberately not alphabetical. Key order alone put
        Manifold first on every search, because its ids start with digits and
        sort before every letter - a comparison site that promises real-money
        gaps should not answer a search with play-money markets while real
        ones sit behind them. So: more matched tokens first, then a venue whose
        price can take part in a gap, then the key.
        """
        hits = self.matches(text)
        ordered = sorted(
            hits,
            key=lambda key: (
                -hits[key],
                not _real_money(key),
                key,
            ),
        )
        return [
            {
                "key": key,
                "matched_tokens": hits[key],
                "market": self.market(key),
                "shard": self.markets_for(key),
            }
            for key in ordered[:limit]
        ]

    def markets_for(self, key: str) -> str:
        """The catalog file a market key lives in, by binary search on ranges."""
        venue = key.split(":", 1)[0]
        entry = (self.manifest.get("venues") or {}).get(venue)
        if not entry:
            return ""
        shards = entry["shards"]
        starts = [shard["from"] for shard in shards]
        market_id = key.split(":", 1)[1]
        index = bisect_right(starts, market_id) - 1
        if index < 0 or market_id > shards[index]["to"]:
            return ""
        return shards[index]["file"]

    def market(self, key: str) -> dict | None:
        """The compact catalog record behind a key, fetching one shard."""
        file = self.markets_for(key)
        if not file:
            return None
        if file not in self._catalog:
            self._catalog[file] = {
                item["i"]: item for item in self._read(*file.split("/"))["markets"]
            }
            self._loaded.add(file)
        return self._catalog[file].get(key.split(":", 1)[1])

    def catalog_loaded(self) -> int:
        return len(self._catalog)


def _real_money(key: str) -> bool:
    """Whether a market key belongs to a venue whose prices can form a gap."""
    return VENUE_TYPES.get(key.split(":", 1)[0], "") in REAL_MONEY_TYPES


def describe(market: dict | None) -> str:
    """One compact market as a readable line, for a terminal report."""
    if not market:
        return "(missing from the catalog)"
    price = "-" if market.get("p") is None else f"{market['p']:.1f}"
    return f"{market['v']:11} {price:>6} pts  {market.get('t') or '(no title)'}"


def as_ascii(text: str) -> str:
    """Text that will not crash a cp1252 console.

    Titles legitimately carry emoji and accents, and a Windows console often
    cannot encode them. Replacing the unencodable characters keeps the report
    printable without pretending the data is ASCII.
    """
    return text.encode("ascii", "replace").decode("ascii")
