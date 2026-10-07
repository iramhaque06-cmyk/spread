/* Movers: the largest observed price changes in the last 24 hours.
 *
 * Reads `data.moves`, which the pipeline derives from the price snapshots it
 * has recorded (plus the one-time Polymarket history backfill), sorted by the
 * size of the change. Nothing is recomputed here: the change shown is
 * to_price - from_price of the window, and the unit is points because every
 * price on this site is in cents.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function eventFor(data, id) {
    var found = null;
    (data.events || []).forEach(function (event) {
      if (!found && event.id === id) {
        found = event;
      }
    });
    return found;
  }

  function eventCell(data, move) {
    var event = eventFor(data, move.event_id);
    // A move whose event has dropped out of the comparison still gets listed,
    // by its stored id, rather than being hidden or linked to nothing.
    if (!event) {
      return '<span class="muted">' + S.escapeHtml(move.event_id) + "</span>";
    }
    return (
      '<a href="' + S.eventUrl(move.event_id) + '">' + S.escapeHtml(event.title) + "</a>"
    );
  }

  function changeCell(move) {
    var change = move.to_price - move.from_price;
    var kind = change > 0 ? "gap--pos" : change < 0 ? "gap--neg" : "gap--none";
    return (
      '<span class="' + kind + '">' + S.escapeHtml(S.signed(change)) + "</span>" +
      '<span class="muted"> pts</span>'
    );
  }

  function row(data, move) {
    return (
      "<tr class=\"enter\">" +
      "<td>" + eventCell(data, move) + "</td>" +
      '<td class="muted">' + S.escapeHtml(S.venueLabel(move.venue)) + "</td>" +
      '<td class="num mono">' + S.escapeHtml(S.price(move.from_price)) + "</td>" +
      '<td class="num mono">' + S.escapeHtml(S.price(move.to_price)) + "</td>" +
      '<td class="num">' + changeCell(move) + "</td>" +
      '<td class="num mono">' + S.escapeHtml(S.timeAgo(move.at)) +
      '<span class="muted" style="display:block">' + S.escapeHtml(S.dateShort(move.at)) + "</span></td>" +
      "</tr>"
    );
  }

  function render(data) {
    var moves = data.moves || [];
    var body = document.getElementById("movers-body");
    if (!moves.length) {
      body.innerHTML =
        '<div class="empty">No price changes have been observed in the last 24 hours yet. ' +
        "Price history accumulates as the pipeline runs, so this page fills in over time." +
        "</div>";
      return;
    }
    body.innerHTML =
      '<div class="row row--between" style="margin-bottom:16px">' +
      '<span class="label label--muted">Largest change first, last 24 hours</span>' +
      '<span class="label label--muted">' + S.escapeHtml(String(moves.length)) +
      (moves.length === 1 ? " move" : " moves") + "</span>" +
      "</div>" +
      '<div class="scroll-x"><table class="table">' +
      "<thead><tr>" +
      '<th scope="col">Event</th>' +
      '<th scope="col">Venue</th>' +
      '<th scope="col" style="text-align:right">From</th>' +
      '<th scope="col" style="text-align:right">To</th>' +
      '<th scope="col" style="text-align:right">Change</th>' +
      '<th scope="col" style="text-align:right">Time</th>' +
      "</tr></thead><tbody>" +
      moves.map(function (move) { return row(data, move); }).join("") +
      "</tbody></table></div>" +
      '<p class="muted" style="margin-top:24px;font-size:12px">' +
      "A move compares the first and last observed price for a market inside the 24 hour window, " +
      "so a quiet market simply does not appear. Prices are in cents, changes in points. " +
      "Kalshi publishes no last-trade time, so its freshness comes from 24 hour traded volume; " +
      "Polymarket's timestamps come from its public price history." +
      "</p>";
  }

  S.start({ page: "movers", title: "Movers" }, render);
})();
