/* Notes: the whole of it, for the reader who wants it.
 *
 * Every section is a collapsed <details>, so the page is a list of headings by
 * default and the long version is one click away - and Detailed mode opens all
 * of them. This is where the site's full caveats live; the one line in the
 * footer is the whole of what a casual reader is asked to read.
 *
 * The disclosures below are reproduced from the constants in app.js, so the
 * footer line and this page can never drift apart.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function section(title, body) {
    return { title: title, body: body };
  }

  function paragraphs(lines) {
    var html = "";
    for (var index = 0; index < lines.length; index += 1) {
      html += "<p>" + lines[index] + "</p>";
    }
    return html;
  }

  function definitions() {
    var rows = [
      ["Points", "One point is one cent of a one-dollar contract. A price of 62 means 62 cents on a dollar, or a 62% implied probability."],
      ["Gap", "The highest fresh real-money price minus the lowest, in points. Null when fewer than two fresh real-money prices exist."],
      ["Fresh", "A price the venue can be shown to have moved within the last 24 hours. Judged per venue from the best evidence it publishes."],
      ["Stale", "A price that cannot be shown to be that recent. It is still shown, marked STALE, and left out of the gap."],
      ["Real money", "Prediction markets and sportsbooks: venues where the price is a price someone can act on."],
      ["Play money", "Mana, points and community forecasts. A price nobody can trade against, so it never enters a gap."],
      ["Possible match", "A pairing the matcher scored below its confidence floor (0.85). Shown, labelled, and never counted in a gap."],
      ["Rules differ", "The venues word their resolution criteria differently, so the same outcome can settle differently."]
    ];
    var html = '<div class="scroll-x"><table class="table"><thead><tr><th>Term</th><th>Means</th></tr></thead><tbody>';
    for (var index = 0; index < rows.length; index += 1) {
      html += "<tr><td>" + S.esc(rows[index][0]) + "</td><td>" + S.esc(rows[index][1]) + "</td></tr>";
    }
    return html + "</tbody></table></div>";
  }

  function render(home) {
    var body = document.getElementById("page-body");
    if (!body) {
      return;
    }
    body.innerHTML =
      '<div class="page-head"><h1 class="headline">Notes</h1>' +
      '<span class="label label--muted">Method, sources and caveats</span></div>' +
      '<p class="subhead" style="max-width:70ch">The short version is in the footer of every ' +
      "page. This is the long one, and it is written so a reader can hold the site to it.</p>" +
      '<div class="card" style="margin-top:24px;padding-top:0;padding-bottom:0">' +
      S.notes([
        section(
          "What this site is, and is not",
          paragraphs([
            "<strong>It is</strong> a read-only record of the prices several venues publish for the " +
              "same real-world event, and of how far apart they are.",
            "<strong>It is not</strong> a trading venue, a broker, a tipster or advice. Nothing here " +
              "is an offer to buy or sell anything, a recommendation, or a prediction of its own.",
            "<strong>The gap is not free money.</strong> Two prices differing by ten points does not " +
              "mean ten points can be captured: each venue charges its own fees, offers its own " +
              "depth, and settles from its own source.",
            S.esc(S.INFORMATIONAL),
            S.esc(S.ADULT),
            S.esc(S.CAVEAT)
          ])
        ),
        section(
          "How this is calculated",
          paragraphs([
            "The gap is the highest fresh real-money price minus the lowest, in points. It exists " +
              "only when at least two fresh real-money prices cover the same event; one fresh venue " +
              "is a single number, not a gap.",
            "Real money means a prediction market or a de-vigged sportsbook price. Play-money mana " +
              "and community forecasts are shown in their own section labelled <em>Not comparable to " +
              "real-money prices</em> and never enter the arithmetic.",
            "A pairing the matcher scored below 0.85 is stored as a possible match, labelled, and " +
              "excluded from every gap so a near-match cannot manufacture a spread that does not exist.",
            "The site never recomputes any of this in the browser. The gap and every freshness " +
              "verdict arrive decided, so a page cannot quietly widen either rule."
          ])
        ),
        section(
          "Freshness and staleness",
          paragraphs([
            "Every price carries the timestamp the venue itself publishes, and freshness is judged " +
              "from the best evidence that venue offers. Kalshi publishes no trade time at all, so " +
              "its freshness comes from 24 hour traded volume; Polymarket's comes from its public " +
              "price history; Manifold publishes a per-market last-bet time.",
            "A price that cannot be shown to be from the last 24 hours is marked <strong>STALE</strong> " +
              "and left out of the gap. It is still shown, with its age, because hiding it would " +
              "hide why a comparison is missing.",
            "The catalog and the search index are rebuilt every six hours; prices, matched events " +
              "and the home document every thirty minutes. The footer says when the data you are " +
              "looking at was built."
          ])
        ),
        section(
          "Sportsbook odds and the margin",
          paragraphs([
            "Sportsbook prices are de-vigged implied probabilities: one divided by the decimal odds, " +
              "normalised across the bookmaker's outcomes so that they sum to 100%.",
            "That normalisation removes the margin the bookmaker keeps, which is the only way a book " +
              "and a prediction market can be compared at all. The raw decimal figure and the " +
              "margin a mark was normalised by appear only inside a collapsed detail on the event " +
              "page, never as a headline number.",
            "Bookmakers are named as data, with no links, no promotional wording and no affiliate " +
              "arrangement of any kind. Odds are only ever read, never placed.",
            "Odds come from a metered provider with a free monthly allowance. When the allowance is " +
              "spent, or a quote is older than the freshness limit, the stored price is shown with " +
              "its age rather than removed - an old number with a date is honest; a missing row is not."
          ])
        ),
        section(
          "Matching, and where it can be wrong",
          paragraphs([
            "Two venues never word the same question identically, so titles are normalised (numbers " +
              "canonicalised, punctuation dropped) and scored, with dates and named entities checked " +
              "as well. Sports pairings additionally need agreeing teams and a start time within a " +
              "day, and only winner markets are paired.",
            "The score is stored. Anything below 0.85 is a possible match: visible, labelled, and " +
              "excluded from the gap. Matching is the weakest link in this pipeline and is treated " +
              "as such - a wrong gap would be worse than no gap.",
            "A perfect score is not a promise that two markets settle the same way. When their rule " +
              "wording differs, the pairing is flagged <strong>rules differ</strong> and the gap is " +
              "still shown, with the flag attached."
          ])
        ),
        section("Definitions", definitions()),
        section(
          "Data sources and terms",
          paragraphs([
            "Read-only public APIs only, with no login and no account: Kalshi's trade API, " +
              "Polymarket's Gamma and CLOB APIs, and Manifold's public API. Sportsbook prices come " +
              "from The Odds API. Responses are cached, requests are paced inside each venue's " +
              "published limits, and no venue is scraped outside its API.",
            "PredictIt and Metaculus are not read: one forbids automated extraction in its terms and " +
              "the other requires an account token. Both are excluded deliberately rather than " +
              "silently. See <a href=\"venues.html\">Venues</a> for the read state of each source."
          ])
        ),
        section(
          "On this device",
          paragraphs([
            "The only thing this site stores in your browser is your Simple / Detailed preference, " +
              "in local storage. There is no account, no cookie, no analytics script, no tracker and " +
              "no third-party script of any kind.",
            "Prices are read from public endpoints and published as static files, so nothing you do " +
              "here is sent to a venue."
          ])
        ),
        section(
          "How to read the colours",
          paragraphs([
            "Green marks a comparison - the stretch between the lowest and highest comparable price. " +
              "Orange marks something that needs attention: a stale price, a possible match, a " +
              "source that failed to answer.",
            "Nothing else is coloured. There are no buy or sell buttons anywhere on this site, no " +
              "balances, no leaderboards, and no account to open."
          ])
        )
      ]) +
      "</div>";

    var notes = body.querySelectorAll("details[data-note]");
    for (var index = 0; index < notes.length; index += 1) {
      notes[index].open = S.mode() === "detailed";
    }
  }

  S.boot({ page: "notes", title: "Notes" }, render);
})();
