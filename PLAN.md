# PLAN — Spread: from 3-event prototype to search-first comparison site

Status: **Phase 1 complete.** No code has been written. Venue decisions are settled (§6).
Waiting for the user to reply **"go"** before Phase 2.
Audit date: **2026-10-06**. Every endpoint below was called live (or read from the venue's own
docs) during this audit; nothing is guessed. Where a fact is unverified it says so.

---

## 0. What the audit found (read this first)

Three venues can be used as-is, one must be skipped, one needs a user decision, and one
sports-odds provider is confirmed. The catalog is much larger than the prototype assumes:

| Venue | Open markets measured | Notes |
|---|---|---|
| Kalshi | **> 200,000** total; **> 120,000** excluding parlay combos | cursor was still not exhausted when the scan was capped |
| Polymarket | **> 20,000** | keyset cursor still not exhausted at 20,000 (see the pagination row: the cursor request parameter is `after_cursor`, and the earlier 12,000 figure came from sending the wrong name, which silently repeated page 1) |
| Manifold | **39,645** | scan exhausted, so this is the true count |
| **Total** | **≈ 180,000+ markets** | before matching, before sports |

The 175k figure is the single most important input to the file/size plan (§4) and the risk
list (§5). It also means "show every market" and "load fast" pull in opposite directions, so
the plan proposes a default and flags it as a decision (§6).

---

## 1. Venue audit

### 1.1 Included venues

| | **Kalshi** | **Polymarket** | **Manifold** |
|---|---|---|---|
| **Type** | `prediction_market` — real money (CFTC-regulated US exchange) | `prediction_market` — real money (CLOB, on-chain) | `play_money` — mana, explicitly not cash |
| **List-all-open endpoint** | `GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&cursor={cursor}` | `GET https://gamma-api.polymarket.com/markets/keyset?closed=false&limit=100&after_cursor={cursor}` | `GET https://api.manifold.markets/v0/search-markets?filter=open&sort=newest&limit=1000&beforeTime={createdTime}` |
| **Pagination, verified live** | Cursor: 1000 rows/page, cursor advances, **0 duplicates in 200,000 rows scanned** | Keyset. Plain `?offset=` breaks past ~2,100 → HTTP 422 *"offset too large, use /markets/keyset"*. The response carries `next_cursor`, but the **request** parameter is **`after_cursor`** — sending `next_cursor` (or `cursor`, `after`, `key`, `token`) is **silently ignored and repeats page 1 forever**. With `after_cursor`: **zero overlap, 20,000 deduped open markets in 200 pages, not exhausted** | `offset` up to 1000, then `beforeTime` = `createdTime` of the last row. Verified: 40 pages, exhausted at 39,645 |
| **Parlay handling** | **Required**: `mve_filter=exclude` (verified: drops `KXMVECROSSCATEGORY`; still >120k rows) | none needed | none needed |
| **Batch price lookup** | `GET /markets?tickers=A,B,C` (verified, returns exactly those) | Gamma **`?slug={slug}`** for one — `?id=` wants the numeric id and answers **422 for a slug**, and stored ids are slugs; CLOB `/prices-history?market={tokenId}` for a timestamp | `GET /v0/market-probs?ids=A&ids=B` (≤100 ids, 1 s cache). **A one-element list is a trap:** requests encodes it as a bare `?ids=A`, and Manifold's validator answers **400 "Expected array, received string"** — so a lone id must take `GET /v0/market/{{id}}` instead (measured 2026-10-07). Markets with no binary `prob` return `answerProbs` (often empty) and therefore have no single price to store |
| **Auth** | none for market data (all audit calls were keyless) | none (all audit calls keyless) | none |
| **Rate limits** | Docs give **authenticated** token buckets (Basic read 200 tok/s, default cost 10 tok ⇒ ~20 req/s; 429 has no `Retry-After`). Keyless limits are undocumented ⇒ **we self-limit to ≤3 req/s** | Documented per IP: Gamma `/markets` **300 req/10 s**, CLOB `/prices-history` **1,000 req/10 s**; excess is queued, not rejected | **500 req/min per IP** (docs) |
| **Terms on automated access + display** | Public developer docs invite read-only market-data use; `robots.txt` allows all; **no redistribution prohibition found**. Confidence: medium-high | Public docs for public data; the standard ToU is about trading/accounts, **no display prohibition found**. Confidence: medium-high (ToU is long and not machine-readable) | **Explicit**: "Permitted uses: building bots, automated trading systems, algorithmic tools, and integrations". Scraping *outside* the API and rate-limit circumvention are prohibited. AI/ML training on API data needs a licence for commercial use; **personal / non-commercial projects permitted** |
| **Trade time published?** | **No** (existing project finding). Freshness from `volume_24h` | No per-market stamp in Gamma (batch `updatedAt`); CLOB last point is the real one (existing finding) | `lastBetTime` / `lastUpdatedTime` (ms epoch) — a real per-market signal |
| **Category** | **Not on the market record** — `category` was `null` for all 1000 sampled markets. Comes from `/events?status=open` (verified, >8,000 events, has `category`) and `/series` | `category: null` on the sample too; Gamma uses tags/events. Use the parent event's tag when present | `groupSlugs` on the market, and `/v0/groups` (500/page) |
| **Price fields verified** | 1000/1000 sampled: `last_price_dollars`, `yes_bid_dollars`/`yes_ask_dollars`, `volume_fp`, `volume_24h_fp` | `lastTradePrice`, `bestBid`/`bestAsk`, `volumeNum`, `volume24hr` | `probability` (0-1), `volume`, `volume24Hours` |
| **Confidence** | **High** on endpoint/auth/pagination/fields. Medium on rate limits for keyless use | **High** on endpoint/auth/pagination (422 → keyset was the key discovery). High on rate limits (docs) | **High** on everything (docs + live) |

