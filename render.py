"""Build the static `index.html` price-gap table from `spread.db`.

The gap always comes from :func:`mapping.spread_between`, so the page and
the CLI can never disagree about the arithmetic. Every number is rendered
next to its source venue, and a venue with no stored observation shows as
missing rather than as a zero.

Three rules shape the table:

* A price the venues cannot show to be fresher than 24 hours old is greyed
  out, tagged **stale**, and left out of the gap - :mod:`freshness` decides
  that, never the renderer.
* Rows are ordered by the size of the gap, freshest comparison first,
  biggest disagreement on top. Rows without a comparable gap are pinned
  below the ranking instead of being sorted into it, whether the table is
  ordered here or by the sort button in the browser.
* Every timestamp is labelled with what it actually is. Neither venue
  publishes a last-trade time, and the page says which stamp each number is
  instead of calling all of them "updated".
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import freshness
import mapping
import store

INDEX_PATH = Path(__file__).resolve().parent / "index.html"

PAGE_TITLE = "Economic event price gaps"

CAVEAT = (
    "Fees, liquidity and resolution rules may differ between venues: prices are "
    "not directly transferable, and paired markets may not resolve the same way."
)
INFORMATIONAL = (
    "Informational only. Nothing here is financial advice, a recommendation, "
    "or an offer to buy or sell anything."
)
ADULT_NOTICE = "18+ only. Prediction markets involve risk of loss."

PROVENANCE = (
    "Data source: live API calls only, read-only and with no accounts or keys - "
    "Kalshi trade-api v2 and Polymarket's Gamma and CLOB APIs. No sample or "
    "fixture data appears on this page; the JSON under tests/fixtures is used "
    "only by the offline unit tests."
)

FRESHNESS_NOTE = (
    "Freshness: a price older than 24 hours is greyed out, tagged stale and "
    "dropped from the gap, so a comparison always uses prices the venues can "
    "show to be current. Neither venue publishes a plain last-trade time. "
    "Kalshi publishes none at all, so its freshness comes from 24 hour traded "
    "volume - any trade in that window means the last trade, and so the last "
    "trade price, is inside it. The stamp Kalshi does publish is when it last "
    "changed the market record, which sat 130.8 hours old on a market that had "
    "traded 109,329 contracts in a day, so it is shown but never used to judge "
    "age. Polymarket's Gamma 'updated' field is a batch stamp shared by whole "
    "sets of markets; its per-market last price change comes from the public "
    "CLOB price history instead. A price that cannot be shown to be fresh "
    "counts as stale."
)

KIND_TEXT = {"last": "last trade", "mid": "bid/ask midpoint"}

# What each venue's own "updated" stamp really is, per venue, as measured.
RECORD_STAMP_NOTE = {
    "kalshi": "when Kalshi last changed this market record, not a trade time",
    "polymarket": "Gamma batch stamp, shared by many markets, not a trade time",
}
DEFAULT_RECORD_STAMP_NOTE = "the venue's own record stamp, not a trade time"

AS_OF_TEXT = {
    "last_price_change": "last price change, from the venue's public price history",
    "batch_stamp": "batch stamp, shared by many markets - not a trade time",
    "none": "no trade time published by this venue",
}

STYLES = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 78rem;
       padding: 0 1rem; color: #16181d; background: #fbfbfd; }
h1 { font-size: 1.4rem; margin-bottom: .2rem; }
p.sub { margin-top: 0; color: #5b6170; }
p.note { color: #3d4350; font-size: .88rem; }
table { border-collapse: collapse; width: 100%; margin: 1.2rem 0; }
th, td { border: 1px solid #d7dae2; padding: .6rem .7rem; text-align: left;
         vertical-align: top; }
thead th { background: #eef0f5; font-size: .85rem; text-transform: uppercase;
           letter-spacing: .04em; }
.event b { display: block; }
.source { display: inline-block; font-size: .72rem; font-weight: 700; letter-spacing: .05em;
          text-transform: uppercase; background: #2b5bd7; color: #fff;
          border-radius: .3rem; padding: .1rem .4rem; }
.price { font-size: 1.35rem; font-weight: 700; }
.gap { font-size: 1.2rem; font-weight: 700; }
.muted, .none { color: #5b6170; font-size: .85rem; display: block; }
.rule { display: block; font-size: .85rem; margin-bottom: .3rem; }
.stale-tag { display: inline-block; font-size: .7rem; font-weight: 700;
             text-transform: uppercase; letter-spacing: .05em; background: #b02a2a;
             color: #fff; border-radius: .3rem; padding: .1rem .4rem; }
.flag { display: inline-block; font-size: .7rem; font-weight: 700;
        text-transform: uppercase; letter-spacing: .05em; background: #a35b00;
        color: #fff; border-radius: .3rem; padding: .1rem .4rem; }
.same { display: inline-block; font-size: .7rem; font-weight: 700;
        text-transform: uppercase; letter-spacing: .05em; background: #217a4b;
        color: #fff; border-radius: .3rem; padding: .1rem .4rem; }
td.venue.stale { background: #f1f2f6; }
td.venue.stale .price { color: #9096a3; font-weight: 500; }
td.venue.stale .source { background: #9096a3; }
td.gap .none { color: #7b5a00; }
button.sort { font: inherit; font-weight: 700; color: inherit; background: none;
              border: 0; padding: 0; cursor: pointer; letter-spacing: inherit;
              text-transform: inherit; text-decoration: underline dotted; }
button.sort[hidden] { display: none; }
a { color: #2b5bd7; }
.notices li { margin-bottom: .35rem; }
details summary { cursor: pointer; font-size: .82rem; color: #5b6170; }
details p { margin: .4rem 0; }
@media (prefers-color-scheme: dark) {
  body { color: #e7e9ee; background: #14161b; }
  th, td { border-color: #333845; }
  thead th { background: #1d212a; }
  .muted, .none { color: #9aa1b1; }
  p.note { color: #b8bec9; }
  td.venue.stale { background: #191c22; }
  td.venue.stale .price { color: #6f7684; }
  td.gap .none { color: #d9b26a; }
}
"""

