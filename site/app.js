/* Spread site runtime: shell, formatters, charts, and the notices every page
 * must carry. Plain ES2017 in one file - no build step, no dependencies,
 * nothing vendored. Charts are hand-written SVG so the whole site is three
 * files: styles, this, and the pages themselves.
 *
 * Contract for page authors:
 *   1. body contains exactly one <main id="page" class="content"> ... </main>
 *   2. the page script calls SPREAD.mountShell({page, title, data}) first,
 *      which inserts the sidebar and top bar and appends the notices
 *   3. then it fills #page; use the classes listed in styles/base.css
 */
(function () {
  "use strict";

  var CAVEAT =
    "Fees, liquidity and resolution rules may differ between venues: prices are " +
    "not directly comparable as a trade, and paired markets may not resolve the same way.";
  var INFORMATIONAL =
    "Informational only. Nothing here is financial advice, a recommendation, or an offer to buy or sell anything.";
  var ADULT = "18+ only. Prediction markets involve risk of loss.";

  var VENUE_LABELS = { kalshi: "Kalshi", polymarket: "Polymarket" };
  var SERIES_COLORS = [
    "var(--color-metric-green)",
    "var(--color-signal-orange)",
    "var(--color-bone)",
    "var(--color-warm-granite)"
  ];
  var DAY_MS = 24 * 60 * 60 * 1000;

  var NAV = [
    { id: "home", href: "index.html", label: "Home", icon: "home" },
    { id: "markets", href: "markets.html", label: "Markets", icon: "markets" },
    { id: "movers", href: "movers.html", label: "Movers", icon: "movers" },
    { id: "venues", href: "venues.html", label: "Venues", icon: "venues" },
    { id: "methodology", href: "methodology.html", label: "Methodology", icon: "methodology" },
    { id: "about", href: "about.html", label: "About", icon: "about" }
  ];

  var ICONS = {
    home: '<path d="M2 7l6-4.5L14 7v6.5H9.5V10h-3v3.5H2z"/>',
    markets: '<path d="M2.5 12.5V8M6.5 12.5V3.5M10.5 12.5V6M13.5 12.5V9.5"/>',
    movers: '<path d="M2 11l4-4 3 3 5-6"/><path d="M10.5 4H14v3.5"/>',
    venues: '<path d="M2.5 13.5h11M3.5 13.5V6l4.5-3 4.5 3v7.5M7 13.5v-3.5h2v3.5"/>',
    methodology: '<path d="M3 3.5h10M3 8h10M3 12.5h6"/>',
    about: '<circle cx="8" cy="8" r="6"/><path d="M8 7.2v4M8 5.2v.6"/>'
  };

  /* --- Data ----------------------------------------------------------- */

  function load() {
    return fetch("data.json", { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        throw new Error("data.json returned HTTP " + response.status);
      }
      return response.json();
    });
  }

  /* --- Formatting ----------------------------------------------------- */

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
    });
  }

  function venueLabel(venue) {
    return VENUE_LABELS[venue] || String(venue || "").replace(/^\w/, function (c) {
      return c.toUpperCase();
    });
  }

  /** Prices and gaps are in cents; the pages call the unit points. */
  function price(cents) {
    if (cents == null) {
      return "—";
    }
    return Number(cents).toFixed(1) + "¢";
  }

  function signed(cents) {
    if (cents == null) {
      return "—";
    }
    var value = Number(cents);
    return (value > 0 ? "+" : value < 0 ? "−" : "") + Math.abs(value).toFixed(1);
  }

  function gapText(cents) {
    if (cents == null) {
      return "—";
    }
    return signed(cents) + " pts";
  }

  function volume(number) {
    if (number == null) {
      return "—";
    }
    return Number(number).toLocaleString("en-US");
  }

  function gapClass(cents) {
    if (cents == null) {
      return "gap--none";
    }
    return Number(cents) > 0 ? "gap--pos" : Number(cents) < 0 ? "gap--neg" : "gap--none";
  }

  function parseTime(value) {
    var stamp = Date.parse(value);
    return isNaN(stamp) ? null : stamp;
  }

  function staleByClock(value) {
    var stamp = parseTime(value);
    return stamp == null ? true : Date.now() - stamp > DAY_MS;
  }

  function timeAgo(value) {
    var stamp = parseTime(value);
    if (stamp == null) {
      return "unknown time";
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

  function dateShort(value) {
    var stamp = parseTime(value);
    if (stamp == null) {
      return "—";
    }
    return new Date(stamp).toISOString().slice(0, 10);
  }

  function daysUntil(value) {
    var stamp = parseTime(value);
    if (stamp == null) {
      return null;
    }
    return (stamp - Date.now()) / DAY_MS;
  }

  function resolvesIn(value) {
    var days = daysUntil(value);
    if (days == null) {
      return "Resolution date unknown";
    }
    if (days < 0) {
      return "Resolution date passed";
    }
    if (days < 1) {
      return "Resolves in under a day";
    }
    return "Resolves in " + Math.round(days) + (Math.round(days) === 1 ? " day" : " days");
  }

  function eventUrl(id) {
    return "event.html?id=" + encodeURIComponent(id);
  }

  /* --- Charts --------------------------------------------------------- */

  /** The signed gap over time, from both venues' observed price history. */
  function gapSeries(event) {
    var venues = Object.keys(event.history || {});
    if (venues.length < 2) {
      return [];
    }
    var stamps = {};
    venues.forEach(function (venue) {
      (event.history[venue] || []).forEach(function (point) {
        stamps[point.t] = true;
      });
    });
    var times = Object.keys(stamps).sort();
    var cursors = venues.map(function () { return 0; });
    var series = [];
    times.forEach(function (time) {
      var prices = [];
      venues.forEach(function (venue, index) {
        var points = event.history[venue] || [];
        while (
          cursors[index] + 1 < points.length &&
          parseTime(points[cursors[index] + 1].t) <= parseTime(time)
        ) {
          cursors[index] += 1;
        }
        var point = points[cursors[index]];
        if (point && parseTime(point.t) <= parseTime(time)) {
          prices.push(point.price);
        }
      });
      if (prices.length === 2) {
        series.push({ t: time, gap: prices[0] - prices[1] });
      }
    });
    return series;
  }

  /** A 40px sparkline. Values are in cents; the axis is the observed range. */
  function sparkline(values, options) {
    options = options || {};
    var points = (values || []).filter(function (value) {
      return value != null && isFinite(value);
    });
    var width = 120;
    var height = 40;
    if (points.length === 0) {
      return '<svg class="spark" viewBox="0 0 ' + width + " " + height + '" aria-hidden="true"></svg>';
    }
    var min = Math.min.apply(null, points);
    var max = Math.max.apply(null, points);
    var span = max - min || 1;
    var step = points.length > 1 ? width / (points.length - 1) : 0;
    var path = points
      .map(function (value, index) {
        var x = (index * step).toFixed(2);
        var y = (height - ((value - min) / span) * (height - 6) - 3).toFixed(2);
        return (index === 0 ? "M" : "L") + x + " " + y;
      })
      .join(" ");
    var stroke = options.color || "var(--color-metric-green)";
    return (
      '<svg class="spark" viewBox="0 0 ' + width + " " + height + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path d="' + path + '" stroke="' + stroke + '"></path></svg>'
    );
  }

  function seriesColor(index) {
    return SERIES_COLORS[index % SERIES_COLORS.length];
  }

  /**
   * A line chart of each venue's price over time, in plain SVG.
   *
   * Stale stretches - points already older than 24 hours, plus the final
   * stretch when the venue's own verdict is stale - are drawn dashed, so the
   * chart shows where a line stops being trustworthy instead of implying
   * continuous truth.
   */
  function lineChart(event, options) {
    options = options || {};
    var width = 720;
    var height = 260;
    var pad = { top: 16, right: 16, bottom: 24, left: 34 };
    var venues = event.venues.map(function (item) { return item.venue; });
    var series = venues
      .map(function (venue, index) {
        return {
          venue: venue,
          index: index,
          color: seriesColor(index),
          dashedStyle: index >= 3,
          stale: !!(event.venues[index] && event.venues[index].is_stale),
          points: (event.history[venue] || []).filter(function (point) {
            return parseTime(point.t) != null;
          })
        };
      })
      .filter(function (item) { return item.points.length > 0; });

    var stamps = [];
    var prices = [];
    series.forEach(function (item) {
      item.points.forEach(function (point) {
        stamps.push(parseTime(point.t));
        prices.push(point.price);
      });
    });
    if (stamps.length === 0) {
      return '<p class="muted">No price history has been recorded for this event yet.</p>';
    }

    var minTime = Math.min.apply(null, stamps);
    var maxTime = Math.max.apply(null, stamps);
    var minPrice = Math.min.apply(null, prices);
    var maxPrice = Math.max.apply(null, prices);
    var low = Math.max(0, Math.floor((minPrice - 4) / 5) * 5);
    var high = Math.min(100, Math.ceil((maxPrice + 4) / 5) * 5);
    if (high - low < 5) {
      high = Math.min(100, low + 5);
    }
    var timeSpan = maxTime - minTime || 1;
    var priceSpan = high - low || 1;

    var x = function (stamp) {
      return pad.left + ((stamp - minTime) / timeSpan) * (width - pad.left - pad.right);
    };
    var y = function (value) {
      return pad.top + (1 - (value - low) / priceSpan) * (height - pad.top - pad.bottom);
    };

    var parts = [];
    for (var step = 0; step <= 4; step += 1) {
      var value = low + (priceSpan * step) / 4;
      var lineY = y(value).toFixed(2);
      parts.push(
        '<line class="gridline" x1="' + pad.left + '" y1="' + lineY + '" x2="' + (width - pad.right) + '" y2="' + lineY + '"></line>',
        '<text class="axis-label" x="0" y="' + (Number(lineY) + 3).toFixed(2) + '">' + value.toFixed(0) + "¢</text>"
      );
    }

    series.forEach(function (item) {
      var runs = [];
      var current = [];
      item.points.forEach(function (point) {
        var isLast = point === item.points[item.points.length - 1];
        var pointStale = staleByClock(point.t) || (isLast && item.stale);
        if (!pointStale) {
          if (current.length) {
            runs.push({ stale: true, points: current });
            current = [];
          }
        }
        current.push(point);
      });
      if (current.length) {
        runs.push({ stale: current[0] && staleByClock(current[0].t), points: current });
      }
      runs = runs.filter(function (run) { return run.points.length > 0; });

      runs.forEach(function (run, runIndex) {
        var d = run.points
          .map(function (point, index) {
            return (index === 0 ? "M" : "L") + x(parseTime(point.t)).toFixed(2) + " " + y(point.price).toFixed(2);
          })
          .join(" ");
        var dashed = item.dashedStyle || (runIndex > 0 && run.points.length > 1 && run.stale);
        parts.push(
          '<path class="series' + (dashed ? " series--dashed" : "") + '" d="' + d + '" stroke="' + item.color + '"></path>'
        );
      });
    });

    parts.push(
      '<text class="axis-label" x="' + pad.left + '" y="' + (height - 6) + '">' + dateShort(new Date(minTime).toISOString()) + "</text>",
      '<text class="axis-label" x="' + (width - pad.right - 70) + '" y="' + (height - 6) + '">' + dateShort(new Date(maxTime).toISOString()) + "</text>"
    );

    return (
      '<svg class="chart" viewBox="0 0 ' + width + " " + height + '" role="img" ' +
      'aria-label="Price history for ' + escapeHtml(event.title) + '">' + parts.join("") + "</svg>"
    );
  }

  function legend(venues) {
    return (
      '<ul class="legend">' +
      venues
        .map(function (item, index) {
          var dashed = index >= 3 ? " legend-swatch--dashed" : "";
          return (
            '<li class="legend-item"><span class="legend-swatch' + dashed + '" style="border-top-color:' +
            seriesColor(index) + '"></span>' + escapeHtml(venueLabel(item.venue)) + "</li>"
          );
        })
        .join("") +
      "</ul>"
    );
  }

  /* --- Notices -------------------------------------------------------- */

  function noticesHtml() {
    return (
      '<section class="card" aria-label="Disclosures">' +
      '<p class="label">Disclosures</p>' +
      '<p class="muted" style="margin-top:8px">' + escapeHtml(CAVEAT) + "</p>" +
      '<p class="muted" style="margin-top:8px">' + escapeHtml(INFORMATIONAL) + "</p>" +
      '<p class="muted" style="margin-top:8px"><strong style="font-weight:500">' + escapeHtml(ADULT) + "</strong></p>" +
      "</section>"
    );
  }

  /* --- Shell ---------------------------------------------------------- */

  function icon(name) {
    return '<svg class="nav-icon" viewBox="0 0 16 16" aria-hidden="true">' + (ICONS[name] || "") + "</svg>";
  }

  function movesItemsHtml(moves, limit) {
    var latest = (moves || []).slice().sort(function (a, b) {
      return (parseTime(b.at) || 0) - (parseTime(a.at) || 0);
    }).slice(0, limit);
    if (!latest.length) {
      return '<li class="moves-item"><span class="moves-venue">No price changes observed yet</span></li>';
    }
    return latest
      .map(function (move) {
        var change = move.to_price - move.from_price;
        return (
          '<li class="moves-item enter">' +
          '<span class="moves-event">' + escapeHtml(eventTitle(move)) + "</span>" +
          '<span class="moves-venue">' + escapeHtml(venueLabel(move.venue)) + "</span>" +
          '<span class="moves-price">' + signed(move.from_price) + " → " + signed(move.to_price) +
          " (" + signed(change) + " pts) · " + escapeHtml(timeAgo(move.at)) + "</span>" +
          "</li>"
        );
      })
      .join("");
  }

  var TITLES = {};

  function eventTitle(move) {
    if (move.title) {
      return move.title;
    }
    var event = (TITLES[move.event_id] || {});
    if (event.title) {
      return event.title;
    }
    return move.event_id;
  }

  function mountShell(config) {
    config = config || {};
    var data = config.data || {};
    var page = document.getElementById("page");
    if (!page) {
      return;
    }
    (data.events || []).forEach(function (event) {
      TITLES[event.id] = event;
    });

    var sidebar = document.createElement("aside");
    sidebar.className = "sidebar";
    sidebar.id = "sidebar";
    sidebar.setAttribute("aria-label", "Primary");
    sidebar.innerHTML =
      '<a class="wordmark" href="index.html">' +
      '<img class="wordmark-mark" src="assets/spread-symbol.svg" alt="" aria-hidden="true">' +
      "<span>Spread</span></a>" +
      '<nav class="nav">' +
      NAV.map(function (item) {
        return (
          '<a class="nav-item" href="' + item.href + '"' +
          (item.id === config.page ? ' aria-current="page"' : "") + ">" +
          icon(item.icon) + "<span>" + item.label + "</span></a>"
        );
      }).join("") +
      "</nav>" +
      '<section class="moves" aria-label="Live moves">' +
      '<div class="moves-head"><span class="dot dot--live" aria-hidden="true"></span>' +
      '<span class="label">Live moves</span></div>' +
      '<ul class="moves-list">' + movesItemsHtml(data.moves, 5) + "</ul>" +
      '<p class="muted" style="font-size:11px">Observed price changes, not trades.</p>' +
      "</section>" +
      '<div class="sidebar-foot">' +
      "<span>18+ · Informational only</span>" +
      '<a href="methodology.html">Methodology</a>' +
      "</div>";

    var scrim = document.createElement("div");
    scrim.className = "scrim";
    scrim.id = "scrim";

    var main = document.createElement("div");
    main.className = "main";
    var topbar = document.createElement("header");
    topbar.className = "topbar";
    topbar.innerHTML =
      '<button class="icon-button topbar-toggle" id="sidebar-toggle" type="button" ' +
      'aria-controls="sidebar" aria-expanded="false" aria-label="Toggle navigation">' +
      '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4h12M2 8h12M2 12h12"/></svg></button>' +
      '<img class="topbar-mark" src="assets/spread-symbol.svg" alt="" aria-hidden="true">' +
      '<span class="topbar-title">' + escapeHtml(config.title || "") + "</span>";
    main.appendChild(topbar);

    var shell = document.createElement("div");
    shell.className = "shell";
    shell.appendChild(sidebar);
    shell.appendChild(scrim);
    page.parentNode.insertBefore(shell, page);
    main.appendChild(page);
    shell.appendChild(main);

    page.insertAdjacentHTML("beforeend", noticesHtml());

    var toggle = document.getElementById("sidebar-toggle");
    function setOpen(open) {
      document.body.setAttribute("data-sidebar", open ? "open" : "closed");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    toggle.addEventListener("click", function () {
      setOpen(document.body.getAttribute("data-sidebar") !== "open");
    });
    scrim.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    });
    setOpen(false);
  }

  function fail(message) {
    var page = document.getElementById("page");
    if (!page) {
      return;
    }
    page.insertAdjacentHTML(
      "afterbegin",
      '<div class="empty"><span class="label">Data unavailable</span><p style="margin-top:8px">' +
        escapeHtml(message) +
        '</p><p class="muted" style="margin-top:8px">Run <span class="mono">python run_all.py</span> and reload.</p></div>'
    );
  }

  /**
   * fetch data.json, build the shell once, then hand the document to a page.
   *
   * The shell is mounted after the fetch so the live-moves widget and the
   * navigation are rendered in a single pass; when the fetch fails the shell
   * still mounts (so the nav works) and the page explains what is missing.
   */
  function start(config, render) {
    load()
      .then(function (data) {
        mountShell({ page: config.page, title: config.title, data: data });
        render(data);
      })
      .catch(function (error) {
        mountShell({ page: config.page, title: config.title, data: {} });
        fail(error.message || String(error));
      });
  }

  window.SPREAD = {
    CAVEAT: CAVEAT,
    INFORMATIONAL: INFORMATIONAL,
    ADULT: ADULT,
    NAV: NAV,
    VENUE_LABELS: VENUE_LABELS,
    load: load,
    start: start,
    mountShell: mountShell,
    noticesHtml: noticesHtml,
    escapeHtml: escapeHtml,
    venueLabel: venueLabel,
    price: price,
    signed: signed,
    gapText: gapText,
    gapClass: gapClass,
    volume: volume,
    timeAgo: timeAgo,
    dateShort: dateShort,
    daysUntil: daysUntil,
    resolvesIn: resolvesIn,
    staleByClock: staleByClock,
    eventUrl: eventUrl,
    eventTitle: eventTitle,
    gapSeries: gapSeries,
    sparkline: sparkline,
    lineChart: lineChart,
    legend: legend,
    seriesColor: seriesColor,
    movesItemsHtml: movesItemsHtml
  };
})();
