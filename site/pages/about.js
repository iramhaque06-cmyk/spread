/* About: what Spread is, what it is not, who it is for, and the disclosures.
 *
 * Every sentence a reader does not need on arrival lives in a collapsed
 * <details> (SPREAD.notes), so the page itself is a card, four lines and four
 * closed headers. The three statements that must always be true - the caveat,
 * the informational notice and the age restriction - are not written out here;
 * they are read from SPREAD, which is the one place they are defined.
 *
 * render() is called again whenever the reader switches Simple / Detailed, so
 * it is written to run twice without side effects.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function line(name, value) {
    return (
      '<div class="stat-row"><span class="label label--muted">' +
      S.esc(name) +
      '</span><span class="stat-value muted">' +
      S.esc(value) +
      "</span></div>"
    );
  }

  function render() {
    var body = document.getElementById("page-body");
    if (!body) {
      return;
    }

    body.innerHTML =
      '<div class="page-head">' +
      '<h1 class="headline">About</h1>' +
      '<span class="label label--muted">Read-only public prices · informational only</span>' +
      "</div>" +

      // The lockup is drawn in the brand's cream, so it sits on the canvas:
      // on the light card it would be cream on bone.
      '<img src="assets/spread-horizontal.svg" alt="Spread" ' +
      'style="height:34px;width:auto;margin-bottom:24px">' +
      // The single light card: the site's only bright object, carrying the two
      // sentences that say what this is.
      '<section class="card card--light">' +
      '<p>The same real-world event, priced on several venues at once.</p>' +
      '<p style="margin-top:8px">Where they disagree, the difference is shown as a gap in points.</p>' +
      "</section>" +

      '<section class="card" style="margin-top:24px">' +
      '<div class="card-head"><h2 class="card-title">In one line</h2></div>' +
      line("What it is", "A read-only comparison of public prices") +
      line("What it is not", "Not advice, not a trading venue, not a bet a gap is tradeable") +
      line("Who it is for", "Anyone curious about where venues disagree") +
      line("What it reads", "Public market APIs; no accounts, no keys, no trading") +
      "</section>" +

      '<section style="margin-top:24px">' +
      S.notes([
        {
          title: "Sources",
          body:
            "<p>Read-only public market data only: Kalshi's trade API, Polymarket's Gamma and " +
            "CLOB APIs, Manifold's public API, and The Odds API for sportsbook odds. No accounts, " +
            "no logins, and no trading endpoints are touched.</p>" +
            "<p>Bookmaker names appear as plain data. There are no links to bookmakers, no " +
            "promotional wording, and no affiliate links anywhere on this site.</p>"
        },
        {
          title: "How this is calculated",
          body:
            "<p>The gap is the highest fresh real-money price minus the lowest, in points. Only " +
            "prediction markets and de-vigged sportsbook prices count. A play-money or forecast " +
            "price is shown in its own section, labelled <strong>Not comparable to real-money " +
            "prices</strong>, and never enters the gap.</p>" +
            "<p>A price that cannot be shown to be from the last 24 hours is left out of the " +
            "arithmetic. Sportsbook prices are de-vigged implied probabilities - the bookmaker's " +
            "margin is removed before they are compared - so the raw figure and the margin are " +
            "shown only inside the details on an event page.</p>"
        },
        {
          title: "Data freshness",
          body:
            "<p>The full catalog is rebuilt every six hours; prices are refreshed every thirty " +
            "minutes. A price older than 24 hours is labelled stale and left out of the gap.</p>" +
            "<p>Kalshi publishes no trade time, so its freshness is judged from 24 hour traded " +
            "volume instead. If sportsbook odds are older than the freshness limit, or the " +
            "monthly quota is used up, the age is shown rather than the row being hidden.</p>"
        },
        {
          title: "Notes and caveats",
          body:
            "<p>" + S.esc(S.CAVEAT) + "</p>" +
            "<p>" + S.esc(S.INFORMATIONAL) + "</p>" +
            "<p>" + S.esc(S.ADULT) + "</p>" +
            "<p>Prices are quoted in points, where one point is one cent of a one-dollar " +
            "contract. Nothing here is an offer to buy or sell anything.</p>"
        }
      ]) +
      "</section>";

    // A section on this page may need its spacing rhythm adjusted by mode, so
    // the notes are re-opened from the current mode rather than assumed.
    var details = body.querySelectorAll("details[data-note]");
    for (var index = 0; index < details.length; index += 1) {
      details[index].open = S.mode() === "detailed";
    }
  }

  S.boot({ page: "about", title: "About" }, render);
})();