# Re-orders the rows in the browser when the reader clicks the Gap header.
# Pinned rows (no comparable gap) are always put back below the ranking, so
# a stale or incomplete comparison can never be sorted into it.
SORT_SCRIPT = """
(function () {
  var body = document.querySelector("#gaps tbody");
  var button = document.getElementById("sort-gap");
  if (!body || !button) { return; }
  var direction = -1;
  var rows = function () { return Array.prototype.slice.call(body.querySelectorAll("tr")); };
  var gapOf = function (row) { return Number(row.getAttribute("data-gap")); };
  var keyOf = function (row) { return row.getAttribute("data-key") || ""; };

  function apply() {
    var ranked = [], pinned = [];
    rows().forEach(function (row) {
      var gap = row.getAttribute("data-gap");
      (gap !== "" && isFinite(Number(gap)) ? ranked : pinned).push(row);
    });
    ranked.sort(function (a, b) {
      var delta = Math.abs(gapOf(a)) - Math.abs(gapOf(b));
      if (delta !== 0) { return direction * delta; }
      return keyOf(a) < keyOf(b) ? -1 : 1;
    });
    pinned.sort(function (a, b) { return keyOf(a) < keyOf(b) ? -1 : 1; });
    ranked.concat(pinned).forEach(function (row) { body.appendChild(row); });
  }

  button.hidden = false;
  button.setAttribute("aria-sort", direction < 0 ? "descending" : "ascending");    button.addEventListener("click", function () {
    direction = -direction;
    button.setAttribute("aria-sort", direction < 0 ? "descending" : "ascending");
    button.textContent = direction < 0 ? "sort \u25bc" : "sort \u25b2";
    apply();
  });
  apply();
})();
"""


def render_page(conn, *, now: datetime | None = None) -> str:
    """Return the full HTML page for whatever is stored in `conn`."""
    now = freshness.as_utc(now) or datetime.now(timezone.utc)
    rows = store.read_observations(conn)
    events = order_events(_grouped_events(rows), now=now)
    generated = now.isoformat(timespec="seconds")
    body = "".join(
        _event_row(key, title, venues, now=now) for key, title, venues in events
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(PAGE_TITLE)}</title>
<style>{STYLES}</style>
</head>
<body>
<h1>{html.escape(PAGE_TITLE)}</h1>
<p class="sub">Prices for the same real-world event on two prediction markets,
generated {html.escape(generated)}.</p>
<p class="note">{html.escape(PROVENANCE)}</p>
<p class="note">{html.escape(_fetch_note(rows, now))}</p>
<p class="note">{html.escape(FRESHNESS_NOTE)}</p>
<table id="gaps">
<thead><tr>
  <th scope="col">Event</th>
  <th scope="col">Kalshi</th>
  <th scope="col">Polymarket</th>
  <th scope="col">Gap
    <button id="sort-gap" class="sort" type="button" aria-sort="descending"
            title="Sort by the size of the gap, biggest first" hidden>sort \u25bc</button>
  </th>
  <th scope="col">Resolution rules</th>
</tr></thead>
<tbody>
{body}</tbody>
</table>
<ul class="notices">
  <li>{html.escape(CAVEAT)}</li>
  <li>{html.escape(INFORMATIONAL)}</li>
  <li><b>{html.escape(ADULT_NOTICE)}</b></li>
