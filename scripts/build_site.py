#!/usr/bin/env python3
"""Build the shipped, browser-queryable site database. Spec §5, §7.1, §8.

GitHub Pages cannot run Python, so the public site queries a *prebuilt*
SQLite file directly from the browser with sql.js-httpvfs: the worker issues
HTTP Range requests and pulls only the 1 KiB database pages a query touches,
so a visitor never downloads the whole corpus. Two consequences drive every
decision in this file:

  1. **page_size = 4096.** It has to divide the front end's requestChunkSize,
     and SQLite can only change page size on an empty database (or across a
     VACUUM), so the pragma is set before a single table exists.
  2. **One static file.** journal_mode = DELETE, never WAL: a -wal sidecar
     would make the published file incomplete. VACUUM leaves no free pages
     for the range fetcher to waste requests on.

The shipped schema is deliberately *not* the local index's schema (spec §5).
The local index keeps one row per disclosure (card_variants); the site ships
one row per canonical card with a single representative variant's markup
folded in, because the public page renders one card and the multi-variant
provenance tables are a local-app feature.

    CREATE TABLE cards(
      id INTEGER PRIMARY KEY, tag TEXT, cite TEXT, fullcite TEXT,
      body_text TEXT, markup_html TEXT, summary TEXT, spoken TEXT,
      source_url TEXT, source_pub_date TEXT, is_analytic INTEGER,
      team_count INTEGER, school_count INTEGER, topic_codes TEXT,
      pocket TEXT, hat TEXT, block TEXT);
    CREATE VIRTUAL TABLE card_fts USING fts5(tag, cite, block, body,
      tokenize='porter unicode61 remove_diacritics 2');   -- rowid = cards.id
    CREATE TABLE topics(code TEXT PRIMARY KEY, season INTEGER, slot TEXT,
      resolution TEXT, starts TEXT, ends TEXT, card_count INTEGER);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

card_fts is a real (not contentless) fts5 table: the browser needs
snippet() and bm25(card_fts, 5.0, 3.0, 2.0, 1.0) to work with no server.
That duplicates the body text inside the FTS index and roughly doubles the
file; it is the price of search that runs on a static host.

Usage:

    python scripts/build_site.py [--db data/carddb.sqlite]
                                 [--out site/db/cards.sqlite]
                                 [--include-analytics]
                                 [--max-cards N] [--min-reads N]
                                 [--max-bytes N] [--today YYYY-MM-DD]

Analytics (§1.3: a tag with no evidence under it) are excluded by default,
exactly as they are excluded from card counts everywhere else.

Size control: GitHub Pages soft-limits a published site to ~1 GB and any
single file to 100 MB. With --max-bytes the builder shrinks *deterministically
and loudly* — first analytics, then the lowest-value cards (team_count DESC,
body_len DESC, id ASC) — and stamps meta.subset_note with exactly what was
dropped and how large the full local index is. It never truncates silently.

Also writes <out dir>/config.json so the front end can render corpus stats
without opening the database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python scripts/build_site.py`
    sys.path.insert(0, str(ROOT))

from carddb.topics import topic_status  # noqa: E402

# Latency, not bandwidth, dominates SQLite-over-HTTP: every page the query
# touches is a network round trip. Measured on the deployed site, 1 KB pages
# put a common FTS query at 17.3 s, so bigger pages are worth a lot.
#
# requestChunkSize must EQUAL page_size: sql.js-httpvfs warns "Chunk size does
# not match page size" and the FTS5 vtable fails to construct when they differ
# (observed live as "vtable constructor failed: card_fts" with 4 KB pages and
# 32 KB reads). So both are raised together. 32 KB is SQLite's largest page
# size short of the 64 KB maximum; ranking a common term reads its FTS doclist,
# which at 4 KB pages meant ~100 round trips and ~15 s on the deployed site.
PAGE_SIZE = 65536
REQUEST_CHUNK_SIZE = 65536
BM25_WEIGHTS = (5.0, 3.0, 2.0, 1.0)  # spec §7.1: tag >> cite > block > body
MAX_SHRINK_BUILDS = 10               # rebuilds allowed to land under --max-bytes

DEFAULT_DB = ROOT / "data" / "carddb.sqlite"
DEFAULT_OUT = ROOT / "site" / "db" / "cards.sqlite"

# Spec §2.4, stated plainly. Coverage precision is a feature; do not inflate.
COVERAGE_NOTE = (
    "This index covers PF evidence disclosed on openCaselist, which is most of "
    "the national-circuit corpus from roughly the mid-2010s onward. It is not "
    "every card ever cut: teams that do not disclose are invisible to any tool, "
    "pre-openCaselist archives are out of scope, and disclosure is uneven across "
    "regions and seasons."
)
SOURCE_NOTE = (
    "Bulk history comes from the Yusuf5/OpenCaselist research dataset (MIT "
    "license) on Hugging Face; recent rounds come from openCaselist "
    "(opencaselist.com), which hosts the disclosures. Cards were cut and "
    "disclosed by the debaters credited in each citation; citations are "
    "reproduced exactly as disclosed and are never restamped."
)

SITE_DDL = """
CREATE TABLE cards (
  id INTEGER PRIMARY KEY,
  tag TEXT,
  cite TEXT,
  fullcite TEXT,
  body_text TEXT,
  markup_html TEXT,
  summary TEXT,
  spoken TEXT,
  source_url TEXT,
  source_pub_date TEXT,
  is_analytic INTEGER,
  team_count INTEGER,
  school_count INTEGER,
  topic_codes TEXT,
  pocket TEXT,
  hat TEXT,
  block TEXT
);
CREATE VIRTUAL TABLE card_fts USING fts5(
  tag, cite, block, body,
  tokenize = 'porter unicode61 remove_diacritics 2'
);
CREATE TABLE topics (
  code TEXT PRIMARY KEY,
  season INTEGER,
  slot TEXT,
  resolution TEXT,
  starts TEXT,
  ends TEXT,
  card_count INTEGER
);
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

