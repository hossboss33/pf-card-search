# PF Card Search: Build Spec

**Audience:** Claude Code. This file is the project brief. Read it fully before writing any code. Save it in the repo root and keep it open; every milestone in §11 refers back to sections here.

**Mission:** A local-first search engine over every Public Forum cut card disclosed on the openCaselist wiki (opencaselist.com), with topic filters for past, present, and future PF resolutions, zero duplicate cards in the index, and a plain, dense, Calibri interface that looks like a research tool built by a debater, not a template.

**Non-goals for v1:** card cutting, AI card generation, multi-user accounts, LD/Policy events, mobile app.

---

## 0. Ground rules

1. **Verify before you trust this document.** Facts below marked `[VERIFIED <date>]` were confirmed against a live source on that date and the source URL is given. Anything marked `[VERIFY]` is a reasonable inference that you must confirm yourself (by fetching the URL, reading the repo, or inspecting data) before building on it. All `[VERIFY]` items are collected in §12. Never hardcode a guessed endpoint or slug.

2. **Do not hammer opencaselist.com.** It is a community-run nonprofit service and its API implements rate limiters on purpose [VERIFIED 2026-08-28, https://github.com/ashtarcommunications/caselist README]. The acquisition plan in §2 gets ~a decade of history without sending the site a single request. Live sync rules: max 1 request/second, exponential backoff on 429/5xx, a User-Agent that names this project and includes a contact email, resume from checkpoints instead of restarting, cache every response to disk forever, run bulk jobs overnight US time. Read the site's terms of use and robots.txt first and follow them; if a full-season bulk pull is ever needed directly from the site, email the maintainer and ask, the project is run by a person who answers.

3. **Access honestly.** openCaselist authenticates against Tabroom [VERIFIED 2026-08-28, caselist README + https://docs.paperlessdebate.com/verbatim/debating-paperless/caselist]. Use the owner's own Tabroom credentials from an env var, never shared or scraped credentials, and never attempt to bypass auth, captchas, or rate limits.

4. **Keep the deployment private by default.** Card bodies are excerpts from copyrighted articles, papers, and books. Redistribution inside the debate community via the wiki is an established norm; republishing the whole corpus on the open internet is a different act with real copyright exposure, and it also re-hosts other students' names and round reports. v1 binds to `127.0.0.1` (or team LAN / password-protected). A public launch is a separate decision the owner makes later, not a default.

5. **Idempotence is the prime engineering invariant.** Running any ingest job twice must add exactly zero new canonical cards. §4 defines how; §11 tests it.

---

## 1. What a cut card is (read this before writing the parser)

A "card" is a quoted excerpt from a published source, annotated by a debater. Every card has three parts, and the body has four visual layers. The parser, the schema, the search index, and the UI all mirror this structure, so get it exact.

### 1.1 The three parts

| Part | What it is | Typical formatting in the source .docx |
|---|---|---|
| **Tag** | A one-sentence claim written by the debater, stating what the card proves. Roughly 8–17 words. Jargon-heavy, argumentative, not neutral. | Bold, larger (≈13pt), styled as a Word heading (see §1.3). |
| **Cite** | Two pieces: the **short cite** (author last name + 2-digit year, e.g. `Diamond '13`, `Rodgers and Cooper 06`, `Smith et al. 24`) spoken in round, and the **full cite** (author full names, qualifications often embedded in brackets, date, "Title," publication, URL, access date). | Short cite bold ≈11–13pt; full cite regular ≈8–11pt, sometimes partly inside the same paragraph as the short cite. |
| **Body** | The verbatim source excerpt, usually one long paragraph. The debater never edits the words; they mark them up. | Four layers, below. |

### 1.2 The four body layers (this is the part most people get wrong)

From least to most emphasized:

1. **Minimized text**: kept for context and evidence-ethics but not meant to be read. Rendered tiny (8pt, sometimes 6pt or smaller) or left plain while everything around it shrinks.
2. **Underlined text**: the sentences the debater judged relevant. Reads as a phrase-level extractive summary.
3. **Bold (usually bold + underlined)**: the strongest warrants supporting the tag.
4. **Highlighted text**: the exact words read aloud in the round. Short fragments, not whole sentences, forming a compressed spoken sentence when concatenated. Highlight colors vary by team; Word's base highlighter palette (yellow, bright green, turquoise, blue) covers most disclosed files.

Consequence for the data model: **the body text of a card is stable across teams; the markup is not.** Two teams disclose "the same card" with different underlining and highlighting. That is why §4 separates a *canonical card* (the text) from *variants* (each team's markup). Store the body twice: once as plain text (for hashing and FTS) and once as markup HTML using exactly these tags: `<u>` underline, `<strong>` bold, `<mark>` highlight, and `<span class="min">` for minimized runs.

Also derive and store two projections per variant, because they are what debaters actually search and skim:
- `summary` = concatenated underlined text.
- `spoken` = concatenated highlighted text.

### 1.3 How speech documents are organized

Disclosed documents follow the Verbatim (the standard Word debate template) heading hierarchy:

- **Pocket** = Heading 1 (usually the speech: `1AC`, `Case`, `2NC`...)
- **Hat** = Heading 2 (broad argument group)
- **Block** = Heading 3 (specific argument; answer blocks are titled `A2: ...` or `AT: ...`, meaning "answers to")
- **Tag** = Heading 4 (one card)

A card = everything from one Heading-4 paragraph until the next heading of any level. This convention is confirmed by the wiki's own tooling docs [VERIFIED 2026-08-28, https://docs.paperlessdebate.com/verbatim/debating-paperless/caselist] and by the fact that the OpenCaselist research dataset's `markup` field wraps tags in `<h4>` (§2.1). Real files are messier: some debaters use direct bold 13pt formatting instead of heading styles, some skip pockets/hats, PF files are shorter and looser than Policy files. The parser needs a style-based pass and a direct-formatting fallback (§3.4).

**Analytics:** a Heading-4 with no body under it is an "analytic," an argument asserted without evidence. Index them (they matter for A2 research) but set `is_analytic = 1` and exclude them from "card" counts by default.

### 1.4 PF specifics

- Sides are **Pro** and **Con** (the wiki and datasets may encode `A`/`N` from Policy convention; normalize to `P`/`C` at ingest and translate on display) `[VERIFY: check the actual side values in both data sources]`.
- Topics change ~5 times per season (Sept/Oct, Nov/Dec, Jan, Feb, Mar/Apr, plus a Nationals topic). §6 builds the whole past/present/future filter on this.
- PF cards are shorter than Policy cards and PF files reuse camp evidence heavily, which raises the duplicate rate. Good: the dedup layer earns its keep.

### 1.5 House cut format (used by the export feature, §9.4)

The owner's team cuts in Calibri, 1.15 spacing, no space before/after paragraphs: tag bold 13pt; cite 11pt with author qualifications embedded in square brackets at 8pt; minimized text at 8pt (6pt if a paragraph, 3pt if longer). Two rules that override anything else in this file: **cites are never restamped.** Display and export keep every card's citation exactly as the original cutter disclosed it, original attribution and any existing initials intact; do not append `//Delbarton` or any other stamp to cards this team did not cut. And **highlighting is a setting, not a constant** (§8.3): the user-selected highlight color applies to on-screen rendering, print, and export alike. The "Export as .docx" feature offers this layout as the default preset plus a plain-Verbatim preset.

---

## 2. Where the cards live, and the acquisition plan

"Get every PF card from openCaselist" decomposes into two very different jobs. Do them in this order.

### 2.1 Source A: bulk history via the OpenCaselist research dataset (no scraping)

Nearly all of the historical corpus already exists as a clean, legally redistributable dataset, so downloading it is both the fastest and the most polite path:

- **`Yusuf5/OpenCaselist` on Hugging Face** [VERIFIED 2026-08-28, https://huggingface.co/datasets/Yusuf5/OpenCaselist]:
  - 4,830,561 rows, ~27.6 GB, parquet, **MIT license**.
  - Evidence from **Policy, LD, and Public Forum, years 2013–2024**, parsed from the .docx files uploaded to openCaselist.
  - Fields you will map directly: `tag`, `cite`, `fullcite`, `summary` (underlined text), `spoken` (highlighted text), `fulltext`, `markup` (HTML with headings/`<u>`/`<strong>`/`<mark>`), `pocket`, `hat`, `block`, plus round metadata (`roundId`, `side`, `round`, `report`, tournament/opponent/judge), team/school ids and names, `caselistName`, `year`, `event`, `level`, and a prior dedup signal: `bucketId` + `duplicateCount`.
  - Debater names are truncated to 2 characters in the dataset; keep that truncation, do not try to re-identify people.
- Companions, both worth pulling:
  - **`Hellisotherpeople/OpenCaseList-Deduplicated`** on Hugging Face: a semantically deduplicated variant [VERIFIED 2026-08-28, linked from the dataset card]. Use it as a cross-check for §4, not as the primary store.
  - **`DebateRounds` on Kaggle** (`yu5uf5/debate-rounds`): `caselist.sqlite` with richer round metadata [VERIFIED 2026-08-28, linked from the dataset card] `[VERIFY: schema on download]`.
  - The dataset's processing code is public at https://github.com/OpenDebate/debate-cards (branch `v3`) and its paper is *OpenDebateEvidence* (NeurIPS 2024 D&B, https://openreview.net/pdf?id=43s8hgGTOX). Read the repo's parsing and dedup code before writing your own; it is prior art for §3–§4.

Load procedure: stream the parquet with `datasets` or `polars`, filter to PF only (`event` / `caselistName`; inspect distinct values first, expect names like `hspf<yy>` on the pattern of `ndtceda14` `[VERIFY: exact PF slugs and the earliest PF season present]`), and run every row through the same normalize/dedup/insert path as scraped data (§3–§4). Do not trust `bucketId` blindly; recompute your own canonical keys and log disagreements.

**Credit the dataset and the wiki on the About page.** This project exists because other people did the hard part.

### 2.2 Source B: 2024 → today via the openCaselist API (the only live syncing)

The gap between the dataset's end (2024) and now (the 2026–27 season just started) must come from the site itself. Do it through the API, not HTML scraping:

- openCaselist is open source: https://github.com/ashtarcommunications/caselist. The server is a Node/Express app with an OpenAPI spec; Swagger UI runs against **`https://api.opencaselist.com/v1/docs`**; auth ties into Tabroom [VERIFIED 2026-08-28, repo README].
- **First task of the sync milestone:** fetch the OpenAPI spec, read `/v1/routes` in the repo, and write down the actual endpoints for: list caselists → schools in a caselist → teams in a school → rounds for a team (cites, reports, metadata) → download an open-source .docx. Do not invent paths; transcribe them from the spec into `config/endpoints.toml` with a comment linking the spec `[VERIFY: every endpoint]`.
- Enumerate PF caselists from the API's own caselist listing (filter by event), never from guessed slugs. Expected coverage to sync: the 2024–25 and 2025–26 PF seasons as backfill, then the current 2026–27 caselist on a weekly schedule.
- Download every open-source .docx a round links to; the full formatted cards live in those files. The pasted "cites" entries on round pages are lossy (often tag + cite + first/last sentence); ingest them only as a fallback record when a round has no open-source doc, flagged `fidelity='cites_only'`.
- Respect §0.2 throughout. The sync must be resumable: a checkpoint row per (caselist, school, team) so a crash resumes instead of re-requesting.

### 2.3 Source C: raw files, kept forever

Everything fetched (API JSON, .docx bytes) goes into a content-addressed store: `data/raw/<sha256[0:2]>/<sha256>` plus a `documents` row. Parsing bugs are certain; re-parsing from local raw files must never require re-downloading.

### 2.4 What "every PF card" honestly means

State this on the About page and believe it internally: the index covers **PF evidence disclosed on openCaselist**, which is most of the national-circuit corpus from roughly the mid-2010s onward, not every card ever cut. Pre-openCaselist archives exist (https://github.com/ashtarcommunications/caselist-archive) but are mostly Policy/LD-era static sites; out of scope for v1 `[VERIFY if PF content exists there at all]`. Local-circuit teams that don't disclose are invisible to any tool. Precision about coverage is a feature; Logos-style sites rarely state theirs.

---

## 3. Ingestion pipeline

One pipeline, four stages, every source flows through it: **fetch → raw store → parse → normalize/dedup/insert**. A single CLI: `python -m carddb ingest --source {hf|api} [--caselist SLUG] [--since DATE]`.

### 3.1 Fetch
Covered in §2. Every fetched object lands in the raw store with provenance (`origin`, `origin_url`, `fetched_at`).

### 3.2 Ingest ledger
Table `ingest_ledger(source, external_id, sha256, ingested_at)` with `PRIMARY KEY (source, external_id)`. Before processing any unit (an HF row id, an API round id, a docx sha), check the ledger; skip if present with the same sha. This is idempotence layer 1.

### 3.3 HF loader
Map dataset fields onto the schema in §5. `markup` → `card_variants.markup_html` (sanitize: allow only `h1–h4, p, u, strong, em, mark, span`, strip attributes except `class`). `fulltext` → normalize → `canonical_key`. Rows with null `fulltext` and non-null `tag` → analytics. Batch inserts in transactions of ~5k rows; the full load is millions of rows and must finish in minutes-to-hours, not days.

### 3.4 Docx parser (for Source B files)
Use `python-docx` to walk `document.paragraphs` in order.

Card segmentation:
1. **Style pass:** paragraph style name in `Heading 1..4` sets the current pocket/hat/block, and each `Heading 4` opens a new card that closes at the next heading.
2. **Fallback pass** (triggered when a file yields 0 cards or its heading counts look degenerate): treat a short paragraph (< ~40 words) whose runs are ≥80% bold at ≥12.5pt, followed within 2 paragraphs by a short-cite-shaped line, as a tag.

Within a card:
- **Cite detection:** first 1–2 paragraphs after the tag. Short cite regex to start from: `^\s*(?:[A-Z][\w'’.-]+(?:,? (?:and|&) [A-Z][\w'’.-]+)?|[A-Z][\w'’.-]+ et al\.?),? ['’]?\d{2}(?:\d{2})?\b`. The remainder of those paragraphs is the full cite. Pull `source_url` (first http(s) token) and `source_pub_date` (date-shaped tokens near the front, formats `M-D-YYYY`, `Month D, YYYY`, `YYYY`) out of the full cite. If no cite-shaped paragraph exists, the block is an analytic.
- **Run markup:** for each run, map `highlight_color set` → `<mark>`, else `bold and underline` → `<strong><u>`, else `underline` → `<u>`, else `bold` → `<strong>`, else `font size ≤ 9pt` → `<span class="min">`, else plain. Concatenate into `markup_html`; also emit `body_text` (all runs, plain), `summary` (underlined+), `spoken` (marked only), `highlight_ratio = len(spoken)/len(body_text)`.
- **Edge cases to handle, with unit fixtures for each:** cards split by manual line breaks; tables and images inside bodies (extract text, note `has_table`); `.doc` legacy files (convert via `libreoffice --headless --convert-to docx`, log failures); PDFs uploaded as "open source" (log and skip in v1); empty files; files where every run is highlighted (some teams paste pre-highlighted text; cap `highlight_ratio` sanity checks, don't crash); non-Verbatim templates.
- Parser failures never abort a batch: record `parse_status='failed'` with the exception, move on, report counts at the end.

### 3.5 Normalization (exact spec, used for hashing and nothing else)

```
normalize(s):
  s = unicode NFKC
  s = lowercase
  s = replace curly quotes/apostrophes with straight, en/em dashes with '-'
  s = strip every character not in [a-z0-9 ] (punctuation, brackets, ellipses all go)
  s = collapse whitespace runs to a single space, trim
  return s
```

Rationale: teams introduce noise (smart quotes, stray brackets from Verbatim's condense macro, OCR artifacts) that must not defeat exact-duplicate detection, while real wording differences must survive. Keep this function frozen and versioned (`NORM_V=1` stored with every hash); changing it means re-keying the corpus.

---

## 4. Deduplication: zero duplicate cards, without merging different cards

The corpus is massively duplicated by construction: the same camp card gets read by hundreds of teams, and one team re-discloses the same card across a dozen rounds. The dataset ships a `duplicateCount` column whose sample values run into the hundreds. The index must show **one canonical card** per underlying piece of evidence, with every disclosure attached to it as a variant. Three layers:

### 4.1 Layer 1: file-level
`sha256` of the raw .docx bytes. Same file uploaded to five rounds = parsed once; the five rounds all point at one `documents` row.

### 4.2 Layer 2: exact card
`canonical_key = sha256(NORM_V + ':' + normalize(body_text))` for evidence cards, and `sha256(NORM_V + ':analytic:' + normalize(tag))` for analytics. `cards.canonical_key` is `UNIQUE`; inserts are `INSERT ... ON CONFLICT(canonical_key) DO NOTHING`, then attach a variant row. This is idempotence layer 2 and catches the overwhelming majority of duplicates, because bodies are pasted verbatim.

**Hash the full body text, never the highlighted or underlined projections.** Highlighting differs team to team; the body is what's stable. That single decision is most of "no duplicates."

### 4.3 Layer 3: near-duplicates (batch job, not inline)
Catches re-cut copies with small textual drift and trimmed cards (a team keeps only the middle of a longer card).

- Candidate generation: MinHash over 5-token shingles of `normalize(body_text)`, 128 permutations, LSH with `b=8, r=16` (similarity knee ≈ 0.88). `datasketch` is fine.
- Verification, both required to merge:
  1. true Jaccard ≥ 0.90, **or** containment ≥ 0.95 in one direction (the trimmed-card case; the longer text becomes canonical and the shorter is linked `relation='trim'`), and
  2. compatible cites: normalized author token overlaps and the 2-digit years match. Never merge across different cite years; two cards quoting different articles can share long boilerplate.
- Merging = repointing variants at the surviving canonical and recording the merge in `card_merges` (reversible).
- Cross-check against the dataset's own `bucketId` and against `Hellisotherpeople/OpenCaseList-Deduplicated`: log every case where they merged and you didn't (or vice versa) to `reports/dedup_disagreements.tsv`, then hand-audit 50 random rows. Tune thresholds from that audit, not from vibes.

### 4.4 Acceptance criteria (tested in §11)
- Re-running any ingest on already-ingested input inserts 0 canonical cards and 0 variants.
- No canonical cluster mixes two different cite years.
- A written sample audit: 50 random clusters, ≥ 48 correct groupings.
- Search results never show two entries whose bodies are ≥ 0.95 Jaccard-similar.

---

## 5. Database schema (SQLite, single file, that's the point)

SQLite + FTS5 handles a few million cards on one laptop with sub-100ms queries, needs zero services, and makes backup = copy one file. Do not add Postgres/Elastic/Docker unless a measured benchmark fails (§11 M3).

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE caselists (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT,
  season INTEGER,            -- 2026 means the 2026-27 season
  event TEXT, level TEXT     -- keep only PF rows, but store the fields
);
CREATE TABLE schools (
  id INTEGER PRIMARY KEY, caselist_id INTEGER REFERENCES caselists(id),
  name TEXT, display_name TEXT, state TEXT, external_id TEXT
);
CREATE TABLE teams (
  id INTEGER PRIMARY KEY, school_id INTEGER REFERENCES schools(id),
  name TEXT, display_name TEXT, notes TEXT, external_id TEXT
);
CREATE TABLE rounds (
  id INTEGER PRIMARY KEY, team_id INTEGER REFERENCES teams(id),
  side TEXT CHECK (side IN ('P','C')),
  tournament TEXT, round_label TEXT, opponent TEXT, judge TEXT,
  report TEXT, round_date TEXT,            -- ISO date when known
  topic_id INTEGER REFERENCES topics(id),  -- assigned by §6
  external_id TEXT UNIQUE
);
CREATE TABLE documents (
  id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, origin TEXT, origin_url TEXT,
  orig_filename TEXT, local_path TEXT, fetched_at TEXT,
  parsed_at TEXT, parse_status TEXT, parse_error TEXT
);
CREATE TABLE cards (            -- one row per canonical card
  id INTEGER PRIMARY KEY,
  canonical_key TEXT UNIQUE NOT NULL,
  tag TEXT, cite TEXT, fullcite TEXT,
  body_text TEXT, body_len INTEGER,
  source_url TEXT, source_pub_date TEXT,
  is_analytic INTEGER DEFAULT 0,
  first_season INTEGER, variant_count INTEGER DEFAULT 0,
  school_count INTEGER DEFAULT 0,
  topic_ids TEXT                -- materialized JSON array, rebuilt nightly
);
CREATE TABLE card_variants (    -- one row per disclosure of that card
  id INTEGER PRIMARY KEY,
  card_id INTEGER REFERENCES cards(id),
  document_id INTEGER REFERENCES documents(id),
  round_id INTEGER REFERENCES rounds(id),
  ordinal INTEGER,              -- position within the document
  pocket TEXT, hat TEXT, block TEXT,
  markup_html TEXT, summary TEXT, spoken TEXT,
  highlight_ratio REAL, fidelity TEXT DEFAULT 'opensource',
  external_id TEXT,
  UNIQUE (document_id, ordinal)
);
CREATE TABLE card_merges (survivor_id INTEGER, absorbed_key TEXT, relation TEXT, merged_at TEXT);
CREATE TABLE topics (
  id INTEGER PRIMARY KEY, season INTEGER, slot TEXT,   -- 'SO','ND','JAN','FEB','MA','NATS'
  code TEXT UNIQUE,             -- e.g. '2026-SO'
  resolution TEXT, starts TEXT, ends TEXT, source_url TEXT
);
CREATE TABLE ingest_ledger (
  source TEXT, external_id TEXT, sha256 TEXT, ingested_at TEXT,
  PRIMARY KEY (source, external_id)
);

CREATE VIRTUAL TABLE card_fts USING fts5(
  tag, cite, block, body,
  tokenize = 'porter unicode61 remove_diacritics 2'
);
-- contentless-style: insert with rowid = cards.id; rebuild rows on card update.
CREATE INDEX idx_variants_card ON card_variants(card_id);
CREATE INDEX idx_variants_round ON card_variants(round_id);
CREATE INDEX idx_rounds_topic ON rounds(topic_id);
```

Notes:
- **Topic lives on the round** (a disclosure happened under exactly one topic); the canonical card gets a materialized `topic_ids` list because good generic cards (frameworks, impact evidence) recur across topics and must show up under every topic they were read on.
- Optional later: `sqlite-vec` table of embeddings keyed by `cards.id` for the semantic layer (§7.4). Not v1.

---

## 6. Topics: past, present, future

### 6.1 Seed data
`data/topics.json`, hand-maintained by the owner ~5 times a year, seeded from the NSDA's official topic history and announcements at https://www.speechanddebate.org/topics/ [VERIFIED 2026-08-28 that this page exists and lists PF resolutions and voting results]. Populate every PF resolution back to the earliest season present in the data, with slot windows.

Verified example rows to include:

```json
{"code":"2026-SO","season":2026,"slot":"SO",
 "resolution":"Resolved: The United States federal government should enact a moratorium on hyperscale data center construction.",
 "starts":"2026-09-01","ends":"2026-10-31",
 "source_url":"https://www.speechanddebate.org/topics/"}
```

(The Sept/Oct 2026 PF resolution above was announced Aug 1, 2026; the losing ballot option was a federal emissions trading system. [VERIFIED 2026-08-28])

### 6.2 Assigning a topic to each round
1. `caselists.season` narrows to one season.
2. `round_date` (or tournament date) falls into a slot window → topic.
3. Overrides table for tournaments with designated topics (e.g., NSDA Nationals uses the Nationals resolution; check TOC's practice per year) `[VERIFY the override list against NSDA/tournament invitations rather than assuming]`.
4. If no date exists, fall back to matching distinctive resolution keywords in the round's pockets/hats against that season's resolutions; else `topic_id = NULL` and the round lands in an "Unassigned" bucket visible in the stats page. Never silently guess.

### 6.3 Past / present / future are computed, not stored
- `past`: `ends < today`
- `present`: `starts <= today <= ends`
- `future`: `starts > today`

Future PF topic slots for the current season are public before they have cards: the NSDA releases the season's potential resolutions after Nationals and announces each slot's winner on a schedule [VERIFIED 2026-08-28, speechanddebate.org/topics]. Ingest announced-but-not-yet-started topics with zero cards; the topic page renders honestly ("Announced. 0 cards disclosed yet.") and becomes the target for §9.10 alerts the day cards appear. The filter UI is one control: a season/slot picker grouped by year, with Past / **Present** / Future section headers and card counts next to every topic.

---

## 7. Search behavior

### 7.1 Ranking
`bm25(card_fts, 5.0, 3.0, 2.0, 1.0)` (tag ≫ cite > block > body). One result row per **canonical card**; the row shows aggregate provenance ("read by 41 teams · 19 schools · 2024-SO, 2025-JAN").

### 7.2 Query language (parse before handing to FTS)
- Bare words → AND across all columns; `"exact phrase"`; `-exclude`.
- Fielded filters, applied as SQL predicates, not FTS: `topic:2026-SO` (or `topic:present`), `season:2025`, `side:pro|con`, `school:"Millburn"`, `team:XY`, `cite:kessler`, `author:kessler` (alias), `year:23` (cite year), `before:2026-01-01` / `after:` (source pub date), `is:analytic`, `min_reads:5`, `sort:reads|recent|relevance|length`.
- Malformed operators degrade to plain terms; never error on a query.

### 7.3 Results and latency
Snippets from FTS `snippet()` on the body with query-term bolding; tag and cite always shown in full. Target p95 < 100 ms at full corpus size on a laptop; measure in §11 M3, and only then consider `fts5 prefix` indexes or moving hot filters into covering indexes.

### 7.4 Optional semantic layer (post-v1, keep behind a flag)
Local embeddings (e.g., a small sentence-transformer) over `tag + spoken`, stored in `sqlite-vec`, used to (a) rerank the top 200 BM25 hits and (b) power "similar cards" on the card page. Logos already advertises natural-language "deep search," so this is parity, not differentiation; ship the exact-search experience first and make it excellent.

---

## 8. The website

### 8.1 Stack
FastAPI + Jinja2 server-rendered HTML + one hand-written CSS file + < 300 lines of vanilla JS (keyboard nav, instant-search fetch). No React, no Tailwind, no build step, no component library. This is a deliberate aesthetic and engineering choice: the fastest way to not look template-generated is to not use the templates, and server-rendered pages over a local SQLite file are instant.

### 8.2 Pages
- `/` search: one input, the topic picker, results list. The empty state shows corpus stats (cards, teams, seasons covered) and the current topic, nothing else.
- `/card/<id>`: the card rendered as it would appear in a speech doc, a **parts legend** (first visit: small labels pointing at tag / cite / underline / highlight, dismissible; this is the "teach the difference between the parts" requirement), variant list with per-team highlighting toggle, provenance table (every round it appeared in), cite health, similar cards.
- `/topic/<code>`: resolution text, dates, status, coverage dashboard (§9.8), top cards, newest cards.
- `/school/<id>`, `/team/<id>`: what they've read, by topic.
- v1.1 pages as §9.13–9.22 land: `/argument/<slug>`, `/source/<id>`, `/new` (this week's cards), `/drill`, `/compare`.
- `/stats`, `/about` (coverage statement §2.4, credits, data licenses).

### 8.3 Typography and design tokens
```css
:root {
  --font: Calibri, Carlito, "Segoe UI", "Trebuchet MS", sans-serif;
  --ink: #1a1a1a;        /* text */
  --paper: #ffffff;      /* background */
  --rule: #d9d9d9;       /* 1px borders everywhere */
  --meta: #5f5f5f;       /* secondary text */
  --accent: #1f4e79;     /* Word heading blue; links + h tags. One accent only. */
  --hl: #00FF00;         /* rendered highlight color; USER SETTING, see below */
  --min-size: 9px;       /* minimized card text */
}
```
- **Highlight color is a user setting**, persisted locally and applied everywhere highlights render (results, card pages, print, .docx export). Offer Word's base highlighter palette as one-click swatches: Bright Green `#00FF00`, Yellow `#FFFF00`, Blue `#0000FF`, plus Turquoise `#00FFFF` (Word's true Blue is dark, so when Blue is active render highlighted text in white; Turquoise is the light blue many debaters mean by "blue"). Default: Bright Green. This palette restriction is load-bearing: .docx files can only store highlights from Word's fixed `WD_COLOR_INDEX` palette, so using base Word colors is what keeps the screen, the printout, and the exported file identical.
- **Calibri everywhere**, including inputs and buttons. Calibri ships with Windows/Office; it is not freely licensed for embedding, so do not bundle the .ttf. Bundle **Carlito** instead (the metric-compatible open substitute, SIL OFL `[VERIFY license file when vendoring]`) as a local `@font-face` so macOS/Linux render identically.
- Base 15px/1.5; card bodies 11pt-equivalent with `--min-size` for minimized runs and a "reading view" toggle that hides minimized text.
- Density over whitespace: results are a ruled list (tag on line 1, cite + meta line in 12px `--meta`, snippet below), not floating cards. Visible 1px `--rule` borders, `border-radius: 0 0 2px 2px` at most, **no box shadows**.
- Underlined links, and keep the browser's visited-link state; it's genuinely useful when grinding through results and almost no modern site does it.

### 8.4 The signature element
The product's look **is** the card. Search results and card pages typographically reproduce a Word speech doc: bold 13pt-scale tag, the cite line, highlights in the selected Word color, shrunk minimized text. A debater should feel like they're reading their own prep. Everything around that (nav, filters, tables) stays quiet, gray, and ruled.

### 8.5 Banned: the AI-coded look
Grep-able checklist; §11 M5 literally lints the CSS/HTML for these. None of the following anywhere:
gradients; glassmorphism/backdrop blur; emoji in UI chrome; purple/violet/indigo anything; a marketing hero section ("Find winning evidence ✨" + three feature cards); Inter/Space Grotesk/generic Google-font stacks; `border-radius` > 4px; floating card grids with drop shadows; shimmer skeleton loaders; dark-mode-first neon; animated blobs or parallax; "Powered by AI" badges; centered single-button landing pages; toast confetti. Also banned in copy: exclamation marks, "seamless," "supercharge," "unleash," title-case buttons. Buttons say what they do in sentence case: "Download .docx", "Copy spoken text".

### 8.6 Interaction, a11y, print
- Keyboard-first: `/` focuses search, `j/k` or arrows move selection, `Enter` opens, `y` copies spoken text, `d` downloads .docx, `f` opens filters. Document the map in the footer.
- Instant search (fetch on input, 150 ms debounce) with a plain text "n results · x ms" line, no spinners for sub-200 ms responses.
- Visible focus rings, semantic HTML, contrast ≥ 4.5:1 (black text passes on yellow/green/turquoise; Blue uses the white-text rule from §8.3), `prefers-reduced-motion` respected (trivially, since there's no motion).
- A print stylesheet for `/card/<id>` that prints the card alone, correctly formatted. Debaters print prep; almost no web tool respects that.

---

## 9. Twenty-two features that separate this from Logos / Vault / defunct card-search sites

Context, so differentiation is honest: Logos is an existing card search scraped from opencaselist.com (as were the now-defunct Debate.Cards and DebateEv; Vault is another) [VERIFIED 2026-08-28, https://docs.paperlessdebate.com/verbatim/advanced/other-projects]. Secondhand descriptions credit Logos with tagline/block search, a natural-language "deep search," and roughly a million indexed cards `[VERIFY by using it before writing the About page comparison]`. So: don't sell keyword search or semantic search as novel. Features 1–12 are the v1 edge, and each falls out of the canonical/variant model and the PF-only focus; 13–22 are the expansion set that builds on them once v1 ships.

1. **Highlight consensus view.** On a card page, toggle between each team's highlighting, plus a "consensus" mode where words glow stronger the more variants highlight them (per-token counts across variants). Answers the real question: *what do good teams actually read from this card?* Implementation: token-align variants against the canonical body (they share it by construction), count marks per token.
2. **Card lineage.** A per-card timeline: first disclosure, spread across schools per month (sparkline), which tournaments it peaked at, `read by N teams / M schools`. Sort any search by `min_reads` / `sort:reads` to surface the meta. All derived from `card_variants ⋈ rounds`.
3. **A2 cross-index.** Parse `A2:`/`AT:` block titles into a normalized target string; on any card or search, show "answers other teams have disclosed to this argument." Two-sided prep in one click.
4. **Verbatim-true .docx export.** Select cards → one .docx with real Word styles (Heading 4 tags, correct sizes, underline/bold/highlight runs), in two presets: standard Verbatim, and the house layout from §1.5. Both presets export cites verbatim as disclosed (the original cutter's attribution, never restamped) and write highlights as the user's selected color's `WD_COLOR_INDEX` value so Word renders it natively. Built with `python-docx`; round-trip test: export a card, re-parse it, get an identical canonical key.
5. **Cite health.** Nightly job samples/queues `source_url` checks: alive, redirected, paywalled, dead; dead links get a Wayback Machine lookup link. Extracted `source_pub_date` powers `before:/after:` filters and a visible "evidence date" on every result, which matters on fast-moving PF topics.
6. **Miscut heuristics, labeled as heuristics.** Flags, never verdicts: highlight_ratio outliers; bracket-insertion density; ellipsis count; tag↔spoken lexical overlap so wildly low values surface possible power-tags. Shown as small gray glyphs with a tooltip explaining the signal, on the card page only. No numeric "quality score"; that would be false precision.
7. **Author & outlet index.** Browse `/authors`: cite-name clusters with aggregated quals text mined from fullcites, card counts, topics they appear on; same for source domains (how much of this topic is Brookings vs. Substack). Pure GROUP BY once cites are parsed.
8. **Topic coverage dashboard.** Per topic: cards/day since release, Pro vs. Con volume, most-read cards, most-cited authors/domains, and an "under-covered" hint (blocks that exist on one side with no disclosed answers). For a live topic (right now: the data-center moratorium) this doubles as a prep radar.
9. **Operator search + keyboard-first instant UI.** The full §7.2 grammar with sub-100ms responses and the §8.6 key map. Fast beats fancy; this is the daily-driver feature.
10. **Card boxes, saved searches, alerts.** Local (single-user) collections; a saved search or topic can emit an RSS feed / weekly digest of newly disclosed cards, which makes the "future topic" pages useful the moment a topic drops.
11. **Round context viewer.** Every variant links to its round: report text, side, opponent, tournament, and the *entire* parsed speech doc with jump-to-card, so a card is never divorced from how it was deployed.
12. **Single-file, offline, yours.** The whole corpus is one `.sqlite` you can copy to a flash drive and run at a tournament with no wifi. Export/import of card boxes as JSON. No accounts, no server dependency, no site that can go defunct like its predecessors did.


### Expansion set (v1.1+): ten more, in the same spirit

13. **Source-side view with cut-integrity diff.** A "view original" button on a card fetches `source_url` (live, else a Wayback snapshot), extracts readable article text, fuzzy-aligns the card body against it (token-level `difflib`), and renders side-by-side: kept text tinted, omissions, ellipses, and bracket insertions flagged. Turns evidence-ethics checking from a vibe into a diff. On-demand only, cached to the raw store, respects robots.txt, degrades politely on paywalls; never bulk-crawls sources.
14. **Tournament scout packs.** Paste a Tabroom entry list or type opponent school/team names; for each opponent, compile their current-topic disclosures (blocks by side, top cards, round reports) plus the answers other teams have already run against them via §9.3, and export one .docx per opponent through the §9.4 writer. Pure joins on `teams ⋈ rounds ⋈ card_variants`; keep the pasted-entries parser forgiving.
15. **Argument index.** Normalize block titles (strip `A2:`/`AT:`, numbering, punctuation) and cluster near-identical ones with the §4.3 shingle machinery at a looser threshold, yielding named argument pages: `/argument/<slug>` shows who runs it, on which side, its trajectory over the topic, the best cards inside it, and its disclosed answers. Add a second FTS table over `rounds.report` so "who has run X" is answerable even when block titles are idiosyncratic.
16. **Meta shift tracker.** Weekly deltas per topic: arguments rising and falling by disclosure count, brand-new blocks this week, cards gaining reads fastest, rendered as a ranked table with inline-SVG sparklines (no chart library). Computed by bucketing `card_variants ⋈ rounds` by week; feeds the §9.10 digest.
17. **Source pages.** Group *different* canonical cards cut from the *same* article: normalize `source_url` (strip tracking params, unify scheme and trailing slash) and fuzzy-match fullcite titles; `/source/<id>` lists every distinct cut of that article so you can pick the strongest one or re-cut longer from §9.13's original view. Not dedup's job (same article, different cards) and don't let it become dedup.
18. **Compare view.** Select any two cards or two variants and get a token-level side-by-side diff of bodies plus a highlighting-overlay diff; reuses §9.1's alignment for variants of one canonical and plain `difflib` otherwise. Answers the daily question "which version of this evidence do we read."
19. **Rebuttal drill mode.** `/drill?topic=present&side=con` deals a random disclosed tag + spoken text with a timer; you answer out loud or in a textbox, then reveal shows the wiki's disclosed A2 blocks (§9.3) and lets you save your answer as a note on a card box. Zero new data; one page; converts the corpus into practice reps.
20. **Prep-status overlay.** Local, single-keystroke states on any card or argument: `answered / needs answer / in our blocks / ignore`, stored in a `prep_status` table and surfaced everywhere as a small mark plus a `status:` search operator. This is the layer that turns search results and scout packs into a to-do list.
21. **Private backfile import.** `carddb ingest --source private ~/tubs/*.docx` runs the team's own files through the identical parser and dedup into rows flagged `origin='private'`: searchable alongside wiki cards with an `is:private` operator and a visible badge, excluded from every export/share path by default, and never touched by sync. Side benefit: dedup will tell you which of "your" cards are actually wiki cards.
22. **Speech math + evidence-exchange pack.** Every card and card box shows spoken-text word counts and read time at a configurable WPM (default 250), so a rebuttal doc says "1:47 at your pace" before you walk in. One click on a card box emits the standard evidence-exchange document (full cards, original cites untouched, no commentary) as .docx or paste-ready text via §9.4.

### Backlog: smaller ideas worth keeping

- Clipboard formats: copy a card as spoken text only, tag+cite only, or rich text with live highlighting for pasting into Word/Docs.
- Read-only local JSON API plus CSV export of any search, so the corpus is scriptable from notebooks or spreadsheets.
- Ingest Open Evidence PF camp files (openCaselist hosts the Open Evidence project): same parser, no round metadata, topic inferred from season/file naming `[VERIFY file organization before building]`.
- Judge index from `rounds.judge`: what a judge has seen run in front of them this topic.
- Colorblind-safe mode: a dotted underline beneath highlights so color is never the only signal.
- "Topic file" export: every canonical card on one topic as a single organized .docx, with a size warning.
- Parse a pasted Tabroom pairings link to auto-build §9.14 packs for tonight's rounds.
- An NSDA evidence-rules quick reference linked from every card page.
- Nightly integrity job: orphan variants, FTS/table sync drift, ledger gaps; report to `reports/`.
- A footer line showing days since last backup, nagging past 14.

---

## 10. Config and ops

- `config.toml`: paths, rate limits, Tabroom creds via env var names (never values), current caselist slugs (filled after API discovery), feature flags (`semantic=false`).
- CLI verbs: `ingest`, `dedup`, `topics assign`, `serve`, `sync` (weekly cron during season), `export`, `stats`.
- Logging: one line per unit with counts; end-of-run summary (fetched / parsed / failed / new canonicals / new variants / merges).
- Backup: `cp carddb.sqlite backups/$(date +%F).sqlite` weekly, keep 8.
- Refresh cadence in season: `sync` weekly, `dedup` after each sync, `topics assign` after NSDA announcements.

## 11. Build order and acceptance tests

Work strictly in this order; each milestone has a done-check. Do not start the UI before dedup passes.

- **M0 Scaffold.** Repo layout, schema migration, config, pytest wired. ✓ `pytest` green on an empty DB; `carddb serve` renders an empty search page.
- **M1 Bulk load.** HF dataset → PF subset → full pipeline. ✓ Log distinct caselist slugs and per-season counts; **rerun the loader: 0 new canonical cards, 0 new variants**; spot-open 20 random cards and compare against the dataset viewer.
- **M2 Dedup.** Layer 3 + reports. ✓ §4.4 criteria met; disagreement report generated and audited.
- **M3 Search.** FTS + query grammar. ✓ Operator unit tests; p95 latency < 100 ms over 30 recorded real queries at full corpus size (measure, print, commit the numbers to `benchmarks.md`).
- **M4 Live sync.** API discovery → endpoints.toml → 2024-25 season backfill first. ✓ One full caselist synced end-to-end; kill -9 mid-run and resume without re-requesting completed units; rate limiter provably caps at 1 rps (test with a mock server).
- **M5 UI.** ✓ `scripts/style_lint.py` greps templates/CSS for every §8.5 banned token and fails CI on any hit; keyboard map works; card page renders a fixture card pixel-plausibly next to the source .docx opened in Word; basic a11y pass (focus visible, landmarks, contrast).
- **M6 Features.** Ship features 1–12 in §9 order; each gets one test (e.g., 9.4's round-trip re-parse; 9.1's token alignment on a 3-variant fixture).
- **M7 Expansion (v1.1+).** Features 13–22, suggested order by prep value: 14 → 20 → 15 → 22 → 21 → 19 → 16 → 17 → 18 → 13. Same one-test rule (e.g., 9.13's alignment on a fixture article; 9.21 proves `origin='private'` rows never appear in any export path).

## 12. Verify-before-coding checklist (open items this spec could not confirm)

1. openCaselist terms of use + robots.txt: read them; confirm automated access posture. If unclear, email the maintainer before M4.
2. Exact API endpoints and auth flow, from https://api.opencaselist.com/v1/docs and the repo's `/v1/routes`.
3. Whether the API serves open-source file downloads directly, and any bulk/archive download the site itself offers for completed seasons.
4. Earliest PF season present in `Yusuf5/OpenCaselist`; exact PF `caselistName` slugs; the `side` encoding for PF rows.
5. `DebateRounds` Kaggle sqlite schema.
6. Carlito font license text when vendoring (expected SIL OFL).
7. Full historical PF resolution list + slot dates from speechanddebate.org/topics; tournament topic overrides (Nationals, TOC).
8. Logos's actual current feature set, before publishing any comparison copy.
9. Anything in this file marked `[VERIFY]`.

---

## Appendix A: hashing and near-dup parameters

```python
NORM_V = "1"
def canonical_key(body_text: str, tag: str, is_analytic: bool) -> str:
    base = f"{NORM_V}:analytic:{normalize(tag)}" if is_analytic \
           else f"{NORM_V}:{normalize(body_text)}"
    return hashlib.sha256(base.encode()).hexdigest()

# near-dup: MinHash(num_perm=128) over 5-token shingles of normalize(body_text)
# LSH bands b=8, rows r=16  -> candidate threshold ~ (1/8)**(1/16) ≈ 0.88
# merge iff (jaccard >= 0.90 or containment >= 0.95) and cite_years_match and author_overlap
```

## Appendix B: canonical card JSON (API + export shape)

```json
{
  "id": 481203,
  "tag": "Moratorium collapses grid interconnection queues",
  "cite": "Kessler '26",
  "fullcite": "…",
  "body_text": "…",
  "is_analytic": false,
  "source_url": "https://…",
  "source_pub_date": "2026-07-14",
  "topics": ["2026-SO"],
  "reads": {"teams": 41, "schools": 19, "first_seen": "2026-09-13"},
  "variants": [
    {"round": {"tournament": "…", "side": "P", "date": "2026-09-13",
               "team": "…", "school": "…"},
     "pocket": "Case", "hat": "C1 Grid", "block": "Uniqueness",
     "markup_html": "…", "spoken": "…", "highlight_ratio": 0.11}
  ]
}
```
(The card above is illustrative shape only; do not seed the DB with invented content.)

## Appendix C: query examples to use as operator tests

```
topic:present side:con "interconnection queue"
cite:kessler year:26 sort:recent
grid reliability -crypto after:2026-06-01 min_reads:5
topic:2026-SO is:analytic block:"A2: Moratorium"
```

## Appendix D: source links used in this spec

- https://opencaselist.com  ·  https://api.opencaselist.com/v1/docs
- https://github.com/ashtarcommunications/caselist  ·  https://github.com/ashtarcommunications/caselist-archive
- https://huggingface.co/datasets/Yusuf5/OpenCaselist  ·  https://huggingface.co/datasets/Hellisotherpeople/OpenCaseList-Deduplicated
- https://www.kaggle.com/datasets/yu5uf5/debate-rounds  ·  https://github.com/OpenDebate/debate-cards (v3)
- https://openreview.net/pdf?id=43s8hgGTOX (OpenDebateEvidence paper)
- https://docs.paperlessdebate.com/verbatim/debating-paperless/caselist  ·  https://docs.paperlessdebate.com/verbatim/advanced/other-projects
- https://www.speechanddebate.org/topics/