</ul>
<script>{SORT_SCRIPT}</script>
</body>
</html>
"""


def write_page(conn, path: Path | str = INDEX_PATH) -> Path:
    """Write the rendered page to `path` and return it."""
    path = Path(path)
    path.write_text(render_page(conn), encoding="utf-8")
    return path


def order_events(events: list[tuple], *, now: datetime | None = None) -> list[tuple]:
    """Fresh comparisons by gap size, biggest first; everything else last.

    A row is ranked only when both sides are present and fresh, in which
    case ``|gap|`` decides; rows without a comparable gap - a stale side or
    a missing venue - are pinned below the ranking in event-key order.
    """
    ranked, pinned = [], []
    for key, title, venues in events:
        verdicts = _verdicts(venues, now)
        gap = _gap_value(venues, verdicts)
        (ranked if gap is not None else pinned).append((key, title, venues, gap))
    ranked.sort(key=lambda item: (-abs(item[3]), item[0]))
    pinned.sort(key=lambda item: item[0])
    return [(key, title, venues) for key, title, venues, _ in ranked + pinned]


def _grouped_events(rows: list[dict]) -> list[tuple[str, str, dict]]:
    """Rows from the store, grouped into (event_key, title, venue -> row)."""
    grouped: dict = {}
    titles: dict = {}
    for row in rows:
        grouped.setdefault(row["event_key"], {})[row["venue"]] = row
        titles[row["event_key"]] = row["event_title"]
    return [(key, titles[key], grouped[key]) for key in sorted(grouped, key=str)]


def _verdicts(venues: dict, now: datetime | None) -> dict:
    """Freshness verdict per venue that has a row."""
    return {
        venue: freshness.assess(row, now=now)
        for venue, row in venues.items()
        if row is not None
    }


def _stale_venues(verdicts: dict) -> list[str]:
    return [
        venue
        for venue in mapping.VENUES
        if verdicts.get(venue) is not None and not verdicts[venue].fresh
    ]


def _gap_value(venues: dict, verdicts: dict) -> float | None:
    """The comparable gap, or None when a stale or missing side rules it out."""
    if _stale_venues(verdicts):
        return None
    kalshi = venues.get("kalshi")
    polymarket = venues.get("polymarket")
    # The one place the gap is computed: mapping.spread_between.
    return mapping.spread_between(
        kalshi["price_cents"] if kalshi else None,
        polymarket["price_cents"] if polymarket else None,
    )


def _event_row(event_key: str, title: str, venues: dict, *, now: datetime) -> str:
    verdicts = _verdicts(venues, now)
    gap = _gap_value(venues, verdicts)
    cells = "".join(
        _venue_cell(venues.get(venue), verdicts.get(venue), now=now)
        for venue in mapping.VENUES
    )
    attributes = f'data-key="{html.escape(event_key, quote=True)}"'
    attributes += f' data-gap="{gap:.2f}"' if gap is not None else ' data-gap=""'
    row_class = ' class="row-stale"' if _stale_venues(verdicts) else ""
    return (
        f"<tr{row_class} {attributes}>"
        f'<td class="event"><b>{html.escape(title)}</b>'
        f'<span class="muted">{html.escape(event_key)}</span></td>'
        f"{cells}{_gap_cell(venues, verdicts, gap)}{_rules_cell(venues)}</tr>\n"
    )


def _venue_cell(row: dict | None, verdict, *, now: datetime) -> str:
    if row is None:
        return '<td class="venue"><span class="none">No data for this venue</span></td>'

    volume = f"{row['volume']:,}" if row["volume"] is not None else "—"
    url = html.escape(row["url"], quote=True)
    label = html.escape(row["source"])
    stale = verdict is not None and not verdict.fresh
    parts = [
        f'<span class="source">{label}</span> ',
        f'<span class="price">{row["price_cents"]:.1f}¢</span> ',
        f'<span class="muted">'
        f'{html.escape(KIND_TEXT.get(row["price_kind"], row["price_kind"]))}</span>',
    ]
    if stale:
        parts.append('<span class="stale-tag">stale</span>')
    parts.append(f'<span class="muted">{label} volume: {volume}</span>')
    if row["volume_24h"] is not None:
        parts.append(
            f'<span class="muted">{label} traded 24h: {row["volume_24h"]:,}</span>'
        )
    if verdict is not None:
        parts.append(
            f'<span class="muted">{label} price age: '
            f'{html.escape(verdict.basis)}</span>'
        )
    parts.append(_as_of_line(row, label))
    parts.append(
        f'<span class="muted">{label} updated: {html.escape(row["last_updated"])} '
        f'({html.escape(RECORD_STAMP_NOTE.get(row["venue"], DEFAULT_RECORD_STAMP_NOTE))})'
        "</span>"
    )
    parts.append(
        f'<span class="muted">{label} fetched: '
        f'{html.escape(row["fetched_at"] or "—")}'
        f' ({html.escape(_age(row["fetched_at"], now))})</span>'
    )
    parts.append(f'<a href="{url}" target="_blank" rel="noopener">{label} market</a>')
    return f'<td class="venue{" stale" if stale else ""}">' + "".join(parts) + "</td>"


def _as_of_line(row: dict, label: str) -> str:
    """When the price itself last moved, named, or say the venue has none."""
    kind = row["price_as_of_kind"] or "none"
    explanation = AS_OF_TEXT.get(kind, kind)
    stamp = row["price_as_of"] or "—"
    return (
        f'<span class="muted">{label} price as of: {html.escape(stamp)} '
        f'({html.escape(explanation)})</span>'
    )


def _gap_cell(venues: dict, verdicts: dict, gap: float | None) -> str:
    if gap is not None:
        return (
            '<td class="gap">'
            f'<span class="gap">{gap:+.1f} points</span>'
            '<span class="muted">Kalshi − Polymarket</span>'
            "</td>"
        )

    stale = _stale_venues(verdicts)
    if stale:
        reasons = "; ".join(
            f"{store.SOURCE_LABELS.get(venue, venue)}: {verdicts[venue].reason}"
            for venue in stale
        )
        return (
            '<td class="gap">'
            '<span class="none">excluded — stale price</span>'
            f'<span class="none">{html.escape(reasons)}</span>'
            '<span class="muted">left out of the gap sort</span>'
            "</td>"
        )

    missing = ", ".join(
        store.SOURCE_LABELS.get(venue, venue)
        for venue in mapping.VENUES
        if venues.get(venue) is None
    )
    return (
        '<td class="gap"><span class="none">—</span>'
        f'<span class="none">missing {html.escape(missing)}</span></td>'
    )


def _rules_cell(venues: dict) -> str:
    """One line per venue's resolution rule, flagged when they differ."""
    sample = next((row for row in venues.values() if row), None) or {}
    kalshi_rule = sample.get("event_kalshi_rule") or ""
    polymarket_rule = sample.get("event_polymarket_rule") or ""
    if not (kalshi_rule or polymarket_rule):
        return (
            '<td class="rules"><span class="none">'
            "No curated rule summary for this event</span></td>"
        )

    parts = []
    for venue, text in (("kalshi", kalshi_rule), ("polymarket", polymarket_rule)):
        if not text:
            continue
        label = html.escape(store.SOURCE_LABELS.get(venue, venue))
        parts.append(
            f'<span class="rule"><span class="source">{label}</span> '
            f"{html.escape(text)}</span>"
        )
    parts.append(
        '<span class="flag">resolution rules differ</span>'
        if sample.get("event_rules_differ")
        else '<span class="same">same resolution rule</span>'
    )
    note = sample.get("event_rules_note") or ""
    if note:
        parts.append(f'<span class="muted">{html.escape(note)}</span>')
    parts.append(_rules_details(venues))
    return '<td class="rules">' + "".join(parts) + "</td>"