### 1.2 Skipped: PredictIt — terms forbid this use

The endpoint works: `GET https://www.predictit.org/api/marketdata/all/` returned **HTTP 200,
433 KB, 5 requests/minute** (`X-Rate-Limit-Limit-Minute: 5`), keyless, with contract-level
`lastTradePrice` / `bestBuyYesCost`. It is a real-money venue and would have been welcome.

But its Terms and Conditions, clause *d*, say (quoted from the official page):

> "Use any manual or automated software, devices or other processes to engage in automated
> trading, **to scrape or extract data from the Website**, or otherwise to …"

That is exactly what a catalog crawler does. Instruction: *skip any venue whose terms forbid
this use* ⇒ **PredictIt is excluded**, despite the working feed. (Endpoint recorded above only
so the decision is auditable.)

### 1.3 Skipped: Metaculus — no longer a public API

Verified live on 2026-10-06:

- `GET https://www.metaculus.com/api/posts/?limit=2` → **403**
- `GET https://www.metaculus.com/api2/questions/?limit=2` → **403**
- Body: *"Permission Error: The API is only available to authenticated users. Please create an
  account and use your API token to access the API."*

Its API docs now say to send a token in the `Authorization` header, and its ToU restricts
automated access outside sanctioned interfaces (the API itself being sanctioned). It is a
**forecast** venue, so it could never enter a gap anyway — it would sit in the
"shown separately" column like Manifold.

**Options:** skip it (default), or include it if you paste/hold a Metaculus API token.
A token is a secret and would live in `.env` beside `ODDS_API_KEY`. **User decision required.**

### 1.4 Sports odds: The Odds API (recommended)

| | **The Odds API (v4)** |
|---|---|
| **Endpoints** | `GET https://api.the-odds-api.com/v4/sports?apiKey=` — in-season sports, **free, no quota**<br>`GET /v4/sports/{sport}/events?apiKey=` — event list, **free, no quota**<br>`GET /v4/sports/{sport}/odds?apiKey=&regions=us&markets=h2h` — the odds |
| **Pagination** | Not paginated (returns the current slate per sport) — verified from docs; `/events` gives the same ids to filter on |
| **Auth** | Yes — API key as a query parameter |
| **Quota** | **500 credits / month free**, reset on the 1st. `x-requests-remaining`, `x-requests-used`, `x-requests-last` on every response. Cost = markets × regions (h2h + 1 region = **1 credit**) |
| **Terms on display** | **Verified, and unusually clear**: "Storing our data and retaining it indefinitely", "Displaying our data in a UI, website, or mobile app, including for commercial use", "Calculating and displaying values you derive from our data" are all **permitted**. Prohibited: reselling the data *as a raw feed/standalone product*. Attribution not required. Responsible-gambling messaging encouraged — the site already carries 18+ |
| **Price type** | `sportsbook` (real money). Decimal odds → implied probability `1/decimal` → **de-vig by normalizing across all outcomes** |
| **Scraping** | **None.** Only this aggregated API; no sportsbook or betting site is ever touched |
| **Confidence** | **High** (docs read in full + endpoint shapes verified) |

