# pf-card-search

A local-first search engine over every Public Forum cut card disclosed on the
[openCaselist wiki](https://opencaselist.com), with topic filters for past,
present, and future PF resolutions, zero duplicate cards in the index, and a
plain, dense, Calibri interface.

The full project brief lives in
[pf-card-search-build-spec.md](pf-card-search-build-spec.md) — read it first;
everything here implements it.

## What this is (and honestly is not)

The index covers **PF evidence disclosed on openCaselist** — most of the
national-circuit corpus from roughly the mid-2010s onward — not every card
ever cut. Local-circuit teams that don't disclose are invisible to any tool.

**The deployment is private by default.** Card bodies are excerpts from
copyrighted articles, papers, and books; redistribution inside the debate
community via the wiki is an established norm, but republishing the corpus on
the open internet is a different act. `carddb serve` binds to `127.0.0.1`.
This repository contains **code only** — no card data ships in it, ever
(`data/` is gitignored).

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest          # everything should be green
.venv/bin/python -m carddb serve    # http://127.0.0.1:8321 (empty until you ingest)
```

## Getting the cards

Two sources, in this order (spec §2):

**1. Bulk history — the OpenCaselist research dataset (no scraping).**
The [Yusuf5/OpenCaselist](https://huggingface.co/datasets/Yusuf5/OpenCaselist)
dataset (MIT license) carries the 2013–2024 corpus pre-parsed.

```bash
.venv/bin/pip install datasets      # optional dependency, pulls pyarrow
.venv/bin/python -m carddb ingest --source hf
```

The full dataset is ~27.6 GB in parquet — check your free disk before running
the unfiltered load. `--limit N` ingests a bounded sample. The loader streams,
filters to PF, logs the distinct caselist slugs it sees, and is idempotent:
running it twice adds zero cards.

**2. 2024 → today — the openCaselist API.**
Requires your own Tabroom credentials in env vars (`TABROOM_USERNAME`,
`TABROOM_PASSWORD`) and a contact email in `config.toml`. The sync is
checkpointed, resumable, and rate-limited to 1 request/second with backoff —
openCaselist is a community-run nonprofit; be polite to it (spec §0.2).

```bash
.venv/bin/python -m carddb sync
```

Then:

```bash
.venv/bin/python -m carddb topics assign   # load data/topics.json, assign rounds to topics
.venv/bin/python -m carddb dedup           # near-duplicate pass (layer 3)
.venv/bin/python -m carddb stats
```

## CLI

| Verb | What it does |
|---|---|
| `ingest --source {hf\|api\|private}` | run a source through the one pipeline (fetch → raw store → parse → normalize/dedup/insert) |
| `dedup` | MinHash/LSH near-duplicate pass + disagreement report |
| `topics [assign]` | load `data/topics.json`, assign every round to a topic |
| `serve` | the website, `127.0.0.1:8321` |
| `sync` | weekly in season: pull new disclosures from the API |
| `export --cards 1,2,3 --out f.docx` | Verbatim-true .docx export (house or verbatim preset) |
| `citehealth` | sample source URLs: alive / redirected / paywalled / dead |
| `stats` | corpus counts |
| `backup` | copy the sqlite to `backups/`, keep 8 |

## Search operators

```
topic:present side:con "interconnection queue"
cite:kessler year:26 sort:recent
grid reliability -crypto after:2026-06-01 min_reads:5
topic:2026-SO is:analytic
```

Full grammar in spec §7.2. Malformed operators degrade to plain terms.

## Design

One SQLite file (FTS5), server-rendered Jinja2, one hand-written CSS file,
under 300 lines of vanilla JS. Calibri (Carlito vendored for macOS/Linux,
SIL OFL — `static/fonts/OFL.txt`). The look *is* the card: search results
typographically reproduce a Word speech doc. `scripts/style_lint.py` fails CI
on any banned template-generated aesthetic (spec §8.5).

## Credits

This project exists because other people did the hard part:

- [Yusuf5/OpenCaselist](https://huggingface.co/datasets/Yusuf5/OpenCaselist)
  (MIT) and the [OpenDebateEvidence paper](https://openreview.net/pdf?id=43s8hgGTOX)
- [openCaselist](https://opencaselist.com) /
  [ashtarcommunications/caselist](https://github.com/ashtarcommunications/caselist)
- [Verbatim / paperlessdebate docs](https://docs.paperlessdebate.com)
- NSDA topic history at [speechanddebate.org/topics](https://www.speechanddebate.org/topics/)

## License

Code: MIT (see LICENSE). Card data is never distributed with this repository;
see spec §0.4 for the copyright posture.
