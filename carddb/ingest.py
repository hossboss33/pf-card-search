"""The single normalize/dedup/insert path. Spec §3, §4.1–4.2.

Every source (HF bulk rows, API-synced .docx files, private backfiles)
flows through insert_card() + attach_variant(). Idempotence layer 2 lives
here: cards.canonical_key is UNIQUE and inserts are ON CONFLICT DO NOTHING;
variants are UNIQUE(document_id, ordinal). Running any ingest twice adds
exactly zero canonical cards and zero variants.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional, Set

from .a2 import a2_target
from .keys import canonical_key
from .rawstore import now_iso


@dataclass
class CardRecord:
    """One parsed card occurrence (canonical fields + this disclosure's markup)."""
    tag: Optional[str] = None
    cite: Optional[str] = None
    fullcite: Optional[str] = None
    body_text: Optional[str] = None
    is_analytic: bool = False
    source_url: Optional[str] = None
    source_pub_date: Optional[str] = None
    # variant fields
    pocket: Optional[str] = None
    hat: Optional[str] = None
    block: Optional[str] = None
    markup_html: Optional[str] = None
    summary: Optional[str] = None
    spoken: Optional[str] = None
    highlight_ratio: Optional[float] = None
    fidelity: str = "opensource"
    ordinal: Optional[int] = None
    external_id: Optional[str] = None
    extras: dict = field(default_factory=dict)

    def key(self) -> str:
        return canonical_key(self.body_text or "", self.tag or "", self.is_analytic)


def insert_card(conn: sqlite3.Connection, rec: CardRecord) -> "tuple[int, bool]":
    """Insert the canonical card if new. Returns (card_id, created).

    On conflict the existing row wins entirely; we only backfill fields the
    existing row is missing (a later disclosure may carry a source_url the
    first one lacked)."""
    key = rec.key()
    cur = conn.execute(
        "INSERT INTO cards (canonical_key, tag, cite, fullcite, body_text, body_len, "
        " source_url, source_pub_date, is_analytic) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(canonical_key) DO NOTHING",
        (key, rec.tag, rec.cite, rec.fullcite, rec.body_text,
         len(rec.body_text or ""), rec.source_url, rec.source_pub_date,
         1 if rec.is_analytic else 0),
    )
    created = cur.rowcount == 1
    row = conn.execute("SELECT id FROM cards WHERE canonical_key = ?", (key,)).fetchone()
    card_id = row["id"]
    if not created:
        conn.execute(
            "UPDATE cards SET "
            " source_url = COALESCE(source_url, ?), "
            " source_pub_date = COALESCE(source_pub_date, ?), "
            " fullcite = COALESCE(fullcite, ?) "
            "WHERE id = ?",
            (rec.source_url, rec.source_pub_date, rec.fullcite, card_id),
        )
    return card_id, created


def attach_variant(conn: sqlite3.Connection, card_id: int, rec: CardRecord,
                   document_id: Optional[int], round_id: Optional[int]) -> "tuple[Optional[int], bool]":
    """Attach one disclosure. UNIQUE(document_id, ordinal) makes re-runs no-ops.
    Returns (variant_id or None, created)."""
    cur = conn.execute(
        "INSERT INTO card_variants (card_id, document_id, round_id, ordinal, "
        " pocket, hat, block, a2_target, markup_html, summary, spoken, "
        " highlight_ratio, fidelity, external_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(document_id, ordinal) DO NOTHING",
        (card_id, document_id, round_id, rec.ordinal, rec.pocket, rec.hat,
         rec.block, a2_target(rec.block), rec.markup_html, rec.summary,
         rec.spoken, rec.highlight_ratio, rec.fidelity, rec.external_id),
    )
    created = cur.rowcount == 1
    if not created:
        return None, False
    row = conn.execute(
        "SELECT id FROM card_variants WHERE document_id = ? AND ordinal = ?",
        (document_id, rec.ordinal),
    ).fetchone()
    return (row["id"] if row else None), created


# --- Entity get-or-create helpers (natural keys, idempotent) --------------

def get_or_create_caselist(conn, slug, display_name=None, season=None,
                           event=None, level=None) -> int:
    conn.execute(
        "INSERT INTO caselists (slug, display_name, season, event, level) "
        "VALUES (?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
        (slug, display_name or slug, season, event, level),
    )
    return conn.execute("SELECT id FROM caselists WHERE slug = ?", (slug,)).fetchone()["id"]


def get_or_create_school(conn, caselist_id, name, display_name=None,
                         state=None, external_id=None) -> int:
    row = conn.execute(
        "SELECT id FROM schools WHERE caselist_id = ? AND name = ?",
        (caselist_id, name),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO schools (caselist_id, name, display_name, state, external_id) "
        "VALUES (?,?,?,?,?)",
        (caselist_id, name, display_name or name, state, external_id),
    )
    return cur.lastrowid


def get_or_create_team(conn, school_id, name, display_name=None,
                       notes=None, external_id=None) -> int:
    row = conn.execute(
        "SELECT id FROM teams WHERE school_id = ? AND name = ?",
        (school_id, name),
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO teams (school_id, name, display_name, notes, external_id) "
        "VALUES (?,?,?,?,?)",
        (school_id, name, display_name or name, notes, external_id),
    )
    return cur.lastrowid


def normalize_side(raw) -> Optional[str]:
    """PF sides are Pro/Con; sources may encode Policy-convention A/N.
    Normalize to 'P'/'C' at ingest (spec §1.4)."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if s in ("P", "PRO", "A", "AFF", "AFFIRMATIVE", "1"):
        return "P"
    if s in ("C", "CON", "N", "NEG", "NEGATIVE", "2"):
        return "C"
    return None


def get_or_create_round(conn, team_id, external_id, side=None, tournament=None,
                        round_label=None, opponent=None, judge=None,
                        report=None, round_date=None) -> int:
    if external_id is not None:
        row = conn.execute("SELECT id FROM rounds WHERE external_id = ?",
                           (str(external_id),)).fetchone()
        if row:
            return row["id"]
    cur = conn.execute(
        "INSERT INTO rounds (team_id, side, tournament, round_label, opponent, "
        " judge, report, round_date, external_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (team_id, normalize_side(side), tournament, round_label, opponent,
         judge, report, round_date,
         str(external_id) if external_id is not None else None),
    )
    return cur.lastrowid


@dataclass
class IngestStats:
    units_seen: int = 0
    units_skipped: int = 0
    parsed: int = 0
    failed: int = 0
    new_cards: int = 0
    new_variants: int = 0
    touched_card_ids: Set[int] = field(default_factory=set)

    def summary(self) -> str:
        return (f"units={self.units_seen} skipped={self.units_skipped} "
                f"parsed={self.parsed} failed={self.failed} "
                f"new_canonicals={self.new_cards} new_variants={self.new_variants}")


def finish_batch(conn: sqlite3.Connection, stats: IngestStats) -> None:
    """Post-batch bookkeeping: FTS rows + derived aggregates for touched cards."""
    from .db import fts_upsert_cards, recompute_aggregates
    ids = list(stats.touched_card_ids)
    fts_upsert_cards(conn, ids)
    recompute_aggregates(conn, ids)
    conn.commit()


def ledger_stamp(conn, source: str, external_id: str, sha256: Optional[str]) -> None:
    from .db import ledger_put
    ledger_put(conn, source, str(external_id), sha256, now_iso())
