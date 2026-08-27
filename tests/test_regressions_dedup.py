"""Regression tests for confirmed dedup/ingest/topics findings.

D1  _merge vs the UNIQUE idx_hf_buckets(card_id, bucket_id) index: when the
    survivor and the absorbed card share a bucket_id (the routine agreement
    case) the merge must not die with sqlite3.IntegrityError.
D2  _merge vs PRAGMA foreign_keys=ON: cite_health and card_box_members rows
    referencing the absorbed card must move to the survivor before the
    absorbed cards row is deleted.
D3  insert_card must consult card_merges so re-ingesting input whose
    canonical was absorbed by dedup resolves to the survivor instead of
    resurrecting a zero-variant orphan; dedup path-compresses card_merges
    so the lookup is always one hop.
D4  run_dedup must refresh the survivors' materialized cards.topic_ids so
    topics inherited from absorbed variants appear immediately, not after
    the next `carddb topics assign`.

Fixture prose and the real-ingest-path helper are shared with
tests/test_dedup.py.
"""
import json
from datetime import date

import pytest

from carddb.db import open_db
from carddb.dedup import run_dedup
from carddb.ingest import CardRecord, insert_card
from carddb.keys import canonical_key
from carddb.topics import assign_topics, load_topics, materialize_topic_ids

from test_dedup import WORDS_A, WORDS_B, _add_card, _count, _drift, _trim_of


@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    yield conn
    conn.close()


