# Module contracts

Interfaces every module implements, so the pieces integrate without touching
each other's files. The spec (pf-card-search-build-spec.md) governs behavior;
this file governs signatures. **Target Python 3.9** (no `match`, no `X | Y`
annotations without `from __future__ import annotations`). Run tests with
`.venv/bin/python -m pytest`.

## Core (already written — import, do not modify)

- `carddb.normalize`: `NORM_V`, `normalize(s) -> str` (§3.5, frozen)
- `carddb.keys`: `canonical_key(body_text, tag, is_analytic) -> str`, `sha256_bytes(b)`
- `carddb.sanitize`: `sanitize_markup(raw_html) -> str` (allowed: h1–h4 p u strong em mark span br; only `class` survives)
- `carddb.db`: `open_db(path)`, `init_db(conn)`, `connect(path)`, `fts_upsert_cards(conn, ids)`, `fts_rebuild(conn)`, `recompute_aggregates(conn, ids=None)`, `ledger_seen(conn, source, external_id, sha256=None)`, `ledger_put(...)`; schema per spec §5 + `cards.team_count` (materialized like school_count), `card_variants.a2_target`, `cite_health`, `card_boxes`, `card_box_members`, `saved_searches`, `sync_checkpoints`
- `carddb.ingest`: `CardRecord` dataclass (canonical + variant fields, `.key()`), `insert_card(conn, rec) -> (card_id, created)`, `attach_variant(conn, card_id, rec, document_id, round_id) -> (variant_id, created)`, `get_or_create_caselist/school/team/round`, `normalize_side(raw) -> 'P'|'C'|None`, `IngestStats`, `finish_batch(conn, stats)`, `ledger_stamp(conn, source, external_id, sha256)`
- `carddb.rawstore`: `store_bytes(raw_root, data) -> (sha, path)`, `record_document(conn, sha, origin, origin_url, orig_filename, local_path) -> doc_id`, `now_iso()`
- `carddb.a2`: `a2_target(block_title) -> Optional[str]`, `argument_key(block_title) -> Optional[str]`
- `carddb.config`: `load_config(path=None) -> dict`, `resolve_path(cfg, key) -> Path`, `ROOT`

## carddb/docx_parser.py  (spec §1.3, §3.4)

```python
@dataclass
class ParsedDocument:
    cards: List[CardRecord]     # ordinal = 0..n-1 in document order
    warnings: List[str]
    used_fallback: bool         # direct-formatting fallback pass triggered

class ParseFailure(Exception): ...   # message = reason; callers record parse_status='failed'

def parse_docx(path) -> ParsedDocument
def parse_docx_bytes(data: bytes, filename: str = "") -> ParsedDocument
def convert_doc_to_docx(path) -> Path   # via `soffice --headless`; raises ParseFailure if unavailable
```

CardRecord fields the parser must fill: tag, cite, fullcite, body_text,
is_analytic, source_url, source_pub_date, pocket, hat, block, markup_html
(sanitized), summary, spoken, highlight_ratio, ordinal, extras
(`has_table: bool` when applicable).

## carddb/dedup.py  (spec §4.3, Appendix A)

```python
def cite_year(cite: Optional[str]) -> Optional[str]      # 2-digit year from a short cite
def author_tokens(cite: Optional[str]) -> Set[str]
def run_dedup(conn, report_dir: Path, seed: int = 0) -> DedupStats
    # MinHash(128) over 5-token shingles of normalize(body_text), LSH b=8 r=16;
    # merge iff (jaccard>=0.90 or containment>=0.95) and cite years match and author overlap;
    # merging repoints variants to survivor, records card_merges(relation 'dup'|'trim'),
    # deletes absorbed card + its FTS row, updates aggregates.
    # If table hf_buckets(card_id, bucket_id) exists, write
    # report_dir/dedup_disagreements.tsv comparing our clusters vs bucketId.
@dataclass
class DedupStats: candidates: int; merged: int; trims: int; disagreements: int
```

## carddb/query.py + carddb/search.py  (spec §7)

```python
# query.py
@dataclass
class ParsedQuery:
    fts: Optional[str]          # FTS5 MATCH expression or None
    filters: Dict[str, Any]     # topic, season, side ('P'/'C'), school, team, cite,
                                # year, before, after, is_analytic, min_reads, status...
    sort: str                   # 'relevance' (default) | 'reads' | 'recent' | 'length'
def parse_query(q: str) -> ParsedQuery   # malformed operators degrade to plain terms, never raise

# search.py
@dataclass
class SearchHit:
    card_id: int; tag: str; cite: str; snippet_html: str; body_len: int
    is_analytic: bool; team_count: int; school_count: int
    topic_codes: List[str]; source_pub_date: Optional[str]
@dataclass
class SearchResult:
    hits: List[SearchHit]; total: int; elapsed_ms: float; query: ParsedQuery
def search(conn, q: str, limit: int = 30, offset: int = 0,
           today: Optional[date] = None) -> SearchResult
    # bm25(card_fts, 5.0, 3.0, 2.0, 1.0); one row per canonical card;
    # fielded filters as SQL predicates; topic:present/past/future resolved
    # via carddb.topics.resolve_topic_token
```

## carddb/hf_loader.py  (spec §2.1, §3.3)

