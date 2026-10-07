/* Home: the three widest gaps, one featured light card, and venue freshness.
 *
 * This file is the reference pattern for the other pages: read the document
 * handed over by SPREAD.start, build HTML with the base classes, escape every
 * value that came from an API, and never recompute a number the pipeline
 * already decided (gaps and staleness both arrive precomputed).
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function ranked(data) {
    return (data.events || [])
      .slice()
      .sort(function (a, b) {
        var left = a.gap == null ? -1 : Math.abs(a.gap);
        var right = b.gap == null ? -1 : Math.abs(b.gap);
        return right - left;
      });
  }

  function venueRows(event) {
    return event.venues
      .map(function (venue) {
        var stale = venue.is_stale;
        return (
          '<div class="venue-row">' +
          '<span class="venue-name">' + S.escapeHtml(S.venueLabel(venue.venue)) + "</span>" +
          '<span class="price' + (stale ? " price--stale" : "") + '">' +
          S.escapeHtml(S.price(venue.price)) +
          (stale ? ' <span class="stale-tag">stale</span>' : "") +
          "</span>" +
          '<span class="venue-vol">vol ' + S.escapeHtml(S.volume(venue.volume)) + "</span>" +
          "</div>"
        );
      })
      .join("");
  }

  function eventCard(event) {
    var spark = S.gapSeries(event);
    var sparkHtml = spark.length > 1
      ? S.sparkline(spark.map(function (point) { return point.gap; }))
      : '<p class="muted" style="font-size:12px">Gap history builds as runs accumulate.</p>';
    return (
      '<article class="card enter">' +
      '<p class="label label--muted">' + S.escapeHtml(event.category || "") + "</p>" +
      '<h2 class="card-title" style="margin:8px 0 16px">' +
      '<a href="' + S.eventUrl(event.id) + '" style="text-decoration:none">' +
      S.escapeHtml(event.title) + "</a></h2>" +
      venueRows(event) +
      '<div class="row row--between" style="margin-top:16px">' +
      '<span class="label">Gap</span>' +
      '<span class="gap ' + S.gapClass(event.gap) + '" style="font-size:20px">' +
      S.escapeHtml(S.gapText(event.gap)) + "</span>" +
      "</div>" +
      '<div style="margin-top:16px">' + sparkHtml + "</div>" +
      "</article>"
    );
  }

  function featuredCard(event) {
    return (
      '<article class="card card--light enter">' +
      '<p class="label"><span class="dot dot--live" style="margin-right:6px" aria-hidden="true"></span>Widest gap</p>' +
      '<h2 class="card-title" style="margin:16px 0 8px;font-size:22px;letter-spacing:-0.6px">' +
      S.escapeHtml(event.title) + "</h2>" +
      '<p class="muted" style="font-size:14px">' + S.escapeHtml(event.category || "") +
      " · gap " + S.escapeHtml(S.gapText(event.gap)) + "</p>" +
      '<div style="margin-top:24px"><a class="btn btn--on-light" href="' +
      S.eventUrl(event.id) + '">View event</a></div>' +
      "</article>"
    );
  }

  function freshnessCard(data) {
    var rows = (data.venues_status || [])
      .map(function (status) {
        var stale = status.last_success_at
          ? S.staleByClock(status.last_success_at)
          : true;
        return (
          '<div class="stat-row">' +
          '<span class="row" style="gap:8px"><span class="dot ' +
          (stale ? "dot--live" : "dot--fresh") + '" aria-hidden="true"></span>' +
          '<span>' + S.escapeHtml(S.venueLabel(status.venue)) + "</span></span>" +
          '<span class="mono muted">' + S.escapeHtml(S.timeAgo(status.last_success_at)) +
          " · " + S.escapeHtml(String(status.market_count)) + " markets</span>" +
          "</div>"
        );
      })
      .join("");
    return (
      '<section class="card" style="margin-top:24px">' +
      '<div class="card-head"><h2 class="card-title">Data freshness</h2>' +
      '<span class="label label--muted">Last successful read</span></div>' +
      (rows || '<p class="muted">No venue has been read yet.</p>') +
      '<p class="muted" style="margin-top:16px;font-size:12px">' +
      "Generated " + S.escapeHtml(S.timeAgo(data.generated_at)) +
      ". Kalshi publishes no last-trade time, so its freshness comes from 24 hour traded volume; " +
      "a green dot means the venue answered and its prices are inside the 24 hour window." +
      "</p></section>"
    );
  }

  function render(data) {
    var top = ranked(data).filter(function (event) { return event.gap != null; });
    var body = document.getElementById("home-body");
    if (!top.length) {
      body.innerHTML =
        '<div class="empty">No event currently has two fresh prices, so there is no gap to show. ' +
        "Check the Venues page for the last successful read.</div>" +
        freshnessCard(data);
      return;
    }
    body.innerHTML =
      '<div class="home-columns">' +
      '<div class="card-grid card-grid--3">' + top.slice(0, 3).map(eventCard).join("") + "</div>" +
      '<div class="home-aside">' + featuredCard(top[0]) + "</div>" +
      "</div>" +
      freshnessCard(data);
  }

  S.start({ page: "home", title: "Home" }, render);
})();
