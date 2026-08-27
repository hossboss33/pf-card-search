# Verification: `Yusuf5/OpenCaselist` Hugging Face dataset (spec §2.1, §12 item 4)

Verified 2026-08-27 against the live Hugging Face datasets-server HTTP API
(`https://datasets-server.huggingface.co`) and the dataset card. All requests were
plain unauthenticated GETs with `User-Agent: pf-card-search-build`. The 300-row PF
sample captured during this verification lives at `tests/fixtures/hf_sample.json`.

Sources used:

- `https://datasets-server.huggingface.co/info?dataset=Yusuf5%2FOpenCaselist`
- `https://datasets-server.huggingface.co/statistics?dataset=Yusuf5%2FOpenCaselist&config=default&split=train`
- `https://datasets-server.huggingface.co/first-rows?dataset=Yusuf5%2FOpenCaselist&config=default&split=train`
- `https://datasets-server.huggingface.co/rows?dataset=Yusuf5%2FOpenCaselist&config=default&split=train&offset=<N>&length=<M>` (many offsets; see §6)
- `https://datasets-server.huggingface.co/parquet?dataset=Yusuf5%2FOpenCaselist`
- `https://datasets-server.huggingface.co/is-valid?dataset=Yusuf5%2FOpenCaselist`
- `https://huggingface.co/datasets/Yusuf5/OpenCaselist/raw/main/README.md` (dataset card)
- `https://huggingface.co/api/datasets/Hellisotherpeople/OpenCaseList-Deduplicated` (companion check)

## 1. Shape, size, license

- One config: `default`; one split: `train`; **4,830,561 rows** (matches spec §2.1),
  `dataset_size` 49.0 GB uncompressed, `download_size` 27.6 GB, 109 parquet shards
  under `refs/convert/parquet/default/train/` (conversion complete, `partial: false`).
- **License: MIT — confirmed.** The dataset card YAML front matter says `license: mit`
  and the card body says "License: MIT"
  (https://huggingface.co/datasets/Yusuf5/OpenCaselist).
- Card confirms: evidence from Policy, LD, and Public Forum, "years 2013-2024",
  parsed from .docx files uploaded to openCaselist; debater names truncated to the
  first 2 characters (the parquet actually omits debater-name columns entirely — §2).

## 2. Full field list (exact, from `/info` — this is authoritative)

31 columns. Every dtype is `string` except `id`, which is `int64`.

| # | field | dtype | notes from live rows |
|---|---|---|---|
| 1 | `id` | int64 | unique per row (see §5) |
| 2 | `tag` | string | nullable (6/300 PF sample rows null) |
| 3 | `cite` | string | short cite; **null on 151/300 PF sample rows** |
| 4 | `fullcite` | string | null on 95/300 |
| 5 | `summary` | string | underlined text; null on 108/300 |
| 6 | `spoken` | string | highlighted text; null on 182/300 (many PF cards have no highlighting) |
| 7 | `fulltext` | string | plain body; **null on 94/300 — those are analytics (null `fulltext`, non-null `tag`)** |
| 8 | `textLength` | string | stringified int, e.g. `"823"`; null when `fulltext` null |
| 9 | `markup` | string | HTML with `<h4> <p> <u> <strong> <mark>` (see §7) |
| 10 | `pocket` | string | null on 188/300 (PF files often skip pockets) |
| 11 | `hat` | string | null on 137/300 |
| 12 | `block` | string | null on 147/300 |
| 13 | `bucketId` | string | dedup bucket id |
| 14 | `duplicateCount` | string | stringified int; sample range 1–2313 |
| 15 | `filePath` | string | **null on all 300 PF sample rows** |
| 16 | `roundId` | string | present on all 300 PF sample rows |
| 17 | `side` | string | `'A'` / `'N'` (see §4) |
| 18 | `round` | string | e.g. `"1"` |
| 19 | `report` | string | round report text; present on 199/300 |
| 20 | `opensourcePath` | string | **null on all 300 PF sample rows** |
| 21 | `caselistUpdatedAt` | string | `"YYYY-MM-DD HH:MM:SS"`; null on 225/300 |
| 22 | `teamId` | string | numeric string |
| 23 | `schoolId` | string | numeric string |
| 24 | `chapterId` | string | null on 281/300 |
| 25 | `caselistId` | string | numeric string (see §3) |
| 26 | `caselistName` | string | slug, e.g. `hspf22` |
| 27 | `caselistDisplayName` | string | e.g. `"HS PF 2022-23"` |
| 28 | `year` | string | `"2019"` = the 2019-20 season |
| 29 | `event` | string | `cx` / `ld` / `pf` observed (also `nfald*` slugs carry `event='ld'`, `level='college'`) |
| 30 | `level` | string | `hs` / `college` |
| 31 | `teamSize` | string | `"1"` / `"2"`; all PF rows sampled are `"2"` |

**Discrepancy vs. the dataset card and spec §2.1:** the card's own field table (and the
spec, which paraphrases it) lists columns that do **not** exist in the actual parquet:
`fileId`, `tournament`, `opponent`, `judge`, `teamName`, `teamDisplayName`,
`teamNotes`, `debater1First/Last`, `debater2First/Last`, `schoolName`,
`schoolDisplayName`, `state`. The real data carries only numeric **ids**
(`teamId`, `schoolId`, `chapterId`, `caselistId`, `roundId`) — no names, no
tournament/opponent/judge. Team/school display names and tournament metadata must
come from the `DebateRounds` Kaggle sqlite (`yu5uf5/debate-rounds`) or the live API,
not from this dataset. `hf_loader` should populate name fields as NULL/placeholder
keyed on the external ids.

