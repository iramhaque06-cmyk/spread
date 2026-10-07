/* Markets page: search, filters, tabs, cards and pagination.
 *
 * All sorting and filtering happens here in the browser, over the one
 * data.json the pipeline wrote. Nothing is recomputed about the data itself:
 * `gap` already excludes stale prices and `is_stale` already encodes the
 * 24 hour rule, so this file only decides what to show and in what order.
 */
(function () {
  "use strict";

  var S = window.SPREAD;
  var PER_PAGE = 12;

  var state = {
    query: "",
    status: "active",
    sort: "gap",
    order: "desc",
    category: "all",
    venue: "all",
    minVolume: "",
    showStale: true,
    page: 1
  };

  var data = { events: [], moves: [], venues_status: [] };

  /* --- Deriving what a row needs -------------------------------------- */

  function volumeOf(event) {
    return event.venues.reduce(function (total, venue) {
      return total + (venue.volume == null ? 0 : Number(venue.volume));
    }, 0);
  }

  function latestStamp(event) {
    var stamps = event.venues
      .map(function (venue) { return Date.parse(venue.updated_at); })
      .filter(function (stamp) { return !isNaN(stamp); });
    return stamps.length ? Math.max.apply(null, stamps) : null;
  }

  function hasStale(event) {
    return event.venues.some(function (venue) { return venue.is_stale; });
  }

  function categories() {
    var seen = {};
    data.events.forEach(function (event) {
      if (event.category) {
        seen[event.category] = true;
      }
    });
    return Object.keys(seen).sort();
  }

  function venueIds() {
    var seen = {};
    data.events.forEach(function (event) {
      event.venues.forEach(function (venue) { seen[venue.venue] = true; });
    });
    return Object.keys(seen).sort();
  }

  /* --- Filter, sort, paginate ---------------------------------------- */

  function visible() {
    var query = state.query.trim().toLowerCase();
    var minimum = state.minVolume === "" ? null : Number(state.minVolume);
    var rows = data.events.filter(function (event) {
      if (state.status !== "all" && event.status !== state.status) {
        return false;
      }
      if (query && event.title.toLowerCase().indexOf(query) === -1 && event.id.indexOf(query) === -1) {
        return false;
      }
      if (state.category !== "all" && event.category !== state.category) {
        return false;
      }
      if (state.venue !== "all" && !event.venues.some(function (venue) {
        return venue.venue === state.venue;
      })) {
        return false;
      }
      if (minimum != null && volumeOf(event) < minimum) {
        return false;
      }
      if (!state.showStale && hasStale(event)) {
        return false;
      }
      return true;
    });

    rows.sort(function (a, b) {
      var left;
      var right;
      if (state.sort === "volume") {
        left = volumeOf(a);
        right = volumeOf(b);
      } else if (state.sort === "updated") {
        left = latestStamp(a) || 0;
        right = latestStamp(b) || 0;
      } else {
        // A null gap means fewer than two fresh prices: always the last row.
        left = a.gap == null ? -1 : Math.abs(a.gap);
        right = b.gap == null ? -1 : Math.abs(b.gap);
      }
      var delta = left - right;
      if (delta === 0) {
        return a.title < b.title ? -1 : 1;
      }
      return state.order === "asc" ? delta : -delta;
    });

    return rows;
  }

  /* --- Fragments ------------------------------------------------------ */

  /** One filter field. `forId` links a real label to its control; groups
   *  (segmented buttons) carry their own aria-label instead. */
  function field(label, content, forId) {
    var caption = forId
      ? '<label class="label label--muted" for="' + forId + '">' + S.escapeHtml(label) + "</label>"
      : '<span class="label label--muted">' + S.escapeHtml(label) + "</span>";
    return '<div class="field">' + caption + content + "</div>";
  }

  function segmented(name, options, active) {
    return (
      '<div class="segmented" role="group" aria-label="' + S.escapeHtml(name) + '">' +
      options
        .map(function (option) {
          return (
            '<button class="segment" type="button" data-group="' + S.escapeHtml(name) +
            '" data-value="' + S.escapeHtml(option.value) + '"' +
            (option.value === active ? ' aria-pressed="true"' : ' aria-pressed="false"') + ">" +
            S.escapeHtml(option.label) + "</button>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function toolbar() {
    var categoryOptions = ['<option value="all">All categories</option>']
      .concat(categories().map(function (name) {
        return '<option value="' + S.escapeHtml(name) + '"' +
          (state.category === name ? " selected" : "") + ">" + S.escapeHtml(name) + "</option>";
      }))
      .join("");
    var venueOptions = ['<option value="all">All venues</option>']
      .concat(venueIds().map(function (id) {
        return '<option value="' + S.escapeHtml(id) + '"' +
          (state.venue === id ? " selected" : "") + ">" + S.escapeHtml(S.venueLabel(id)) + "</option>";
      }))
      .join("");
    var tabs = [
      { value: "active", label: "Active" },
      { value: "resolved", label: "Resolved" },
      { value: "all", label: "All" }
    ];

    return (
      '<div class="markets-toolbar">' +
      '<div class="markets-search">' +
      '<label class="label label--muted" for="markets-search">Search events</label>' +
      '<input class="input" id="markets-search" type="search" placeholder="Fed, CPI, payrolls…" ' +
      'autocomplete="off" value="' + S.escapeHtml(state.query) + '">' +
      "</div>" +
      '<div class="markets-actions">' +
      segmented("Status", tabs, state.status) +
      '<span class="filter-anchor">' +
      '<button class="btn" id="filters-toggle" type="button" aria-expanded="false" ' +
      'aria-controls="filters-panel">Filters</button>' +
      '<div class="popover" id="filters-panel" hidden aria-label="Filters">' +
      field("Sort by", segmented("Sort by", [
        { value: "gap", label: "Gap" },
        { value: "volume", label: "Volume" },
        { value: "updated", label: "Updated" }
      ], state.sort)) +
      field("Sort order", segmented("Sort order", [
        { value: "desc", label: "Descending" },
        { value: "asc", label: "Ascending" }
      ], state.order)) +
      '<div class="filters-row">' +
      field("Category", '<select class="select" id="category">' + categoryOptions + "</select>", "category") +
      field("Venue", '<select class="select" id="venue">' + venueOptions + "</select>", "venue") +
      "</div>" +
      field("Minimum combined volume",
        '<input class="input" id="min-volume" type="number" min="0" step="100" inputmode="numeric" ' +
        'placeholder="0" value="' + S.escapeHtml(state.minVolume) + '">', "min-volume") +
      '<label class="checkbox"><input type="checkbox" id="show-stale"' +
      (state.showStale ? " checked" : "") + "> Show stale prices</label>" +
      "</div>" +
      "</span>" +
      '<button class="btn btn--ghost" id="refresh" type="button">Refresh</button>' +
      "</div>" +
      "</div>"
    );
  }

  function venueRows(event) {
    return event.venues
      .map(function (venue) {
        return (
          '<div class="venue-row">' +
          '<span class="venue-name">' + S.escapeHtml(S.venueLabel(venue.venue)) + "</span>" +
          '<span class="venue-price">' +
          '<span class="price' + (venue.is_stale ? " price--stale" : "") + '">' +
          S.escapeHtml(S.price(venue.price)) + "</span>" +
          (venue.is_stale
            ? '<span class="stale-tag">stale</span><span class="dot dot--live" aria-hidden="true"></span>'
            : "") +
          "</span>" +
          '<span class="venue-volume">vol ' + S.escapeHtml(S.volume(venue.volume)) + "</span>" +
          "</div>"
        );
      })
      .join("");
  }

  function card(event) {
    var series = S.gapSeries(event);
    var spark = series.length > 1
      ? S.sparkline(series.map(function (point) { return point.gap; }))
      : '<p class="card-note">Gap history builds as runs accumulate.</p>';
    var stamp = latestStamp(event);
    return (
      '<article class="card enter">' +
      '<p class="label label--muted">' + S.escapeHtml(event.category || "Uncategorised") + "</p>" +
      '<h2 class="card-title"><a href="' + S.eventUrl(event.id) + '">' +
      S.escapeHtml(event.title) + "</a></h2>" +
      venueRows(event) +
      '<div class="row row--between card-gap-row">' +
      '<span class="label">Gap</span>' +
      '<span class="gap ' + S.gapClass(event.gap) + '">' + S.escapeHtml(S.gapText(event.gap)) + "</span>" +
      "</div>" +
      (event.gap == null
        ? '<p class="card-note muted">Fewer than two fresh prices, so no gap is shown.</p>'
        : "") +
      '<p class="card-note muted">Last update ' +
      S.escapeHtml(stamp ? S.timeAgo(new Date(stamp).toISOString()) : "unknown") +
      " · combined volume " + S.escapeHtml(S.volume(volumeOf(event))) +
      (event.rules_differ ? " · rules differ" : "") + "</p>" +
      '<div class="card-spark">' + spark + "</div>" +
      '<div class="card-foot">' +
      '<a class="btn btn--ghost" href="' + S.eventUrl(event.id) + '">View event</a>' +
      '<span class="label label--muted">' + S.escapeHtml(event.id) + "</span>" +
      "</div>" +
      "</article>"
    );
  }

  function pager(total) {
    var pages = Math.max(1, Math.ceil(total / PER_PAGE));
    if (total <= PER_PAGE) {
      return "";
    }
    return (
      '<div class="pager">' +
      '<button class="btn btn--ghost" type="button" id="prev-page"' +
      (state.page <= 1 ? " disabled" : "") + ">Previous</button>" +
      '<span class="label">Page ' + state.page + " of " + pages + "</span>" +
      '<button class="btn btn--ghost" type="button" id="next-page"' +
      (state.page >= pages ? " disabled" : "") + ">Next</button>" +
      "</div>"
    );
  }

  /* --- Rendering ------------------------------------------------------ */

  function renderResults() {
    var rows = visible();
    var pages = Math.max(1, Math.ceil(rows.length / PER_PAGE));
    if (state.page > pages) {
      state.page = pages;
    }
    var start = (state.page - 1) * PER_PAGE;
    var slice = rows.slice(start, start + PER_PAGE);

    document.getElementById("markets-count").textContent = rows.length
      ? "Showing " + (start + 1) + "–" + (start + slice.length) + " of " + rows.length + " events"
      : "Showing 0 of 0 events";

    document.getElementById("markets-grid").innerHTML = slice.length
      ? '<div class="card-grid card-grid--3">' + slice.map(card).join("") + "</div>"
      : '<div class="empty">No events match these filters. Widen the search, or turn ' +
        "stale prices back on.</div>";
    document.getElementById("markets-pager").innerHTML = pager(rows.length);
    wireResults();
  }

  function wireResults() {
    var previous = document.getElementById("prev-page");
    var next = document.getElementById("next-page");
    if (previous) {
      previous.addEventListener("click", function () {
        state.page = Math.max(1, state.page - 1);
        renderResults();
      });
    }
    if (next) {
      next.addEventListener("click", function () {
        state.page += 1;
        renderResults();
      });
    }
  }

  function renderToolbar() {
    document.getElementById("markets-toolbar").innerHTML = toolbar();
    wireToolbar();
  }

  function setState(patch) {
    Object.keys(patch).forEach(function (key) { state[key] = patch[key]; });
    if (!("page" in patch)) {
      state.page = 1;
    }
  }

  function closePanel() {
    var panel = document.getElementById("filters-panel");
    var toggle = document.getElementById("filters-toggle");
    if (panel) {
      panel.hidden = true;
    }
    if (toggle) {
      toggle.setAttribute("aria-expanded", "false");
    }
  }

  /** Document-level listeners, bound once so re-rendering cannot stack them. */
  function wireDocument() {
    document.addEventListener("click", function (event) {
      var panel = document.getElementById("filters-panel");
      var target = event.target;
      if (
        panel && !panel.hidden &&
        target && target.closest && !target.closest(".filter-anchor")
      ) {
        closePanel();
      }
    });
    document.addEventListener("keydown", function (event) {
      var panel = document.getElementById("filters-panel");
      var toggle = document.getElementById("filters-toggle");
      if (event.key === "Escape" && panel && !panel.hidden) {
        closePanel();
        if (toggle) {
          toggle.focus();
        }
      }
    });
  }

  function wireToolbar() {
    var search = document.getElementById("markets-search");
    var toggle = document.getElementById("filters-toggle");
    var panel = document.getElementById("filters-panel");

    search.addEventListener("input", function () {
      setState({ query: search.value });
      renderResults();
    });

    toggle.addEventListener("click", function () {
      var open = panel.hidden;
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });

    // Segmented controls: the panel's own groups are handled by delegation,
    // the status tabs by direct listeners, so no click is ever handled twice.
    function applySegment(segment) {
      var group = segment.getAttribute("data-group");
      var value = segment.getAttribute("data-value");
      if (group === "Sort by") {
        setState({ sort: value });
      } else if (group === "Sort order") {
        setState({ order: value });
      } else if (group === "Status") {
        setState({ status: value });
      }
      renderToolbar();
      renderResults();
    }

    panel.addEventListener("click", function (event) {
      var segment = event.target.closest(".segment");
      if (segment) {
        applySegment(segment);
      }
    });

    Array.prototype.forEach.call(
      document.querySelectorAll(".segmented .segment"),
      function (segment) {
        if (segment.closest(".popover")) {
          return; // the delegated panel handler owns these
        }
        segment.addEventListener("click", function () { applySegment(segment); });
      }
    );

    document.getElementById("category").addEventListener("change", function (event) {
      setState({ category: event.target.value });
      renderResults();
    });
    document.getElementById("venue").addEventListener("change", function (event) {
      setState({ venue: event.target.value });
      renderResults();
    });
    document.getElementById("min-volume").addEventListener("input", function (event) {
      setState({ minVolume: event.target.value });
      renderResults();
    });
    document.getElementById("show-stale").addEventListener("change", function (event) {
      setState({ showStale: event.target.checked });
      renderResults();
    });

    document.getElementById("refresh").addEventListener("click", function () {
      var button = document.getElementById("refresh");
      button.disabled = true;
      S.load()
        .then(function (document_) {
          data = document_;
          state.page = 1;
          renderToolbar();
          renderResults();
        })
        .catch(function (error) {
          document.getElementById("markets-grid").innerHTML =
            '<div class="empty">Could not reload data.json: ' + S.escapeHtml(error.message) + "</div>";
        })
        .then(function () { button.disabled = false; });
    });
  }

  var wiredDocument = false;

  function render(next) {
    data = next;
    if (!wiredDocument) {
      wireDocument();
      wiredDocument = true;
    }
    renderToolbar();
    renderResults();
  }

  S.start({ page: "markets", title: "Markets" }, render);
})();
