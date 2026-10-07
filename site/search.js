/* The client half of the search index, and the top bar's type-ahead.
 *
 * This mirrors searchindex.py, which is the reference reader for the same
 * files, so a search in the browser and a search in the smoke test agree:
 *
 *   1. data/search/index-0.json is the dictionary and the first slice of
 *      postings. It is fetched on the first focus of the search box - never on
 *      page load - because it is the one big file the brief allows a search to
 *      cost (1.9 MB at nine thousand markets).
 *   2. Every other shard is fetched only when a typed token actually lands in
 *      it, and fetched once.
 *   3. A posting is a market key, "<venue>:<market_id>". To draw a row for one,
 *      data/events-index.json says whether that market belongs to a matched
 *      event (and if so what its venue count, price range and gap are), and
 *      data/catalog/manifest.json names the shard a standalone market lives in.
 *
 * Tokenizing has to match the export exactly, or a word nobody can find looks
 * like a word nobody typed; matching.normalize/tokens is reimplemented below
 * with the same rules (dashes folded, "100k" -> 100000, "%" -> " percent",
 * stopwords and words of two letters or fewer dropped).
 */
(function () {
  "use strict";

  var S = window.SPREAD;

  var INDEX_FILE = "search/index-0.json";
  var EVENTS_FILE = "events-index.json";
  var MANIFEST_FILE = "catalog/manifest.json";

  /* How many candidates a query resolves into rows. A query for a common word
   * can match thousands of markets, and every standalone market it resolves
   * costs a catalog shard; the cap keeps a broad search bounded and the results
   * page says when it was hit. */
  var MAX_CANDIDATES = 300;
  var SUGGEST_LIMIT = 8;

  var STOPWORDS =
    ("a an and are as at be before by did do does for from has have in into is it its of on or " +
      "that the their there these this to was were will with within yes no if any about after " +
      "during between end than then when which who whom").split(" ");

  var DASHES = {
    "\u2019": "'",
    "\u2018": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-"
  };

  var REAL_MONEY = { prediction_market: true, sportsbook: true };

  function realMoneyKey(key) {
    // The venue NAME has to be mapped to its type first: reading the key's
    // venue as if it were a type made the test false for every key, which is
    // how alphabetical order put the play-money venue first on every search.
    return !!REAL_MONEY[S.VENUE_TYPES[keyVenue(key)] || ""];
  }

  /* --- Tokenizer (must match matching.normalize / matching.tokens) ------- */

  function normalize(text) {
    var source = String(text == null ? "" : text);
    var folded = "";
    for (var index = 0; index < source.length; index += 1) {
      var char = source.charAt(index);
      folded += DASHES[char] === undefined ? char : DASHES[char];
    }
    return folded
      .toLowerCase()
      .replace(/(\d+(?:\.\d+)?)\s*k\b/g, function (match, digits) {
        return String(Math.trunc(parseFloat(digits) * 1000));
      })
      .replace(/,/g, "")
      .replace(/(\d+(?:\.\d+)?)\s*%/g, "$1 percent")
      .replace(/[^a-z0-9.]+/g, " ")
      .replace(/\s+/g, " ")
      .replace(/^\s+|\s+$/g, "");
  }

  function tokenize(text) {
    var words = normalize(text).split(" ");
    var out = [];
    for (var index = 0; index < words.length; index += 1) {
      var word = words[index];
      if (!word || STOPWORDS.indexOf(word) !== -1) {
        continue;
      }
      if (word.length > 2 || /^[0-9]+$/.test(word)) {
        out.push(word);
      }
    }
    return out;
  }

  /* --- The index -------------------------------------------------------- */

  var loading = null;
  var eventsLoading = null;
  var index = null; // {tokens, bounds, postings, markets, vocab}
  var eventIndex = null; // {events, markets}
  var eventsById = {};
  var manifest = null;
  var state = { ready: false, eventsReady: false, error: null, shards: [] };

  /**
   * The matched-event index on its own: small (about 80 KB), and all the
   * results page needs to browse. Browsing must not cost the 1.9 MB dictionary,
   * which is why this is separate from load().
   */
  function loadEvents() {
    if (eventsLoading) {
      return eventsLoading;
    }
    eventsLoading = S.json(EVENTS_FILE)
      .catch(function () {
        return null;
      })
      .then(function (document) {
        eventIndex = document || { events: [], markets: {} };
        for (var index_ = 0; index_ < eventIndex.events.length; index_ += 1) {
          eventsById[eventIndex.events[index_].i] = eventIndex.events[index_];
        }
        state.eventsReady = true;
        return eventIndex;
      });
    return eventsLoading;
  }

  /** The token index too - what a search costs, and nothing loads it earlier. */
  function load() {
    if (loading) {
      return loading;
    }
    loading = Promise.all([
      S.json(INDEX_FILE),
      loadEvents(),
      S.json(MANIFEST_FILE).catch(function () {
        return null;
      })
    ])
      .then(function (results) {
        var first = results[0];
        index = {
          tokens: first.tokens || {},
          bounds: first.bounds || [],
          postings: { 0: first.postings || {} },
          markets: first.markets || 0,
          vocab: first.vocab || 0
        };
        manifest = results[2];
        state.ready = true;
        state.shards = ["search/index-0.json"];
        return index;
      })
      .catch(function (error) {
        loading = null;
        state.error = error && error.message ? error.message : String(error);
        throw error;
      });
    return loading;
  }

  function ready() {
    return state.ready;
  }

  function ensure(number) {
    if (index.postings[number] !== undefined) {
      return Promise.resolve();
    }
    return S.json("search/index-" + number + ".json").then(function (document) {
      index.postings[number] = document.postings || {};
      state.shards.push("search/index-" + number + ".json");
    });
  }

  function shardsFor(token) {
    var found = index.tokens[token];
    if (typeof found === "number") {
      return [found];
    }
    if (Object.prototype.toString.call(found) === "[object Array]") {
      return found;
    }
    // Not in the dictionary: fall back to the alphabet range, so a token that
    // only appears as part of a longer one still resolves.
    var out = [];
    for (var number = 0; number < index.bounds.length; number += 1) {
      if (index.bounds[number][0] <= token && token <= index.bounds[number][1]) {
        out.push(number);
      }
    }
    return out;
  }

  function look(token) {
    var numbers = shardsFor(token);
    return Promise.all(numbers.map(ensure)).then(function () {
      var keys = [];
      for (var index_ = 0; index_ < numbers.length; index_ += 1) {
        var found = index.postings[numbers[index_]][token] || [];
        keys = keys.concat(found);
      }
      return keys;
    });
  }

  /**
   * Every candidate market for a query, with how many of its tokens hit. All
   * tokens must match; when none of them all do - a half-typed word, or words
   * that never co-occur - it falls back to any-token, so the first keystrokes
   * still answer with something real rather than nothing.
   */
  function matches(text) {
    var list = tokenize(text).sort(function (a, b) {
      return b.length - a.length;
    });
    if (!list.length) {
      return Promise.resolve({});
    }
    return Promise.all(list.map(look)).then(function (lists) {
      var hits = {};
      for (var each = 0; each < lists.length; each += 1) {
        for (var key = 0; key < lists[each].length; key += 1) {
          var market = lists[each][key];
          hits[market] = (hits[market] || 0) + 1;
        }
      }
      var wanted = list.length;
      var strict = {};
      var anyStrict = false;
      for (var name in hits) {
        if (hits[name] === wanted) {
          strict[name] = hits[name];
          anyStrict = true;
        }
      }
      return anyStrict ? strict : hits;
    });
  }

  /**
   * Candidates in rank order. Ranking is deliberately not alphabetical: an id
   * order alone puts the play-money venue first on every search, and a site
   * that promises real-money comparisons should not bury them. So: more matched
   * tokens first, then a venue whose price can take part in a gap, then the key.
   */
  function ranked(text) {
    return matches(text).then(function (hits) {
      var keys = Object.keys(hits);
      keys.sort(function (left, right) {
        if (hits[left] !== hits[right]) {
          return hits[right] - hits[left];
        }
        var leftReal = realMoneyKey(left) ? 0 : 1;
        var rightReal = realMoneyKey(right) ? 0 : 1;
        if (leftReal !== rightReal) {
          return leftReal - rightReal;
        }
        return left < right ? -1 : left > right ? 1 : 0;
      });
      return { keys: keys, hits: hits };
    });
  }

  function keyVenue(key) {
    return String(key).split(":", 1)[0];
  }

  function count(text) {
    return matches(text).then(function (hits) {
      return Object.keys(hits).length;
    });
  }

  /* --- Resolving a key to a row ---------------------------------------- */

  var catalog = {}; // file -> {market_id: record}

  function shardFor(key) {
    if (!manifest) {
      return "";
    }
    var split = String(key).indexOf(":");
    var venue = String(key).slice(0, split);
    var marketId = String(key).slice(split + 1);
    var entry = (manifest.venues || {})[venue];
    if (!entry || !entry.shards) {
      return "";
    }
    var shards = entry.shards;
    var found = -1;
    for (var index_ = 0; index_ < shards.length; index_ += 1) {
      if (shards[index_].from <= marketId) {
        found = index_;
      } else {
        break;
      }
    }
    if (found < 0 || marketId > shards[found].to) {
      return "";
    }
    return shards[found].file;
  }

  function market(key) {
    var file = shardFor(key);
    if (!file) {
      return Promise.resolve(null);
    }
    if (catalog[file]) {
      return Promise.resolve(catalog[file][String(key).split(":")[1]] || null);
    }
    return S.json(file).then(function (document) {
      var byId = {};
      var markets = document.markets || [];
      for (var index_ = 0; index_ < markets.length; index_ += 1) {
        byId[markets[index_].i] = markets[index_];
      }
      catalog[file] = byId;
      return byId[String(key).split(":")[1]] || null;
    });
  }

  function eventFor(key) {
    if (!eventIndex) {
      return null;
    }
    var id = eventIndex.markets[key];
    return id ? eventsById[id] || null : null;
  }

  /**
   * One candidate as a row. The row is the market the query found, so its
   * title always answers the search; when that market is part of a matched
   * event the row also carries the event's venue count, the range across those
   * venues and the gap, and links to the event so the comparison can be read in
   * full. A market on its own says "1 venue" and links to the venue instead.
   */
  function rowFor(key, score) {
    return market(key).then(function (record) {
      if (!record) {
        return null;
      }
      var event = eventFor(key);
      if (!event) {
        var alone = S.marketRow(record, key, { score: score });
        alone.kind = "market";
        alone.key = key;
        alone.venue = record.v;
        alone.types = record.y ? [record.y] : [];
        alone.volume = record.w;
        alone.close = record.e;
        return alone;
      }
      var row = S.marketRow(record, key, { score: score });
      row.kind = "event";
      row.key = key;
      row.venue = record.v;
      row.types = event.y || [];
      row.volume = event.w;
      row.close = event.e;
      row.href = S.eventUrl(event.i);
      row.offsite = false;
      row.venues = event.n || 1;
      row.count = row.venues > 1 ? row.venues + " venues" : "1 venue";
      row.lo = event.lo;
      row.hi = event.hi;
      row.gap = event.g;
      row.possible = !!event.p;
      row.rulesDiffer = !!event.r;
      row.stale = !!event.x || !!record.x;
      return row;
    });
  }

  function rows(text, options) {
    options = options || {};
    var cap = options.limit || MAX_CANDIDATES;
    return ranked(text).then(function (found) {
      var total = found.keys.length;
      var chosen = found.keys.slice(0, cap);
      return Promise.all(
        chosen.map(function (key) {
          return rowFor(key, found.hits[key]);
        })
      ).then(function (built) {
        var out = [];
        for (var index = 0; index < built.length; index += 1) {
          if (built[index]) {
            out.push(built[index]);
          }
        }
        return { rows: out, matches: total, capped: total > cap };
      });
    });
  }

  /** Browse: the matched-event index is the only file it needs. */
  function browseRows() {
    return loadEvents().then(browse);
  }

  /** Every matched event as a row, widest gap first - the no-query browse. */
  function browse() {
    if (!eventIndex) {
      return { rows: [], matches: 0, capped: false };
    }
    var out = [];
    for (var index_ = 0; index_ < eventIndex.events.length; index_ += 1) {
      var event = eventIndex.events[index_];
      var row = S.eventRow(event, { score: 0 });
      row.kind = "event";
      row.id = event.i;
      row.types = event.y || [];
      row.volume = event.w;
      row.close = event.e;
      out.push(row);
    }
    return { rows: out, matches: out.length, capped: false };
  }

  /* --- The top bar's type-ahead ---------------------------------------- */

  function suggestItem(row, position) {
    var meta = row.gap == null ? row.count : row.count + " · " + S.gapText(row.gap);
    return (
      '<button class="suggest-item" type="button" role="option" id="spread-option-' +
      position +
      '" aria-selected="false" data-href="' +
      S.esc(row.href) +
      '" data-offsite="' +
      (row.offsite ? "1" : "0") +
      '"><span class="suggest-title">' +
      S.esc(row.title) +
      '</span><span class="suggest-meta">' +
      S.esc(meta) +
      "</span></button>"
    );
  }

  function init() {
    var input = document.getElementById("spread-q");
    var panel = document.getElementById("spread-suggest");
    if (!input || !panel || input.getAttribute("data-wired")) {
      return;
    }
    input.setAttribute("data-wired", "1");

    // The results page carries its query in the URL, so the box starts filled.
    var params = new URLSearchParams(window.location.search);
    if (params.get("q")) {
      input.value = params.get("q");
    }

    var items = [];
    var selected = -1;
    var timer = null;
    var lastQuery = "";

    function close() {
      panel.hidden = true;
      panel.innerHTML = "";
      input.setAttribute("aria-expanded", "false");
      items = [];
      selected = -1;
    }

    function choose(position) {
      var item = items[position];
      if (!item) {
        return;
      }
      if (item.offsite) {
        window.open(item.href, "_blank", "noopener");
      } else {
        window.location.href = item.href;
      }
    }

    function open() {
      panel.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function draw(html) {
      panel.innerHTML =
        html +
        '<div class="suggest-foot"><span>Enter to search all</span><span>Esc to close</span></div>';
      items = [];
      var found = panel.querySelectorAll(".suggest-item");
      for (var index = 0; index < found.length; index += 1) {
        items.push({
          href: found[index].getAttribute("data-href"),
          offsite: found[index].getAttribute("data-offsite") === "1"
        });
      }
      open();
    }

    function highlight(position) {
      var found = panel.querySelectorAll(".suggest-item");
      for (var index = 0; index < found.length; index += 1) {
        found[index].setAttribute("aria-selected", String(index === position));
      }
      selected = position;
      if (position >= 0 && found[position]) {
        input.setAttribute("aria-activedescendant", found[position].id);
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function run(query) {
      if (!query) {
        close();
        return;
      }
      if (!state.ready) {
        draw('<p class="suggest-empty">Loading the search index…</p>');
        return;
      }
      ranked(query).then(function (found) {
        if (found.keys.length === 0) {
          draw('<p class="suggest-empty">No market matches “' + S.esc(query) + "”.</p>");
          return;
        }
        var chosen = found.keys.slice(0, SUGGEST_LIMIT);
        return Promise.all(
          chosen.map(function (key) {
            return rowFor(key, found.hits[key]);
          })
        ).then(function (built) {
          var html = "";
          var shown = 0;
          for (var index2 = 0; index2 < built.length; index2 += 1) {
            if (!built[index2] || shown >= SUGGEST_LIMIT) {
              continue;
            }
            html += suggestItem(built[index2], shown);
            shown += 1;
          }
          draw(html);
        });
      });
    }

    function schedule() {
      var query = input.value.replace(/^\s+|\s+$/g, "");
      lastQuery = query;
      if (timer) {
        window.clearTimeout(timer);
      }
      if (!query) {
        close();
        return;
      }
      if (!state.ready) {
        // The first focus is what pays for the dictionary; typing before it
        // lands just says so rather than dropping the keystroke.
        open();
        panel.innerHTML = '<p class="suggest-empty">Loading the search index…</p>';
        load().then(function () {
          if (input.value.replace(/^\s+|\s+$/g, "") === lastQuery) {
            run(lastQuery);
          }
        });
        return;
      }
      timer = window.setTimeout(function () {
        if (input.value.replace(/^\s+|\s+$/g, "") === lastQuery) {
          run(lastQuery);
        }
      }, 110);
    }

    input.addEventListener("focus", function () {
      load().then(function () {
        if (!input.value) {
          return;
        }
        run(input.value.replace(/^\s+|\s+$/g, ""));
      });
    });
    input.addEventListener("input", schedule);
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!items.length) {
          return;
        }
        event.preventDefault();
        var next = selected + (event.key === "ArrowDown" ? 1 : -1);
        if (next < 0) {
          next = items.length - 1;
        }
        if (next >= items.length) {
          next = 0;
        }
        highlight(next);
        return;
      }
      if (event.key === "Enter") {
        if (selected >= 0 && items[selected]) {
          event.preventDefault();
          choose(selected);
        }
        return;
      }
      if (event.key === "Escape") {
        close();
        input.blur();
      }
    });
    input.addEventListener("blur", function () {
      window.setTimeout(close, 120);
    });
    panel.addEventListener("mousedown", function (event) {
      var target = event.target.closest ? event.target.closest(".suggest-item") : null;
      if (!target) {
        return;
      }
      event.preventDefault();
      if (target.getAttribute("data-offsite") === "1") {
        window.open(target.getAttribute("data-href"), "_blank", "noopener");
      } else {
        window.location.href = target.getAttribute("data-href");
      }
    });

    // "/" focuses the box from anywhere that is not already typing.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }
      var target = event.target;
      var tag = target && target.tagName ? target.tagName.toLowerCase() : "";
      if (
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        (target && target.isContentEditable)
      ) {
        return;
      }
      event.preventDefault();
      input.focus();
      input.select();
    });
  }

  window.SpreadSearch = {
    init: init,
    load: load,
    ready: ready,
    normalize: normalize,
    tokenize: tokenize,
    matches: matches,
    count: count,
    ranked: ranked,
    rows: rows,
    browse: browse,
    browseRows: browseRows,
    market: market,
    eventFor: eventFor,
    events: function () {
      return eventIndex ? eventIndex.events : [];
    },
    params: function () {
      return new URLSearchParams(window.location.search);
    },
    MAX_CANDIDATES: MAX_CANDIDATES,
    state: state
  };
})();
