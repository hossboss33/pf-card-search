"""SQLite schema and connection handling. Spec §5.

One file, WAL mode, FTS5. The schema below is the spec's §5 DDL plus the
support tables for features 3/5/10 (A2 targets ride on card_variants;
cite health and card boxes get their own tables). All DDL is idempotent
(IF NOT EXISTS) so init can run on every startup.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS caselists (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT,
  season INTEGER,            -- 2026 means the 2026-27 season
  event TEXT, level TEXT     -- keep only PF rows, but store the fields
);
CREATE TABLE IF NOT EXISTS schools (
  id INTEGER PRIMARY KEY, caselist_id INTEGER REFERENCES caselists(id),
  name TEXT, display_name TEXT, state TEXT, external_id TEXT
);
CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY, school_id INTEGER REFERENCES schools(id),
  name TEXT, display_name TEXT, notes TEXT, external_id TEXT
);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY, season INTEGER, slot TEXT,   -- 'SO','ND','JAN','FEB','MA','NATS'
  code TEXT UNIQUE,             -- e.g. '2026-SO'
  resolution TEXT, starts TEXT, ends TEXT, source_url TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY, team_id INTEGER REFERENCES teams(id),
  side TEXT CHECK (side IN ('P','C')),
  tournament TEXT, round_label TEXT, opponent TEXT, judge TEXT,
  report TEXT, round_date TEXT,            -- ISO date when known
  topic_id INTEGER REFERENCES topics(id),  -- assigned by §6
  external_id TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, origin TEXT, origin_url TEXT,
  orig_filename TEXT, local_path TEXT, fetched_at TEXT,
  parsed_at TEXT, parse_status TEXT, parse_error TEXT
);
CREATE TABLE IF NOT EXISTS cards (            -- one row per canonical card
  id INTEGER PRIMARY KEY,
  canonical_key TEXT UNIQUE NOT NULL,
  tag TEXT, cite TEXT, fullcite TEXT,
  body_text TEXT, body_len INTEGER,
  source_url TEXT, source_pub_date TEXT,
  is_analytic INTEGER DEFAULT 0,
  first_season INTEGER, variant_count INTEGER DEFAULT 0,
  team_count INTEGER DEFAULT 0,
  school_count INTEGER DEFAULT 0,
  topic_ids TEXT                -- materialized JSON array, rebuilt by `carddb topics assign`
);
CREATE TABLE IF NOT EXISTS card_variants (    -- one row per disclosure of that card
  id INTEGER PRIMARY KEY,
  card_id INTEGER REFERENCES cards(id),
  document_id INTEGER REFERENCES documents(id),
  round_id INTEGER REFERENCES rounds(id),
  ordinal INTEGER,              -- position within the document
  pocket TEXT, hat TEXT, block TEXT,
  a2_target TEXT,               -- normalized "answers to" target when block is A2:/AT: (feature 9.3)
  markup_html TEXT, summary TEXT, spoken TEXT,
  highlight_ratio REAL, fidelity TEXT DEFAULT 'opensource',
  external_id TEXT,
  UNIQUE (document_id, ordinal)
);
CREATE TABLE IF NOT EXISTS card_merges (
  survivor_id INTEGER, absorbed_key TEXT, relation TEXT, merged_at TEXT
);
CREATE TABLE IF NOT EXISTS ingest_ledger (
  source TEXT, external_id TEXT, sha256 TEXT, ingested_at TEXT,
  PRIMARY KEY (source, external_id)
);
CREATE TABLE IF NOT EXISTS sync_checkpoints (   -- §2.2: resume, never re-request
  caselist TEXT, school TEXT, team TEXT, state TEXT, updated_at TEXT,
  PRIMARY KEY (caselist, school, team)
);
CREATE TABLE IF NOT EXISTS cite_health (        -- feature 9.5, latest check per card
  card_id INTEGER PRIMARY KEY REFERENCES cards(id),
  status TEXT,                 -- 'alive' | 'redirected' | 'paywalled' | 'dead' | 'no_url'
  http_status INTEGER, final_url TEXT, wayback_url TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS card_boxes (         -- feature 9.10, local single-user
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, created_at TEXT
);
CREATE TABLE IF NOT EXISTS card_box_members (
  box_id INTEGER REFERENCES card_boxes(id),
  card_id INTEGER REFERENCES cards(id),
  note TEXT, added_at TEXT,
  PRIMARY KEY (box_id, card_id)
);
CREATE TABLE IF NOT EXISTS saved_searches (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, query TEXT, created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_variants_card ON card_variants(card_id);
CREATE INDEX IF NOT EXISTS idx_variants_round ON card_variants(round_id);
CREATE INDEX IF NOT EXISTS idx_variants_a2 ON card_variants(a2_target) WHERE a2_target IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rounds_topic ON rounds(topic_id);
CREATE INDEX IF NOT EXISTS idx_rounds_team ON rounds(team_id);
CREATE INDEX IF NOT EXISTS idx_schools_caselist ON schools(caselist_id);
CREATE INDEX IF NOT EXISTS idx_teams_school ON teams(school_id);
"""

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS card_fts USING fts5(
  tag, cite, block, body,
  tokenize = 'porter unicode61 remove_diacritics 2'
);
"""
# card_fts rows use rowid = cards.id; kept in sync by fts_upsert_cards().


def connect(path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.executescript(FTS_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def open_db(path) -> sqlite3.Connection:
    conn = connect(path)
    init_db(conn)
    return conn


# --- FTS sync -------------------------------------------------------------

def fts_upsert_cards(conn: sqlite3.Connection, card_ids: Iterable[int]) -> None:
    """Rebuild the FTS row for each card id (rowid = cards.id).

    block column = distinct block titles across the card's variants, so
    block search covers every context the card was filed under.
    """
    for cid in card_ids:
        row = conn.execute(
            "SELECT id, tag, cite, fullcite, body_text FROM cards WHERE id = ?", (cid,)
        ).fetchone()
        if row is None:
            conn.execute("DELETE FROM card_fts WHERE rowid = ?", (cid,))
            continue
        blocks = conn.execute(
            "SELECT DISTINCT block FROM card_variants WHERE card_id = ? AND block IS NOT NULL",
            (cid,),
        ).fetchall()
        block_text = " ; ".join(b["block"] for b in blocks if b["block"])
        cite_text = " ".join(x for x in (row["cite"], row["fullcite"]) if x)
        conn.execute("DELETE FROM card_fts WHERE rowid = ?", (cid,))
        conn.execute(
            "INSERT INTO card_fts (rowid, tag, cite, block, body) VALUES (?,?,?,?,?)",
            (cid, row["tag"] or "", cite_text, block_text, row["body_text"] or ""),
        )


def fts_rebuild(conn: sqlite3.Connection) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM cards")]
    conn.execute("DELETE FROM card_fts")
    fts_upsert_cards(conn, ids)
    conn.commit()
    return len(ids)


# --- Aggregates -----------------------------------------------------------

def recompute_aggregates(conn: sqlite3.Connection,
                         card_ids: Optional[Iterable[int]] = None) -> None:
    """Rebuild variant_count / school_count / first_season on cards.

    Full recompute per batch keeps ingest idempotent by construction:
    the numbers are always derived, never incremented.
    """
    where, params = "", ()
    if card_ids is not None:
        ids = list(card_ids)
        if not ids:
            return
        where = f" WHERE c.id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    conn.execute(f"""
        UPDATE cards SET
          variant_count = (SELECT COUNT(*) FROM card_variants v WHERE v.card_id = cards.id),
          team_count = (
            SELECT COUNT(DISTINCT r.team_id)
            FROM card_variants v
            JOIN rounds r ON r.id = v.round_id
            WHERE v.card_id = cards.id
          ),
          school_count = (
            SELECT COUNT(DISTINCT t.school_id)
            FROM card_variants v
            JOIN rounds r ON r.id = v.round_id
            JOIN teams t ON t.id = r.team_id
            WHERE v.card_id = cards.id
          ),
          first_season = (
            SELECT MIN(cl.season)
            FROM card_variants v
            JOIN rounds r ON r.id = v.round_id
            JOIN teams t ON t.id = r.team_id
            JOIN schools s ON s.id = t.school_id
            JOIN caselists cl ON cl.id = s.caselist_id
            WHERE v.card_id = cards.id
          )
        WHERE cards.id IN (SELECT c.id FROM cards c{where})
    """, params)
    conn.commit()


# --- Ingest ledger (idempotence layer 1) ----------------------------------

def ledger_seen(conn: sqlite3.Connection, source: str, external_id: str,
                sha256: Optional[str] = None) -> bool:
    row = conn.execute(
        "SELECT sha256 FROM ingest_ledger WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    if row is None:
        return False
    if sha256 is not None and row["sha256"] != sha256:
        return False  # same unit, new content — reprocess
    return True


def ledger_put(conn: sqlite3.Connection, source: str, external_id: str,
               sha256: Optional[str], ingested_at: str) -> None:
    conn.execute(
        "INSERT INTO ingest_ledger (source, external_id, sha256, ingested_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(source, external_id) DO UPDATE SET sha256=excluded.sha256, "
        "ingested_at=excluded.ingested_at",
        (source, external_id, sha256, ingested_at),
    )
