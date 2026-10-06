"""Build the static `index.html` price-gap table from `spread.db`.

The gap always comes from :func:`mapping.spread_between`, so the page and
the CLI can never disagree about the arithmetic. Every number is rendered
next to its source venue, and a venue with no stored observation shows as
missing rather than as a zero.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

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

KIND_TEXT = {"last": "last trade", "mid": "bid/ask midpoint"}

STYLES = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 70rem;
       padding: 0 1rem; color: #16181d; background: #fbfbfd; }
h1 { font-size: 1.4rem; margin-bottom: .2rem; }
p.sub { margin-top: 0; color: #5b6170; }
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
a { color: #2b5bd7; }
.notices li { margin-bottom: .35rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e7e9ee; background: #14161b; }
  th, td { border-color: #333845; }
  thead th { background: #1d212a; }
  .muted, .none { color: #9aa1b1; }
}
"""


def render_page(conn) -> str:
    """Return the full HTML page for whatever is stored in `conn`."""
    events = _grouped_events(store.read_observations(conn))
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")

    rows = "".join(_event_row(key, title, venues) for key, title, venues in events)
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
<table>
<thead><tr>
  <th scope="col">Event</th>
  <th scope="col">Kalshi</th>
  <th scope="col">Polymarket</th>
  <th scope="col">Gap</th>
</tr></thead>
<tbody>
{rows}</tbody>
</table>
<ul class="notices">
  <li>{html.escape(CAVEAT)}</li>
  <li>{html.escape(INFORMATIONAL)}</li>
  <li><b>{html.escape(ADULT_NOTICE)}</b></li>
</ul>
</body>
</html>
"""


def write_page(conn, path: Path | str = INDEX_PATH) -> Path:
    """Write the rendered page to `path` and return it."""
    path = Path(path)
    path.write_text(render_page(conn), encoding="utf-8")
    return path


def _grouped_events(rows: list[dict]) -> list[tuple[str, str, dict]]:
    """Rows from the store, grouped into (event_key, title, venue -> row)."""
    grouped: dict = {}
    titles: dict = {}
    for row in rows:
        grouped.setdefault(row["event_key"], {})[row["venue"]] = row
        titles[row["event_key"]] = row["event_title"]
    return [
        (key, titles[key], grouped[key]) for key in sorted(grouped, key=str)
    ]


def _event_row(event_key: str, title: str, venues: dict) -> str:
    gap_html = _gap_cell(venues)
    cells = "".join(_venue_cell(venues.get(venue)) for venue in mapping.VENUES)
    return (
        "<tr>"
        f'<td class="event"><b>{html.escape(title)}</b>'
        f'<span class="muted">{html.escape(event_key)}</span></td>'
        f"{cells}{gap_html}</tr>\n"
    )


def _venue_cell(row: dict | None) -> str:
    if row is None:
        return '<td><span class="none">No data for this venue</span></td>'

    volume = f"{row['volume']:,}" if row["volume"] is not None else "—"
    url = html.escape(row["url"], quote=True)
    label = html.escape(row["source"])
    return (
        "<td>"
        f'<span class="source">{label}</span> '
        f'<span class="price">{row["price_cents"]:.1f}¢</span> '
        f'<span class="muted">{html.escape(KIND_TEXT.get(row["price_kind"], row["price_kind"]))}</span>'
        f'<span class="muted">{label} volume: {volume}</span>'
        f'<span class="muted">{label} updated: {html.escape(row["last_updated"])}</span>'
        f'<a href="{url}" target="_blank" rel="noopener">{label} market</a>'
        "</td>"
    )


def _gap_cell(venues: dict) -> str:
    kalshi = venues.get("kalshi")
    polymarket = venues.get("polymarket")
    # The one place the gap is computed: mapping.spread_between.
    gap = mapping.spread_between(
        kalshi["price_cents"] if kalshi else None,
        polymarket["price_cents"] if polymarket else None,
    )
    if gap is None:
        missing = ", ".join(
            store.SOURCE_LABELS.get(venue, venue)
            for venue in mapping.VENUES
            if venues.get(venue) is None
        )
        return (
            '<td><span class="none">—</span>'
            f'<span class="none">missing {html.escape(missing)}</span></td>'
        )
    return (
        "<td>"
        f'<span class="gap">{gap:+.1f} points</span>'
        '<span class="muted">Kalshi − Polymarket</span>'
        "</td>"
    )
