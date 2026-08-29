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

Two sources, in this order (spec §2). **Neither requires downloading the full
27.6 GB dataset.**

**1. Bulk history — the OpenCaselist research dataset, streamed remotely.**
[Yusuf5/OpenCaselist](https://huggingface.co/datasets/Yusuf5/OpenCaselist)
(MIT) holds the pre-parsed corpus as 109 parquet shards. `carddb.hf_remote`
queries those shards *in place* over HTTP range requests with DuckDB, pulling
only `event='pf'` rows. Shards with no PF content are never fetched at all —
`scripts/hf_census.py` reads just the `event` column of each shard first to
find out which ones matter. Nothing lands on disk except the SQLite index.

```bash
.venv/bin/python scripts/hf_census.py          # once: which shards hold PF
.venv/bin/python -m carddb ingest --source hf-remote
```

This covers **PF seasons 2019-20 through 2022-23** (`hspf19`–`hspf22`).
That is the dataset's full PF extent: PF disclosure on openCaselist begins
with the site's 2019 launch, and the dataset ends at the 2022-23 caselists.

A fully local load is still available (`--source hf`, needs `pip install
datasets` and ~28 GB free) but there is no reason to prefer it.

**2. 2023-24 → today — the openCaselist API.**
Seasons `hspf23`, `hspf24`, `hspf25`, `hspf26` are **not** in any public
dataset and must come from the site itself. **This requires your own Tabroom
login.** openCaselist has no public read path: every data route is behind a
`caselist_token` session cookie (`server/v1/helpers/auth.js`; only `/status`
and `/login` are public). Supply credentials as environment variables — they
are never stored in the repo:

```bash
.venv/bin/python -m carddb signin   # opens the sign-in page, then "Sync all seasons"
```

or, entirely from the terminal:

```bash
.venv/bin/python -m carddb login    # asks once, stores the session token
.venv/bin/python -m carddb sync     # no season name: walks every PF season
```

Naming a season (`sync --caselist hspf25`) does just that one.

`carddb login` prompts for your Tabroom email and password, sends them
straight to openCaselist, and saves only the two-week session token it gets
back (0600, under `~/.config/pf-card-search/`). The password is never written
to disk, never echoed to the terminal, and never lands in shell history.
`carddb logout` removes the token. The `TABROOM_USERNAME` / `TABROOM_PASSWORD`
environment variables still work for unattended cron runs.

**Want the sign-in on a website?** Run the app on a host rather than on
static Pages — see [DEPLOY.md](DEPLOY.md). Same code, same `/connect` page,
and the login works because the server does it.

**A browser sign-in on the *static* site is not possible**, and that is
openCaselist's design, not a gap here: their session cookie is `SameSite=Lax`
and scoped to `opencaselist.com`, so no other origin can ever hold it. See
`docs/api_access.md`.

The sync is checkpointed, resumable, and capped at 1 request/second with
backoff. openCaselist is a community-run nonprofit — be polite to it, and run
bulk backfills overnight (spec §0.2).

Then:

```bash
.venv/bin/python -m carddb topics assign   # load data/topics.json, assign rounds
.venv/bin/python -m carddb dedup           # near-duplicate pass (layer 3)
.venv/bin/python -m carddb stats
```

## CLI

| Verb | What it does |
|---|---|
| `ingest --source {hf-remote\|hf\|api\|private}` | run a source through the one pipeline (fetch → raw store → parse → normalize/dedup/insert) |
| `dedup` | MinHash/LSH near-duplicate pass + disagreement report |
| `topics [assign]` | load `data/topics.json`, assign every round to a topic |
| `serve` | the website, `127.0.0.1:8321` |
| `sync` | weekly in season: pull new disclosures from the API |
| `export --cards 1,2,3 --out f.docx` | Verbatim-true .docx export (house or verbatim preset) |
| `citehealth` | sample source URLs: alive / redirected / paywalled / dead |
| `stats` | corpus counts |
| `login` / `logout` | sign in to openCaselist once; stores the session token, never the password |
| `reindex` | rebuild FTS rows + derived aggregates from the tables of record |
| `backup` | WAL-safe online backup to `backups/`, keep 8 |

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
