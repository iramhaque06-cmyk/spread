/* About: what Spread is, and the three statements that must always be true. */
(function () {
  "use strict";

  var S = window.SPREAD;

  function render(data) {
    var body = document.getElementById("about-body");
    var events = (data.events || []).length;
    var venues = (data.venues_status || []).length;

    body.innerHTML =
      '<div class="split">' +
      '<div class="stack">' +
      '<section class="card">' +
      '<img class="brand-lockup" src="assets/spread-horizontal.svg" alt="Spread" width="232" height="59">' +
      '<p class="label">What Spread is</p>' +
      '<p style="margin-top:12px;max-width:72ch">' +
      "Spread compares the prices that two prediction markets put on the same economic event, and " +
      "shows where they disagree. It currently tracks " +
      S.escapeHtml(String(events)) + " events across " + S.escapeHtml(String(venues)) +
      " venues, reading only public, read-only market data - no accounts, no keys, and no trading. " +
      "Every number is labelled with the venue it came from, and a price that cannot be shown to be " +
      "fresher than 24 hours old is greyed out and dropped from the gap." +
      "</p>" +
      '<p style="margin-top:12px;max-width:72ch">' +
      "It is a prototype: a small, readable experiment in putting two venues side by side, not a " +
      "product and not a signal." +
      "</p>" +
      "</section>" +
      '<section class="card">' +
      '<p class="label">Informational only</p>' +
      '<p style="margin-top:12px;max-width:72ch">' + S.escapeHtml(S.INFORMATIONAL) + "</p>" +
      "</section>" +
      '<section class="card">' +
      '<p class="label">Age restriction</p>' +
      '<p style="margin-top:12px;max-width:72ch">' + S.escapeHtml(S.ADULT) + "</p>" +
      "</section>" +
      "</div>" +
      '<div class="stack">' +
      '<section class="card card--light">' +
      '<p class="label">Nothing here is advice</p>' +
      '<p style="margin-top:12px;font-size:14px;line-height:1.5">' +
      "Nothing on this site is financial advice, a recommendation, or an offer to buy or sell " +
      "anything. Prices are shown so the two venues can be compared, not so that anyone acts on the " +
      "difference." +
      "</p></section>" +
      '<section class="card">' +
      '<p class="label">Where to go next</p>' +
      '<div class="row" style="margin-top:12px">' +
      '<a class="btn btn--ghost" href="markets.html">Browse markets</a>' +
      '<a class="btn btn--ghost" href="methodology.html">Methodology</a>' +
      "</div></section>" +
      "</div></div>";
  }

  S.start({ page: "about", title: "About" }, render);
})();