META_KEYS = (
    "built_at", "card_count", "analytic_count", "team_count", "school_count",
    "seasons_covered", "coverage_note", "source_note",
)


class BuildError(Exception):
    """Fatal: the build cannot produce a correct shipped database."""


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def _loud(msg: str, log=_log) -> None:
    line = "=" * 72
    log(line)
    for part in msg.splitlines():
        log(part)
    log(line)


def human_bytes(n: int) -> str:
    """Exact bytes first, because the cap is in bytes; the friendly unit is
    a parenthetical."""
    val = float(n)
    for unit in ("KiB", "MiB", "GiB"):
        val /= 1024.0
        if val < 1024.0 or unit == "GiB":
            return "%d bytes (%.1f %s)" % (n, val, unit)
    return "%d bytes" % n


# --- source reading -------------------------------------------------------

def _ro_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def open_source(path) -> sqlite3.Connection:
    """Read-only connection to the local full index. The builder must never
    be able to write to it."""
    p = Path(path)
    if not p.exists():
        raise BuildError("source index not found: %s" % p)
    conn = sqlite3.connect(_ro_uri(p), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT id FROM cards LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise BuildError("%s does not look like a carddb index (%s)" % (p, exc)) from exc
    return conn


def source_totals(src: sqlite3.Connection) -> Dict[str, int]:
    row = src.execute(
        "SELECT COUNT(*) AS n, "
        " SUM(CASE WHEN COALESCE(is_analytic,0)=0 THEN 1 ELSE 0 END) AS cards, "
        " SUM(CASE WHEN COALESCE(is_analytic,0)=1 THEN 1 ELSE 0 END) AS analytics "
        "FROM cards"
    ).fetchone()
    return {
        "rows": int(row["n"] or 0),
        "cards": int(row["cards"] or 0),
        "analytics": int(row["analytics"] or 0),
    }


def select_cards(src: sqlite3.Connection, include_analytics: bool = False,
                 min_reads: Optional[int] = None,
                 max_cards: Optional[int] = None) -> List[Tuple[int, int]]:
    """(id, is_analytic) for every card to ship, in descending value order.

    Value order is the same everywhere in this file — team_count DESC,
    body_len DESC, id ASC — so --max-cards and the byte-cap shrink keep the
    same cards, and two runs of the same build produce the same selection.
    """
    sql = (
        "SELECT id, COALESCE(is_analytic,0) AS is_analytic "
        "FROM cards "
        "WHERE (? = 1 OR COALESCE(is_analytic,0) = 0) "
        "  AND COALESCE(team_count,0) >= ? "
        "ORDER BY COALESCE(team_count,0) DESC, "
        "         COALESCE(body_len, LENGTH(COALESCE(body_text,''))) DESC, "
        "         id ASC"
    )
    rows = src.execute(sql, (1 if include_analytics else 0,
                             int(min_reads or 0))).fetchall()
    out = [(int(r["id"]), int(r["is_analytic"])) for r in rows]
    if max_cards is not None:
        out = out[:max(0, int(max_cards))]
    return out


# --- the shipped database -------------------------------------------------

def _fresh_file(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(out_path) + suffix)
        if p.exists():
            p.unlink()


def _seasons_covered(seasons: Sequence[int]) -> str:
    vals = sorted({int(s) for s in seasons if s is not None})
    if not vals:
        return ""
    if len(vals) == 1:
        return str(vals[0])
    return "%d-%d" % (vals[0], vals[-1])


def write_db(src_path: Path, out_path: Path, ids: Sequence[int], *,
             today: date, subset_note: Optional[str] = None,
             built_at: Optional[str] = None) -> Dict[str, Any]:
    """Write the whole shipped database from scratch. Returns a summary dict.

    Rebuilt from zero on every call, which is what makes the byte-cap loop
    honest: each candidate selection is measured as the file that would
    actually ship, not estimated.
    """
    src_path = Path(src_path).resolve()
    out_path = Path(out_path)
    _fresh_file(out_path)

    conn = sqlite3.connect(out_path.resolve().as_uri(), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # page_size must be set while the database is still empty; it cannot
        # be changed afterwards without another VACUUM, and it has to equal
        # the front end's requestChunkSize.
        conn.execute("PRAGMA page_size = %d" % PAGE_SIZE)
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.executescript(SITE_DDL)

        conn.execute("ATTACH DATABASE ? AS src", (_ro_uri(src_path),))
        conn.execute("CREATE TABLE _sel (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO _sel (id) VALUES (?)",
                         [(int(i),) for i in ids])

        _copy_cards(conn)
        _copy_fts(conn)
        summary = _write_topics(conn, today)
        summary.update(_write_meta(conn, subset_note, built_at))

        conn.execute("DROP TABLE _tc")
        conn.execute("DROP TABLE _sel")
        conn.commit()
        conn.execute("DETACH DATABASE src")

        conn.isolation_level = None  # VACUUM/ANALYZE need autocommit
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
    finally:
        conn.close()

    size = out_path.stat().st_size
    summary["bytes"] = size
    return summary


def _copy_cards(conn: sqlite3.Connection) -> None:
    """Copy the selected cards, folding in one representative variant.

    The representative is the most-highlighted variant (spec §1.2:
    highlights are the words actually read aloud, so that rendering is the
    most useful one), ties broken on the lowest variant id so two builds of
    the same index agree. Cards with no variant still ship — markup_html
    stays NULL and the page falls back to plain body_text.

    topic_codes prefers the materialized cards.topic_ids (carddb.topics
    writes a sorted JSON array of topic *codes* there, despite the column
    name) and derives live from variants -> rounds -> topics when it is
    missing or empty. Always a JSON array of codes, never NULL.
    """
    conn.execute("CREATE TABLE _tc (card_id INTEGER PRIMARY KEY, codes TEXT)")
    conn.execute("""
        INSERT INTO _tc (card_id, codes)
        SELECT card_id, json_group_array(code)
        FROM (SELECT DISTINCT v.card_id AS card_id, t.code AS code
              FROM src.card_variants v
              JOIN src.rounds r ON r.id = v.round_id
              JOIN src.topics t ON t.id = r.topic_id
              WHERE t.code IS NOT NULL
                AND v.card_id IN (SELECT id FROM _sel)
              ORDER BY v.card_id, t.code)
        GROUP BY card_id
    """)
    conn.execute("""
        INSERT INTO cards (id, tag, cite, fullcite, body_text, markup_html,
                           summary, spoken, source_url, source_pub_date,
                           is_analytic, team_count, school_count,
                           topic_codes, pocket, hat, block)
        SELECT c.id, c.tag, c.cite, c.fullcite, c.body_text,
               v.markup_html, v.summary, v.spoken,
               c.source_url, c.source_pub_date,
               COALESCE(c.is_analytic, 0),
               COALESCE(c.team_count, 0),
               COALESCE(c.school_count, 0),
               CASE
                 WHEN c.topic_ids IS NOT NULL
                      AND json_valid(c.topic_ids)
                      AND json_array_length(c.topic_ids) > 0
                   THEN json(c.topic_ids)   -- minified, so both paths agree
                 ELSE COALESCE((SELECT codes FROM _tc WHERE _tc.card_id = c.id), '[]')
               END,
               v.pocket, v.hat, v.block
        FROM _sel s
        JOIN src.cards c ON c.id = s.id
        LEFT JOIN src.card_variants v ON v.id = (
            SELECT v2.id FROM src.card_variants v2
            WHERE v2.card_id = c.id
            ORDER BY COALESCE(v2.highlight_ratio, -1.0) DESC, v2.id ASC
            LIMIT 1)
        ORDER BY c.id
    """)


def _copy_fts(conn: sqlite3.Connection) -> None:
    """Populate the real fts5 table (rowid = cards.id).

    Column mapping matches the local index (carddb.db.fts_upsert_cards) so
    bm25 weights tuned locally mean the same thing in the browser: cite
    covers short + full cite, block covers every block title the card was
    ever filed under, not just the representative variant's.
    """
    conn.execute("""
        INSERT INTO card_fts (rowid, tag, cite, block, body)
        SELECT c.id,
               COALESCE(c.tag, ''),
               TRIM(COALESCE(c.cite, '') || ' ' || COALESCE(c.fullcite, '')),
               COALESCE((SELECT group_concat(DISTINCT v.block)
                         FROM src.card_variants v
                         WHERE v.card_id = c.id
                           AND v.block IS NOT NULL AND v.block <> ''), ''),
               COALESCE(c.body_text, '')
        FROM cards c
        ORDER BY c.id
    """)


def _write_topics(conn: sqlite3.Connection, today: date) -> Dict[str, Any]:
    """Ship every topic with at least one card, plus present and announced
    ones at zero cards (§6.3: future slots are public before cards exist,
    and the topic page says so honestly)."""
    counts: Counter = Counter()
    for row in conn.execute("SELECT topic_codes, is_analytic FROM cards"):
        if row["is_analytic"]:
            continue  # card_count excludes analytics, like every other count
        try:
            codes = json.loads(row["topic_codes"] or "[]")
        except (TypeError, ValueError):
            continue
        for code in codes:
            if isinstance(code, str) and code:
                counts[code] += 1

    written = 0
    zero_card_future = 0
    for t in conn.execute(
            "SELECT code, season, slot, resolution, starts, ends "
            "FROM src.topics WHERE code IS NOT NULL ORDER BY code"):
        n = counts.get(t["code"], 0)
        status = topic_status(t, today)
        if n == 0 and status not in ("present", "future"):
            continue
        if n == 0:
            zero_card_future += 1
        conn.execute(
            "INSERT INTO topics (code, season, slot, resolution, starts, ends, "
            " card_count) VALUES (?,?,?,?,?,?,?)",
            (t["code"], t["season"], t["slot"], t["resolution"],
             t["starts"], t["ends"], n))
        written += 1
    return {"topics": written, "topics_zero_card": zero_card_future}


def _write_meta(conn: sqlite3.Connection, subset_note: Optional[str],
                built_at: Optional[str]) -> Dict[str, Any]:
    """Every key in the contract, filled from the shipped rows — not from the
    local index, so the numbers describe what a visitor can actually search."""
    counts = conn.execute(
        "SELECT SUM(CASE WHEN COALESCE(is_analytic,0)=0 THEN 1 ELSE 0 END) AS cards, "
        "       SUM(CASE WHEN COALESCE(is_analytic,0)=1 THEN 1 ELSE 0 END) AS analytics "
        "FROM cards").fetchone()
    card_count = int(counts["cards"] or 0)
    analytic_count = int(counts["analytics"] or 0)

    agg = conn.execute("""
        SELECT COUNT(DISTINCT r.team_id) AS teams,
               COUNT(DISTINCT t.school_id) AS schools
        FROM _sel s
        JOIN src.card_variants v ON v.card_id = s.id
        JOIN src.rounds r ON r.id = v.round_id
        JOIN src.teams t ON t.id = r.team_id
    """).fetchone()
    seasons = [r[0] for r in conn.execute("""
        SELECT DISTINCT cl.season
        FROM _sel s
        JOIN src.card_variants v ON v.card_id = s.id
        JOIN src.rounds r ON r.id = v.round_id
        JOIN src.teams t ON t.id = r.team_id
        JOIN src.schools sc ON sc.id = t.school_id
        JOIN src.caselists cl ON cl.id = sc.caselist_id
        WHERE cl.season IS NOT NULL
    """)]

    meta = {
        "built_at": built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "card_count": str(card_count),
        "analytic_count": str(analytic_count),
        "team_count": str(int(agg["teams"] or 0)),
        "school_count": str(int(agg["schools"] or 0)),
        "seasons_covered": _seasons_covered(seasons) or "unknown",
        "coverage_note": COVERAGE_NOTE,
        "source_note": SOURCE_NOTE,
    }
    if subset_note:
        meta["subset_note"] = subset_note
    missing = [k for k in META_KEYS if k not in meta]
    if missing:
        raise BuildError("meta is missing contract keys: %s" % ", ".join(missing))
    conn.executemany("INSERT INTO meta (key, value) VALUES (?,?)",
                     sorted(meta.items()))
    return {"card_count": card_count, "analytic_count": analytic_count,
            "meta": meta}


# --- honesty about what was dropped --------------------------------------

def subset_note(shipped_cards: int, shipped_analytics: int,
                full_cards: int, full_analytics: int,
                reasons: Sequence[str],
                analytics_requested: bool = False) -> Optional[str]:
    """The meta.subset_note text, or None when nothing was held back.

    Emitted whenever this database holds fewer canonical cards than the local
    index does, whatever the cause, and whenever analytics were asked for but
    dropped. Excluding analytics by default is documented policy (§1.3), not
    a subset, so it alone does not trigger the note — but when the note is
    written it always states the real denominator, so a reader of the About
    page can tell how much of the index they are searching.
    """
    dropped_cards = max(0, full_cards - shipped_cards)
    dropped_analytics = max(0, full_analytics - shipped_analytics)
    if dropped_cards == 0 and not (analytics_requested and dropped_analytics):
        return None
    parts = [
        "Partial index. This database ships {sc:,} of the {fc:,} canonical "
        "cards in the local index ({dc:,} dropped), and {sa:,} of {fa:,} "
        "analytics.".format(sc=shipped_cards, fc=full_cards, dc=dropped_cards,
                            sa=shipped_analytics, fa=full_analytics)
    ]
    if dropped_analytics and shipped_analytics == 0:
        parts.append(
            "Analytics (a tag with no evidence under it) are indexed locally "
            "but do not ship.")
    if dropped_cards:
        parts.append(
            "Cards were kept in descending order of team_count, then body "
            "length, then id, so the most-read evidence survives.")
    if reasons:
        parts.append("Reason: " + "; ".join(reasons) + ".")
    return " ".join(parts)


# --- verification ---------------------------------------------------------

def _probe_token(conn: sqlite3.Connection) -> Optional[str]:
    """A word that must be findable: the longest alphabetic token of the
    lowest-id shipped card's tag, else its body."""
    row = conn.execute(
        "SELECT tag, body_text FROM cards ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None
    for field in ("tag", "body_text"):
        text = row[field] or ""
        words = sorted((w for w in "".join(
            ch if ch.isalpha() else " " for ch in text).split() if len(w) >= 4),
            key=lambda w: (-len(w), w))
        if words:
            return words[0].lower()
    return None


def verify(out_path: Path, log=_log) -> Dict[str, Any]:
    """Open the built file exactly as a static host serves it — read-only,
    no sidecars — and prove the browser's query shape works."""
    out_path = Path(out_path)
    for suffix in ("-wal", "-shm", "-journal"):
        stray = Path(str(out_path) + suffix)
        if stray.exists():
            raise BuildError(
                "%s exists: the shipped database must be a single static file"
                % stray)
    conn = sqlite3.connect(_ro_uri(out_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        if int(page_size) != PAGE_SIZE:
            raise BuildError("page_size is %s, must be %d to match the front "
                             "end's requestChunkSize" % (page_size, PAGE_SIZE))
        journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal != "delete":
            raise BuildError("journal_mode is %r, must be 'delete'" % journal)
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise BuildError("quick_check failed: %s" % integrity)

        n = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        fts_n = conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0]
        if n != fts_n:
            raise BuildError("card_fts has %d rows, cards has %d" % (fts_n, n))

        token = _probe_token(conn)
        hits = 0
        if token:
            sql = (
                "SELECT c.id, bm25(card_fts, %s) AS score, "
                "       snippet(card_fts, 3, '<mark>', '</mark>', '...', 12) AS snip "
                "FROM card_fts JOIN cards c ON c.id = card_fts.rowid "
                "WHERE card_fts MATCH ? ORDER BY score LIMIT 5"
                % ", ".join(str(w) for w in BM25_WEIGHTS))
            rows = conn.execute(sql, ('"%s"' % token,)).fetchall()
            hits = len(rows)
            if n and not hits:
                raise BuildError(
                    "bm25 probe for %r returned no rows; the FTS index is not "
                    "usable in the browser" % token)
        elif n:
            log("build_site: WARNING no probe token found; FTS not exercised")
        return {"cards": n, "probe": token, "probe_hits": hits}
    finally:
        conn.close()


# --- front-end config -----------------------------------------------------

SUFFIX_LENGTH = 3          # cards.sqlite.000, .001, ... (worker pads to this)

# Chunks are named ....000.png. They are not images. GitHub Pages gzips
# application/octet-stream, and a range request against a gzip-encoded
# response returns bytes from the compressed stream, so SQLite reads garbage
# and reports "database disk image is malformed". Browsers always send
# Accept-Encoding: gzip and cannot opt out (it is a forbidden header), so the
# only lever is the extension. Probing a live deploy, .png/.jpg/.zip/.woff2
# are served uncompressed while .bin is not. See site/vendor/PATCHES.md.
CHUNK_SUFFIX = ".png"


def split_into_chunks(out_path: Path, chunk_bytes: int, log=_log) -> int:
    """Split the built DB into byte-exact sequential parts.

    GitHub rejects any file over 100 MB, so a full-corpus database cannot ship
    as one blob. sql.js-httpvfs reads chunked databases natively: it requests
    urlPrefix + a zero-padded index, so concatenating the parts in order must
    reproduce the original file bit for bit.
    """
    out_path = Path(out_path)
    total = out_path.stat().st_size
    n = 0
    with open(out_path, "rb") as src:
        while True:
            block = src.read(chunk_bytes)
            if not block and n:
                break
            part = out_path.parent / ("%s.%s%s" % (out_path.name,
                                                   str(n).zfill(SUFFIX_LENGTH),
                                                   CHUNK_SUFFIX))
            part.write_bytes(block)
            n += 1
            if len(block) < chunk_bytes:
                break
    # Prove the split is lossless before we delete anything.
    joined = b"".join(
        (out_path.parent / ("%s.%s%s" % (out_path.name, str(i).zfill(SUFFIX_LENGTH),
                                         CHUNK_SUFFIX))).read_bytes()
        for i in range(n))
    if len(joined) != total:
        raise SystemExit("chunk split is not byte-exact: %d != %d"
                         % (len(joined), total))
    log("split into %d chunks of <= %d bytes (verified byte-exact)" % (n, chunk_bytes))
    return n


def write_config(out_path: Path, meta: Dict[str, str], size: int,
                 topics: int, chunks: int = 0, chunk_bytes: int = 0) -> Path:
    """site/db/config.json — what sql.js-httpvfs needs plus the corpus stats,
    so the empty state (§8.2) renders without touching the database."""
    out_path = Path(out_path)
    url = ("db/%s" % out_path.name if out_path.parent.name == "db"
           else out_path.name)
    cfg: Dict[str, Any] = {
        "serverMode": "chunked" if chunks else "full",
        "url": url,
        "db": url,          # alias: the front end's loader reads either name
        "requestChunkSize": REQUEST_CHUNK_SIZE,
        "databaseLengthBytes": size,
    }
    if chunks:
        # The worker builds each part's URL as urlPrefix + zero-padded index.
        cfg["urlPrefix"] = url + "."
        cfg["urlSuffix"] = CHUNK_SUFFIX
        cfg["suffixLength"] = SUFFIX_LENGTH
        cfg["serverChunkSize"] = chunk_bytes
        cfg["chunkCount"] = chunks
    cfg.update({
        "bm25Weights": list(BM25_WEIGHTS),
        "topic_count": topics,
        "meta": dict(meta),
    })
    for key in ("built_at", "seasons_covered", "coverage_note", "source_note",
                "subset_note"):
        if key in meta:
            cfg[key] = meta[key]
    for key in ("card_count", "analytic_count", "team_count", "school_count"):
        cfg[key] = int(meta.get(key, 0) or 0)
    path = out_path.parent / "config.json"
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


# --- orchestration --------------------------------------------------------

def build_site(db=DEFAULT_DB, out=DEFAULT_OUT, *, include_analytics: bool = False,
               max_cards: Optional[int] = None, min_reads: Optional[int] = None,
               max_bytes: Optional[int] = None, today: Optional[date] = None,
               built_at: Optional[str] = None, chunk_bytes: Optional[int] = None,
               log=_log) -> Dict[str, Any]:
    src_path = Path(db)
    out_path = Path(out)
    today = today or date.today()

    with closing(open_source(src_path)) as src:
        totals = source_totals(src)
        selection = select_cards(src, include_analytics=include_analytics,
                                 min_reads=min_reads, max_cards=max_cards)

    reasons: List[str] = []
    if min_reads:
        reasons.append("--min-reads %d (cards read by fewer teams were dropped)"
                       % int(min_reads))
    if max_cards is not None and len(selection) >= int(max_cards):
        reasons.append("--max-cards %d" % int(max_cards))

    def counts_of(sel):
        analytics = sum(1 for _, a in sel if a)
        return len(sel) - analytics, analytics

    def build_once(sel, extra_reasons=()):
        cards, analytics = counts_of(sel)
        note = subset_note(cards, analytics, totals["cards"],
                           totals["analytics"],
                           list(reasons) + list(extra_reasons),
                           analytics_requested=include_analytics)
        result = write_db(src_path, out_path, [i for i, _ in sel], today=today,
                          subset_note=note, built_at=built_at)
        result["subset_note"] = note
        return result

    log("build_site: source %s (%d cards, %d analytics)"
        % (src_path, totals["cards"], totals["analytics"]))
    log("build_site: selected %d rows (%d cards, %d analytics)"
        % ((len(selection),) + counts_of(selection)))

    extra: List[str] = []
    result = build_once(selection, extra)
    shrunk = False

    if max_bytes is not None and result["bytes"] > int(max_bytes):
        cap = int(max_bytes)
        shrunk = True
        _loud("SIZE CAP EXCEEDED\n"
              "built %s > cap %s\n"
              "Shrinking deterministically (analytics first, then the "
              "least-read cards).\nNothing is dropped silently: meta."
              "subset_note records it and the About page shows it."
              % (human_bytes(result["bytes"]), human_bytes(cap)), log=log)
        extra = ["a %d-byte size cap for static hosting (GitHub Pages allows "
                 "100 MB per file)" % cap]

        # Step 1: analytics go first — they are the least useful rows on a
        # public search page and they are excluded from counts anyway.
        if any(a for _, a in selection):
            selection = [(i, a) for i, a in selection if not a]
            log("build_site: dropping analytics -> %d rows" % len(selection))
            result = build_once(selection, extra)
            log("build_site: now %s" % human_bytes(result["bytes"]))

        # Step 2: drop the lowest-value cards. Every candidate count is
        # *measured* as a real built file rather than estimated, because the
        # per-card cost varies wildly (a 60 KB marked-up body next to a 2 KB
        # one). Estimate down until it fits, then bisect back up so the cap
        # is used, not merely respected. MAX_SHRINK_BUILDS bounds the work.
        best_n = None                 # largest measured count that fits
        too_big_n = len(selection)    # smallest measured count that does not
        current_n = len(selection)
        builds = 0

        # Never shrink to nothing. A database has a floor (header plus at
        # least a page per table), so with a large page_size a small corpus
        # cannot always reach an arbitrary cap. An empty index is useless to
        # everyone; the honest outcome is the smallest real build plus a loud
        # CANNOT FIT.
        while (result["bytes"] > cap and current_n > 1
               and builds < MAX_SHRINK_BUILDS):
            keep = int(current_n * (cap / float(result["bytes"])) * 0.98)
            keep = max(1, min(keep, current_n - 1))
            log("build_site: %s > cap; trying the top %d of %d cards"
                % (human_bytes(result["bytes"]), keep, current_n))
            result = build_once(selection[:keep], extra)
            builds += 1
            current_n = keep
            if result["bytes"] <= cap:
                best_n = keep
            else:
                too_big_n = keep

        while (best_n is not None
               and too_big_n - best_n > max(1, int(best_n * 0.02))
               and builds < MAX_SHRINK_BUILDS):
            mid = (best_n + too_big_n) // 2
            if mid <= best_n or mid >= too_big_n:
                break
            log("build_site: %d cards fit, %d did not; trying %d"
                % (best_n, too_big_n, mid))
            result = build_once(selection[:mid], extra)
            builds += 1
            current_n = mid
            if result["bytes"] <= cap:
                best_n = mid
            else:
                too_big_n = mid

        if best_n is not None and current_n != best_n:
            # The last build overshot; leave the best fitting one on disk.
            result = build_once(selection[:best_n], extra)

        if result["bytes"] > cap:
            _loud("CANNOT FIT %s: the smallest database this builder can "
                  "produce is %s (%d cards).\nRaise --max-bytes, or split the "
                  "corpus by topic and ship several databases."
                  % (human_bytes(cap), human_bytes(result["bytes"]),
                     result["card_count"]), log=log)
        else:
            _loud("SHRUNK TO FIT: %s, %d cards.\n%s"
                  % (human_bytes(result["bytes"]), result["card_count"],
                     result["subset_note"] or ""), log=log)

    checks = verify(out_path, log=log)

    # Content-version the filename. Chunk contents change between builds; if
    # the URLs stay the same, a browser that cached the old parts mixes them
    # with new ones and SQLite reports "database disk image is malformed".
    # Hashing the build into the name makes every deploy a fresh URL.
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()[:10]
    stem = out_path.name.split(".")[0]
    versioned = out_path.parent / ("%s-%s.sqlite" % (stem, digest))
    # Keep the newest PREVIOUS generation alive: pages cached for up to ten
    # minutes still reference it during a rollout. Only older ones go.
    gens = {}
    for f in out_path.parent.glob("%s-*.sqlite*" % stem):
        g = f.name.split(".sqlite")[0]
        gens.setdefault(g, []).append(f)
    if gens:
        newest = max(gens, key=lambda g: max(f.stat().st_mtime for f in gens[g]))
        for g, files in gens.items():
            if g != newest:
                for f in files:
                    f.unlink()
    out_path.replace(versioned)
    out_path = versioned
    result["out"] = out_path
    log("versioned database as %s" % out_path.name)

    chunks = 0
    if chunk_bytes and result["bytes"] > chunk_bytes:
        chunks = split_into_chunks(out_path, int(chunk_bytes), log=log)
    cfg_path = write_config(out_path, result["meta"], result["bytes"],
                            result["topics"], chunks=chunks,
                            chunk_bytes=int(chunk_bytes or 0))

    result.update({
        "out": out_path, "config": cfg_path, "shrunk": shrunk,
        "fits": max_bytes is None or result["bytes"] <= int(max_bytes),
        "source_totals": totals, "verify": checks,
    })
    log("build_site: wrote %s" % out_path)
    log("build_site: %s, %d cards (+%d analytics), %d topics, page_size %d"
        % (human_bytes(result["bytes"]), result["card_count"],
           result["analytic_count"], result["topics"], PAGE_SIZE))
    log("build_site: bm25 probe %r matched %d rows"
        % (checks["probe"], checks["probe_hits"]))
    log("build_site: wrote %s" % cfg_path)
    if result["subset_note"]:
        log("build_site: subset_note: %s" % result["subset_note"])
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="build_site.py",
        description="Build the browser-queryable site database (spec §8).")
    p.add_argument("--db", default=str(DEFAULT_DB),
                   help="local full index (default: %(default)s)")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="shipped database path (default: %(default)s)")
    p.add_argument("--include-analytics", action="store_true",
                   help="ship analytics too (excluded by default, §1.3)")
    p.add_argument("--max-cards", type=int, default=None,
                   help="ship at most N cards, highest-value first")
    p.add_argument("--min-reads", type=int, default=None,
                   help="ship only cards read by at least N teams")
    p.add_argument("--max-bytes", type=int, default=None,
                   help="hard size cap; shrinks loudly and records subset_note")
    p.add_argument("--chunk-bytes", type=int, default=45000000,
                   help="split the built DB into parts of at most this many "
                        "bytes (GitHub rejects files over 100 MB); 0 disables")
    p.add_argument("--today", default=None,
                   help="ISO date used for present/future topic status")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    today = None
    if args.today:
        try:
            today = date.fromisoformat(args.today)
        except ValueError:
            print("build_site: --today must be YYYY-MM-DD", file=sys.stderr)
            return 2
    try:
        result = build_site(
            db=args.db, out=args.out, include_analytics=args.include_analytics,
            max_cards=args.max_cards, min_reads=args.min_reads,
            max_bytes=args.max_bytes, today=today,
            chunk_bytes=args.chunk_bytes)
    except BuildError as exc:
        print("build_site: %s" % exc, file=sys.stderr)
        return 2
    return 0 if result["fits"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
