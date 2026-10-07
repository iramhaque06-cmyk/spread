/* Event detail: one event, both venues, the chart, the rules, the stats.
 *
 * Everything numeric comes from data.json. The gap is not recomputed here -
 * the pipeline decides it, excluding any stale venue - so this page can never
 * disagree with the Markets page about what the gap is.
 *
 * Wording discipline, because it is a factual claim: a price change is a
 * "price change", never a "trade"; Kalshi's updated_at is its market RECORD
 * STAMP, not a trade time; and a price can be current while that stamp looks
 * old, because Kalshi's freshness is judged from 24 hour traded volume.
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  function requestedId() {
    var match = /[?&]id=([^&]*)/.exec(window.location.search || "");
    if (!match) {
      return "";
    }
    try {
      return decodeURIComponent(match[1].replace(/\+/g, " "));
    } catch (error) {
      return match[1];
    }
  }

  function historyStamps(event) {
    var stamps = [];
    Object.keys(event.history || {}).forEach(function (venue) {
      (event.history[venue] || []).forEach(function (point) {
        var stamp = Date.parse(point.t);
        if (!isNaN(stamp)) {
          stamps.push(stamp);
        }
      });
    });
    return stamps;
  }

  function firstSeen(event) {
    var stamps = historyStamps(event);
    if (!stamps.length) {
      return null;
    }
    return new Date(Math.min.apply(null, stamps)).toISOString();
  }

  function lastUpdate(event) {
    var stamps = (event.venues || [])
      .map(function (venue) {
        return Date.parse(venue.updated_at);
      })
      .filter(function (stamp) {
        return !isNaN(stamp);
      });
    if (!stamps.length) {
      return null;
    }
    return new Date(Math.max.apply(null, stamps)).toISOString();
  }

  function combinedVolume(event) {
    var total = 0;
    var counted = false;
    (event.venues || []).forEach(function (venue) {
      if (venue.volume != null) {
        total += Number(venue.volume);
        counted = true;
      }
    });
    return counted ? total : null;
  }

  function header(event) {
    var gapHtml = event.gap == null
      ? '<span class="event-gap-value gap gap--none">—</span>' +
        '<span class="muted" style="font-size:12px">Not comparable: fewer than two fresh venues</span>'
      : '<span class="event-gap-value gap ' + S.gapClass(event.gap) + '">' +
        S.escapeHtml(S.gapText(event.gap)) + "</span>" +
        '<span class="muted" style="font-size:12px">max − min of fresh venues</span>';

    return (
      '<div class="event-head">' +
      '<div class="event-head-main">' +
      '<p class="label">' + S.escapeHtml(event.category || "Economic data") + "</p>" +
      '<h1 class="title" style="margin:8px 0">' + S.escapeHtml(event.title) + "</h1>" +
      '<p class="subhead">' + S.escapeHtml(S.resolvesIn(event.resolves_at)) +
      (event.resolves_at ? " · " + S.escapeHtml(S.dateShort(event.resolves_at)) : "") +
      " · " + S.escapeHtml(event.status === "resolved" ? "Resolved" : "Active") +
      "</p></div>" +
      '<div class="event-gap"><span class="label">Gap · points</span>' + gapHtml + "</div>" +
      "</div>"
    );
  }

  function chartCard(event) {
    var chart = S.lineChart(event);
    var legend = S.legend(event.venues);
    return (
      '<section class="card">' +
      '<div class="card-head"><h2 class="card-title">Price history</h2>' +
      '<span class="label label--muted">Observed prices, in points</span></div>' +
      chart + legend +
      '<p class="chart-note">Stretches dashed in the line are older than 24 hours, so a ' +
      "line never implies a current price where none was observed. Polymarket's series is " +
      "seeded from a one-time read of its public price history; Kalshi publishes no price " +
      "history, so its series begins with this project's first run and stays short until " +
      "more runs accumulate.</p>" +
      "</section>"
    );
  }

  function venueRow(event, venue) {
    var stale = venue.is_stale;
    var isKalshi = venue.venue === "kalshi";
    var stampWord = isKalshi ? "market record stamp" : "last price change";
    var lines = [
      '<div class="venue-top">' +
      '<span><span class="label label--muted">' +
      S.escapeHtml(S.venueLabel(venue.venue)) + "</span>" +
      '<span class="venue-price' + (stale ? " price--stale" : "") + '" style="display:block">' +
      S.escapeHtml(S.price(venue.price)) + "</span></span>" +
      (stale ? '<span class="stale-tag">stale</span>' : "") +
      "</div>",
      '<p class="venue-note">volume ' + S.escapeHtml(S.volume(venue.volume)) +
        " · updated " + S.escapeHtml(S.timeAgo(venue.updated_at)) +
        " (" + S.escapeHtml(S.dateShort(venue.updated_at)) + ")" +
        " · " + S.escapeHtml(stampWord) + "</p>"
    ];

    if (isKalshi) {
      lines.push(
        '<p class="venue-note">Kalshi publishes no last-trade time, so its freshness is judged ' +
        "from 24 hour traded volume rather than this stamp.</p>"
      );
      if (!stale && S.staleByClock(venue.updated_at)) {
        lines.push(
          '<p class="venue-note">This price counts as current even though the stamp above looks ' +
          "old: traded volume shows a contract changed hands inside the 24 hour window.</p>"
        );
      }
    } else {
      lines.push(
        '<p class="venue-note">Taken from the venue\'s public price history, which times this ' +
        "market's last price change. The venue's own updated stamp is a batch value shared by " +
        "many markets and is not shown here.</p>"
      );
    }

    if (stale) {
      lines.push(
        '<p class="venue-note">Excluded from the gap: its price cannot be shown to be fresher ' +
        "than 24 hours.</p>"
      );
    }

    lines.push(
      '<a class="btn btn--on-light" href="' + S.escapeHtml(venue.url) + '" target="_blank" ' +
      'rel="noopener">View on ' + S.escapeHtml(S.venueLabel(venue.venue)) + "</a>"
    );

    return '<div class="venue-block">' + lines.join("") + "</div>";
  }

  function venuesCard(event) {
    return (
      '<section class="card card--light">' +
      '<div class="card-head"><h2 class="card-title">Venue prices</h2>' +
      '<span class="label">Read-only</span></div>' +
      (event.venues || []).map(function (venue) {
        return venueRow(event, venue);
      }).join("") +
      '<p class="venue-note" style="margin-top:16px">Prices are quoted in points, where one ' +
      "point is one cent of a one-dollar contract. Volume units differ by venue.</p>" +
      "</section>"
    );
  }

  function rulesCard(event) {
    var differ = !!event.rules_differ;
    var flag = differ
      ? '<span class="label label--strong rule-flag"><span class="dot dot--live" ' +
        'aria-hidden="true"></span>Rules differ</span>'
      : '<span class="label label--muted">Wording matches</span>';
    return (
      '<section class="card" style="margin-top:24px">' +
      '<div class="card-head"><h2 class="card-title">Resolution rules</h2>' + flag + "</div>" +
      (event.venues || []).map(function (venue) {
        return (
          '<div class="rule-block">' +
          '<p class="label label--strong">' + S.escapeHtml(S.venueLabel(venue.venue)) + "</p>" +
          (venue.rule_text
            ? '<p class="rule-text">' + S.escapeHtml(venue.rule_text) + "</p>"
            : '<p class="muted">No rule text was returned for this market.</p>') +
          "</div>"
        );
      }).join("") +
      '<p class="muted" style="margin-top:8px;font-size:12px;line-height:1.5">' +
      (differ
        ? "These two rules are not the same, so the same outcome can settle differently on each venue."
        : "Matching wording is not a guarantee: each venue settles from its own source, and fees, liquidity and resolution timing can still differ.") +
      "</p></section>"
    );
  }

  function statsCard(event) {
    var venues = event.venues || [];
    var seen = firstSeen(event);
    var total = combinedVolume(event);
    var rows = [
      ["Venues", venues.map(function (item) {
        return S.venueLabel(item.venue);
      }).join(" · ") || "—"],
      ["First seen", seen
        ? S.dateShort(seen) + " (" + S.timeAgo(seen) + ")"
        : "no history recorded yet"],
      ["Resolves", event.resolves_at
        ? S.dateShort(event.resolves_at) + " · " + S.resolvesIn(event.resolves_at)
        : "resolution date unknown"],
      ["Combined volume", total == null ? "—" : S.volume(total) + " contracts and shares"]
    ];
    return (
      '<section class="card" style="margin-top:24px">' +
      '<div class="card-head"><h2 class="card-title">Market stats</h2>' +
      '<span class="label label--muted">From stored observations</span></div>' +
      rows.map(function (row) {
        return (
          '<div class="stat-row"><span class="muted">' + S.escapeHtml(row[0]) + "</span>" +
          '<span class="stat-value">' + S.escapeHtml(row[1]) + "</span></div>"
        );
      }).join("") +
      '<p class="muted" style="margin-top:12px;font-size:12px">Combined volume adds the two ' +
      "venues' figures even though their units differ (Kalshi reports contracts, Polymarket " +
      "reports shares), so treat it as indicative, not as a like-for-like total.</p>" +
      "</section>"
    );
  }

  function tiles(event) {
    var venueCount = (event.venues || []).length;
    var fresh = (event.venues || []).filter(function (item) {
      return !item.is_stale;
    }).length;
    var total = combinedVolume(event);
    var last = lastUpdate(event);
    var gapTile = event.gap == null
      ? { value: "—", className: "gap--none" }
      : { value: S.gapText(event.gap), className: S.gapClass(event.gap) };
    return (
      '<section class="card tiles">' +
      '<div class="metric-row">' +
      '<div class="metric"><span class="metric-label label">Gap</span>' +
      '<span class="metric-value gap ' + gapTile.className + '">' +
      S.escapeHtml(gapTile.value) + "</span></div>" +
      '<div class="metric"><span class="metric-label label">Combined volume</span>' +
      '<span class="metric-value">' + S.escapeHtml(total == null ? "—" : S.volume(total)) + "</span></div>" +
      '<div class="metric"><span class="metric-label label">Venues</span>' +
      '<span class="metric-value">' + S.escapeHtml(String(venueCount)) +
      '<span class="muted" style="font-size:14px"> fresh ' + S.escapeHtml(String(fresh)) + "</span></span></div>" +
      '<div class="metric"><span class="metric-label label">Last update</span>' +
      '<span class="metric-value">' + S.escapeHtml(last ? S.timeAgo(last) : "—") + "</span></div>" +
      "</div>" +
      '<p class="muted" style="margin-top:16px;font-size:12px">"Last update" is the newest ' +
      "timestamp across venues. For Kalshi that timestamp is its market record stamp, not a " +
      "trade time; the freshness verdict comes from traded volume instead.</p>" +
      "</section>"
    );
  }

  function howToRead() {
    return (
      '<section class="card how-to-read">' +
      '<p class="label">How to read this</p>' +
      '<p class="muted" style="margin-top:8px">Fees, liquidity and resolution rules may ' +
      "differ between venues. A gap between two prices is not a tradeable spread: each venue " +
      "charges its own fees, offers its own depth, and settles from its own source. Both " +
      "markets are read-only public data, and nothing on this page is financial advice or an " +
      "offer to buy or sell anything.</p>" +
      "</section>"
    );
  }

  function render(data) {
    var body = document.getElementById("event-body");
    if (!body) {
      return;
    }
    var id = requestedId();
    var event = (data.events || []).filter(function (item) {
      return item.id === id;
    })[0];

    if (!event) {
      body.innerHTML =
        '<div class="empty"><span class="label">Event not found</span>' +
        '<p style="margin-top:8px">' +
        (id ? "No event with the id " + S.escapeHtml(id) + " is in this build of the data."
            : "This page needs an event id, for example <span class=\"mono\">event.html?id=fed-2026-10</span>.") +
        '</p><p style="margin-top:16px"><a class="btn btn--ghost" href="markets.html">Back to markets</a></p></div>';
      return;
    }

    document.title = event.title + " — Spread";
    // A detail page belongs to the Markets section in the sidebar, and the top
    // bar names the event rather than the generic word "Event".
    var topbar = document.querySelector(".topbar-title");
    if (topbar) {
      topbar.textContent = event.title;
    }
    body.innerHTML =
      header(event) +
      '<div class="split" style="margin-top:40px">' +
      '<div>' + chartCard(event) + statsCard(event) + tiles(event) + "</div>" +
      '<div class="stack">' + venuesCard(event) + rulesCard(event) + "</div>" +
      "</div>" +
      howToRead();
  }

  S.start({ page: "markets", title: "Markets" }, render);
})();
