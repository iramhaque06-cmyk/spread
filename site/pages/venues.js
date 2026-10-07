/* Venues: one card per market this comparison reads.
 *
 * The status dot is driven by the last successful read, not by a price
 * timestamp: green means we read that venue inside the last 24 hours, orange
 * means the last read is older than that or missing. What each venue's own
 * "updated" stamp means is different, so each card says which kind it is
 * rather than implying both are last-trade times.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  var SITES = {
    kalshi: "https://kalshi.com",
    polymarket: "https://polymarket.com"
  };

  var TIMESTAMP_NOTES = {
    kalshi:
      "Publishes no last-trade time. The stamp on a Kalshi price is its " +
      "market-record time, which can look old on a market that traded heavily " +
      "today, so freshness is judged from 24 hour traded volume instead.",
    polymarket:
      "Price timestamps come from the public CLOB price history, which is " +
      "per market. Gamma's own updated stamp is a batch value shared by whole " +
      "sets of markets and is not used to date a price."
  };

  function statusCard(status) {
    var fresh = status.last_success_at ? !S.staleByClock(status.last_success_at) : false;
    var site = SITES[status.venue];
    return (
      '<article class="card enter">' +
      '<div class="card-head">' +
      '<h2 class="card-title row" style="gap:8px">' +
      '<span class="dot ' + (fresh ? "dot--fresh" : "dot--live") + '" aria-hidden="true"></span>' +
      S.escapeHtml(S.venueLabel(status.venue)) + "</h2>" +
      '<span class="label ' + (fresh ? "label--muted" : "label--strong") + '">' +
      (fresh ? "Fresh read" : "Read overdue") + "</span>" +
      "</div>" +
      '<div class="stat-row"><span class="muted">Last successful read</span>' +
      '<span class="mono">' + S.escapeHtml(S.timeAgo(status.last_success_at)) +
      ' <span class="muted">· ' + S.escapeHtml(S.dateShort(status.last_success_at)) +
      "</span></span></div>" +
      '<div class="stat-row"><span class="muted">Markets tracked in this comparison</span>' +
      '<span class="mono">' + S.escapeHtml(String(status.market_count)) + "</span></div>" +
      '<p class="muted" style="margin-top:16px;font-size:14px">' +
      S.escapeHtml(status.notes || "") + "</p>" +
      '<p class="muted" style="margin-top:8px;font-size:14px">' +
      S.escapeHtml(TIMESTAMP_NOTES[status.venue] || "") + "</p>" +
      (site
        ? '<div style="margin-top:24px"><a class="btn btn--ghost" href="' + site +
          '" target="_blank" rel="noopener">Open ' + S.escapeHtml(S.venueLabel(status.venue)) +
          " site</a></div>"
        : "") +
      "</article>"
    );
  }

  function render(data) {
    var statuses = data.venues_status || [];
    var body = document.getElementById("venues-body");
    if (!statuses.length) {
      body.innerHTML =
        '<div class="empty">No venue has been read yet. Run the pipeline and reload.</div>';
      return;
    }
    // Two venues, so auto-fit keeps them side by side when there is room and
    // stacked when there is not, instead of leaving an empty third column.
    body.innerHTML =
      '<div class="card-grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">' +
      statuses.map(statusCard).join("") + "</div>" +
      '<p class="muted" style="margin-top:24px;font-size:14px;max-width:80ch">' +
      "Market counts are the paired markets this project tracks for the comparison, " +
      "not a venue's whole catalogue: each venue lists far more markets than are matched here. " +
      "Prices, volumes and timestamps are read from public, read-only, keyless APIs, cached " +
      "between runs, and every number on this site is labelled with the venue it came from." +
      "</p>" +
      '<section class="card" style="margin-top:24px">' +
      '<div class="card-head"><h2 class="card-title">How each venue dates a price</h2>' +
      '<span class="label label--muted">Timestamp semantics</span></div>' +
      '<div class="stat-row"><span class="mono">Kalshi</span>' +
      '<span class="muted" style="max-width:60ch;text-align:right">No last-trade time published; ' +
      "freshness from 24 hour traded volume</span></div>" +
      '<div class="stat-row"><span class="mono">Polymarket</span>' +
      '<span class="muted" style="max-width:60ch;text-align:right">Last price change from the ' +
      "public CLOB price history</span></div>" +
      '<p class="muted" style="margin-top:16px;font-size:12px">' +
      "Both venues also return a record update stamp, which this site shows but never uses to " +
      "judge age, because it does not mark the moment a price moved." +
      "</p></section>";
  }

  S.start({ page: "venues", title: "Venues" }, render);
})();
