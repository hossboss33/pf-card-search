# site/ — the public PF card search site

A static site that runs a real full-text search over the card corpus **in the
browser**. GitHub Pages cannot run Python, so instead of an API the page opens a
prebuilt SQLite file with [sql.js-httpvfs][httpvfs], which issues HTTP range
requests and fetches only the database pages a query actually touches. A visitor
who searches for three words downloads a few dozen kilobytes, not the corpus.

No build step, no bundler, no CDN. Everything is vendored here.

[httpvfs]: https://github.com/phiresky/sql.js-httpvfs

## Layout

| Path | What it is |
|---|---|
| `index.html` | the whole site: search, card and about views, hash-routed |
| `app.js` | query parser, SQL builder, renderer, sanitizer, keyboard map |
| `style.css` | copied from `static/style.css` (spec 8.3 tokens) and extended |
| `config.json` | database path, range chunk size, page size, repo link |
| `db/cards.sqlite` | the shipped database (built by `scripts/`, not by hand) |
| `vendor/sqlite.worker.js` | sql.js-httpvfs worker, vendored, MIT |
| `vendor/sql-wasm.wasm` | the SQLite build the worker loads |
| `vendor/httpvfs-client.js` | the main-thread half of the worker protocol (see below) |
| `fonts/` | Carlito, the metric-compatible Calibri substitute, SIL OFL |
| `.nojekyll` | stops Pages from running Jekyll over the tree |

### Why `vendor/httpvfs-client.js` exists

Upstream sql.js-httpvfs ships two halves: the worker, and an ESM entry point
that wraps it with Comlink and needs a bundler. Only the worker and the `.wasm`
are vendored, so that file reimplements the small slice of the Comlink wire
protocol the worker speaks (~120 lines, no dependencies) and exposes one
function, `httpvfs.createDbWorker(configs, workerUrl, wasmUrl)`. It also
rewrites every URL to an absolute one, because the worker resolves relative
paths against its own location rather than the page's.

## The database contract

`db/cards.sqlite` must be built with `page_size = 1024` (matching
`requestChunkSize` in `config.json`), `journal_mode = DELETE` so it is a single
static file, and `VACUUM`ed so its pages are laid out contiguously. Schema:

```sql
CREATE TABLE cards(
  id INTEGER PRIMARY KEY, tag TEXT, cite TEXT, fullcite TEXT,
  body_text TEXT, markup_html TEXT, summary TEXT, spoken TEXT,
  source_url TEXT, source_pub_date TEXT, is_analytic INTEGER,
  team_count INTEGER, school_count INTEGER, topic_codes TEXT, -- JSON array
  pocket TEXT, hat TEXT, block TEXT);
CREATE VIRTUAL TABLE card_fts USING fts5(tag, cite, block, body,
  tokenize='porter unicode61 remove_diacritics 2');   -- rowid = cards.id
CREATE TABLE topics(code TEXT PRIMARY KEY, season INTEGER, slot TEXT,
  resolution TEXT, starts TEXT, ends TEXT, card_count INTEGER);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
```

`meta` keys the site reads: `built_at`, `card_count`, `analytic_count`,
`team_count`, `school_count`, `seasons_covered`, `coverage_note`, `source_note`,
and `subset_note` when present. Missing keys are skipped rather than rendered
blank.

Ranking is `bm25(card_fts, 5.0, 3.0, 2.0, 1.0)` — tag, then cite, then block,
then body. Analytics (`is_analytic = 1`) are excluded unless the query says
`is:analytic`.

## Query language

The practical subset of spec 7.2 that a single-table client can answer:

```
grid reliability            both words, ANDed
"interconnection queue"     exact phrase
-crypto                     exclude
topic:2026-SO               a topic code
topic:present               also past, future (computed from topics.starts/ends)
season:2025                 all topics in one season
cite:kessler                short cite or full cite; author: is an alias
year:23                     two-digit year in the cite
block:"A2: Moratorium"      the block heading the card sat under
before:/ after:2026-06-01   source publication date
is:analytic                 analytics, otherwise excluded
min_reads:5                 read by at least five teams
sort:reads|recent|relevance|length
```

`side:` is deliberately absent: the shipped database carries one row per
canonical card and no per-round side column, so offering the filter would be a
lie. Anything the parser does not recognize degrades to a plain search term; a
malformed operator narrows a search instead of raising.

**Every user term is quoted before it reaches FTS5** (wrapped in `"` with
embedded quotes doubled), so no input can produce a syntax error or inject an
operator. Fielded filters are SQL predicates with bound parameters. FTS5's `NOT`
is binary, so a query of nothing but exclusions drops them rather than failing.

## Safety

`markup_html` is treated as untrusted input even though we build the database.
`app.js` reparses it and rebuilds it from an allowlist — `h1`–`h4`, `p`, `u`,
`strong`, `em`, `mark`, `span`, `br` — keeping only a scrubbed `class`
attribute. Script, style, iframe, object and embed subtrees are dropped whole;
any other unknown tag is unwrapped and its text kept.

## Interaction

- `/` focuses search, `j`/`k` or arrows move the selection, `Enter` opens the
  selected card, `y` copies its spoken text, `Esc` leaves the search box.
- Deep links: `#/q/<encoded query>` and `#/card/<id>`. The back button works,
  and both are shareable.
- Highlight color is a user setting (bright green, yellow, blue, turquoise),
  stored in `localStorage` and applied by setting `--hl`; blue renders
  highlighted text white, as Word's blue is dark.
- Reading view hides minimized runs. The parts legend shows once, then stays
  dismissed.
- If the database cannot be opened, the page says so plainly with the HTTP
  status, rather than rendering blank.

## Working on it locally

`python3 -m http.server` **does not serve HTTP range requests** — it answers
`200` with the whole file and ignores the `Range` header, which silently feeds
sql.js-httpvfs the wrong bytes. Use any static server that supports ranges
(GitHub Pages does). Then open `index.html` over `http://`, not `file://`.

After replacing `db/cards.sqlite`, hard-reload: browsers cache range responses,
and mixing chunks from two builds of the same URL produces empty or wrong
results.

## House style

`scripts/style_lint.py` encodes the spec 8.5 banned list: the grep-able
signature of the AI-coded look, plus generic font stacks, marketing copy, radii
over 4px, emoji and exclamation marks. It scans `templates/` and `static/`;
point a copy of it at `site/` to check this tree.
