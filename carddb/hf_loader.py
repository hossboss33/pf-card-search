"""Bulk loader for the `Yusuf5/OpenCaselist` Hugging Face dataset. Spec §2.1, §3.3.

Maps dataset rows onto the §5 schema through the single normalize/dedup/insert
path in carddb.ingest. Facts about the live dataset referenced below were
verified against the datasets-server API on 2026-08-27; see
docs/hf_verify.md.

Design decisions (documented per spec/contract):

* **Ledger unit / external_id** (idempotence layer 1): the dataset's int64
  ``id`` column — the dataset card documents it as "Unique identifier for the
  evidence", it is the only integer-typed column, and every PF row fetched
  during verification carried a distinct value (hf_verify.md §5). It is NOT
  the dataset row index (row_idx 4,400,000 carried id 64223), so never use
  offsets. Ledger rows are ``(source='hf', external_id=str(row['id']))`` with
  a sha256 of the row's canonical JSON, so a re-shipped row with changed
  content is reprocessed instead of skipped.

* **Synthetic documents** (idempotence for attach_variant): PF rows carry no
  .docx — ``filePath`` and ``opensourcePath`` were null on every PF row
  sampled (hf_verify.md §2). Variants are UNIQUE(document_id, ordinal), so
  each row needs a deterministic (document, ordinal) home. We create one
  synthetic ``documents`` row per distinct source-file reference when the row
  has one (``filePath``, else ``opensourcePath``), else one per ``roundId``,
  else one per row id, keyed by ``sha256("hf:doc:<kind>:<identity>")`` —
  documents.sha256 is UNIQUE, so re-runs find the same row. The ordinal
  within that synthetic document is the dataset row ``id``: deterministic,
  unique per row, and (ids ascend with disclosure order) it preserves
  relative order of cards within a round.

* **Entity names**: the actual parquet has NO name columns — only numeric ids
  (teamId/schoolId/chapterId/caselistId/roundId), despite the dataset card
  listing name fields (hf_verify.md §2). Schools/teams are created with
  deterministic placeholder names ``school-<schoolId>`` / ``team-<teamId>``
  and the numeric id in ``external_id``; display names can be enriched later
  from the DebateRounds Kaggle sqlite or the live API. Round external ids are
  namespaced ``hf-<roundId>`` so a future API sync using raw site round ids
  can never collide with them by accident.

* **Sides**: PF rows encode 'A'/'N' (Policy convention, hf_verify.md §4);
  normalized to 'P'/'C' via carddb.ingest.normalize_side.

* **Analytics**: rows with null/empty ``fulltext`` and non-null ``tag`` are
  analytics (spec §3.3); rows with neither are unmappable and are counted as
  failed (and ledger-stamped so they are not retried every run).

* **Debater privacy**: the dataset truncates debater names to 2 characters
  (spec §2.1) and the parquet actually ships no debater-name columns at all;
  nothing here attempts re-identification.

* **bucketId**: the dataset's own dedup signal is preserved in
  ``hf_buckets(card_id, bucket_id)`` (created here IF NOT EXISTS, with a
  unique index for idempotent inserts) for the §4.3 cross-check.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import sqlite3
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .a2 import a2_target as _a2_target
from .db import fts_upsert_cards, ledger_seen, recompute_aggregates
from .ingest import (CardRecord, IngestStats, attach_variant,
                     get_or_create_caselist, get_or_create_round,
                     get_or_create_school, get_or_create_team, insert_card,
                     ledger_stamp, normalize_side)
from .keys import sha256_bytes
from .rawstore import record_document
from .sanitize import sanitize_markup

logger = logging.getLogger("carddb.hf_loader")

HF_SOURCE = "hf"
BATCH_SIZE = 5000  # spec §3.3: batch inserts in transactions of ~5k rows

# Below this many visible characters, a tag/fulltext-less row is an empty
# heading rather than a card. Measured on the PF subset: 250 of 255 such rows
# fall under 40 characters, the other 5 are real evidence.
MARKUP_RECOVERY_MIN_CHARS = 40
_TAGS = re.compile(r"<[^>]*>")


def _visible_text(markup) -> str:
    """Plain text of a markup blob, for rows that carry nothing else."""
    if not markup:
        return ""
    import html as _html
    return _html.unescape(_TAGS.sub(" ", str(markup))).strip()

DATASETS_SERVER = "https://datasets-server.huggingface.co"
DEFAULT_DATASET = "Yusuf5/OpenCaselist"

# Known PF row-offset runs, used only as a /rows fallback when the
# datasets-server /filter index is down (it was for the entire verification
# window; hf_verify.md §6). Offsets verified 2026-08-27.
_PF_OFFSET_HINTS = (1820000, 2650000, 3350000, 4325000)

HF_BUCKETS_DDL = """
CREATE TABLE IF NOT EXISTS hf_buckets(card_id INTEGER, bucket_id TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hf_buckets ON hf_buckets(card_id, bucket_id);
"""


# --- fullcite mining (spec §3.4 date/url shapes) ---------------------------

_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")
_MDY_RE = re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b")
_MONTH_D_Y_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _extract_source_url(fullcite: Optional[str]) -> Optional[str]:
    if not fullcite:
        return None
    m = _URL_RE.search(fullcite)
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?'’”") or None


def _extract_pub_date(fullcite: Optional[str]) -> Optional[str]:
    """Date-shaped tokens near the front of the full cite (spec §3.4):
    ``M-D-YYYY`` and ``Month D, YYYY`` -> ISO ``YYYY-MM-DD``; bare ``YYYY``
    stays a year. URLs are cut off first so path digits never look like
    dates."""
    if not fullcite:
        return None
    front = fullcite.split("http", 1)[0][:200]

    m = _MDY_RE.search(front)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            pass  # e.g. 13-45-2019; fall through to the other shapes

    m = _MONTH_D_Y_RE.search(front)
    if m:
        mo = _MONTHS.get(m.group(1).lower())
        if mo:
            try:
                return date(int(m.group(3)), mo, int(m.group(2))).isoformat()
            except ValueError:
                pass

    m = _YEAR_RE.search(front)
    if m:
        return m.group(1)
    return None


# --- row mapping -----------------------------------------------------------

def _s(row: dict, key: str) -> Optional[str]:
    """Row field as a stripped non-empty string, else None."""
    v = row.get(key)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _doc_identity(row: dict) -> Tuple[str, str]:
    """(identity, sha256) of the synthetic document this row belongs to.
    Precedence: source-file path if the dataset references one, else the
    round, else the row itself. See module docstring."""
    file_path = _s(row, "filePath")
    opensource = _s(row, "opensourcePath")
    round_id = _s(row, "roundId")
    if file_path:
        identity = "hf:doc:filepath:" + file_path
    elif opensource:
        identity = "hf:doc:opensource:" + opensource
    elif round_id:
        identity = "hf:doc:round:" + round_id
    else:
        identity = "hf:doc:row:" + str(row.get("id"))
    return identity, sha256_bytes(identity.encode("utf-8"))


def map_hf_row(row: dict) -> Tuple[CardRecord, Dict[str, Any]]:
    """Map one dataset row to (CardRecord, metadata dict).

    Raises ValueError only when the row carries no readable content at all.

    A small number of rows (255 of the 43,131 PF rows) have null `tag` AND
    null `fulltext` while still carrying a populated `markup` field. Most are
    an empty heading a debater left in the document, but five held a real
    card — the parser that built the dataset put the text in `markup` alone.
    Rejecting on tag/fulltext therefore silently dropped genuine evidence, so
    the markup is used as the fallback source of body text."""
    fulltext = row.get("fulltext") or None
    tag = _s(row, "tag")
    if not fulltext and not tag:
        recovered = _visible_text(row.get("markup"))
        if len(recovered) >= MARKUP_RECOVERY_MIN_CHARS:
            fulltext = recovered
        else:
            raise ValueError(
                f"hf row {row.get('id')}: no tag, no fulltext, and markup "
                f"holds {len(recovered)} visible characters")
    is_analytic = not fulltext and bool(tag)  # spec §3.3

    spoken = row.get("spoken") or None
    summary = row.get("summary") or None
    fullcite = row.get("fullcite") or None
    markup = row.get("markup") or None

    highlight_ratio = None
    if fulltext and spoken and len(fulltext) > 0:
        highlight_ratio = len(spoken) / len(fulltext)

    row_id = row.get("id")
    rec = CardRecord(
        tag=tag,
        cite=_s(row, "cite"),
        fullcite=fullcite,
        body_text=None if is_analytic else fulltext,
        is_analytic=is_analytic,
        source_url=_extract_source_url(fullcite),
        source_pub_date=_extract_pub_date(fullcite),
        pocket=_s(row, "pocket"),
        hat=_s(row, "hat"),
        block=_s(row, "block"),
        markup_html=sanitize_markup(markup) if markup else None,
        summary=summary,
        spoken=spoken,
        highlight_ratio=highlight_ratio,
        fidelity="opensource",
        ordinal=_int(row_id),           # unique per row; see module docstring
        external_id=str(row_id) if row_id is not None else None,
    )

    slug = _s(row, "caselistName")
    school_id = _s(row, "schoolId")
    team_id = _s(row, "teamId")
    round_id = _s(row, "roundId")
    doc_identity, doc_sha = _doc_identity(row)

    meta: Dict[str, Any] = {
        "external_id": str(row_id) if row_id is not None else None,
        "bucket_id": _s(row, "bucketId"),
        "duplicate_count": _int(row.get("duplicateCount")),
        "event": _s(row, "event"),
        "level": _s(row, "level"),
        "year": _s(row, "year"),
        "caselist": None,
        "school": None,
        "team": None,
        "round": None,
        "doc_identity": doc_identity,
        "doc_sha": doc_sha,
        "file_path": _s(row, "filePath"),
        "opensource_path": _s(row, "opensourcePath"),
    }
    if slug:
        meta["caselist"] = {
            "slug": slug,
            "display_name": _s(row, "caselistDisplayName") or slug,
            "season": _int(row.get("year")),
            "event": _s(row, "event"),
            "level": _s(row, "level"),
        }
    if school_id:
        # No name columns exist in the parquet (hf_verify.md §2):
        # deterministic placeholder names, numeric id kept in external_id.
        meta["school"] = {"name": f"school-{school_id}", "external_id": school_id}
    if team_id:
        meta["team"] = {"name": f"team-{team_id}", "external_id": team_id}
    if round_id:
        meta["round"] = {
            "external_id": f"hf-{round_id}",     # namespaced; see docstring
            "side": normalize_side(row.get("side")),
            "round_label": _s(row, "round"),
            "report": row.get("report") or None,
            "round_date": None,                  # not present in the parquet
        }
    return rec, meta


# --- ingest ----------------------------------------------------------------

def _row_sha(row: dict) -> str:
    """sha256 of the row's canonical JSON, stored in the ledger so a
    re-shipped row with changed content gets reprocessed."""
    return sha256_bytes(
        json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    )


def ingest_hf_rows(conn: sqlite3.Connection, rows: Iterable[dict], cfg: dict,
                   stats: IngestStats, pf_only: bool = True) -> IngestStats:
    """Run dataset rows through the single ingest path (spec §3.3).

    Per row: ledger check (source='hf', external_id=row id) -> map ->
    get_or_create entities -> synthetic document -> insert_card +
    attach_variant -> hf_buckets -> ledger stamp. Commits every ~5k processed
    rows, flushing FTS + aggregates with every commit. Rows failing the
    event filter are not ledger-stamped (a later pf_only=False run can still
    ingest them) and are counted only in the log, not in stats.units_seen.
    """
    conn.executescript(HF_BUCKETS_DDL)

    caselists_seen: set = set()
    events_seen: set = set()
    filtered_out = 0
    since_commit = 0
    pending_ids: set = set()  # card ids awaiting an FTS/aggregate flush

    # get_or_create caches — the full load is millions of rows; skip the
    # per-row SELECTs for entities we've already resolved.
    cl_cache: Dict[str, int] = {}
    school_cache: Dict[Tuple[int, str], int] = {}
    team_cache: Dict[Tuple[int, str], int] = {}
    round_cache: Dict[str, int] = {}
    doc_cache: Dict[str, int] = {}

    for row in rows:
        slug = _s(row, "caselistName")
        event = _s(row, "event")
        if slug:
            caselists_seen.add(slug)
        if event:
            events_seen.add(event)

        if pf_only and (event or "").lower() != "pf":
            filtered_out += 1
            continue

        stats.units_seen += 1
        external_id = str(row.get("id"))
        sha = _row_sha(row)
        if ledger_seen(conn, HF_SOURCE, external_id, sha):
            stats.units_skipped += 1
            continue

        try:
            rec, meta = map_hf_row(row)
        except ValueError as e:
            logger.warning("unmappable hf row: %s", e)
            stats.failed += 1
            ledger_stamp(conn, HF_SOURCE, external_id, sha)
            continue

        # entities
        round_db_id: Optional[int] = None
        if meta["caselist"]:
            cl = meta["caselist"]
            cl_id = cl_cache.get(cl["slug"])
            if cl_id is None:
                cl_id = get_or_create_caselist(
                    conn, cl["slug"], display_name=cl["display_name"],
                    season=cl["season"], event=cl["event"], level=cl["level"])
                cl_cache[cl["slug"]] = cl_id
            if meta["school"] and meta["team"] and meta["round"]:
                sc = meta["school"]
                sc_key = (cl_id, sc["name"])
                sc_id = school_cache.get(sc_key)
                if sc_id is None:
                    sc_id = get_or_create_school(
                        conn, cl_id, sc["name"], external_id=sc["external_id"])
                    school_cache[sc_key] = sc_id
                tm = meta["team"]
                tm_key = (sc_id, tm["name"])
                tm_id = team_cache.get(tm_key)
                if tm_id is None:
                    tm_id = get_or_create_team(
                        conn, sc_id, tm["name"], external_id=tm["external_id"])
                    team_cache[tm_key] = tm_id
                rd = meta["round"]
                round_db_id = round_cache.get(rd["external_id"])
                if round_db_id is None:
                    round_db_id = get_or_create_round(
                        conn, tm_id, rd["external_id"], side=rd["side"],
                        round_label=rd["round_label"], report=rd["report"],
                        round_date=rd["round_date"])
                    round_cache[rd["external_id"]] = round_db_id

        # synthetic document (see module docstring)
        doc_id = doc_cache.get(meta["doc_sha"])
        if doc_id is None:
            doc_id = record_document(
                conn, meta["doc_sha"], origin="hf", origin_url=None,
                orig_filename=meta["file_path"] or meta["opensource_path"],
                local_path=None)
            doc_cache[meta["doc_sha"]] = doc_id

        card_id, created = insert_card(conn, rec)

        # A re-shipped row with changed content (ledger sha mismatch) must
        # fully reprocess: move its variant off the stale card, refresh the
        # markup, and drop the stale canonical if nothing points at it.
        existing = conn.execute(
            "SELECT id, card_id FROM card_variants WHERE document_id = ? AND ordinal = ?",
            (doc_id, rec.ordinal)).fetchone()
        if existing is not None and existing["card_id"] != card_id:
            old_id = existing["card_id"]
            conn.execute("DELETE FROM card_variants WHERE id = ?", (existing["id"],))
            left = conn.execute(
                "SELECT COUNT(*) FROM card_variants WHERE card_id = ?",
                (old_id,)).fetchone()[0]
            if left == 0:
                for tbl in ("hf_buckets", "cite_health", "card_box_members"):
                    try:
                        conn.execute(f"DELETE FROM {tbl} WHERE card_id = ?", (old_id,))
                    except sqlite3.OperationalError:
                        pass  # table not created in this DB yet
                conn.execute("DELETE FROM card_fts WHERE rowid = ?", (old_id,))
                conn.execute("DELETE FROM cards WHERE id = ?", (old_id,))
            else:
                pending_ids.add(old_id)
        elif existing is not None:
            # same canonical card, updated markup/metadata on the re-ship
            conn.execute(
                "UPDATE card_variants SET pocket=?, hat=?, block=?, a2_target=?, "
                " markup_html=?, summary=?, spoken=?, highlight_ratio=? WHERE id=?",
                (rec.pocket, rec.hat, rec.block, _a2_target(rec.block),
                 rec.markup_html, rec.summary, rec.spoken, rec.highlight_ratio,
                 existing["id"]))

        _, vcreated = attach_variant(conn, card_id, rec, doc_id, round_db_id)
        stats.new_cards += int(created)
        stats.new_variants += int(vcreated)
        stats.touched_card_ids.add(card_id)
        pending_ids.add(card_id)
        stats.parsed += 1

        if meta["bucket_id"]:
            conn.execute(
                "INSERT OR IGNORE INTO hf_buckets (card_id, bucket_id) VALUES (?, ?)",
                (card_id, meta["bucket_id"]))

        ledger_stamp(conn, HF_SOURCE, external_id, sha)

        since_commit += 1
        if since_commit >= BATCH_SIZE:
            # Flush FTS + aggregates INSIDE every batch so an interrupted
            # multi-hour load never leaves committed cards unfindable (the
            # ledger would skip them forever on rerun). `carddb reindex`
            # remains the belt-and-braces repair path.
            fts_upsert_cards(conn, pending_ids)
            recompute_aggregates(conn, pending_ids)
            conn.commit()
            pending_ids.clear()
            since_commit = 0
            logger.info("hf ingest progress: %s", stats.summary())

    fts_upsert_cards(conn, pending_ids)
    recompute_aggregates(conn, pending_ids)
    conn.commit()
    pending_ids.clear()
    # spec §11 M1: log the distinct caselist/event census.
    logger.info("hf ingest: distinct caselistName values seen: %s",
                sorted(caselists_seen))
    logger.info("hf ingest: distinct event values seen: %s", sorted(events_seen))
    if filtered_out:
        logger.info("hf ingest: %d non-%s rows filtered out", filtered_out,
                    "pf" if pf_only else "?")
    logger.info("hf ingest done: %s", stats.summary())
    return stats


def ingest_hf(conn: sqlite3.Connection, cfg: dict, stats: IngestStats,
              limit: Optional[int] = None, streaming: bool = True) -> IngestStats:
    """Stream the full dataset through ingest_hf_rows via the `datasets`
    library (optional dependency). ``limit`` caps rows read from the stream
    (pre-filter)."""
    try:
        import datasets  # type: ignore
    except ImportError:
        raise RuntimeError(
            "the `datasets` package is required for --source hf; install it with:\n"
            "    pip install datasets\n"
            "note: the full Yusuf5/OpenCaselist load downloads ~27.6 GB of "
            "parquet — make sure ~28GB of disk is free before starting."
        )
    name = (cfg.get("hf") or {}).get("dataset", DEFAULT_DATASET)
    ds = datasets.load_dataset(name, split="train", streaming=streaming)
    rows: Iterable[dict] = iter(ds)
    if limit is not None:
        rows = itertools.islice(rows, limit)
    return ingest_hf_rows(conn, rows, cfg, stats)


# --- dev/integration sample fetch (network; never used by unit tests) ------

def fetch_sample_rows(n: int = 200, event_filter: str = "pf") -> List[dict]:
    """Fetch up to n rows from the datasets-server HTTP API for dev use.

    Tries the /filter endpoint first; that index was down for the whole
    verification window (hf_verify.md §6), so on failure it falls back to
    plain /rows pages at the verified PF offset runs, filtering client-side.
    """
    import httpx

    headers = {"User-Agent": "pf-card-search (dev sample fetch)"}
    base_params = {"dataset": DEFAULT_DATASET, "config": "default", "split": "train"}
    out: List[dict] = []

    with httpx.Client(headers=headers, timeout=30.0) as client:
        # attempt 1: server-side filter
        try:
            offset = 0
            while len(out) < n:
                length = min(100, n - len(out))
                params = dict(base_params)
                params.update({
                    "where": "\"event\"='{0}'".format(event_filter.replace("'", "")),
                    "offset": offset, "length": length,
                })
                resp = client.get(DATASETS_SERVER + "/filter", params=params)
                resp.raise_for_status()
                page = resp.json().get("rows", [])
                out.extend(r["row"] for r in page)
                if len(page) < length:
                    break
                offset += length
            if out:
                return out[:n]
        except Exception as e:  # index loading / 500s — fall back to /rows
            logger.warning("datasets-server /filter unavailable (%s); "
                           "falling back to /rows offset probes", e)

        out = []
        for hint in _PF_OFFSET_HINTS:
            offset = hint
            while len(out) < n:
                length = min(100, n - len(out))
                params = dict(base_params)
                params.update({"offset": offset, "length": length})
                resp = client.get(DATASETS_SERVER + "/rows", params=params)
                resp.raise_for_status()
                page = resp.json().get("rows", [])
                matched = [r["row"] for r in page
                           if (r["row"].get("event") or "").lower() == event_filter.lower()]
                out.extend(matched)
                if len(page) < length or not matched:
                    break  # left the PF run (or end of data): next hint
                offset += length
            if len(out) >= n:
                break
    return out[:n]
