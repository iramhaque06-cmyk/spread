/* Event detail: one matched event, every venue that prices it.
 *
 * What a reader sees in Simple mode is deliberately small: the question, when
 * it resolves, the gap in large Geist Mono, one comparison strip in the site's
 * single light card, the price history, and a way out to each venue. Everything
 * else - the rules, how the gap was derived, what the sportsbook's raw odds
 * were, when each price was read - is in a collapsed <details>, and Detailed
 * mode opens them.
 *
 * Two rules the page must not bend:
 *   - the gap and every freshness verdict are the pipeline's; nothing here
 *     recomputes either (see gaps.py, freshness.py).
 *   - a play-money or forecast price is never in the gap, and never in the
 *     same section as a real-money price: it gets its own card, labelled
 *     "Not comparable to real-money prices".
 */
(function () {
  "use strict";

  var S = window.SPREAD;
  var MAX_SERIES = 8;

  function requestedId() {
    var found = /[?&]id=([^&]*)/.exec(window.location.search || "");
    if (!found) {
      return "";
    }
    try {
      return decodeURIComponent(found[1].replace(/\+/g, " "));
    } catch (error) {
      return found[1];
    }
  }

  function realMoney(price) {
    return S.isRealMoney(price.venue_type);
  }

  function playMoney(price) {
    return S.isPlayMoney(price.venue_type);
  }

  function values(list) {
    var out = [];
    for (var index = 0; index < list.length; index += 1) {
      if (list[index].price != null) {
        out.push(Number(list[index].price));
      }
    }
    return out;
  }

  /** The range a bar is drawn against: the comparable prices when there are
   * any, then the real-money ones, then everything the event has. */
  function range(prices) {
    var eligible = [];
    var real = [];
    for (var index = 0; index < prices.length; index += 1) {
      if (prices[index].gap_eligible) {
        eligible.push(prices[index]);
      }
      if (realMoney(prices[index])) {
        real.push(prices[index]);
      }
    }
    var chosen = values(eligible);
    if (!chosen.length) {
      chosen = values(real);
    }
    if (!chosen.length) {
      chosen = values(prices);
    }
    if (!chosen.length) {
      return { lo: null, hi: null };
    }
    return {
      lo: Math.min.apply(null, chosen),
      hi: Math.max.apply(null, chosen)
    };
  }

  function group(prices, types) {
    var out = [];
    for (var index = 0; index < prices.length; index += 1) {
      if (types.indexOf(prices[index].venue_type) !== -1) {
        out.push(prices[index]);
      }
    }
    return out;
  }

  /**
   * One line each, keyed by venue **and outcome**.
   *
   * The brief asks for one line per venue. That is exactly right for a normal
   * event, where each venue contributes one market. Two cases need the outcome
   * in the key: a book quotes one price per outcome and both are the point of
   * showing it, and a pairing the matcher cast too wide can leave one venue
   * holding several markets for what is allegedly one event - those collapse to
   * the most confident one, and the rest stay listed in the freshness note. A
   * market is never dropped merely for being stale: staleness is shown with its
   * age, which is the whole reason the note is there.
   */
  function oneLineEach(prices) {
    var chosen = {};
    var order = [];
    for (var index = 0; index < prices.length; index += 1) {
      var price = prices[index];
      var slot = price.venue + "|" + (price.outcome || "");
      if (!chosen[slot]) {
        chosen[slot] = price;
        order.push(slot);
        continue;
      }
      if (confidence(price) > confidence(chosen[slot])) {
        chosen[slot] = price;
      }
    }
    var out = [];
    for (var each = 0; each < order.length; each += 1) {
      out.push(chosen[order[each]]);
    }
    return out;
  }

  function confidence(price) {
    return price.confidence == null ? -1 : Number(price.confidence);
  }

  /* --- Pieces ----------------------------------------------------------- */

  function head(event) {
    var tags = "";
    if (event.possible) {
      tags +=
        '<span class="stale-tag">possible match</span> ' +
        S.tip(
          "The matcher scored this pairing below its confidence floor, so it is shown but never counted in a gap.",
          "Possible match"
        ) +
        " ";
    }
    if (event.rules_differ) {
      tags +=
        '<span class="stale-tag">rules differ</span> ' +
        S.tip(
          "The two venues do not word their resolution criteria the same way, so the same outcome can settle differently.",
          "Rules differ"
        ) +
        " ";
    }
    return (
      '<div class="page-head"><h1 class="headline">' +
      S.esc(event.title) +
      "</h1>" +
      '<span class="label label--muted">' +
      S.esc(event.category || "uncategorised") +
      " · resolves " +
      S.esc(S.dateMed(event.close_time)) +
      " (" +
      S.esc(S.resolvesIn(event.close_time)) +
      ")</span></div>" +
      (tags ? '<p class="row" style="margin-bottom:8px">' + tags + "</p>" : "")
    );
  }

  function gapBlock(event) {
    var eligible = 0;
    var prices = event.prices || [];
    for (var index = 0; index < prices.length; index += 1) {
      if (prices[index].gap_eligible) {
        eligible += 1;
      }
    }
    if (event.gap == null) {
      var why = event.possible
        ? "a possible match is never counted"
        : eligible < 2
          ? "needs two fresh real-money prices; " + eligible + " available"
          : "not comparable";
      return (
        '<span class="label">Gap · points</span>' +
        '<span class="big gap--none">—</span>' +
        '<span class="label label--muted">' +
        S.esc(why) +
        " " +
        S.tip(
          "The gap is the highest fresh real-money price minus the lowest. One fresh venue is a single number, not a gap.",
          "How the gap is calculated"
        ) +
        "</span>"
      );
    }
    return (
      '<span class="label">Gap · points</span>' +
      '<span class="big">' +
      S.esc(S.points(event.gap)) +
      "</span>" +
      '<span class="label label--muted">highest − lowest fresh real-money price ' +
      S.tip(
        "One point is one cent of a one-dollar contract.",
        "Prices are quoted in points"
      ) +
      "</span>"
    );
  }

  function venueLine(price, span) {
    var priceText = S.points(price.price);
    var label = price.title || price.market_id;
    var stale = price.fresh ? "" : '<span class="stale-tag">stale</span>';
    return (
      '<div class="venue-line">' +
      '<span class="venue-name" title="' +
      S.esc(label) +
      '">' +
      S.esc(S.venueLabel(price.venue)) +
      (price.outcome ? " · " + S.esc(price.outcome) : "") +
      "</span>" +
      '<span class="venue-bar">' +
      S.bar(span.lo, span.hi, price.price == null ? [] : [price.price], {
        big: true,
        label: S.venueLabel(price.venue) + " at " + priceText + " points"
      }) +
      "</span>" +
      '<span class="venue-price' +
      (price.fresh ? "" : " venue-price--stale") +
      '">' +
      S.esc(priceText) +
      "</span>" +
      stale +
      '<span class="venue-note">' +
      S.esc(noteFor(price)) +
      "</span></div>"
    );
  }

  /**
   * The one line a venue gets under its price. A stale row says its age; the
   * row is shown either way, because hiding it would hide the reason the gap
   * is narrower than it looks.
   */
  function noteFor(price) {
    if (price.fresh) {
      return "";
    }
    if (price.reason) {
      return price.reason + (price.price_as_of ? " (quoted " + S.timeAgo(price.price_as_of) + ")" : "");
    }
    return "price older than the 24 hour freshness limit";
  }

  function viewButtons(prices, className) {
    var venues = [];
    var seen = {};
    var html = "";
    for (var index = 0; index < prices.length; index += 1) {
      var price = prices[index];
      if (!price.url || seen[price.venue]) {
        continue;
      }
      seen[price.venue] = true;
      venues.push(price);
    }
    for (var each = 0; each < venues.length; each += 1) {
      html +=
        '<a class="btn ' +
        className +
        '" href="' +
        S.esc(venues[each].url) +
        '" target="_blank" rel="noopener noreferrer">View on ' +
        S.esc(S.venueLabel(venues[each].venue)) +
        "</a>";
    }
    return html ? '<div class="row" style="margin-top:16px">' + html + "</div>" : "";
  }

  function booksFootNote(books, home) {
    var status = venueStatus(home, "odds_api");
    if (status && status.errors && !books.length) {
      return (
        '<p class="venue-note" style="margin-top:8px">No sportsbook price is shown: ' +
        S.esc(shortError(status.errors)) +
        "</p>"
      );
    }
    if (!books.length) {
      return "";
    }
    return (
      '<p class="venue-note" style="margin-top:8px">Sportsbook prices are de-vigged implied ' +
      "probabilities, and include the bookmaker's margin. " +
      S.tip(
        "The implied probability is 1 / decimal odds, normalised across the book's outcomes so the margin comes out. The raw decimal and the margin itself are in the collapsed note below.",
        "What a sportsbook price is"
      ) +
      "</p>"
    );
  }

  function venueStatus(home, venue) {
    var statuses = (home && home.venue_status) || [];
    for (var index = 0; index < statuses.length; index += 1) {
      if (statuses[index].venue === venue) {
        return statuses[index];
      }
    }
    return null;
  }

  function shortError(text) {
    var trimmed = String(text || "").replace(/\s+/g, " ").trim();
    return trimmed.length > 120 ? trimmed.slice(0, 117) + "…" : trimmed;
  }

  function stripCard(event, home) {
    var prices = event.prices || [];
    var real = oneLineEach(group(prices, ["prediction_market"]));
    var books = oneLineEach(group(prices, ["sportsbook"]));
    var span = range(real.concat(books));
    var groups = "";
    if (real.length) {
      groups += groupHtml("Prediction markets", real, span);
    }
    if (books.length) {
      groups += groupHtml("Sportsbooks", books, span);
    }
    if (!groups) {
      groups =
        '<p class="venue-note">No venue price was stored for this event. ' +
        "The freshness note below says why.</p>";
    }
    return (
      '<section class="card card--light strip" aria-label="Venue comparison">' +
      groups +
      booksFootNote(books, home) +
      viewButtons(real, "btn--on-light") +
      "</section>"
    );
  }

  function groupHtml(label, prices, span) {
    var lines = "";
    for (var index = 0; index < prices.length; index += 1) {
      lines += venueLine(prices[index], span);
    }
    return (
      '<div class="strip-group"><span class="strip-label label">' +
      S.esc(label) +
      "</span>" +
      lines +
      "</div>"
    );
  }

  function playCard(event) {
    var play = oneLineEach(group(event.prices || [], ["play_money", "forecast"]));
    if (!play.length) {
      return "";
    }
    var span = range(play);
    var lines = "";
    for (var index = 0; index < play.length; index += 1) {
      var price = play[index];
      lines +=
        '<div class="venue-line">' +
        '<span class="venue-name">' +
        S.esc(S.venueLabel(price.venue)) +
        (price.outcome ? " · " + S.esc(price.outcome) : "") +
        "</span>" +
        '<span class="venue-bar">' +
        S.bar(span.lo, span.hi, price.price == null ? [] : [price.price], {
          label: S.venueLabel(price.venue) + " at " + S.points(price.price) + " points"
        }) +
        "</span>" +
        '<span class="venue-price' +
        (price.fresh ? "" : " venue-price--stale") +
        '">' +
        S.esc(S.points(price.price)) +
        "</span>" +
        (price.fresh ? "" : '<span class="stale-tag">stale</span>') +
        '<span class="venue-note">' +
        S.esc(noteFor(price)) +
        "</span></div>";
    }
    return (
      '<section class="card" style="margin-top:24px" aria-label="Not comparable to real-money prices">' +
      '<div class="card-head"><h2 class="card-title">Not comparable to real-money prices</h2>' +
      '<span class="label label--muted">Play money / forecast</span></div>' +
      lines +
      viewButtons(play, "btn--ghost") +
      "</section>"
    );
  }

  function chartCard(event) {
    var prices = event.prices || [];
    var history = event.history || {};
    var series = [];
    for (var index = 0; index < prices.length && series.length < MAX_SERIES; index += 1) {
      var key = prices[index].venue + ":" + prices[index].market_id;
      series.push({
        label:
          S.venueLabel(prices[index].venue) +
          (prices[index].outcome ? " · " + prices[index].outcome : ""),
        color: S.seriesColor(index),
        stale: !prices[index].fresh,
        points: history[key] || []
      });
    }
    var more =
      prices.length > series.length
        ? '<p class="label label--muted" style="margin-top:8px">Showing ' +
          S.esc(String(series.length)) +
          " of " +
          S.esc(String(prices.length)) +
          " series</p>"
        : "";
    return (
      '<section class="card" style="margin-top:24px">' +
      '<div class="card-head"><h2 class="card-title">Price history</h2>' +
      '<span class="label label--muted">Observed prices, in points</span></div>' +
      S.historyChart(series, { label: "Price history for " + event.title }) +
      S.legend(series) +
      more +
      "</section>"
    );
  }

  /* --- Collapsed detail ------------------------------------------------- */

  function rulesBody(event) {
    var rules = event.rules || {};
    var keys = Object.keys(rules);
    if (!keys.length) {
      return "<p>No resolution rule text was returned for this event.</p>";
    }
    var html =
      '<p><strong>Wording matches</strong> is not a guarantee: each venue settles from its own ' +
      "source, and fees, liquidity and timing can still differ.</p>";
    for (var index = 0; index < keys.length; index += 1) {
      var rule = rules[keys[index]];
      html +=
        "<p><strong>" +
        S.esc(S.venueLabel(rule.venue)) +
        (rule.outcome ? " · " + S.esc(rule.outcome) : "") +
        "</strong><br>" +
        (rule.rule_text
          ? S.esc(rule.rule_text) + " <em>(truncated to 600 characters)</em>"
          : "No rule text was returned for this market.") +
        "</p>";
    }
    return html;
  }

  function calculationBody(event) {
    var html =
      "<p>The gap is the highest fresh real-money price minus the lowest, in points. " +
      "Only prediction markets and de-vigged sportsbook prices count; a play-money price is " +
      "shown in its own section and never enters the arithmetic. A price that cannot be shown " +
      "to be from the last 24 hours is left out.</p>";
    var books = group(event.prices || [], ["sportsbook"]);
    if (books.length) {
      var rows = "";
      for (var index = 0; index < books.length; index += 1) {
        rows +=
          "<tr><td>" +
          S.esc(S.venueLabel(books[index].venue)) +
          (books[index].outcome ? " · " + S.esc(books[index].outcome) : "") +
          "</td><td>" +
          S.esc(S.points(books[index].price)) +
          "</td><td>" +
          S.esc(books[index].raw == null ? "—" : Number(books[index].raw).toFixed(2)) +
          "</td><td>" +
          S.esc(
            books[index].margin == null
              ? "—"
              : Number(books[index].margin).toFixed(1) + "%"
          ) +
          "</td></tr>";
      }
      html =
        "<p>Sportsbook prices are de-vigged implied probabilities " +
        "(1 / decimal odds, normalised across the book's outcomes). That normalisation removes " +
        "the bookmaker's margin, which is why a book's raw figure and the margin it was " +
        "normalised by are shown here and nowhere else.</p>" +
        '<div class="scroll-x"><table class="table"><thead><tr><th>Book</th>' +
        "<th>De-vigged</th><th>Raw decimal</th><th>Margin</th></tr></thead><tbody>" +
        rows +
        "</tbody></table></div>" +
        html;
    }
    html +=
      "<p>The category shown at the top is a display grouping: the venues' own categories are " +
      "inconsistent, so each question is bucketed from its wording.</p>";
    return html;
  }

  function freshnessBody(event, home) {
    var html = "";
    var prices = event.prices || [];
    for (var index = 0; index < prices.length; index += 1) {
      var price = prices[index];
      html +=
        "<p><strong>" +
        S.esc(S.venueLabel(price.venue)) +
        (price.outcome ? " · " + S.esc(price.outcome) : "") +
        "</strong><br>" +
        S.esc(
          (price.fresh ? "fresh" : "stale: " + (price.reason || "past the limit")) +
            " · " +
            (price.basis || "no basis published") +
            (price.price_as_of ? " · quoted " + S.timeAgo(price.price_as_of) : "") +
            (price.volume == null ? "" : " · volume " + S.num(price.volume))
        ) +
        "</p>";
    }
    var statuses = (home && home.venue_status) || [];
    for (var each = 0; each < statuses.length; each += 1) {
      html +=
        "<p><strong>" +
        S.esc(S.venueLabel(statuses[each].venue)) +
        "</strong><br>" +
        S.esc(
          (statuses[each].last_prices_ok_at
            ? "last price read " + S.timeAgo(statuses[each].last_prices_ok_at)
            : "not read yet") +
            (statuses[each].errors ? " · " + shortError(statuses[each].errors) : "")
        ) +
        "</p>";
    }
    if (!prices.length && !statuses.length) {
      html = "<p>No price has been recorded for this event yet.</p>";
    }
    return html;
  }

  function sourcesBody(event) {
    var sources = event.sources || [];
    if (!sources.length) {
      return "<p>No source was recorded for this event.</p>";
    }
    var html = "";
    for (var index = 0; index < sources.length; index += 1) {
      var source = sources[index];
      var book = S.venueLabel(source.venue) === "Books" || source.venue === "odds_api";
      html +=
        "<p><strong>" +
        S.esc(S.venueLabel(source.venue));
      if (source.url && !book) {
        html +=
          '</strong><br><a href="' +
          S.esc(source.url) +
          '" target="_blank" rel="noopener noreferrer">' +
          S.esc(source.url) +
          "</a>";
      } else {
        // A bookmaker is named as data only: no link, no promotion.
        html += " · " + S.esc(String(source.market_id || ""));
      }
      html +=
        "<br>" +
        S.esc("read " + S.timeAgo(source.fetched_at) + " (" + S.dateShort(source.fetched_at) + ")") +
        "</p>";
    }
    return html;
  }

  function notesBlock(event, home) {
    return S.notes([
      { title: "Resolution rules", body: rulesBody(event) },
      { title: "How this is calculated", body: calculationBody(event) },
      { title: "Data freshness", body: freshnessBody(event, home) },
      { title: "Sources", body: sourcesBody(event) }
    ]);
  }

  /* --- Page ------------------------------------------------------------- */

  function notFound(id) {
    return (
      '<div class="empty"><span class="label">Event not found</span><p>' +
      (id
        ? "No event with the id " + S.esc(id) + " is in this build of the data."
        : "This page needs an event id, for example <span class=\"mono\">event.html?id=ev-abc</span>.") +
      '</p><p style="margin-top:8px"><a class="btn btn--ghost btn--sm" href="search.html">' +
      "Browse the widest gaps</a></p></div>"
    );
  }

  function draw(event, home) {
    var body = document.getElementById("page-body");
    document.title = event.title + " — Spread";
    body.innerHTML =
      head(event) +
      '<section style="margin-bottom:24px">' +
      gapBlock(event) +
      "</section>" +
      stripCard(event, home) +
      playCard(event) +
      chartCard(event) +
      '<div style="margin-top:24px">' + notesBlock(event, home) + "</div>";
  }

  function render(home) {
    var body = document.getElementById("page-body");
    if (!body) {
      return;
    }
    var id = requestedId();
    var key = eventKey();
    if (!key) {
      body.innerHTML = notFound(id);
      return;
    }
    body.innerHTML =
      '<div class="page-head"><h1 class="headline">Loading event…</h1></div>';
    S.json("events/" + key + ".json")
      .then(function (event) {
        draw(event, home);
      })
      .catch(function () {
        body.innerHTML = notFound(id);
      });
  }

  /** The event file is named after the id, sanitised the same way as export.py. */
  function eventKey() {
    var id = requestedId();
    if (!id) {
      return "";
    }
    if (/^[A-Za-z0-9._-]+$/.test(id)) {
      return id;
    }
    return id.replace(/[^A-Za-z0-9._-]/g, "_");
  }

  S.boot({ page: "search", title: "Event" }, render);
})();