```python
def map_hf_row(row: dict) -> Tuple[CardRecord, dict]
    # dict = round/team/school/caselist metadata extracted from the row
def ingest_hf_rows(conn, rows: Iterable[dict], cfg: dict, stats: IngestStats,
                   pf_only: bool = True) -> IngestStats
    # ledger unit per row (source='hf'); normalize side; sanitize markup;
    # bucketId -> table hf_buckets(card_id INTEGER, bucket_id TEXT)
    #   (CREATE TABLE IF NOT EXISTS here);
    # batch commits ~5k rows; logs distinct caselistName/event values seen.
def ingest_hf(conn, cfg, stats, limit=None, streaming=True) -> IngestStats
    # requires `datasets` (optional dep); clear error message if missing
def fetch_sample_rows(n: int = 200, event_filter: str = "pf") -> List[dict]
    # via https://datasets-server.huggingface.co (rows/filter API), for tests/dev
```

## carddb/topics.py  (spec §6)

```python
def load_topics(conn, topics_json_path) -> int        # upsert into topics table
def topic_status(topic_row, today: date) -> str       # 'past'|'present'|'future'
def resolve_topic_token(conn, token: str, today: date) -> List[int]  # 'present'|'past'|'future'|code
def assign_topics(conn, today: Optional[date] = None) -> AssignStats
    # §6.2: season -> date window -> overrides -> keyword fallback -> NULL bucket;
    # then materialize cards.topic_ids as sorted JSON list of topic codes.
def current_topic(conn, today) -> Optional[sqlite3.Row]
```

## carddb/ratelimit.py + carddb/api_sync.py  (spec §0.2, §2.2)

```python
class RateLimiter:
    def __init__(self, rps: float, sleep=time.sleep, clock=time.monotonic): ...
    def wait(self) -> None
def request_with_backoff(client, method, url, *, limiter, max_retries, **kw) -> httpx.Response
    # exponential backoff on 429/5xx, honors Retry-After
def discover_endpoints(api_base: str, out_path: Path) -> dict
    # fetch the OpenAPI spec, transcribe real paths into config/endpoints.toml
def sync(conn, cfg, caselist: Optional[str] = None, since: Optional[str] = None) -> IngestStats
    # checkpoint row per (caselist, school, team) in sync_checkpoints; resume skips
    # completed units; docx bytes -> rawstore -> docx_parser -> ingest path
```

## carddb/export_docx.py  (spec §1.5, §9.4)

```python
HIGHLIGHT_COLORS = {"green": WD_COLOR_INDEX.BRIGHT_GREEN, "yellow": ..., "blue": ..., "turquoise": ...}
def export_cards(conn, card_ids: List[int], out_path,
                 preset: str = "house",      # 'house' | 'verbatim'
                 highlight: str = "green",
                 variant_ids: Optional[List[int]] = None) -> Path
    # Heading 4 tags; markup_html -> runs (<u>/<strong>/<mark>/<span class="min">);
    # cites exported VERBATIM, never restamped (spec §1.5);
    # round-trip invariant: re-parsing the export yields the same canonical_key.
def spoken_word_count(spoken: str) -> int
def read_time_str(words: int, wpm: int = 250) -> str    # "1:47"
```

## Feature modules  (spec §9)

```python
# carddb/consensus.py (9.1)
def variant_mark_vector(body_text: str, markup_html: str) -> List[bool]  # per body token
def consensus(body_text: str, markup_htmls: List[str]) -> List[Tuple[str, int]]
    # (token, highlight_count) aligned to the canonical body's tokens

# carddb/heuristics.py (9.6) — flags, never verdicts
def miscut_flags(card_row, variant_rows) -> List[Flag]   # Flag(code, label, detail)

# carddb/citehealth.py (9.5)
def check_url(client: httpx.Client, url: str) -> dict    # status/'http_status'/'final_url'/'wayback_url'
def run_citehealth(conn, limit: int = 200, timeout: float = 10.0) -> int
```

## Web UI  (spec §8)

```python
# carddb/server.py
def create_app(db_path=None, cfg: Optional[dict] = None) -> FastAPI
```
Routes: `/` (search + topic picker + corpus stats empty state), `/search`
(JSON for instant search), `/card/{id}`, `/topic/{code}`, `/school/{id}`,
`/team/{id}`, `/round/{id}`, `/authors`, `/stats`, `/about`, `/boxes`…,
`/export/docx` (POST card ids + preset + highlight -> .docx),
`/feed/topic/{code}.rss`. Templates in `templates/`, assets in `static/`
(`style.css` per §8.3 tokens, `app.js` < 300 lines vanilla JS).
`scripts/style_lint.py` fails on any §8.5 banned token in templates/ or static/.

## Shared rules

- Never modify core files or another module's files; note needed core
  changes in your final report instead.
- Every module gets pytest coverage in `tests/test_<module>.py`; fixtures
  under `tests/fixtures/` (generate .docx fixtures in code with python-docx).
- No network in tests except where the test is explicitly an integration
  test marked `@pytest.mark.skipif` on the fixture/network being absent.
- Cites are never restamped anywhere (spec §1.5). Sides display as
  Pro/Con, stored as 'P'/'C'.