def _rules_details(venues: dict) -> str:
    """The venue's own rule text, verbatim, behind a disclosure triangle."""
    blocks = []
    for venue in mapping.VENUES:
        row = venues.get(venue)
        text = ((row or {}).get("rules_text") or "").strip()
        if not text:
            continue
        label = html.escape(store.SOURCE_LABELS.get(venue, venue))
        blocks.append(
            f'<p class="muted"><b>{label} rule text, verbatim from the API</b><br>'
            f"{html.escape(text)}</p>"
        )
    if not blocks:
        return '<span class="none">No live rule text stored for this event.</span>'
    return (
        '<details><summary>full rules from each venue</summary>'
        + "".join(blocks)
        + "</details>"
    )


def _fetch_note(rows: Iterable[dict], now: datetime) -> str:
    """When the live data on this page was pulled, oldest and newest."""
    stamps = sorted({row["fetched_at"] for row in rows if row.get("fetched_at")})
    if not stamps:
        return "No fetch timestamp is stored: this database has no observations yet."
    oldest, newest = stamps[0], stamps[-1]
    if oldest == newest:
        return f"Live fetch: {newest} ({_age(newest, now)} ago)."
    return (
        f"Live fetches: oldest {oldest} ({_age(oldest, now)} ago), "
        f"newest {newest} ({_age(newest, now)} ago)."
    )


def _age(stamp: str, now: datetime) -> str:
    """Compact age of a stored timestamp, or an empty string if unreadable."""
    if not stamp:
        return "unknown time"
    try:
        moment = freshness.parse_iso(stamp)
    except ValueError:
        return "unknown time"
    hours = (now - moment).total_seconds() / 3600
    return freshness.human_age(hours)
