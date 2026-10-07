/* Home: the three widest gaps, the freshness readout, and recent moves.
 *
 * No explanatory paragraphs live here by design - anything that needs saying
 * is a label with an "i" tip, or a collapsed note at the bottom of the page.
 * Every number comes from data/home.json; the gap and the freshness verdicts
 * are the pipeline's, never recomputed here.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function venueCount(prices) {
    var seen = {};
    var count = 0;
    for (var index = 0; index < prices.length; index += 1) {
      var venue = prices[index].venue;
      if (!seen[venue]) {
        seen[venue] = true;
        count += 1;
      }
    }
    return count;
  }

  function gapRow(gap) {
    var prices = gap.prices || [];
    var marks = [];
    var values = [];
    for (var index = 0; index < prices.length; index += 1) {
      if (prices[index].price != null) {
        marks.push(prices[index].price);
        values.push(prices[index].price);
      }
    }
    var row = S.eventRow(
      {
        i: gap.id,
        t: gap.title,
        c: gap.category,
        e: gap.close_time,
        g: gap.gap,
        // home.json carries the venue count the pipeline counted; the derived
        // one is only a fallback for a document written before it did.
        n: gap.venue_count || venueCount(prices),
        lo: values.length ? Math.min.apply(null, values) : null,
        hi: values.length ? Math.max.apply(null, values) : null,
        p: gap.possible,
        r: gap.rules_differ
      },
      { marks: marks }
    );
    return row;
  }

  function gapsCard(home) {
    var gaps = (home.top_gaps || []).slice(0, 3);
    if (!gaps.length) {
      return '<p class="muted">No event has two fresh real-money prices to compare right now.</p>';
    }
    var rows = "";
    for (var index = 0; index < gaps.length; index += 1) {
      rows += S.rowHtml(gapRow(gaps[index]));
    }
    return '<ul class="results-list">' + rows + "</ul>";
  }

  /**
   * The footnote under gap rows. It annotates the bar, so with no rows there is
   * nothing to annotate and it is not drawn - otherwise it floats between
   * sections as a stray line about bars that are not on the page.
   */
  function gapLegend(home) {
    if (!(home.top_gaps || []).length) {
      return "";
    }
    return (
      '<p class="label label--muted" style="margin-top:16px">Gap ' +
      S.tip(
        "Max minus min of fresh real-money prices, in points. One point is one cent of a one-dollar contract.",
        "How the gap is calculated"
      ) +
      " · a stale price is left out of the arithmetic</p>"
    );
  }

  function freshnessCard(home) {
    var statuses = home.venue_status || [];
    var rows = "";
    for (var index = 0; index < statuses.length; index += 1) {
      var status = statuses[index];
      var when = status.last_prices_ok_at || status.last_catalog_ok_at;
      var fresh = when ? !S.staleByClock(when) : false;
      var dot = when
        ? '<span class="dot ' + (fresh ? "dot--fresh" : "dot--live") + '" aria-hidden="true"></span> '
        : '<span class="dot dot--muted" aria-hidden="true"></span> ';
      // One line per venue, as the card promises. A venue that could not be
      // read says why in place of "not read yet" rather than adding an indented
      // second row of the provider's own error text.
      var detail = when
        ? S.num(status.priced) + " priced · read " + S.timeAgo(when)
        : S.venueTrouble(status) || "not read yet";
      rows +=
        '<div class="stat-row"><span class="row"><span class="mono">' +
        dot +
        S.esc(S.venueLabel(status.venue)) +
        '</span><span class="label label--muted">' +
        S.esc(S.venueTypeLabel(venueType(status.venue))) +
        '</span></span><span class="stat-value muted">' +
        S.esc(detail) +
        "</span></div>";
    }
    var quota = quotaLine(home);
    return (
      '<div class="card-head"><h2 class="card-title">Data freshness</h2>' +
      '<span class="label label--muted">One line per venue</span></div>' +
      (rows || '<p class="muted">No venue has been read yet.</p>') +
      quota
    );
  }

  function venueType(venue) {
    return venue === "kalshi" || venue === "polymarket"
      ? "prediction_market"
      : venue === "odds_api"
        ? "sportsbook"
        : "play_money";
  }

  /**
   * The provider's monthly credit count, and only when it has actually been
   * reported. This used to draw a second row for the sportsbook venue reading
   * "0 credits used; no reply from the provider yet" - the same venue the list
   * above already has, saying nothing a reader can use.
   */
  function quotaLine(home) {
    var quota = home.odds_quota || {};
    if (quota.remaining == null) {
      return "";
    }
    return (
      '<div class="stat-row"><span class="muted">Sportsbook odds quota</span>' +
      '<span class="stat-value muted">' +
      S.esc(S.num(quota.remaining) + " of 500 credits left this month") +
      "</span></div>"
    );
  }

  function movesList(home) {
    var moves = home.recent_moves || [];
    if (!moves.length) {
      return '<p class="muted">No price change was observed in the last 24 hours.</p>';
    }
    var rows = "";
    for (var index = 0; index < moves.length; index += 1) {
      var move = moves[index];
      rows +=
        '<li class="result"><span class="result-cat label">' +
        S.esc(S.venueLabel(move.venue)) +
        '</span><a class="result-title" href="' +
        S.esc(move.url || "#") +
        '" target="_blank" rel="noopener noreferrer">' +
        S.esc(move.title || move.market_id) +
        '</a><span class="result-count">' +
        S.esc(S.timeAgo(move.at)) +
        '</span><span class="result-bar muted">' +
        S.esc(S.num(move.from_price) + " → " + S.num(move.to_price)) +
        '</span><span class="result-gap">' +
        S.esc(S.signed(move.change) + " pts") +
        "</span></li>";
    }
    return '<ul class="results-list">' + rows + "</ul>";
  }

  function metrics(home) {
    var stats = home.stats || {};
    return (
      '<div class="metric-row">' +
      // `open_markets` is every open market in the catalog, so "compared" would
      // claim a comparison that has not happened.
      metric("Open markets", S.num(stats.open_markets)) +
      metric("Matched events", S.num(stats.matched_events)) +
      metric("Events with a gap", S.num(stats.events_with_gap)) +
      "</div>"
    );
  }

  function metric(label, value) {
    return (
      '<div class="metric"><span class="metric-label label">' +
      S.esc(label) +
      '</span><span class="metric-value">' +
      S.esc(value) +
      "</span></div>"
    );
  }

  function render(home) {
    var body = document.getElementById("page-body");
    if (!body) {
      return;
    }
    if (!home) {
      return;
    }
    body.innerHTML =
      '<div class="page-head">' +
      '<h1 class="headline">Where the same event is priced differently</h1>' +
      '<span class="label label--muted">Updated ' +
      S.esc(S.timeAgo(home.generated_at)) +
      "</span></div>" +
      metrics(home) +
      '<section class="stack" style="margin-top:40px">' +
      '<div class="card">' +
      '<div class="card-head"><h2 class="card-title">Widest gaps</h2>' +
      '<a class="label" href="search.html">All events</a></div>' +
      gapsCard(home) +
      gapLegend(home) +
      "</div>" +
      '<div class="card">' +
      freshnessCard(home) +
      "</div>" +
      '<div class="card"><div class="card-head"><h2 class="card-title">Recent moves</h2>' +
      '<span class="label label--muted">Last 24 hours</span></div>' +
      movesList(home) +
      "</div>" +
      "</section>" +
      S.notes([
        {
          title: "How this is calculated",
          body:
            "<p>The gap is the highest fresh real-money price minus the lowest, in points. " +
            "Only prediction markets and de-vigged sportsbook prices count; a play-money or " +
            "forecast price is shown beside them and never enters the arithmetic.</p>" +
            "<p>A price that cannot be shown to be from the last 24 hours is left out. One fresh " +
            "venue is not a gap - it is a single number. See <a href=\"notes.html\">Notes</a> for " +
            "the whole method.</p>"
        },
        {
          title: "Data freshness",
          body:
            "<p>Each venue is read on the pipeline's own cadence: the full catalog every six " +
            "hours, prices every thirty minutes. A venue's freshness is judged from the best " +
            "evidence it publishes - Kalshi publishes no trade time, so its verdict comes from " +
            "24 hour traded volume instead.</p>"
        },
        {
          title: "Sources",
          body:
            "<p>Read-only public APIs: Kalshi's trade API, Polymarket's Gamma and CLOB APIs, and " +
            "Manifold's public API. Sportsbook prices come from The Odds API and are shown as " +
            "de-vigged implied probabilities. The <a href=\"venues.html\">Venues</a> page lists " +
            "when each was last read.</p>"
        }
      ]);

    var links = body.querySelectorAll("details[data-note]");
    for (var index = 0; index < links.length; index += 1) {
      links[index].open = S.mode() === "detailed";
    }
  }

  S.boot(
    {
      page: "home",
      title: "Home",
      documentTitle: "Spread — where the same event is priced differently"
    },
    render
  );
})();