**Quota math for the 80 % rule:** 80 % of 500 = 400 credits. Sports odds are fetched only for
events already matched to a prediction market (or top by volume), at 1 market × 1 region = 1
credit each. At 2 runs/hour for 2 hours of a match window, even 10 matched events × 2 updates
= 40 credits/day = ~1,200/month — **too much**. So the plan budgets **≤ 8 credits per run,
≤ 3 runs/day for any single event, hard stop at 400/month**, keeps the last known odds when the
quota is spent, and shows their age (requirement restated in §3.4).

**Not chosen:** TheRundown (20k datapoints/day, terms less clear) and SportsGameOdds. The Odds
API is the only one whose free tier *and* display terms are verified here; if you prefer another,
say so and I'll audit it before "go". **No new dependency is needed** for any of this: stdlib +
`requests` + `rapidfuzz` cover it (CLAUDE.md).

---

## 2. Data model

One normalized record, whichever venue it came from. SQLite (`spread.db`) is the store; the
JSON export uses **compact field names** (column `json_key`).

### 2.1 `markets` table

| Field | Type | Meaning |
|---|---|---|
| `venue` | TEXT | `kalshi` \| `polymarket` \| `manifold` \| `odds_api` |
| `venue_type` | TEXT | `prediction_market` \| `sportsbook` \| `play_money` \| `forecast` |
| `market_id` | TEXT | the venue's own id (Kalshi ticker, Polymarket id/slug, Manifold id, event+book key) |
| `event_ref` | TEXT | the venue's parent (Kalshi `event_ticker`, Polymarket event slug, Manifold group) — used for category and URL |
| `title` | TEXT | the question, verbatim |
| `category` | TEXT | from the parent series/event/tag (Kalshi market records do **not** carry it) |
| `close_time` | TEXT | ISO-8601 UTC, when the venue says it settles |
| `outcomes` | TEXT (JSON) | `[{"name","price","token_id"?}]`; binary = YES/NO |
| **`price`** | REAL | **0–100 points** for the headline outcome (YES), matching the existing prototype convention |
| `price_kind` | TEXT | `last` \| `mid` \| `implied` \| `implied_devig` |
| `price_raw` | REAL | sportsbook only: the raw decimal odds |
| `price_devig` | REAL | sportsbook only: de-vigged, normalized across outcomes |
| `volume` | INTEGER | lifetime, venue's own number |
| `volume_24h` | INTEGER | trailing 24 h where published |
| `updated_at` | TEXT | the venue's own record stamp (never claimed to be a trade time) |
| `price_as_of` | TEXT | when the price itself moved, where the venue publishes it |
| `as_of_kind` | TEXT | `last_price_change` \| `batch_stamp` \| `volume_24h` \| `none` (existing vocabulary) |
| `url` | TEXT | canonical public page |
| `rule_text` | TEXT | venue's resolution rule, verbatim |
| `status` | TEXT | venue's own status |
| `is_open` | INTEGER | our reading of "open", refreshed each catalog pass |
| `fetched_at`, `first_seen_at`, `last_seen_at` | TEXT | provenance; `last_seen_at` lets a disappeared market be marked closed instead of deleted |

Primary key: `(venue, market_id)`. Indexes on `volume DESC`, `(venue, is_open)`, `close_time`.

### 2.2 Matching tables

- **`events`** — one row per real-world event: `event_key`, `title`, `category`, `close_time`,
  `rules_differ`, `confidence`, `possible` (1 when confidence < 85 on the 0–100 scale),
  `published` (1 when the comparison may be shown as a real one), `venue_count`.
- **`event_members`** — `(event_key, venue, market_id, confidence, rules_differ, matched_on,
  inverted)`. A market belongs to at most one event, and **one event holds at most one market
  per venue** (enforced by taking that venue's best-connected market, asserted after every
  pass, and re-checked by `verify_events.py`). `inverted` marks the "above" side of an
  above/below pair, whose price reads as `100 - price`.
