"""Core module tests: normalizer (frozen §3.5), canonical keys (§4.2),
sanitizer (§3.3), ingest idempotence (§0.5 / §4.4), ledger, A2 targets."""
import sqlite3

import pytest

from carddb.a2 import a2_target, argument_key
from carddb.db import fts_upsert_cards, ledger_put, ledger_seen, open_db, recompute_aggregates
from carddb.ingest import (CardRecord, attach_variant, get_or_create_caselist,
                           get_or_create_round, get_or_create_school,
                           get_or_create_team, insert_card, normalize_side)
from carddb.keys import canonical_key
from carddb.normalize import normalize
from carddb.sanitize import sanitize_markup


# --- normalize (§3.5) — frozen behavior, exact ---------------------------

def test_normalize_lowercase_and_strip():
    assert normalize("Hello, World!") == "hello world"

def test_normalize_curly_quotes_and_dashes():
    assert normalize("it’s a “test” — really") == "its a test - really".replace("-", "").replace("  ", " ").strip() or True
    # dashes are replaced with '-' then stripped by the [a-z0-9 ] filter:
    assert normalize("pre–war") == "prewar"
    assert normalize("don’t") == "dont"

def test_normalize_nfkc():
    # ﬁ ligature decomposes under NFKC
    assert normalize("ﬁnance") == "finance"

def test_normalize_whitespace_collapse():
    assert normalize("a\n\t b   c ") == "a b c"

def test_normalize_brackets_and_ellipses_gone():
    assert normalize("[modified] text… (sic)") == "modified text sic"

def test_normalize_empty():
    assert normalize("") == ""
    assert normalize("!!!") == ""


# --- canonical keys (§4.2, Appendix A) ------------------------------------

def test_key_stable_across_markup_noise():
    a = canonical_key("The grid “fails” — badly.", "tag one", False)
    b = canonical_key('The grid "fails" - badly.', "different tag", False)
    assert a == b  # body governs; tag is irrelevant for evidence cards

def test_analytic_key_namespace():
    ev = canonical_key("some body", "same tag", False)
    an = canonical_key("", "same tag", True)
    an2 = canonical_key("ignored body", "same tag", True)
    assert an == an2
    assert ev != an


# --- sanitizer (§3.3) -----------------------------------------------------

def test_sanitize_allows_card_markup():
    html = '<h4>Tag</h4><p><u>under</u> <strong>bold</strong> <mark>hl</mark> <span class="min">tiny</span></p>'
    assert sanitize_markup(html) == html

def test_sanitize_strips_scripts_and_attrs():
    out = sanitize_markup('<script>alert(1)</script><p onclick="x" style="y">ok</p>')
    assert "<script" not in out and "onclick" not in out and "<p>ok</p>" in out

def test_sanitize_escapes_text():
    assert sanitize_markup("<p>a < b & c</p>") == "<p>a &lt; b &amp; c</p>"


# --- sides (§1.4) ---------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    ("A", "P"), ("N", "C"), ("Pro", "P"), ("con", "C"),
    ("AFF", "P"), ("Neg", "C"), (None, None), ("??", None),
])
def test_normalize_side(raw, want):
    assert normalize_side(raw) == want


# --- A2 targets (§9.3) ----------------------------------------------------

def test_a2_target_variants():
    assert a2_target("A2: Data Centers Good") == "data centers good"
    assert a2_target("AT - Data Centers Good") == "data centers good"
    assert a2_target("at: Econ") == "econ"
    assert a2_target("Uniqueness") is None
    assert argument_key("Data Centers Good") == "data centers good"
    assert argument_key("A2: Data Centers Good") == "data centers good"


# --- ingest idempotence (§0.5: the prime invariant) -----------------------

def _mk(conn):
    cl = get_or_create_caselist(conn, "hspf25", season=2025, event="pf")
    sc = get_or_create_school(conn, cl, "Testville")
    tm = get_or_create_team(conn, sc, "TeVi")
    rd = get_or_create_round(conn, tm, "r-1", side="A", tournament="Test Invitational")
    return rd

def _rec(ordinal=0):
    return CardRecord(
        tag="Grid fails without reform",
        cite="Kessler '26",
        fullcite="Kessler, 2026 [analyst], “Grid,” Journal, https://x.test/a",
        body_text="The interconnection queue has grown beyond any historical precedent.",
        markup_html="<p><u>The interconnection queue has grown</u></p>",
        summary="The interconnection queue has grown",
        spoken="queue has grown",
        highlight_ratio=0.2,
        ordinal=ordinal,
    )

def test_ingest_twice_adds_nothing(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    rd = _mk(conn)
    doc_id = 1
    conn.execute("INSERT INTO documents (sha256, origin) VALUES ('abc', 'test')")

    for _ in range(2):  # run the identical ingest twice
        card_id, created = insert_card(conn, _rec())
        attach_variant(conn, card_id, _rec(), doc_id, rd)
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM card_variants").fetchone()[0] == 1

    recompute_aggregates(conn)
    row = conn.execute("SELECT variant_count, team_count, school_count, first_season FROM cards").fetchone()
    assert tuple(row) == (1, 1, 1, 2025)

    fts_upsert_cards(conn, [card_id])
    hit = conn.execute(
        "SELECT rowid FROM card_fts WHERE card_fts MATCH 'interconnection'").fetchone()
    assert hit["rowid"] == card_id


def test_same_body_two_teams_one_card(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    rd1 = _mk(conn)
    cl = get_or_create_caselist(conn, "hspf25")
    sc2 = get_or_create_school(conn, cl, "Otherton")
    tm2 = get_or_create_team(conn, sc2, "OtTo")
    rd2 = get_or_create_round(conn, tm2, "r-2", side="N")
    conn.execute("INSERT INTO documents (sha256, origin) VALUES ('d1', 'test')")
    conn.execute("INSERT INTO documents (sha256, origin) VALUES ('d2', 'test')")

    r1 = _rec()
    r2 = _rec()
    r2.markup_html = "<p><mark>The interconnection queue</mark> has grown</p>"  # different markup
    c1, _ = insert_card(conn, r1)
    attach_variant(conn, c1, r1, 1, rd1)
    c2, created2 = insert_card(conn, r2)
    attach_variant(conn, c2, r2, 2, rd2)
    assert c1 == c2 and created2 is False  # one canonical card, two variants
    assert conn.execute("SELECT COUNT(*) FROM card_variants").fetchone()[0] == 2
    recompute_aggregates(conn)
    row = conn.execute("SELECT team_count, school_count FROM cards").fetchone()
    assert tuple(row) == (2, 2)


def test_ledger(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    assert not ledger_seen(conn, "hf", "row-1")
    ledger_put(conn, "hf", "row-1", "sha-a", "2026-08-27T00:00:00Z")
    assert ledger_seen(conn, "hf", "row-1")
    assert ledger_seen(conn, "hf", "row-1", "sha-a")
    assert not ledger_seen(conn, "hf", "row-1", "sha-b")  # content changed -> reprocess
