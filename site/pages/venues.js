/* Venues: what the pipeline reads, when it last succeeded, and what it could
 * not get.
 *
 * The page is a readout, not a pitch. Every value comes from data/home.json's
 * venue_status and odds_quota, which the pipeline writes from its own runs, and
 * a read that never happened says so in words ("not read yet") instead of a
 * dash that hides it. A stored error is shown, because a venue failing quietly
 * is the one thing a reader cannot see anywhere else.
 *
 * Bookmaker names, where they appear, are plain data: no links, no promotional
 * wording, no affiliate anything.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  /** The pipeline's venue_type for a venue, for the type label. */
  function venueType(venue) {
    if (venue === "kalshi" || venue === "polymarket") {
      return "prediction_market";
    }
    if (venue === "odds_api") {
      return "sportsbook";
    }
    return "play_money";
  }

  /** A read time, or the words that say it never happened. */
  function readTime(stamp) {
    if (!stamp) {
      return "not read yet";
    }
    return S.timeAgo(stamp) + " (" + S.dateMed(stamp) + ")";
  }

  /** One fact, as the right-hand value of a stat row. */
  function row(label, value, options) {
    options = options || {};
    return (
      '<div class="stat-row"><span class="muted">' +
      S.esc(label) +
      '</span><span class="stat-value' +
      (options.muted ? " muted" : "") +
      '">' +
      value +
      "</span></div>"
    );
  }

  function venueCard(status) {
    var when = status.last_prices_ok_at || status.last_catalog_ok_at;
    var fresh = when ? !S.staleByClock(when) : false;
    var dot = when
      ? '<span class="dot ' +
        (fresh ? "dot--fresh" : "dot--live") +
        '" aria-hidden="true"></span> '
      : '<span class="dot dot--muted" aria-hidden="true"></span> ';
    var rows =
      row("Markets stored", S.esc(S.num(status.markets))) +
      row("Open now", S.esc(S.num(status.open))) +
      row("Priced", S.esc(S.num(status.priced))) +
      row("Full catalog read", S.esc(readTime(status.last_catalog_ok_at))) +
      row("Prices read", S.esc(readTime(status.last_prices_ok_at)));
    // The provider's own wording stays in the pipeline log. A reader gets the
    // fact - this venue is not being read - in words, and the "Last run" row
    // ("listed=0 closed=0 in 0.0s") is gone: a run that did nothing is not a
    // statistic.
    var trouble = S.venueTrouble(status);
    if (trouble) {
      rows +=
        '<div class="stat-row"><span class="muted">Last read</span>' +
        '<span class="stat-value muted">' +
        S.esc(trouble) +
        "</span></div>";
    }
    return (
      '<section class="card"><div class="card-head"><h2 class="card-title">' +
      dot +
      S.esc(S.venueLabel(status.venue)) +
      '</h2><span class="label label--muted">' +
      S.esc(S.venueTypeLabel(venueType(status.venue))) +
      "</span></div>" +
      rows +
      "</section>"
    );
  }

  function quotaCard(home) {
    var quota = home.odds_quota || {};
    var statuses = home.venue_status || [];
    var books = null;
    for (var index = 0; index < statuses.length; index += 1) {
      if (statuses[index].venue === "odds_api") {
        books = statuses[index];
      }
    }
    var rows =
      row(
        "Credits left this month",
        quota.remaining == null ? "not reported yet" : S.esc(S.num(quota.remaining)),
        { muted: quota.remaining == null }
      ) +
      row("Credits used this month", S.esc(S.num(quota.used))) +
      row("Odds calls this month", S.esc(S.num(quota.calls))) +
      row(
        "Last odds call",
        quota.last_call_at ? S.esc(S.timeAgo(quota.last_call_at)) : "never"
      );
    var why = S.venueTrouble(books) || "";
    var standing = books && books.markets
      ? S.num(books.markets) + " bookmaker prices stored"
      : "No sportsbook price is stored right now.";
    return (
      '<section class="card"><div class="card-head"><h2 class="card-title">Sportsbook odds' +
      "</h2><span class=\"label label--muted\">The Odds API</span></div>" +
      rows +
      '<p class="label label--muted" style="margin-top:12px">' +
      S.esc(standing) +
      (why ? " · " + S.esc(why) : "") +
      " " +
      S.tip(
        "The free tier is 500 credits a month and the pipeline stops calling before 80% is used. Sportsbook prices are shown as de-vigged implied probabilities, and a stale or missing price keeps showing its age rather than disappearing."
      ) +
      "</p></section>"
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
    var statuses = (home.venue_status || []).slice();
    statuses.sort(function (left, right) {
      var leftKnown = left.venue === "kalshi" ? 0 : 1;
      var rightKnown = right.venue === "kalshi" ? 0 : 1;
      if (leftKnown !== rightKnown) {
        return leftKnown - rightKnown;
      }
      return left.venue < right.venue ? -1 : left.venue > right.venue ? 1 : 0;
    });
    var cards = "";
    for (var index = 0; index < statuses.length; index += 1) {
      cards += venueCard(statuses[index]);
    }

    body.innerHTML =
      '<div class="page-head">' +
      '<h1 class="headline">Venues</h1>' +
      '<span class="label label--muted">Last build ' +
      S.esc(S.timeAgo(home.generated_at)) +
      "</span></div>" +
      '<div class="grid grid--2">' +
      (cards || '<div class="empty">No venue has been read yet.</div>') +
      quotaCard(home) +
      "</div>" +
      S.notes([
        {
          title: "Data freshness",
          body:
            "<p>The full catalog is listed every six hours and prices are re-read every thirty " +
            "minutes. A venue's freshness is judged from the best evidence it publishes: " +
            "Manifold and Polymarket time their own last price change, and Kalshi publishes no " +
            "trade time at all, so its verdict comes from 24 hour traded volume.</p>" +
            "<p>A price older than 24 hours stays on the page with its age - it is left out of " +
            "the arithmetic, never hidden.</p>"
        },
        {
          title: "Sources",
          body:
            "<p>Every price is read from a read-only public API: Kalshi's trade API, " +
            "Polymarket's Gamma and CLOB APIs, and Manifold's public API. Sportsbook prices " +
            "come from The Odds API and are shown as de-vigged implied probabilities, with the " +
            "raw odds and the book's margin kept in the collapsed detail on an event.</p>" +
            "<p>Bookmaker names are shown as data. There are no links to a bookmaker, no " +
            "promotional wording and no affiliate links anywhere on this site.</p>"
        },
        {
          title: "How this is calculated",
          body:
            "<p>\"Priced\" counts the markets that currently carry a price the pipeline could " +
            "read; a market with no published price is stored and shown but cannot enter a " +
            "gap. \"Open now\" counts the markets the venue still lists as open.</p>" +
            "<p>One refresh pass is a handful of requests a few times an hour, paced by a " +
            "per-host limiter and served from a local cache. Nothing here logs in, trades, or " +
            "reads a private endpoint.</p>"
        }
      ]);

    var notes = body.querySelectorAll("details[data-note]");
    for (var each = 0; each < notes.length; each += 1) {
      notes[each].open = S.mode() === "detailed";
    }
  }

  S.boot({ page: "venues", title: "Venues" }, render);
})();