def _make_hf_table(conn):
    """hf_buckets exactly as hf_loader creates it, UNIQUE index included."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS hf_buckets(card_id INTEGER, bucket_id TEXT);"
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hf_buckets "
        "  ON hf_buckets(card_id, bucket_id);"
    )


def _codes(conn, card_id):
    row = conn.execute(
        "SELECT topic_ids FROM cards WHERE id = ?", (card_id,)).fetchone()
    return json.loads(row["topic_ids"])


# --- D1: survivor and absorbed share a bucket_id ---------------------------

def test_merge_survives_shared_bucket_under_unique_index(db, tmp_path):
    c1 = _add_card(db, "Alpha", "AA", "r-a1", "sha-1",
                   " ".join(WORDS_A), "Kessler '26")
    c2 = _add_card(db, "Beta", "BB", "r-b1", "sha-2",
                   _drift(WORDS_A), "Kessler '26")
    _make_hf_table(db)
    # Routine agreement: the HF signal put both cards in the SAME bucket.
    db.executemany("INSERT INTO hf_buckets (card_id, bucket_id) VALUES (?,?)",
                   [(c1, "bkt"), (c2, "bkt")])
    db.commit()

    stats = run_dedup(db, tmp_path / "reports")  # pre-fix: IntegrityError
    assert stats.merged == 1
    assert _count(db, "cards") == 1
    survivor = db.execute("SELECT id FROM cards").fetchone()["id"]
    rows = db.execute(
        "SELECT card_id, bucket_id FROM hf_buckets").fetchall()
    assert [(r["card_id"], r["bucket_id"]) for r in rows] == [(survivor, "bkt")]


# --- D2: FK rows on the absorbed card (foreign_keys=ON) --------------------

def test_merge_moves_cite_health_and_box_membership(db, tmp_path):
    # Trim pair: the LONGER card deterministically survives.
    c_long = _add_card(db, "Alpha", "AA", "r-a1", "sha-l",
                       " ".join(WORDS_A), "Diamond '13")
    c_short = _add_card(db, "Beta", "BB", "r-b1", "sha-s",
                        _trim_of(WORDS_A), "Diamond '13")
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.execute("INSERT INTO card_boxes (id, name, created_at) "
               "VALUES (1, 'aff-box', 'now')")
    db.execute("INSERT INTO card_box_members (box_id, card_id, note, added_at) "
               "VALUES (1, ?, 'keep this', 'now')", (c_short,))
    db.execute("INSERT INTO cite_health (card_id, status, http_status) "
               "VALUES (?, 'dead', 404)", (c_short,))
    db.commit()

    stats = run_dedup(db, tmp_path / "reports")  # pre-fix: FK IntegrityError
    assert stats.merged == 1
    assert _count(db, "cards") == 1
    members = db.execute(
        "SELECT box_id, card_id, note FROM card_box_members").fetchall()
    assert [(m["box_id"], m["card_id"], m["note"]) for m in members] == \
        [(1, c_long, "keep this")]
    health = db.execute("SELECT card_id, status FROM cite_health").fetchall()
    assert [(h["card_id"], h["status"]) for h in health] == [(c_long, "dead")]


def test_merge_keeps_survivor_cite_health_and_dedups_membership(db, tmp_path):
    c_long = _add_card(db, "Alpha", "AA", "r-a1", "sha-l",
                       " ".join(WORDS_A), "Diamond '13")
    c_short = _add_card(db, "Beta", "BB", "r-b1", "sha-s",
                        _trim_of(WORDS_A), "Diamond '13")
    db.execute("INSERT INTO card_boxes (id, name, created_at) "
               "VALUES (1, 'aff-box', 'now')")
    # BOTH cards sit in the same box, and both have a cite_health row.
    db.executemany(
        "INSERT INTO card_box_members (box_id, card_id, note, added_at) "
        "VALUES (1, ?, ?, 'now')",
        [(c_long, "survivor note"), (c_short, "absorbed note")])
    db.executemany(
        "INSERT INTO cite_health (card_id, status) VALUES (?, ?)",
        [(c_long, "alive"), (c_short, "dead")])
    db.commit()

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 1
    # one membership on the survivor; the survivor's own row won
    members = db.execute(
        "SELECT box_id, card_id, note FROM card_box_members").fetchall()
    assert [(m["box_id"], m["card_id"], m["note"]) for m in members] == \
        [(1, c_long, "survivor note")]
    health = db.execute("SELECT card_id, status FROM cite_health").fetchall()
    assert [(h["card_id"], h["status"]) for h in health] == [(c_long, "alive")]


# --- D3: re-ingest after dedup must not resurrect absorbed canonicals ------

def test_reingest_after_dedup_adds_nothing_and_resolves_to_survivor(db, tmp_path):
    body1, body2 = " ".join(WORDS_A), _drift(WORDS_A)

    def ingest_all():
        a = _add_card(db, "Alpha", "AA", "r-a1", "sha-1", body1, "Kessler '26")
        b = _add_card(db, "Beta", "BB", "r-b1", "sha-2", body2, "Kessler '26")
        return a, b

    ingest_all()
    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 1
    survivor = db.execute("SELECT id FROM cards").fetchone()["id"]
    snapshot = (_count(db, "cards"), _count(db, "card_variants"))

    # Identical re-ingest through the real path: 0 new cards, 0 new variants,
    # and both disclosures resolve to the surviving canonical.
    r1, r2 = ingest_all()
    assert (_count(db, "cards"), _count(db, "card_variants")) == snapshot
    assert r1 == survivor and r2 == survivor

    # The absorbed key itself resolves to the survivor, not a new row,
    # and the missing-field backfill lands on the survivor.
    absorbed_key = db.execute(
        "SELECT absorbed_key FROM card_merges").fetchone()["absorbed_key"]
    by_key = {canonical_key(b, "Fixture tag", False): b for b in (body1, body2)}
    absorbed_body = by_key[absorbed_key]
    rec = CardRecord(tag="Fixture tag", body_text=absorbed_body,
                     source_url="https://backfill.test/x")
    card_id, created = insert_card(db, rec)
    assert (card_id, created) == (survivor, False)
    assert _count(db, "cards") == 1
    row = db.execute("SELECT source_url FROM cards WHERE id = ?",
                     (survivor,)).fetchone()
    assert row["source_url"] == "https://backfill.test/x"


def test_chain_merges_are_path_compressed_to_live_survivor(db, tmp_path):
    # Three nested trims of the same paragraph: within one pass card 1 is
    # absorbed into card 2, then card 2 into card 3. Every card_merges row
    # must end up pointing at the live survivor (one-hop resolution).
    b1 = " ".join(WORDS_A[4:])   # shortest
    b2 = " ".join(WORDS_A[2:])
    b3 = " ".join(WORDS_A)       # longest -> final survivor
    _add_card(db, "Alpha", "AA", "r-a1", "sha-1", b1, "Kessler '26")
    _add_card(db, "Beta", "BB", "r-b1", "sha-2", b2, "Kessler '26")
    c3 = _add_card(db, "Gamma", "CC", "r-c1", "sha-3", b3, "Kessler '26")

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 2
    assert _count(db, "cards") == 1
    survivor = db.execute("SELECT id FROM cards").fetchone()["id"]
    assert survivor == c3
    assert {r["survivor_id"] for r in
            db.execute("SELECT survivor_id FROM card_merges")} == {c3}

    # The key absorbed in the FIRST merge (whose original survivor was
    # itself absorbed later) still resolves to a live card.
    card_id, created = insert_card(
        db, CardRecord(tag="Fixture tag", body_text=b1))
    assert (card_id, created) == (c3, False)
    assert _count(db, "cards") == 1


# --- D4: survivors' topic_ids refresh immediately after run_dedup ----------

TOPICS_FIXTURE = {
    "_notes": "synthetic regression fixture",
    "topics": [
        {"code": "2025-SO", "season": 2025, "slot": "SO",
         "resolution": "Resolved: Test question about zebra corridors.",
         "starts": "2025-09-01", "ends": "2025-10-31", "source_url": "http://t"},
        {"code": "2025-ND", "season": 2025, "slot": "ND",
         "resolution": "Resolved: Test question about kumquat tariffs.",
         "starts": "2025-11-01", "ends": "2025-12-31", "source_url": "http://t"},
    ],
    "overrides": [],
}


def _load_fixture_topics(conn, tmp_path):
    p = tmp_path / "topics.json"
    p.write_text(json.dumps(TOPICS_FIXTURE), encoding="utf-8")
    load_topics(conn, p)


def test_survivor_topic_ids_include_absorbed_topics_after_dedup(db, tmp_path):
    _load_fixture_topics(db, tmp_path)
    # Near-dup pair read in rounds that land on two DIFFERENT topics.
    c1 = _add_card(db, "Alpha", "AA", "r-a1", "sha-1",
                   " ".join(WORDS_A), "Kessler '26")
    c2 = _add_card(db, "Beta", "BB", "r-b1", "sha-2",
                   _drift(WORDS_A), "Kessler '26")
    db.execute("UPDATE rounds SET round_date = '2025-09-15' "
               "WHERE external_id = 'r-a1'")
    db.execute("UPDATE rounds SET round_date = '2025-11-20' "
               "WHERE external_id = 'r-b1'")
    db.commit()
    assign_topics(db, today=date(2025, 12, 1))
    assert _codes(db, c1) == ["2025-SO"]
    assert _codes(db, c2) == ["2025-ND"]

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 1
    survivor = db.execute("SELECT id FROM cards").fetchone()["id"]
    # both codes present immediately, without another `carddb topics assign`
    assert _codes(db, survivor) == ["2025-ND", "2025-SO"]


def test_materialize_subset_touches_only_given_cards(db, tmp_path):
    _load_fixture_topics(db, tmp_path)
    c1 = _add_card(db, "Alpha", "AA", "r-a1", "sha-1",
                   " ".join(WORDS_A), "Kessler '26")
    c2 = _add_card(db, "Beta", "BB", "r-b1", "sha-2",
                   " ".join(WORDS_B), "Diamond '13")
    db.execute("UPDATE rounds SET round_date = '2025-09-15' "
               "WHERE external_id = 'r-a1'")
    db.execute("UPDATE rounds SET round_date = '2025-11-20' "
               "WHERE external_id = 'r-b1'")
    db.commit()
    assign_topics(db, today=date(2025, 12, 1))
    # wipe both materializations, then rebuild only c1
    db.execute("UPDATE cards SET topic_ids = '[]'")
    assert materialize_topic_ids(db, [c1]) == 1
    assert _codes(db, c1) == ["2025-SO"]
    assert _codes(db, c2) == []          # untouched by the subset rebuild


def test_materialize_subset_noop_when_topics_empty(db):
    c1 = _add_card(db, "Alpha", "AA", "r-a1", "sha-1",
                   " ".join(WORDS_A), "Kessler '26")
    db.execute("UPDATE cards SET topic_ids = '[\"stale\"]' WHERE id = ?", (c1,))
    assert materialize_topic_ids(db, [c1]) == 0
    assert materialize_topic_ids(db, []) == 0
    # topics table is empty: the targeted call must leave the card alone
    assert _codes(db, c1) == ["stale"]
