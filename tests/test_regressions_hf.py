"""Regression tests for review findings [14] and [17]: interrupted bulk loads
must never leave committed cards without FTS rows, and a re-shipped HF row
with changed content must fully reprocess (no orphan canonicals)."""
import json
from pathlib import Path

import pytest

import carddb.hf_loader as hf
from carddb.config import load_config
from carddb.db import open_db
from carddb.ingest import IngestStats

FIXTURE = Path(__file__).parent / "fixtures" / "hf_sample.json"


def _rows(n=10):
    if not FIXTURE.exists():
        pytest.skip("hf_sample.json fixture missing")
    rows = json.load(open(FIXTURE))["rows"]
    ev = [r for r in rows if r.get("fulltext")][:n]
    if len(ev) < n:
        pytest.skip("not enough evidence rows in fixture")
    return ev


class _Boom(Exception):
    pass


def _interrupting(rows, after):
    for i, r in enumerate(rows):
        if i == after:
            raise _Boom()
        yield r


def test_interrupted_load_keeps_fts_consistent(tmp_path, monkeypatch):
    """Crash mid-load: every committed card must already have its FTS row,
    because the ledger will skip those rows forever on the rerun."""
    monkeypatch.setattr(hf, "BATCH_SIZE", 3)
    cfg = load_config()
    conn = open_db(tmp_path / "t.sqlite")
    rows = _rows(10)

    with pytest.raises(_Boom):
        hf.ingest_hf_rows(conn, _interrupting(rows, 8), cfg, IngestStats())
    conn.rollback()  # drop the uncommitted tail, as a killed process would

    n_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    n_fts = conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0]
    assert n_cards > 0, "batches before the crash should have committed"
    assert n_fts == n_cards, "committed cards must be searchable"

    # rerun to completion: everything findable, no duplicates
    hf.ingest_hf_rows(conn, rows, cfg, IngestStats())
    n_cards2 = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    n_fts2 = conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0]
    assert n_fts2 == n_cards2
    stats = IngestStats()
    hf.ingest_hf_rows(conn, rows, cfg, stats)  # idempotence still holds
    assert stats.new_cards == 0 and stats.new_variants == 0


def test_changed_row_reprocesses_fully(tmp_path):
    """Re-shipped row with edited fulltext: the variant moves to the new
    canonical, and the stale zero-variant canonical is removed."""
    cfg = load_config()
    conn = open_db(tmp_path / "t.sqlite")
    row = dict(_rows(1)[0])

    hf.ingest_hf_rows(conn, [row], cfg, IngestStats())
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1

    changed = dict(row)
    changed["fulltext"] = (row["fulltext"] or "") + " Editors later appended this correction sentence."
    hf.ingest_hf_rows(conn, [changed], cfg, IngestStats())

    cards = conn.execute("SELECT id, body_text FROM cards").fetchall()
    assert len(cards) == 1, "stale canonical must not survive as an orphan"
    assert "correction sentence" in cards[0]["body_text"]
    variants = conn.execute("SELECT card_id FROM card_variants").fetchall()
    assert len(variants) == 1 and variants[0]["card_id"] == cards[0]["id"]
    fts = conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0]
    assert fts == 1