Also note: a large tail block of the dataset (~rows 4,335,000–4,830,560, roughly
470k rows) has **null** `caselistName`/`caselistId`/`roundId`/`side`/`year`/`event`/
`level` — evidence rows not linked to any round/caselist (low `id` values; likely
Open Evidence / unmatched files). These cannot be event-filtered and are excluded
by a PF filter on `event='pf'` by construction.

## 3. PF caselist slugs discovered (each verified by fetching live rows)

| slug | `caselistId` | `caselistDisplayName` | `year` | verified at `/rows` offset |
|---|---|---|---|---|
| `hspf19` | 1032 | HS PF 2019-20 | 2019 | 1,820,000 |
| `hspf20` | 1033 | HS PF 2020-21 | 2020 | 2,650,000 |
| `hspf21` | 1034 | HS PF 2021-22 | 2021 | 3,350,000 |
| `hspf22` | 2004 | HS PF 2022-23 | 2022 | 4,325,000 |

All four have `event='pf'`, `level='hs'`, `teamSize='2'`. The slug pattern is
`hspf<yy>` as the spec guessed, where `<yy>` is the season-start year.

**Earliest PF season present: 2019 (hspf19, "HS PF 2019-20").** Evidence that no
earlier PF exists: (a) `/statistics` (partial, covering exactly the first 531,812
rows = the 2014–2016 old-wiki era) lists 10 caselists — `ndtceda14/15/16`,
`hspolicy14/15/16`, `hsld14/15/16`, `nfald16` — no `hspf*`; (b) the `caselistId`
layout is contiguous blocks per event (`ndtceda14..21` = 1004–1011, `hspolicy14..21`
= 1013–1020, `hsld14..21` = 1022–1029) with PF starting at 1032 = `hspf19`, i.e.
PF disclosure begins with opencaselist.com's 2019 launch; (c) offset probes across
the 2014–2018 regions hit only cx/ld caselists.

**Latest PF caselist observed: `hspf22` (HS PF 2022-23).** The caselist-labeled data
ends with the 2022-23 caselists (`nfald22`, id 2005, at offset ~4,330,000) followed
immediately by the null-metadata tail; no `hspf23` was observed. Caveat: caselist
runs are interleaved, not strictly sorted, so a small unprobed 2023 run cannot be
absolutely ruled out — the loader's "log distinct caselistName values seen" step
(spec §11 M1) is the definitive census.

