# What this index actually covers

Numbers below are measured, not estimated. The shard census
(`reports/shard_census.json`, built by `scripts/hf_census.py`) read the
`event` and `caselistName` columns of all 109 parquet shards of
`Yusuf5/OpenCaselist` on 2026-08-28.

## The dataset in full

| event | rows | |
|---|---:|---|
| `cx` (Policy) | 2,768,419 | out of scope |
| `ld` (Lincoln-Douglas) | 1,526,383 | out of scope |
| **`pf` (Public Forum)** | **43,131** | **what this index ingests** |
| *(null event, null caselist)* | 492,628 | see "The unattributed block" |
| total | 4,830,561 | |

## PF, by season

| caselist | season | rows |
|---|---|---:|
| `hspf17` | 2017-18 | 285 |
| `hspf18` | 2018-19 | 907 |
| `hspf19` | 2019-20 | 4,746 |
| `hspf20` | 2020-21 | 10,794 |
| `hspf21` | 2021-22 | 9,301 |
| `hspf22` | 2022-23 | 17,098 |

PF rows live in 36 of the 109 shards; the other 73 are never fetched.

**Correction to `docs/hf_verify.md` §3:** that report concluded the earliest PF
season was `hspf19`, inferred from caselist-id blocks and offset probes. The
full census finds `hspf17` (285 rows) and `hspf18` (907 rows) as well. The
earlier conclusion came from sampling; this one reads every shard.

## What the published site ships

All 43,131 PF rows were ingested — the ingest ledger holds exactly 43,131
`source='hf'` units, matching the census. They collapse to **25,771 unique
cards** (18,818 evidence + 6,953 analytics) across 42,876 disclosures; the
difference is the same card disclosed by many teams, plus 527 near-duplicate
merges. The site ships all 25,771.

## The unattributed block

492,628 rows (10% of the dataset) carry a null `event`, `caselistName`,
`roundId`, `side`, and `year` — evidence not linked to any round or caselist,
most likely Open Evidence camp files and unmatched uploads. Some fraction is
probably PF camp evidence.

**These rows are deliberately excluded, and sampling confirms that is right.**
Reading rows from the tail (shard 100) returns cards like "Substantial can be
0.3%" and "In context, substantial can be 902 million" (US GAO 18) alongside
drone- and arms-sales evidence — topicality cards from the 2018-19 arms sales
topic. That is Policy/LD camp evidence, not PF. Folding it in would bury PF
results under Policy cards, and at roughly 10x the PF corpus it would also
blow past what static hosting can serve.

There is no metadata to attribute these rows by, and the alternative —
guessing from resolution keywords — would put Policy and LD cards on PF topic
pages with no way for a reader to tell. Spec §6.2's rule applies: never
silently guess.

## Seasons 2023-24 onward are not here

No public dataset contains them. `hspf23`, `hspf24`, `hspf25`, `hspf26` exist
only on openCaselist, which has no public read path — every data route
requires a Tabroom session cookie (`docs/api_access.md`). Two ways to add
them, both needing your own Tabroom login:

- the local app's **Connect** page (`carddb serve` → `/connect`), which runs
  the full Python docx parser and produces complete cards;
- the public site's **Connect to openCaselist** control, which authenticates
  your browser directly against openCaselist and reads the recent seasons
  live.

## The honest summary

This index covers **PF evidence disclosed on openCaselist and captured by the
OpenCaselist research dataset, seasons 2017-18 through 2022-23** — not every
card ever cut. Teams that do not disclose are invisible to any tool built this
way, disclosure is far less complete in PF than in Policy, and the dataset's
own parser will have dropped files it could not read.
