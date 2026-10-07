/* Spread site runtime: the shell, the formatters, the shared widgets, and the
 * collapsed-notes system. Plain ES5-flavoured JavaScript in one file - no build
 * step, no dependency, nothing vendored, no CDN script. The search itself
 * lives in search.js; charts are hand-written SVG.
 *
 * Contract for a page author:
 *   1. the document body holds exactly one <main class="content" id="page">
 *   2. the page script ends with
 *        SPREAD.boot({page: "home", title: "Home"}, render);
 *      which mounts the top bar and footer, then calls render(home), where
 *      home is data/home.json or null when it could not be read
 *   3. render() must be safe to call again: it is called once per Simple /
 *      Detailed switch. Fetch through SPREAD.json(), which caches, and never
 *      recompute a number the pipeline already decided (gaps, freshness).
 *   4. build HTML with the classes in styles/base.css; escape every value that
 *      came from an API with SPREAD.esc()
 */
(function () {
  "use strict";

  /* --- Where the data is ------------------------------------------------ *
   * The published artifact root holds index.html next to data/. Locally the
   * pages are served from the repository root under /site/, so data/ is one
   * directory up from them. Resolving it once here beats a probe request on
   * every page load, and it keeps every fetch in the site relative to this. */
  var DATA = (function () {
    var dir = window.location.pathname.replace(/[^/]*$/, "");
    if (/(^|\/)site\/$/.test(dir)) {
      dir = dir.replace(/site\/$/, "");
    }
    return dir + "data/";
  })();

  var CAVEAT =
    "Informational only. Prices differ by fees, liquidity and rules. 18+ or the legal age where you live.";
  var INFORMATIONAL =
    "Informational only. Nothing here is financial advice, a recommendation, or an offer to buy or sell anything.";
  var ADULT = "18+ only. Prediction markets involve risk of loss.";

  var MODE_KEY = "spread:mode";
  var DAY_MS = 24 * 60 * 60 * 1000;

  var NAV = [
    { id: "home", href: "index.html", label: "Home" },
    { id: "search", href: "search.html", label: "Search" },
    { id: "venues", href: "venues.html", label: "Venues" },
    { id: "notes", href: "notes.html", label: "Notes" },
    { id: "about", href: "about.html", label: "About" }
  ];

  var VENUE_LABELS = {
    kalshi: "Kalshi",
    polymarket: "Polymarket",
    manifold: "Manifold",
    odds_api: "Books"
  };

  /* The three labels the brief asks for, mapped from the pipeline's own
   * venue_type vocabulary (venues/__init__.py: VENUE_TYPES). */
  /* Which venue is which kind - venues/__init__.py's VENUE_TYPES, mirrored
   * here because ranking has to know whether a market's price could ever take
   * part in a gap. */
  var VENUE_TYPES = {
    kalshi: "prediction_market",
    polymarket: "prediction_market",
    odds_api: "sportsbook",
    manifold: "play_money"
  };

  var VENUE_TYPE_LABELS = {
    prediction_market: "Prediction markets",
    sportsbook: "Sportsbooks",
    play_money: "Play-money/forecasts",
    forecast: "Play-money/forecasts"
  };

  var REFERENCE_COUNT = 3;
  var PLAY_MONEY_TYPES = { play_money: true, forecast: true };
  var REAL_MONEY_TYPES = { prediction_market: true, sportsbook: true };

  /* The five buckets the brief asks a reader to filter by. The venues'
   * categories are inconsistent (Kalshi says "Sports" and "Financials",
   * Polymarket sends tag slugs, Manifold sends nothing), so the bucket is
   * derived from the category and the question itself. It is a display
   * grouping and is labelled as one wherever it appears. */
  var CATEGORIES = ["Politics", "Economy", "Sports", "Crypto", "Other"];
  var CATEGORY_PATTERNS = [
    ["Sports", /\b(sport|nfl|nba|mlb|nhl|ncaa|football|basket|soccer|tennis|golf|boxing|ufc|olymp|world cup|super bowl|playoff|game|match|league|f1|formula 1|cricket|baseball|hockey)\b/],
    ["Crypto", /\b(crypto|bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|stablecoin|token|blockchain|defi|coin|binance|coinbase|etf outflows?)\b/],
    ["Politics", /\b(politic|election|elect|senate|congress|house|governor|president|presidential|trump|biden|democrat|republican|vote|voter|ballot|midterm|impeach|mayor|parliament|prime minister|cabinet|scotus|supreme court|primary|nominee|party)\b/],
    ["Economy", /\b(econ|financ|fed|federal reserve|inflation|cpi|interest rate|rate cut|rate hike|gdp|jobs|unemploy|payroll|recession|treasury|yield|stock|s&p|nasdaq|market cap|earnings|ipo|tariff|debt ceiling|shutdown|oil|gold|housing|mortgage|exchange rate|currency|dollar|euro|yen|yuan|peso|lira|commodity)\b/]
  ];

  /* --- Fetch ------------------------------------------------------------ */

  var cache = {};

  /** Fetch a JSON file under data/, once per page load. */
  function json(path) {
    if (cache[path]) {
      return cache[path];
    }
    var promise = fetch(DATA + path, { credentials: "same-origin" }).then(function (
      response
    ) {
      if (!response.ok) {
        throw new Error(path + " returned HTTP " + response.status);
      }
      return response.json();
    });
    promise.catch(function () {
      // A failure must not be remembered as the answer.
      delete cache[path];
    });
    cache[path] = promise;
    return promise;
  }

  function loadHome() {
    return json("home.json").catch(function () {
      return null;
    });
  }

  /* --- Formatting ------------------------------------------------------- */

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char];
    });
  }

  /** Thousands separators for a count, or an em dash for a missing number. */
  function num(value) {
    if (value == null || value === "") {
      return "—";
    }
    var number = Number(value);
    return isFinite(number) ? number.toLocaleString("en-US") : "—";
  }

  /** A price, in points. One point is one cent of a one-dollar contract. */
  function points(value) {
    if (value == null) {
      return "—";
    }
    var number = Number(value);
    return isFinite(number) ? number.toFixed(1) : "—";
  }

  function signed(value) {
    if (value == null) {
      return "—";
    }
    var number = Number(value);
    if (!isFinite(number)) {
      return "—";
    }
    return (number > 0 ? "+" : number < 0 ? "−" : "") + Math.abs(number).toFixed(1);
  }

  /** The gap, in points. max minus min, so it is never negative. */
  function gapText(value) {
    if (value == null) {
      return "—";
    }
    return Number(value).toFixed(1) + " pts";
  }

  function venueLabel(venue) {
    return (
      VENUE_LABELS[venue] ||
      String(venue || "").replace(/^./, function (char) {
        return char.toUpperCase();
      })
    );
  }

  function venueTypeLabel(type) {
    return VENUE_TYPE_LABELS[type] || String(type || "");
  }

  function isRealMoney(type) {
    return !!REAL_MONEY_TYPES[type];
  }

  function isPlayMoney(type) {
    return !!PLAY_MONEY_TYPES[type];
  }

  function parseTime(value) {
    var stamp = Date.parse(value);
    return isNaN(stamp) ? null : stamp;
  }

  function timeAgo(value) {
    var stamp = parseTime(value);
    if (stamp == null) {
      return "unknown";
    }
    var seconds = Math.max(0, (Date.now() - stamp) / 1000);
    if (seconds < 90) {
      return Math.round(seconds) + "s ago";
    }
    var minutes = seconds / 60;
    if (minutes < 90) {
      return Math.round(minutes) + "m ago";
    }
    var hours = minutes / 60;
    if (hours < 36) {
      return Math.round(hours) + "h ago";
    }
    return Math.round(hours / 24) + "d ago";
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /** 2026-11-04T04:59:00Z -> "4 Nov 2026". */
  function dateMed(value) {
    var stamp = parseTime(value);
    if (stamp == null) {
      return "—";
    }
    var date = new Date(stamp);
    return (
      date.getUTCDate() +
      " " +
      MONTHS[date.getUTCMonth()] +
      " " +
      date.getUTCFullYear()
    );
  }

  function dateShort(value) {
    var stamp = parseTime(value);
    return stamp == null ? "—" : new Date(stamp).toISOString().slice(0, 10);
  }

  /** "in 28 days", "under a day", "passed" - short enough for one line. */
  function resolvesIn(value) {
    var stamp = parseTime(value);
    if (stamp == null) {
      return "resolve date unknown";
    }
    var days = (stamp - Date.now()) / DAY_MS;
    if (days < 0) {
      return "passed";
    }
    if (days < 1) {
      return "under a day";
    }
    var rounded = Math.round(days);
    return "in " + rounded + (rounded === 1 ? " day" : " days");
  }

  function staleByClock(value) {
    var stamp = parseTime(value);
    return stamp == null ? true : Date.now() - stamp > DAY_MS;
  }

  /** Politics / Economy / Sports / Crypto / Other, for the filter chips. */
  function categoryBucket(item) {
    var text = String((item && (item.title || item.t)) || "") + " " + String((item && (item.category || item.c)) || "");
    var haystack = " " + text.toLowerCase().replace(/[^a-z0-9&]+/g, " ") + " ";
    for (var index = 0; index < CATEGORY_PATTERNS.length; index += 1) {
      if (CATEGORY_PATTERNS[index][1].test(haystack)) {
        return CATEGORY_PATTERNS[index][0];
      }
    }
    return "Other";
  }

  function eventUrl(id) {
    return "event.html?id=" + encodeURIComponent(id);
  }

  function searchUrl(query) {
    return "search.html?q=" + encodeURIComponent(query);
  }

  /* --- Mode: Simple / Detailed ----------------------------------------- */

  function readMode() {
    try {
      var stored = window.localStorage.getItem(MODE_KEY);
      if (stored === "detailed" || stored === "simple") {
        return stored;
      }
    } catch (error) {
      /* A blocked localStorage is not a reason to stop rendering. */
    }
    return "simple";
  }

  var mode = readMode();
  var modeHandlers = [];

  function writeMode(next) {
    try {
      window.localStorage.setItem(MODE_KEY, next);
    } catch (error) {
      /* ignore */
    }
  }

  function onModeChange(handler) {
    modeHandlers.push(handler);
  }

  function setMode(next, options) {
    options = options || {};
    if (next !== "simple" && next !== "detailed") {
      return mode;
    }
    mode = next;
    writeMode(next);
    document.body.setAttribute("data-mode", next);
    var buttons = document.querySelectorAll("[data-mode-btn]");
    for (var index = 0; index < buttons.length; index += 1) {
      var button = buttons[index];
      button.setAttribute(
        "aria-pressed",
        String(button.getAttribute("data-mode-btn") === next)
      );
    }
    if (!options.keepNotes) {
      // Detailed mode opens every collapsed section; Simple closes them.
      var notes = document.querySelectorAll("details[data-note]");
      for (var note = 0; note < notes.length; note += 1) {
        notes[note].open = next === "detailed";
      }
    }
    for (var handler = 0; handler < modeHandlers.length; handler += 1) {
      modeHandlers[handler](next);
    }
    return next;
  }

  /* --- Widgets --------------------------------------------------------- */

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  function barLabel(lo, hi, marks) {
    if (lo == null || hi == null) {
      return "no comparable price";
    }
    var low = Math.min(lo, hi);
    var high = Math.max(lo, hi);
    var where = low === high
      ? points(low) + " points"
      : points(low) + " to " + points(high) + " points";
    var count = (marks || []).length;
    return count > 1 ? where + " across " + count + " prices" : where;
  }

  /**
   * The one-line price bar: the full 0-100 point scale, with the stretch
   * between the lowest and highest comparable price filled in and a tick per
   * venue. It is the site's central readout, so it is drawn in SVG-less
   * percentages and carries a spoken description for screen readers.
   */
  function bar(lo, hi, marks, options) {
    options = options || {};
    var span = "";
    if (lo != null && hi != null) {
      var low = clamp(Math.min(lo, hi), 0, 100);
      var high = clamp(Math.max(lo, hi), 0, 100);
      span =
        '<span class="bar-span" style="left:' +
        low.toFixed(2) +
        "%;width:" +
        (high - low).toFixed(2) +
        '%"></span>';
    }
    var ticks = "";
    var seen = {};
    for (var index = 0; index < (marks || []).length; index += 1) {
      var mark = Number(marks[index]);
      if (!isFinite(mark) || seen[mark]) {
        continue;
      }
      seen[mark] = true;
      ticks +=
        '<span class="bar-mark" style="left:' +
        clamp(mark, 0, 100).toFixed(2) +
        '%"></span>';
    }
    return (
      '<span class="bar' +
      (options.big ? " bar--lg" : "") +
      '" role="img" aria-label="' +
      esc(options.label || barLabel(lo, hi, marks)) +
      '"><span class="bar-track"></span>' +
      span +
      ticks +
      "</span>"
    );
  }

  function tip(text, label) {
    return (
      '<span class="tip tip--left" tabindex="0" role="img" data-tip="' +
      esc(text) +
      '" aria-label="' +
      esc(label || text) +
      '">i</span>'
    );
  }

  /**
   * The collapsed sections every page carries. Native <details>, so they work
   * with no script and land in the tab order a reader expects; Detailed mode
   * opens them all (see setMode).
   */
  function notes(items) {
    var open = mode === "detailed" ? " open" : "";
    var html = "";
    for (var index = 0; index < items.length; index += 1) {
      var item = items[index];
      if (!item || !item.body) {
        continue;
      }
      html +=
        '<details class="note" data-note' +
        open +
        '><summary><span class="note-mark" aria-hidden="true"></span>' +
        esc(item.title) +
        '</summary><div class="note-body">' +
        item.body +
        "</div></details>";
    }
    return html;
  }

  /** The row a search result, a dropdown hit and a home gap all share. */
  function rowHtml(row) {
    var gaps =
      row.gap == null
        ? '<span class="result-gap gap--none">—</span>'
        : '<span class="result-gap">' + esc(gapText(row.gap)) + "</span>";
    var flags = [];
    if (row.possible) {
      flags.push('<span class="stale-tag">possible match</span>');
    }
    if (row.rulesDiffer) {
      flags.push('<span class="stale-tag">rules differ</span>');
    }
    if (row.stale) {
      flags.push('<span class="stale-tag">stale</span>');
    }
    // There is no "off-site" tag: a standalone market's title always goes to
    // its venue, so the tag was true on every such row and said nothing. The
    // `offsite` flag still drives target="_blank"; it is just not content.
    return (
      '<li class="result">' +
      '<span class="result-cat label">' +
      esc(row.bucket) +
      "</span>" +
      '<a class="result-title" href="' +
      esc(row.href) +
      '"' +
      (row.offsite ? ' target="_blank" rel="noopener noreferrer"' : "") +
      ">" +
      esc(row.title) +
      "</a>" +
      '<span class="result-count">' +
      esc(row.count) +
      "</span>" +
      '<span class="result-bar">' +
      bar(row.lo, row.hi, row.marks) +
      "</span>" +
      gaps +
      (flags.length ? '<span class="result-flags">' + flags.join("") + "</span>" : "") +
      "</li>"
    );
  }

  /** One matched event, from an events-index row, as a result row. */
  function eventRow(row, meta) {
    meta = meta || {};
    return {
      bucket: categoryBucket({ title: row.t, category: row.c }),
      title: row.t,
      href: eventUrl(row.i),
      count: row.n > 1 ? row.n + " venues" : "1 venue",
      venues: row.n || 1,
      lo: row.lo,
      hi: row.hi,
      marks: meta.marks || [],
      gap: row.g,
      possible: !!row.p,
      rulesDiffer: !!row.r,
      // The range is drawn from stale prices: say so rather than showing a bar
      // that looks comparable while the gap beside it reads as missing.
      stale: !!row.x,
      score: meta.score
    };
  }

  /**
   * One standalone market, from a compact catalog record.
   *
   * A single venue has no cross-venue range, so the bar is the market's own
   * price and nothing wider. The record's `o` outcomes must not be used for the
   * range: they are the venue's own other side, so a market trading at 2 points
   * carries a 98-point complement and the bar would claim the widest
   * disagreement on the site while the gap beside it reads "—". Outcomes are
   * the fallback only for a market that has no price of its own.
   */
  function marketRow(record, key, meta) {
    meta = meta || {};
    var marks = record.p == null ? [] : [record.p];
    var lo = record.p;
    var hi = record.p;
    if (lo == null) {
      var outcomes = [];
      for (var index = 0; index < (record.o || []).length; index += 1) {
        if (record.o[index].p != null) {
          outcomes.push(record.o[index].p);
        }
      }
      if (outcomes.length) {
        lo = Math.min.apply(null, outcomes);
        hi = Math.max.apply(null, outcomes);
      }
    }
    return {
      bucket: categoryBucket({ title: record.t, category: record.c }),
      title: record.t || key,
      href: record.u || "#",
      offsite: true,
      count: "1 venue",
      venues: 1,
      lo: lo,
      hi: hi,
      marks: marks,
      gap: null,
      volume: record.w,
      close: record.e,
      // Not `record.x`: a standalone row shows no price number, so a stale tag
      // on it tells a reader nothing they can act on. Freshness is stated where
      // a price is actually shown - the event page and the freshness card.
      stale: false,
      score: meta.score
    };
  }

  /**
   * What to say when a venue could not be read.
   *
   * The pipeline stores the operator's own reason - an unset API key, the
   * status line of the last run - and that belongs in a log, not on a page:
   * "ODDS_API_KEY is not set. Put it in .env (gitignored) or the environment."
   * tells a reader nothing and looks like a bug. This keeps the fact (the venue
   * is not connected) and drops the plumbing, and returns "" when there is
   * nothing wrong so callers can treat it as an optional suffix.
   */
  function venueTrouble(status) {
    if (!status || !status.errors) {
      return "";
    }
    var said = String(status.errors);
    if (/ODDS_API_KEY|api[_ ]key|not set|not configured/i.test(said)) {
      return "Not connected - no API key is configured";
    }
    if (/quota|credit/i.test(said)) {
      return "Paused - the monthly quota is used up";
    }
    if (/rate limit|too many requests|429/i.test(said)) {
      return "Paused - the venue's rate limit was reached";
    }
    if (/timeout|timed out/i.test(said)) {
      return "The last read timed out";
    }
    return "The last read failed";
  }

  /* --- Charts ----------------------------------------------------------- */

  var SERIES_COLORS = [
    "var(--color-metric-green)",
    "var(--color-signal-orange)",
    "var(--color-bone)",
    "var(--color-warm-granite)"
  ];

  function seriesColor(index) {
    return SERIES_COLORS[index % SERIES_COLORS.length];
  }

  /**
   * Every series' price over time, in plain SVG. Stretches already older than
   * 24 hours - and the final stretch when the venue's own verdict is stale -
   * are drawn dashed, so a line never implies a current price where none was
   * observed.
   */
  function historyChart(series, options) {
    options = options || {};
    var width = 720;
    var height = 260;
    var pad = { top: 16, right: 16, bottom: 24, left: 36 };
    var plotted = [];
    for (var index = 0; index < series.length; index += 1) {
      var item = series[index];
      var points_ = [];
      for (var point = 0; point < (item.points || []).length; point += 1) {
        var stamp = parseTime(item.points[point].t);
        if (stamp != null && item.points[point].price != null) {
          points_.push({ stamp: stamp, price: Number(item.points[point].price) });
        }
      }
      if (points_.length) {
        plotted.push({
          label: item.label,
          color: item.color || seriesColor(index),
          dashed: index >= 3,
          stale: !!item.stale,
          points: points_
        });
      }
    }

    if (!plotted.length) {
      return (
        '<p class="subhead">No price history has been recorded for this event yet. ' +
        "It builds up as the pipeline records each 30 minute observation.</p>"
      );
    }

    var stamps = [];
    var prices = [];
    for (var each = 0; each < plotted.length; each += 1) {
      for (var entry = 0; entry < plotted[each].points.length; entry += 1) {
        stamps.push(plotted[each].points[entry].stamp);
        prices.push(plotted[each].points[entry].price);
      }
    }

    var minTime = Math.min.apply(null, stamps);
    var maxTime = Math.max.apply(null, stamps);
    var low = Math.max(0, Math.floor((Math.min.apply(null, prices) - 4) / 5) * 5);
    var high = Math.min(100, Math.ceil((Math.max.apply(null, prices) + 4) / 5) * 5);
    if (high - low < 5) {
      high = Math.min(100, low + 5);
    }
    var timeSpan = maxTime - minTime || 1;
    var priceSpan = high - low || 1;

    function x(stamp) {
      return pad.left + ((stamp - minTime) / timeSpan) * (width - pad.left - pad.right);
    }

    function y(value) {
      return pad.top + (1 - (value - low) / priceSpan) * (height - pad.top - pad.bottom);
    }

    var parts = [];
    for (var step = 0; step <= 4; step += 1) {
      var value = low + (priceSpan * step) / 4;
      var lineY = y(value).toFixed(2);
      parts.push(
        '<line class="gridline" x1="' +
          pad.left +
          '" y1="' +
          lineY +
          '" x2="' +
          (width - pad.right) +
          '" y2="' +
          lineY +
          '"></line>',
        '<text class="axis-label" x="0" y="' +
          (Number(lineY) + 3).toFixed(2) +
          '">' +
          value.toFixed(0) +
          "</text>"
      );
    }

    for (var eachSeries = 0; eachSeries < plotted.length; eachSeries += 1) {
      var one = plotted[eachSeries];
      var runs = [];
      var current = [];
      for (var step2 = 0; step2 < one.points.length; step2 += 1) {
        var last = step2 === one.points.length - 1;
        var isStale = staleByClock(new Date(one.points[step2].stamp).toISOString()) ||
          (last && one.stale);
        if (!isStale && current.length) {
          runs.push({ stale: true, points: current });
          current = [];
        }
        current.push(one.points[step2]);
      }
      if (current.length) {
        runs.push({
          stale: staleByClock(new Date(current[0].stamp).toISOString()),
          points: current
        });
      }

      for (var run = 0; run < runs.length; run += 1) {
        var path = "";
        for (var p = 0; p < runs[run].points.length; p += 1) {
          path +=
            (p === 0 ? "M" : "L") +
            x(runs[run].points[p].stamp).toFixed(2) +
            " " +
            y(runs[run].points[p].price).toFixed(2);
        }
        var dashed = one.dashed || (run > 0 && runs[run].points.length > 1 && runs[run].stale);
        parts.push(
          '<path class="series' +
            (dashed ? " series--dashed" : "") +
            '" d="' +
            path +
            '" stroke="' +
            one.color +
            '"></path>'
        );
      }
    }

    parts.push(
      '<text class="axis-label" x="' +
        pad.left +
        '" y="' +
        (height - 6) +
        '">' +
        dateShort(new Date(minTime).toISOString()) +
        "</text>",
      '<text class="axis-label" x="' +
        (width - pad.right - 70) +
        '" y="' +
        (height - 6) +
        '">' +
        dateShort(new Date(maxTime).toISOString()) +
        "</text>"
    );

    return (
      '<svg class="chart" viewBox="0 0 ' +
      width +
      " " +
      height +
      '" role="img" aria-label="' +
      esc(options.label || "Price history, in points") +
      '">' +
      parts.join("") +
      "</svg>"
    );
  }

  function legend(items) {
    var html = "";
    for (var index = 0; index < items.length; index += 1) {
      html +=
        '<li class="legend-item"><span class="legend-swatch' +
        (index >= 3 ? " legend-swatch--dashed" : "") +
        '" style="border-top-color:' +
        (items[index].color || seriesColor(index)) +
        '"></span>' +
        esc(items[index].label) +
        "</li>";
    }
    return '<ul class="legend">' + html + "</ul>";
  }

  /* --- Shell ------------------------------------------------------------ */

  var SEARCH_ICON =
    '<svg class="search-icon" viewBox="0 0 16 16" aria-hidden="true">' +
    '<circle cx="7" cy="7" r="4.5"></circle><path d="M10.4 10.4L14 14"></path></svg>';

  function topbarHtml(config) {
    var links = "";
    for (var index = 0; index < NAV.length; index += 1) {
      links +=
        '<a class="topnav-link" href="' +
        NAV[index].href +
        '"' +
        (NAV[index].id === config.page ? ' aria-current="page"' : "") +
        ">" +
        esc(NAV[index].label) +
        "</a>";
    }
    return (
      '<a class="brand" href="index.html">' +
      '<img class="brand-mark" src="assets/spread-horizontal.svg" alt="Spread">' +
      "</a>" +
      '<form class="search" role="search" action="search.html" id="spread-search-form">' +
      SEARCH_ICON +
      '<input class="search-input" id="spread-q" name="q" type="search" ' +
      'placeholder="Search every market" autocomplete="off" spellcheck="false" ' +
      'aria-label="Search every market" aria-expanded="false" ' +
      'aria-controls="spread-suggest" role="combobox">' +
      '<kbd class="search-key" title="Press / to search">/</kbd>' +
      // The panel is a child of the form so it hangs from the box rather than
      // from the top bar: as a sibling of the form its containing block was the
      // whole bar, which is why it once stretched the width of the viewport.
      '<div class="suggest" id="spread-suggest" role="listbox" aria-label="Search results" hidden></div>' +
      "</form>" +
      '<nav class="topnav" aria-label="Sections">' +
      links +
      "</nav>" +
      '<div class="modeswitch" role="group" aria-label="Level of detail">' +
      '<button class="mode-btn" type="button" data-mode-btn="simple" aria-pressed="true">Simple</button>' +
      '<button class="mode-btn" type="button" data-mode-btn="detailed" aria-pressed="false">Detailed</button>' +
      "</div>"
    );
  }

  function footerHtml(home) {
    var links = "";
    for (var index = 0; index < NAV.length; index += 1) {
      links += '<a href="' + NAV[index].href + '">' + esc(NAV[index].label) + "</a>";
    }
    var generated = home && home.generated_at ? "Data built " + timeAgo(home.generated_at) : "";
    return (
      '<div class="foot-inner">' +
      '<p class="foot-caveat">' +
      esc(CAVEAT) +
      "</p>" +
      '<nav class="foot-nav" aria-label="Footer">' +
      links +
      '<a href="notes.html">Full notes</a>' +
      "</nav>" +
      (generated ? '<span class="foot-meta">' + esc(generated) + "</span>" : "") +
      "</div>"
    );
  }

  function mountShell(config, home) {
    var body = document.body;
    var bar = document.createElement("header");
    bar.className = "topbar";
    bar.innerHTML = topbarHtml(config);
    body.insertBefore(bar, body.firstChild);

    var foot = document.createElement("footer");
    foot.className = "sitefoot";
    foot.innerHTML = footerHtml(home);
    body.appendChild(foot);

    document.body.setAttribute("data-page", config.page || "");
    document.title = config.documentTitle || (config.title ? config.title + " — Spread" : "Spread");
    setMode(mode, { keepNotes: true });

    var buttons = document.querySelectorAll("[data-mode-btn]");
    for (var index = 0; index < buttons.length; index += 1) {
      buttons[index].addEventListener("click", function (event) {
        setMode(event.currentTarget.getAttribute("data-mode-btn"));
      });
    }

    if (window.SpreadSearch && window.SpreadSearch.init) {
      window.SpreadSearch.init();
    }
  }

  /**
   * The whole-page failure state, for when the data cannot be read at all.
   *
   * `message` is the technical cause, and it goes to the console rather than
   * onto the page: this is what a reader sees when a deploy is missing its
   * data, and "build it with python run_all.py" is not something a reader can
   * act on. What they get is what happened and what to do about it.
   */
  function fail(message) {
    var page = document.getElementById("page");
    if (!page) {
      return;
    }
    if (window.console && console.error) {
      console.error("Spread: " + message);
    }
    page.insertAdjacentHTML(
      "afterbegin",
      '<div class="empty"><span class="label">Prices unavailable</span>' +
        "<p>The price files could not be loaded. Check your connection and reload the page." +
        "</p></div>"
    );
  }

  /**
   * Mount the shell, then hand the landing document to the page. render() is
   * called again whenever the reader switches Simple / Detailed, so a page is
   * written once and both modes read from it.
   */
  function boot(config, render) {
    config = config || {};
    return loadHome().then(function (home) {
      mountShell(config, home);
      if (!home) {
        fail("home.json could not be read, so this page has nothing to show.");
      }
      function draw() {
        try {
          render(home, { mode: mode });
        } catch (error) {
          fail(error && error.message ? error.message : String(error));
        }
      }
      onModeChange(draw);
      draw();
      return home;
    });
  }

  window.SPREAD = {
    DATA: DATA,
    CAVEAT: CAVEAT,
    INFORMATIONAL: INFORMATIONAL,
    ADULT: ADULT,
    NAV: NAV,
    CATEGORIES: CATEGORIES,
    VENUE_LABELS: VENUE_LABELS,
    VENUE_TYPES: VENUE_TYPES,
    VENUE_TYPE_LABELS: VENUE_TYPE_LABELS,
    json: json,
    boot: boot,
    setMode: setMode,
    onModeChange: onModeChange,
    mode: function () {
      return mode;
    },
    esc: esc,
    num: num,
    points: points,
    signed: signed,
    gapText: gapText,
    venueLabel: venueLabel,
    venueTypeLabel: venueTypeLabel,
    venueTrouble: venueTrouble,
    isRealMoney: isRealMoney,
    isPlayMoney: isPlayMoney,
    timeAgo: timeAgo,
    dateMed: dateMed,
    dateShort: dateShort,
    resolvesIn: resolvesIn,
    staleByClock: staleByClock,
    categoryBucket: categoryBucket,
    eventUrl: eventUrl,
    searchUrl: searchUrl,
    bar: bar,
    tip: tip,
    notes: notes,
    rowHtml: rowHtml,
    eventRow: eventRow,
    marketRow: marketRow,
    historyChart: historyChart,
    legend: legend,
    seriesColor: seriesColor,
    fail: fail,
    DATA_DAY_MS: DAY_MS
  };
})();