- **Matching rules (Phase 2):**
  - **candidates:** each market is offered the markets it shares a token with, weighted by
    inverse document frequency, and keeps the ten heaviest cross-venue ones.
  - non-sports, in order: rapidfuzz `token_set_ratio` on normalized titles, **plus**
    close-date proximity (`DATE_WINDOW_DAYS = 3`, one day for sports), **plus** three hard
    checks that reject rather than weaken a pair — the **strikes** (numbers with units, years
    and dates stripped: "0.7%" ≠ "0.8%", "1600 or higher" ≠ "2000 or higher"), the **entities**
    (proper nouns, tickers, team alias table: an Iowa governor race is not a California one),
    and the **directions** (above/below, before/after, win/lose). One clean exception: an
    above/below pair on the same strike and instant is one event read from its two sides,
    stored with `inverted`.
  - sports: **teams (alias table) + start date within 1 day + market type = winner/moneyline
    only**, with confidence built from that evidence rather than a constant. Sport keys come
    from The Odds API; team aliases live in a small JSON file (e.g. "LA Lakers"/"Los Angeles
    Lakers").
  - confidence < 85 ⇒ `possible = 1`, shown as "possible match", **never** counted in a gap,
    and never `published`.
- **Gap rule (unchanged in spirit, widened in scope):** gap = **max − min of fresh prices from
  `venue_type IN ('prediction_market','sportsbook')` only**, in points; `null` when fewer than
  two fresh real-money venues. `play_money` and `forecast` rows are rendered in their own
  section and **never enter a gap**. Matches below 85 excluded. `rules_differ` = 1 is shown
  as a caveat, and **a differing-rules pair still shows its gap but is flagged** (same as today).
  `published` = confidence ≥ 85 **and** at least two distinct real-money venues with a fresh
  price; a play-money or forecast venue may attach to a published event and never counts
  toward those two.

### 2.3 Support tables

- `odds_quota` — `month, used, remaining, last_call_at, last_cost` (from the response headers).
- `catalog_state` — per venue: `last_catalog_ok_at`, `pages`, `rows`, `cursor_bookmark`,
  `errors` (resumable pagination; a run that dies mid-scan resumes).
- `price_snapshots` — reused as-is (append-only history), with `event_key` becoming the matched
  event id; the curated 3 events keep their existing keys so the current site does not break.

---

## 3. Refresh plan

### 3.1 Catalog refresh — every 6 hours

One full listing pass per venue, paginated, resumable, cached:

| Venue | Requests (measured) | Wall time at ≤3 req/s |
|---|---|---|
| Kalshi (`mve_filter=exclude`) | ≥ 120 pages × 1000 | ~1 min + pauses |
| Polymarket (keyset 100/page) | ≥ 200 pages | ~1 min |
| Manifold (1000/page) | 40 pages | ~15 s |

- Upsert every market; set `is_open = 0` for anything not seen in the pass (never delete).
- Category is backfilled from Kalshi `/events` and `/series` and Polymarket tags, cached for 24 h.
- Cache: every response goes through the existing `venues.get_json` disk cache; the 6 h pass
  runs with `max_age=0` (full refresh) but ETag/If-Modified-Since are sent where the venue
  offers them, so an unchanged page costs no body bytes.

### 3.2 Price refresh — every 30 minutes

Scope is **exactly** what the brief asks: the **top 500 markets by volume** plus **every market
in a matched event**.

- Kalshi: `GET /markets?tickers=A,B,…` — verified batch-by-ticker, ~1 request per 100–200
  tickers.
- Polymarket: Gamma `?id=` per market (1 request each, 300 req/10 s allowed) or CLOB
  `/prices-history` only for the markets where a timestamp is needed.
- Manifold: `GET /v0/market-probs?ids=…` — up to 100 ids per request with a 1 s cache, so the
  whole top-500 set is ~5 requests.
- Every fetched price appends one `price_snapshots` row (idempotent per run), exactly as today.
- Freshness is judged per venue by the existing `freshness.py` rules (24 h; Kalshi via
  `volume_24h`, Polymarket via the CLOB last point, Manifold via `lastBetTime`).

### 3.3 Caching and rate-limit handling

- **Per-venue limiter** (token bucket, one per host) with the ceilings in §1.1; a single shared
  `get_json` upgrade so every caller is automatically paced.
- **Retries:** 3 attempts on 429/5xx/connection errors; exponential backoff with jitter
  (1 s → 4 s → 15 s); honour `Retry-After` when present (Kalshi sends none).
- **Circuit breaker:** > 5 consecutive failures for a venue ⇒ stop that venue for the run, keep
  serving the stored data, record it in `catalog_state.errors`, and surface it on the Venues page.
- **Cache:** disk cache keyed by URL+params (existing); `PRICE_MAX_AGE=60` for prices,
  `DEFAULT_MAX_AGE=300` for catalog, 24 h for series/tags.

### 3.4 Sportsbook refresh — quota-guarded

- Run only inside the 30-minute pass, and only for **sports events that have a
  prediction-market match** or are top-by-volume.
- Read `x-requests-remaining` **before** each call; if `remaining ≤ 0.2 × 500` (**80 % used**),
  stop calling odds and keep the last known odds, showing their age in the UI data
  (`odds_as_of` + `is_stale`).
- Budget: ≤ 8 credits/run, ≤ 3 runs/day per event; log every call's cost to `odds_quota`.
- Convert decimal → implied `1/decimal`; de-vig by dividing each implied probability by the sum
  across **all** outcomes of that market; store raw and de-vigged.

---

## 4. File and size plan (Phase 3)

Measured scale drives everything: ~175,000 markets.

| Artifact | Contents | Size rule | Estimated count |
|---|---|---|---|
| `data/catalog/manifest.json` | venue → shard list, counts, generated_at, schema version | small | 1 file |
| `data/catalog/<venue>-<n>.json` | compact records (`v,id,t,c,ct,o,p,k,vol,v24,u,ts,url,rt` …) | **≤ 1.5 MB each** | ~4,500 records/shard ⇒ Kalshi ~27, Polymarket ~3, Manifold ~9 ⇒ **~40 shards, ~35 MB** |
| `data/search/index-0.json` | inverted index, **first file on first keystroke** | **< 2 MB** | covers the top ~8,000 markets by volume + all categories/venues/tokens |
| `data/search/index-<n>.json` | remaining tokens, lazy-loaded | < 2 MB | ~6–10 more files |
| `data/events/<id>.json` | one matched event: prices, history, rules, sources, confidence | small | one per matched event (hundreds, not thousands) |
| `data/home.json` | top gaps, recent moves, venue status | small | 1 file |

- Compact keys + no whitespace (`separators`) + prices rounded to 2 dp keep records ~250–350 B.
- Search index: normalized lowercase tokens (strip punctuation, expand `k`→`000`, `%`), posting
  lists as shard-relative ids with a delta/varint-ish encoding if needed; first file is
  **reserved for high-volume markets so the first keystroke is useful**, per the brief.
- **Total published payload ≈ 40–60 MB.** That is fine for GitHub Pages' 1 GB site limit but see
  the bandwidth risk in §5.

### 4.1 Pipeline wiring

`run_all.py` becomes four ordered steps with logging and a `--prices-only` flag:

```
python run_all.py                 # catalog → prices → matching → export (6 h job)
python run_all.py --prices-only   # prices → export only (30 min job)
```

- Steps are the new modules in `venues/` plus `matching.py` and `export.py`; the existing
  `spread.py`, `mapping.json`, `dataview.py` and the current `site/data.json` **keep working**,
  so the 7 existing pages never see a broken build while the new data lands.
- Each step logs start/end, rows, errors, and writes a machine-readable summary the smoke test
  reads.

### 4.2 Brand and logos (per your instruction, no UI change now)

- DESIGN.md's "Geist and Geist Mono only" line is **superseded for brand assets**: the logo kit's
  wordmark face is **Instrument Sans** (the kit's own README says it was thickened toward a
  Roobert 600 anchor and converted to outlines). Logos and brand lockups use the kit's fonts;
  no other font guidance is being carried into the plan.
- Already placed and verified from the previous turn: favicon set on all 7 pages, symbol in the
  sidebar/topbar, horizontal lockup on About.
- **Where logos will be required when the UI does change** (Phase 5, after "go"): the new search
  page header, the venue row on Markets/Venues, event pages that mix venues, and the footer.
  Phase 1–4 writes data only; **no UI is touched in this plan.**

---

## 5. Risk list

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | **Catalog is ~175k markets** (Kalshi > 120k non-parlay alone) | Static payload ~40–60 MB; every 30-min deploy re-uploads it | Shard ≤ 1.5 MB; catalog shards only change every 6 h; consider `Last-Modified`-friendly shard names; cap is a user decision (§6) |
| 2 | **GitHub Pages bandwidth**: 50 MB × 48 deploys/day ≈ 2.4 GB/day ≈ 72 GB/month against a soft 100 GB/month | Site could be throttled | Run deploys less often than every 30 min, or publish only `home.json`+`events` on the 30-min job and full shards on the 6 h job |
| 3 | **Kalshi keyless rate limits are undocumented** (docs describe authenticated buckets only) | Blocking or 429s | Self-limit to ≤ 3 req/s, jittered backoff, cache, resumable cursor, circuit breaker |
| 4 | **Kalshi category is null on market records** | Categories cost extra requests via `/events` + `/series` | Cache series/event metadata 24 h; derive category from the series ticker prefix as a fallback |
| 5 | **Matching false positives** across 175k markets | A bogus gap is worse than no gap | Confidence stored; < 85 ⇒ "possible", never in a gap; strike + entity + direction + date checks; one venue per event; rules_differ flag; manual alias table |
| 6 | **Sportsbook quota burn** (500 credits/month) | Losing odds mid-month | ≤ 8 credits/run, ≤ 3 runs/day/event, hard stop at 80 %, keep last odds + show age, log every call |
| 7 | **De-vig correctness** — normalizing across outcomes assumes a complete market | Wrong implied probability | Only de-vig when every outcome of that market is present; otherwise store raw and mark it `no_devig` |
| 8 | **PredictIt skipped**; **Metaculus closed to the public** | Three venues instead of five | Decision §6; both are documented, not forgotten |
| 9 | **`.env` is NOT in `.gitignore` yet** | A leaked key | Add `.env` (and `*.key`) to `.gitignore` **before** any Phase 2 key handling; load with a stdlib parser; never print/log the value |
| 10 | **Terms drift** — Kalshi/Polymarket display terms were not machine-readable | Compliance risk | Re-check at each phase; store the venue's ToS URL in `catalog_state`; the plan already excludes the one venue that clearly forbids it |
| 11 | **Static-site search freshness** — index built every 6 h | Search misses brand-new markets for up to 6 h | Acceptable; the ≤ 2 MB first index holds the high-volume set, which is what people search for |
| 12 | **Windows/Git Bash + background jobs** (established hazard) | Orphaned servers, half-written exports | All new work runs as foreground scripts; writes go to temp files then `os.replace` (atomic) |
| 13 | **No git remote** | Phases land as local commits only | Phase 4 commits locally per phase; pushing stays impossible until a remote exists |
| 14 | **Dependency creep** | CLAUDE.md violation | Nothing new is needed; if one becomes necessary, I will ask first |

---

## 6. Decisions — settled by the user, 2026-10-06

1. **PredictIt — skipped.** T&C clause *d* forbids automated extraction (§1.2). Confirmed.
2. **Metaculus — skipped.** API requires an account token (403 keyless), and it is a `forecast`
   venue that could never enter a gap (§1.3). Confirmed.
3. **Catalog scope — keep all open markets** (~175,000 markets, ~40–60 MB static). No volume cap.
   Kalshi parlays (`KXMVECROSSCATEGORY`) are still excluded via `mve_filter=exclude`; that is a
   correctness decision (a parlay is not one event to compare), not a size cap.
4. **Sports provider — The Odds API**, free tier, 80 % quota guard (§1.4). Confirmed.
5. **Refresh cadence — split.** The 6-hourly job rebuilds the catalog shards; the 30-minute job
   writes only `home.json`, `events/<id>.json` and the price snapshots. This keeps Pages
   bandwidth at roughly 10–15 % of what a full 30-minute republish would cost (§5, risk 2).

Remaining pre-Phase-2 requirement, not a decision: **add `.env` (and `*.key`) to `.gitignore`
before any key handling** (§5, risk 9).

---

## 7. Phase 2–4 preview (unchanged from your brief, restated so "go" is unambiguous)

- **Phase 2 — data layer.** `venues/` gains adapters with the same interface
  `list_open_markets()` / `get_prices(ids)` for kalshi, polymarket, manifold, plus the
  `odds_api` sportsbook adapter (`.env: ODDS_API_KEY`, never printed; decimal → implied →
  de-vig; quota guard). Pagination, retries, backoff, caching, per-venue limits as in §3.
  Normalize into `spread.db` (§2). Matching with rapidfuzz + date/entity checks; sports by
  alias table + start date ±1 day + moneyline only. Confidence + `rules_differ` stored;
  < 85 ⇒ possible, never in a gap, and never published. Gap = fresh **real-money** max − min
  only; play-money and forecast shown separately.
- **Phase 3 — export.** `manifest.json`, ≤ 1.5 MB catalog shards with compact keys, a
  first-keystroke < 2 MB search index plus lazy shards, `events/<id>.json`, `home.json`;
  `run_all.py` with `--prices-only`.
- **Phase 4 — verify.** `test_smoke.py` printing per venue (markets fetched, errors, shard
  sizes, matched events, sample searches for **fed**, **bitcoin**, **nfl**, sports quota used).
  Run it and show the real output. **Commit after each phase. No UI changes.**
