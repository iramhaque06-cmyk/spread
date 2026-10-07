/* The full results page: filters, four sorts, and pagination.
 *
 * The rows themselves come from search.js, which is the same engine the top
 * bar's dropdown uses - so a dropdown hit and a result row can never disagree.
 * A search resolves at most search.js' MAX_CANDIDATES markets and says so when
 * it does; that cap is what keeps a one-word query from fetching a whole
 * catalog.
 *
 * With no query in the URL the page browses instead: every matched event,
 * widest gap first, straight out of events-index.json. That is the site's whole
 * point, so it is what the page shows before a reader types anything.
 */
(function () {
  "use strict";

  var S = window.SPREAD;
  var PAGE_SIZE = 25;

  var SORTS = [
    { id: "best", label: "Best match" },
    { id: "gap", label: "Gap" },
    { id: "volume", label: "Volume" },
    { id: "closing", label: "Closing soon" }
  ];

  var CATEGORIES = ["Politics", "Economy", "Sports", "Crypto", "Other"];
  var TYPES = ["prediction_market", "sportsbook", "play_money"];

  var filters = { categories: [], types: [], comparison: false };
  var sort = "best";
  var page = 1;
  var query = "";
  var source = { rows: [], matches: 0, capped: false };

  function toggled(list, value) {
    var out = [];
    for (var index = 0; index < list.length; index += 1) {
      if (list[index] !== value) {
        out.push(list[index]);
      }
    }
    return out;
  }

  function has(list, value) {
    return list.indexOf(value) !== -1;
  }

  function passes(row) {
    if (filters.categories.length && !has(filters.categories, row.bucket)) {
      return false;
    }
    if (filters.types.length) {
      var matched = false;
      for (var index = 0; index < row.types.length; index += 1) {
        if (has(filters.types, row.types[index])) {
          matched = true;
        }
      }
      if (!matched) {
        return false;
      }
    }
    if (filters.comparison && !(row.venues > 1)) {
      return false;
    }
    return true;
  }

  function compare(left, right) {
    if (sort === "gap") {
      return descending(left.gap, right.gap);
    }
    if (sort === "volume") {
      return descending(left.volume, right.volume);
    }
    if (sort === "closing") {
      return ascending(left.close, right.close);
    }
    if (left.score !== right.score) {
      return right.score - left.score;
    }
    if (!!left.gap !== !!right.gap) {
      return left.gap ? -1 : 1;
    }
    return descending(left.gap, right.gap);
  }

  function descending(left, right) {
    var a = left == null ? -1 : Number(left);
    var b = right == null ? -1 : Number(right);
    return b - a;
  }

  function ascending(left, right) {
    var a = left ? Date.parse(left) : Infinity;
    var b = right ? Date.parse(right) : Infinity;
    if (isNaN(a)) {
      a = Infinity;
    }
    if (isNaN(b)) {
      b = Infinity;
    }
    return a - b;
  }

  function visible() {
    var out = [];
    for (var index = 0; index < source.rows.length; index += 1) {
      if (passes(source.rows[index])) {
        out.push(source.rows[index]);
      }
    }
    out.sort(compare);
    return out;
  }

  function chip(list, value, label, name) {
    return (
      '<button class="chip" type="button" data-filter="' +
      name +
      '" data-value="' +
      S.esc(value) +
      '" aria-pressed="' +
      (has(list, value) ? "true" : "false") +
      '">' +
      S.esc(label) +
      "</button>"
    );
  }

  function filterbar() {
    var active = filters.categories.length + filters.types.length + (filters.comparison ? 1 : 0);
    var categoryChips = "";
    for (var index = 0; index < CATEGORIES.length; index += 1) {
      categoryChips += chip(filters.categories, CATEGORIES[index], CATEGORIES[index], "category");
    }
    var typeChips = "";
    for (var each = 0; each < TYPES.length; each += 1) {
      typeChips += chip(filters.types, TYPES[each], S.venueTypeLabel(TYPES[each]), "type");
    }
    var sorts = "";
    for (var sortIndex = 0; sortIndex < SORTS.length; sortIndex += 1) {
      sorts +=
        '<option value="' +
        SORTS[sortIndex].id +
        '"' +
        (SORTS[sortIndex].id === sort ? " selected" : "") +
        ">" +
        S.esc(SORTS[sortIndex].label) +
        "</option>";
    }
    return (
      '<div class="filterbar">' +
      '<div class="filterrow"><span class="label">Category</span><div class="chips">' +
      categoryChips +
      "</div></div>" +
      '<div class="filterrow"><span class="label">Venue type</span><div class="chips">' +
      typeChips +
      '<button class="chip" type="button" data-filter="comparison" data-value="1" ' +
      'aria-pressed="' +
      (filters.comparison ? "true" : "false") +
      '">Has comparison</button>' +
      "</div></div>" +
      '<div class="filterrow"><span class="label">Sort</span>' +
      '<select class="select select--auto" id="sort">' +
      sorts +
      "</select>" +
      (active
        ? '<button class="btn btn--ghost btn--sm" type="button" id="clear">Clear ' +
          active +
          " filter" +
          (active === 1 ? "" : "s") +
          "</button>"
        : "") +
      "</div></div>"
    );
  }

  /**
   * What the list is, in one line. It says "N of M" only when a filter is
   * actually hiding rows - "47 of 47" is two numbers where one will do - and
   * it counts what the list really holds: events priced by two or more venues.
   */
  function countLine(rows) {
    var filtered = rows.length !== source.matches;
    var text;
    if (query) {
      text =
        (filtered ? S.num(rows.length) + " of " : "") +
        S.num(source.matches) +
        " matching market" +
        (source.matches === 1 ? "" : "s");
      if (source.capped) {
        text +=
          " · the top " + S.num(SpreadSearch.MAX_CANDIDATES) + " are shown, so refine the query";
      }
    } else {
      text =
        (filtered ? S.num(rows.length) + " of " : "") +
        S.num(source.matches) +
        " event" +
        (source.matches === 1 ? "" : "s") +
        " priced by more than one venue";
    }
    return '<p class="label label--muted" style="margin:16px 0 8px">' + S.esc(text) + "</p>";
  }

  function pager(total) {
    var pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (pages < 2) {
      return "";
    }
    return (
      '<div class="pager">' +
      '<button class="btn btn--ghost btn--sm" type="button" id="prev"' +
      (page <= 1 ? " disabled" : "") +
      ">Previous</button>" +
      '<span class="pager-status">Page ' +
      S.esc(String(page)) +
      " of " +
      S.esc(String(pages)) +
      "</span>" +
      '<button class="btn btn--ghost btn--sm" type="button" id="next"' +
      (page >= pages ? " disabled" : "") +
      ">Next</button></div>"
    );
  }

  function resultsHtml(rows) {
    if (!rows.length) {
      return (
        '<div class="empty"><span class="label">Nothing to show</span>' +
        "<p>" +
        (source.rows.length
          ? "No result passes the filters above. Clear one and try again."
          : query
            ? "No market in the catalog contains “" + S.esc(query) + "”."
            : "No event has been matched across venues yet.") +
        "</p></div>"
      );
    }
    var start = (page - 1) * PAGE_SIZE;
    var html = "";
    var slice = rows.slice(start, start + PAGE_SIZE);
    for (var index = 0; index < slice.length; index += 1) {
      html += S.rowHtml(slice[index]);
    }
    return '<ul class="results-list">' + html + "</ul>" + pager(rows.length);
  }

  function draw() {
    var body = document.getElementById("page-body");
    var rows = visible();
    if (page > Math.max(1, Math.ceil(rows.length / PAGE_SIZE))) {
      page = 1;
    }
    body.innerHTML =
      '<div class="page-head"><h1 class="headline">' +
      (query ? "Results" : "Matched events") +
      "</h1></div>" +
      filterbar() +
      countLine(rows) +
      resultsHtml(rows) +
      S.notes([
        {
          title: "How this is calculated",
          body:
            "<p>A result is one event, not one market: markets that the pipeline has matched to " +
            "the same real-world event are gathered into a single row, which is why the row can " +
            "show how many venues price it and how far apart they are. A market with no " +
            "cross-venue match says <strong>1 venue</strong> and has no gap.</p>" +
            "<p>The category is a display grouping: the venues' own categories are inconsistent, " +
            "so each question is bucketed from its wording. The gap itself is the pipeline's - " +
            "highest fresh real-money price minus lowest - and is never recomputed here.</p>"
        },
        {
          title: "Data freshness",
          body:
            "<p>The catalog and the token index are rebuilt every six hours; prices, the matched " +
            "events and the home document every thirty minutes. A search therefore finds a " +
            "brand-new market within six hours and a new price within thirty minutes.</p>"
        },
        {
          title: "Sources",
          body:
            "<p>Read-only public APIs: Kalshi's trade API, Polymarket's Gamma and CLOB APIs, and " +
            "Manifold's public API. Sportsbook prices, when present, come from The Odds API and " +
            "are shown as de-vigged implied probabilities. Bookmaker names are plain data.</p>"
        }
      ]);
    wire();
  }

  function wire() {
    var body = document.getElementById("page-body");
    var chips = body.querySelectorAll("[data-filter]");
    for (var index = 0; index < chips.length; index += 1) {
      chips[index].addEventListener("click", function (event) {
        var target = event.currentTarget;
        var name = target.getAttribute("data-filter");
        var value = target.getAttribute("data-value");
        if (name === "category") {
          filters.categories = has(filters.categories, value)
            ? toggled(filters.categories, value)
            : filters.categories.concat([value]);
        } else if (name === "type") {
          filters.types = has(filters.types, value)
            ? toggled(filters.types, value)
            : filters.types.concat([value]);
        } else {
          filters.comparison = !filters.comparison;
        }
        page = 1;
        draw();
      });
    }
    var select = document.getElementById("sort");
    if (select) {
      select.addEventListener("change", function (event) {
        sort = event.currentTarget.value;
        page = 1;
        draw();
      });
    }
    var clear = document.getElementById("clear");
    if (clear) {
      clear.addEventListener("click", function () {
        filters = { categories: [], types: [], comparison: false };
        page = 1;
        draw();
      });
    }
    var prev = document.getElementById("prev");
    if (prev) {
      prev.addEventListener("click", function () {
        page = Math.max(1, page - 1);
        draw();
      });
    }
    var next = document.getElementById("next");
    if (next) {
      next.addEventListener("click", function () {
        page += 1;
        draw();
      });
    }
  }

  function render(home) {
    var body = document.getElementById("page-body");
    if (!body || !home) {
      return;
    }
    // The form in the top bar owns the query; this page reads the same URL, so
    // a search submitted anywhere lands here with the box already filled.
    query = (SpreadSearch.params().get("q") || "").replace(/^\s+|\s+$/g, "");
    body.innerHTML =
      '<div class="page-head"><h1 class="headline">' +
      (query ? "Results" : "Matched events") +
      '</h1></div>';

    if (!query) {
      // Browsing needs the matched events only. The 1.9 MB token index is not
      // fetched until there is a query - or the reader focuses the search box,
      // which is the same thing.
      SpreadSearch
        .browseRows()
        .then(function (found) {
          source = found;
          draw();
        })
        .catch(unavailable);
      return;
    }
    SpreadSearch.load()
      .then(function () {
        return SpreadSearch.rows(query).then(function (found) {
          source = found;
          draw();
        });
      })
      .catch(unavailable);
  }

  function unavailable(error) {
    var body = document.getElementById("page-body");
    // The cause is for whoever is debugging, not for the page: a reader cannot
    // act on a failed fetch, and the browser has already logged it.
    if (window.console && console.error) {
      console.error("Spread: could not load the market files", error);
    }
    body.innerHTML =
      '<div class="empty"><span class="label">Search unavailable</span><p>' +
      "The market files could not be loaded. Check your connection and reload the page." +
      "</p></div>";
  }

  S.boot({ page: "search", title: "Search" }, render);
})();
