/* Methodology: how pairing, the gap, staleness, sourcing and the limits work.
 *
 * The prose here is meant to be checkable against the repository: mapping.json
 * for pairing, freshness.py for the 24 hour rule, dataview.py for the data
 * shape, the two venue adapters for the endpoints, and store.py for the
 * append-only price_snapshots table the charts are drawn from.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function section(label, title, body) {
    return (
      '<section class="card">' +
      '<p class="label">' + S.escapeHtml(label) + "</p>" +
      '<h2 class="card-title" style="margin:8px 0 12px;font-size:20px;letter-spacing:-0.5px">' +
      S.escapeHtml(title) + "</h2>" +
      body +
      "</section>"
    );
  }

  function paragraph(text) {
    return '<p style="margin-top:12px;max-width:72ch">' + text + "</p>";
  }

  function hint(text) {
    return '<p class="muted" style="margin-top:12px;max-width:72ch;font-size:14px">' + text + "</p>";
  }

  function timestampTable() {
    return (
      '<div class="scroll-x" style="margin-top:16px">' +
      '<table class="table">' +
      "<thead><tr><th>Venue</th><th>What it publishes</th><th>How freshness is judged here</th></tr></thead>" +
      "<tbody>" +
      "<tr>" +
      '<td class="mono">Kalshi</td>' +
      "<td>No trade time at all. Its <span class=\"mono\">updated_time</span> is a market-record stamp, " +
      "not a trade: it sat 130.8 hours old on the Fed market while that market had traded 119,581 " +
      "contracts in a single day.</td>" +
      "<td>24 hour traded volume. A non-zero volume proves at least one contract traded inside the " +
      "window, so the last trade - and the last trade price - is inside it too. Zero proves the opposite.</td>" +
      "</tr>" +
      "<tr>" +
      '<td class="mono">Polymarket</td>' +
      "<td>The last price change for that market, from the public CLOB price history. Gamma's own " +
      "<span class=\"mono\">updatedAt</span> is a batch stamp, byte-identical across whole sets of markets.</td>" +
      "<td>The age of that last price change.</td>" +
      "</tr>" +
      "</tbody></table></div>"
    );
  }

  function sections() {
    return [
      section(
        "How events are matched",
        "One event, one market per venue, checked by hand",
        paragraph(
          "Every row on this site is one real-world event. Each event is mapped by hand in the " +
          "project's <span class=\"mono\">mapping.json</span>: one market on Kalshi, one on Polymarket, " +
          "plus the event title, a category, and a note recording why those two markets are the same " +
          "event. Nothing is paired automatically."
        ) +
        paragraph(
          "A fuzzy matcher is available to compare market titles and suggest candidates, but it only " +
          "ever suggests. A suggestion has to be read against both markets' resolution rules before it " +
          "enters the mapping."
        ) +
        hint(
          "Matching can be wrong. Two markets can ask a very similar question and still settle on " +
          "different numbers, so a gap can be a matching error rather than a real price difference. " +
          "Where the two rules are known to part company, the event carries a RULES DIFFER flag."
        )
      ),
      section(
        "How the gap is computed",
        "Highest fresh price minus lowest fresh price",
        paragraph(
          "The gap is the maximum minus the minimum price among the venues pricing that event whose " +
          "price is not stale, measured in points where 1 point = 1 cent. Every pair here uses two " +
          "venues, so in practice the gap is the distance between two prices - the width of the " +
          "disagreement, shown as a positive number rather than a signed direction."
        ) +
        paragraph(
          "If fewer than two venues have a fresh price, the gap is null and the site shows " +
          "&ldquo;&mdash;&rdquo; instead of a number. One price cannot disagree with itself, and a stale " +
          "price is not allowed to manufacture a gap."
        )
      ),
      section(
        'What "stale" means',
        "Older than 24 hours, or unprovable",
        paragraph(
          "A price is stale when it cannot be shown to be fresher than 24 hours old. That sounds simple, " +
          "and it is not: neither venue publishes a real last-trade time, so freshness has to be argued " +
          "from whatever each one does publish."
        ) +
        timestampTable() +
        paragraph(
          "Stale prices are excluded from the gap, drawn in muted grey with a STALE label, and dashed " +
          "where they appear in a chart."
        ) +
        hint(
          "A price that cannot be shown to be fresh is treated as stale. The rule never assumes " +
          "freshness it cannot demonstrate - and because Kalshi publishes no trade clock, its freshness " +
          "rests on volume rather than on the timestamp shown beside it."
        )
      ),
      section(
        "Data sources and their terms",
        "Public, read-only, keyless",
        paragraph(
          "Kalshi is read through its public market-data API, <span class=\"mono\">trade-api v2</span> " +
          "at <span class=\"mono\">api.elections.kalshi.com</span>. Polymarket is read through " +
          "<span class=\"mono\">gamma-api.polymarket.com</span> for market records and " +
          "<span class=\"mono\">clob.polymarket.com</span> for price history."
        ) +
        paragraph(
          "All of it is public, read-only and keyless: no accounts, no API keys, no order placement, no " +
          "money. Responses are cached on disk and requests are paced with a deliberate pause, so the " +
          "venues' rate limits and terms are respected and a repeat run rarely touches the network. Only " +
          "public market data is read - no order books, no user data."
        ) +
        hint(
          "This is a prototype covering one category of events, economic data releases. The flat table " +
          "view shipped alongside the site is generated by the same pipeline that writes this site's data."
        )
      ),
      section(
        "Limitations",
        "What a gap is not",
        '<ul class="stack stack--tight" style="margin-top:12px;max-width:72ch">' +
          '<li>&#8212; Fees and liquidity differ between venues, so a gap is not a reachable profit and ' +
          "this site does not present it as one.</li>" +
          '<li>&#8212; Resolution rules differ. A pair can look like the same event and still settle on ' +
          "different numbers, which is why each event carries its own rule summaries and a RULES DIFFER flag.</li>" +
          '<li>&#8212; Prices are not comparable as trades. They are the venue\'s own last-trade price, or a ' +
          "bid/ask midpoint when the market has not traded, and they ignore order depth and fees.</li>" +
          '<li>&#8212; Matching errors are possible, and a gap is only as good as the pairing behind it.</li>' +
          '<li>&#8212; History covers what this pipeline observed, plus a one-time backfill of Polymarket\'s ' +
          "public CLOB price history at 15 minute fidelity. Kalshi publishes no price history at all, so " +
          "its series begins when this project started running.</li>" +
          "</ul>"
      )
    ].join("");
  }

  function glance(data) {
    var events = data.events || [];
    var moves = data.moves || [];
    var points = events.reduce(function (total, event) {
      var series = event.history || {};
      return (
        total +
        Object.keys(series).reduce(function (inner, venue) {
          return inner + (series[venue] || []).length;
        }, 0)
      );
    }, 0);
    var withGap = events.filter(function (event) { return event.gap != null; }).length;
    var rows = [
      ["Events tracked", String(events.length)],
      ["Events with a gap", String(withGap)],
      ["Observed price changes", String(moves.length)],
      ["Price points stored", String(points)],
      ["Venues", String((data.venues_status || []).length)]
    ];
    return (
      '<section class="card">' +
      '<p class="label">At a glance</p>' +
      '<div style="margin-top:12px">' +
      rows
        .map(function (row) {
          return (
            '<div class="stat-row"><span>' + S.escapeHtml(row[0]) + "</span>" +
            '<span class="mono muted">' + S.escapeHtml(row[1]) + "</span></div>"
          );
        })
        .join("") +
      "</div>" +
      '<p class="muted" style="margin-top:16px;font-size:12px">Data generated ' +
      S.escapeHtml(S.timeAgo(data.generated_at)) + ".</p>" +
      "</section>"
    );
  }

  function render(data) {
    var body = document.getElementById("methodology-body");
    body.innerHTML =
      '<div class="split">' +
      '<div class="stack">' + sections() + "</div>" +
      '<div class="stack">' +
      glance(data) +
      '<section class="card card--light">' +
      '<p class="label"><span class="dot dot--live" style="margin-right:6px" aria-hidden="true"></span>Read this first</p>' +
      '<p style="margin-top:12px;font-size:14px;line-height:1.5">' +
      S.escapeHtml(S.CAVEAT) +
      "</p></section>" +
      "</div></div>";
  }

  S.start({ page: "methodology", title: "Methodology" }, render);
})();