Non-PF slugs confirmed while probing (for the loader's expect-list): `ndtceda14..22`,
`hspolicy14..22`, `hsld14..22`, `nfald16`, `nfald22`.

## 4. `side` encoding in PF rows

PF rows use **`'A'` and `'N'`** (Policy Aff/Neg convention), exactly as the spec's
`[VERIFY]` note suspected — not "Pro"/"Con" and not 'P'/'C'. Sample of 550 PF rows
fetched: only `'A'` and `'N'` appear (e.g. hspf22 pull: 201 A / 99 N). Normalize at
ingest with `carddb.ingest.normalize_side`: `A → 'P'`, `N → 'C'`.

## 5. Per-row unique identifier (for the ingest ledger)

Use the **`id`** column (int64). The dataset card documents it as "Unique identifier
for the evidence"; all 550 PF rows fetched have distinct values (sample range
1,622,651–4,817,640), and it is the only integer-typed column. Ledger unit:
`source='hf'`, `external_id=str(row['id'])`. Note `id` is NOT the row index —
row_idx 4,400,000 carried `id` 64223 — so never use dataset offsets as ids.
`bucketId` is a dedup-bucket id (shared across duplicates) and `roundId` is shared
by all cards in one round; neither is row-unique.

## 6. How the sample was captured (and its limitations)

The `/filter` endpoint (and by implication `/search`) was **unavailable for this
dataset during the whole verification window** (~40 min): every
`where="event"='pf'` call returned HTTP 500 with alternating bodies
`{"error":"the dataset index is loading, this can take a minute"}` and
`{"error":"Unexpected error."}` across ~11 spaced attempts. `/is-valid` claims
`"filter": true`, but the DuckDB index never came up; note that for a 27.6 GB
dataset the index is likely *partial* (the `/statistics` response is `partial: true`
and covers only the first 531,812 rows, which are 2014–2016 and contain zero PF),
so even a working `/filter` may not reach PF rows.

Fallback used: the plain `/rows` endpoint (no index required, spans the full
dataset, no cell truncation — `truncated_cells` was empty on every fetched row).
Rows are grouped in long contiguous single-caselist runs, roughly ascending by
season but interleaved within a season (the same caselist appears in multiple
runs). ~43 one-row offset probes located four PF runs, then five 50–100-row page
reads pulled the sample:

- 100 rows of `hspf19` @ offset 1,820,000
- 50 rows of `hspf20` @ offset 2,650,000
- 100 rows of `hspf21` @ offset 3,350,000 (75 kept)
- 300 rows of `hspf22` @ offsets 4,324,900–4,325,199 (75 kept)

**The fixture IS strictly PF-filtered** — `event == 'pf'` was asserted on every row
client-side — but it is **four contiguous slices, not a uniform random sample**: it
over-represents whichever teams/files sit at those offsets, and A/N balance
(173/127) reflects that. Do not use it to estimate corpus-level distributions;
do use it for parser/loader/mapping tests. Total requests to Hugging Face during
this verification: ~69 small GETs spread over ~45 minutes (the ~50 target was
exceeded because of the /filter outage and the resulting offset bisection; all
overage was 1-row `/rows` reads).

## 7. `markup` field details (for sanitizer/loader)

Observed tags in PF rows: `<h4>` (tag), `<p>`, `<u>`, `<strong>`, `<mark>` — matches
spec §2.1. Two things to handle:

- **Improperly nested close tags are common**, e.g.
  `<u><strong><mark>Pomeroy</u></strong></mark>` and `<h4><strong>…</h4><p>…</strong>`
  (close order does not mirror open order; elements straddle block boundaries).
  `sanitize_markup` / any HTML parsing must tolerate non-well-formed HTML.
- Analytics appear as rows with non-null `tag`/`markup` but null `fulltext`
  (94/300 in the sample), exactly the spec §3.3 rule "null `fulltext` and
  non-null `tag` → analytics".

## 8. Companion dataset check

`Hellisotherpeople/OpenCaseList-Deduplicated` (spec §4.3 cross-check source)
currently returns **HTTP 401** from both the datasets-server and the Hub API
(`https://huggingface.co/api/datasets/Hellisotherpeople/OpenCaseList-Deduplicated`)
— it is private/gated or removed as of 2026-08-27. The §4.3 dedup cross-check
should rely on `bucketId`/`duplicateCount` from the primary dataset (→ `hf_buckets`
table) unless access to the deduplicated variant is restored; treat that spec item
as blocked, not skipped.
