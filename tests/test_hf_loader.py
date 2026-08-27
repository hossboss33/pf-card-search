"""HF loader tests: mapping (spec §2.1/§3.3), the M1 idempotence invariant
(spec §11: rerun the loader -> 0 new canonical cards, 0 new variants),
analytic mapping, side normalization, bucketId capture. No network."""
import json
from pathlib import Path

import pytest

from carddb.db import open_db
from carddb.hf_loader import ingest_hf, ingest_hf_rows, map_hf_row
from carddb.ingest import IngestStats
from carddb.keys import canonical_key

FIXTURE = Path(__file__).parent / "fixtures" / "hf_sample.json"


def _rows():
    if not FIXTURE.exists():
        pytest.skip("tests/fixtures/hf_sample.json missing")
    return json.loads(FIXTURE.read_text())["rows"]


def _by_id(rows, rid):
    return next(r for r in rows if r["id"] == rid)


def _row(**over):
    """Minimal synthetic dataset row (all 31 fields present, PF-shaped)."""
    base = {
        "id": 999001, "tag": "Test tag", "cite": "Smith '20",
        "fullcite": "Smith (). 2-13-2019. \"Title.\" Pub. https://ex.test/a",
        "summary": "under text", "spoken": "hl text",
        "fulltext": "The full body text of the card.",
        "textLength": "31",
        "markup": "<h4><strong>Test tag</strong></h4><p><u>under</u> body</p>",
        "pocket": None, "hat": "Hat", "block": "Block",
        "bucketId": "b-1", "duplicateCount": "2",
        "filePath": None, "roundId": "500", "side": "A", "round": "1",
        "report": "we won", "opensourcePath": None, "caselistUpdatedAt": None,
        "teamId": "42", "schoolId": "7", "chapterId": None,
        "caselistId": "1032", "caselistName": "hspf19",
        "caselistDisplayName": "HS PF 2019-20", "year": "2019",
        "event": "pf", "level": "hs", "teamSize": "2",
    }
    base.update(over)
    return base


# --- mapping: evidence rows ------------------------------------------------

def test_map_evidence_row_from_fixture():
    row = _by_id(_rows(), 2993357)
    rec, meta = map_hf_row(row)
    assert rec.body_text == row["fulltext"]
    assert rec.tag == row["tag"]
    assert rec.is_analytic is False
    assert rec.fullcite == row["fullcite"]
    assert rec.summary == row["summary"]
    assert rec.external_id == "2993357"
    assert rec.ordinal == 2993357
    # fullcite mining: "2-13-2019 ... https://worldview.stratfor.com/..."
    assert rec.source_pub_date == "2019-02-13"
    assert rec.source_url.startswith("https://worldview.stratfor.com/")
    # metadata
    assert meta["external_id"] == "2993357"
    assert meta["caselist"]["slug"] == "hspf19"
    assert meta["caselist"]["season"] == 2019
    assert meta["caselist"]["event"] == "pf"
    assert meta["round"]["side"] == "C"          # dataset 'N' -> Con
    assert meta["round"]["external_id"] == "hf-905652"
    assert meta["school"]["external_id"] == "25577"
    assert meta["team"]["external_id"] == "76269"
    assert meta["bucket_id"] == "1289169"
    assert meta["duplicate_count"] == 4


def test_map_highlight_ratio_from_fixture():
    row = _by_id(_rows(), 2993371)  # has both spoken and fulltext
    rec, _ = map_hf_row(row)
    assert rec.spoken == row["spoken"]
    assert rec.highlight_ratio == pytest.approx(len(row["spoken"]) / len(row["fulltext"]))


def test_map_no_spoken_no_ratio():
    rec, _ = map_hf_row(_row(spoken=None))
    assert rec.spoken is None
    assert rec.highlight_ratio is None


# --- mapping: analytics (null fulltext + non-null tag, spec §3.3) ----------

def test_map_analytic_from_fixture():
    row = _by_id(_rows(), 2993366)
    assert row["fulltext"] is None and row["tag"]
    rec, _ = map_hf_row(row)
    assert rec.is_analytic is True
    assert not rec.body_text
    assert rec.key() == canonical_key("", row["tag"], True)
    assert rec.highlight_ratio is None


def test_map_analytic_count_matches_fixture():
    rows = _rows()
    expect = sum(1 for r in rows if not r["fulltext"] and r["tag"])
    got = sum(1 for r in rows if map_hf_row(r)[0].is_analytic)
    assert got == expect == 94


def test_map_raises_when_neither_fulltext_nor_tag():
    with pytest.raises(ValueError):
        map_hf_row(_row(fulltext=None, tag=None))


# --- side normalization ----------------------------------------------------

def test_side_normalized_on_every_fixture_row():
    for r in _rows():
        _, meta = map_hf_row(r)
        want = {"A": "P", "N": "C"}[r["side"]]
        assert meta["round"]["side"] == want


# --- markup sanitization ---------------------------------------------------

def test_markup_is_sanitized():
    rec, _ = map_hf_row(_row(
        markup='<h4 style="x"><script>bad()</script>Tag</h4><p onclick="y">body</p>'))
    assert "<script" not in rec.markup_html
    assert "onclick" not in rec.markup_html and "style" not in rec.markup_html
    assert "Tag" in rec.markup_html and "body" in rec.markup_html


def test_markup_tolerates_out_of_order_close_tags():
    # the dataset routinely closes tags out of order (hf_verify.md §7)
    rec, _ = map_hf_row(_row(markup="<u><strong><mark>Pomeroy</u></strong></mark>"))
    assert rec.markup_html == "<u><strong><mark>Pomeroy</mark></strong></u>"


def test_null_markup_maps_to_none():
    rec, _ = map_hf_row(_row(markup=None))
    assert rec.markup_html is None


# --- source_pub_date / source_url mining -----------------------------------

@pytest.mark.parametrize("fullcite,want", [
    ("Smith (). 2-13-2019. \"T.\" Pub.", "2019-02-13"),
    ("Bosworth (). 11-25-2019. \"2019 has been hard - 2020 worse.\"", "2019-11-25"),
    ("Jones, June 4, 2020, \"T,\" Pub", "2020-06-04"),
    ("Lee, Sept. 3, 2021. \"T.\"", "2021-09-03"),
    ("Riegg, Ryan. \"T?\" Newsweek. March 2017.", "2017"),      # no day -> year
    ("Smith 13-45-2019 nonsense", "2019"),                      # invalid M-D -> year
    ("No dates here at all", None),
    ("See https://example.com/2019/05/x for more", None),       # URL digits ignored
])
def test_source_pub_date_formats(fullcite, want):
    rec, _ = map_hf_row(_row(fullcite=fullcite))
    assert rec.source_pub_date == want


def test_source_url_extraction_and_trailing_punctuation():
    rec, _ = map_hf_row(_row(
        fullcite='X 1-2-2020. "T." Pub. (https://ex.test/a-b_c). More'))
    assert rec.source_url == "https://ex.test/a-b_c"
    rec2, _ = map_hf_row(_row(fullcite="no url in sight"))
    assert rec2.source_url is None


# --- ingest: the M1 idempotence invariant (spec §0.5, §11) -----------------

def _counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "cards": q("SELECT COUNT(*) FROM cards"),
        "variants": q("SELECT COUNT(*) FROM card_variants"),
        "analytics": q("SELECT COUNT(*) FROM cards WHERE is_analytic = 1"),
        "rounds": q("SELECT COUNT(*) FROM rounds"),
        "teams": q("SELECT COUNT(*) FROM teams"),
        "schools": q("SELECT COUNT(*) FROM schools"),
        "documents": q("SELECT COUNT(*) FROM documents"),
        "ledger": q("SELECT COUNT(*) FROM ingest_ledger"),
        "buckets": q("SELECT COUNT(*) FROM hf_buckets"),
    }


def test_m1_ingest_twice_adds_zero(tmp_path):
    rows = _rows()
    conn = open_db(tmp_path / "t.sqlite")

    s1 = IngestStats()
    ingest_hf_rows(conn, rows, {}, s1)
    assert s1.units_seen == 300
    assert s1.parsed == 300 and s1.failed == 0
    assert s1.new_cards > 0 and s1.new_variants == 300
    first = _counts(conn)
    assert first["variants"] == 300
    assert first["analytics"] > 0
    assert first["ledger"] == 300
    assert first["cards"] == s1.new_cards

    # THE invariant: rerun the loader on identical input.
    s2 = IngestStats()
    ingest_hf_rows(conn, rows, {}, s2)
    assert s2.new_cards == 0
    assert s2.new_variants == 0
    assert s2.units_skipped == 300 and s2.parsed == 0
    assert _counts(conn) == first


def test_ledger_units_are_row_ids(tmp_path):
    rows = _rows()[:5]
    conn = open_db(tmp_path / "t.sqlite")
    ingest_hf_rows(conn, rows, {}, IngestStats())
    got = {r["external_id"] for r in conn.execute(
        "SELECT external_id FROM ingest_ledger WHERE source = 'hf'")}
    assert got == {str(r["id"]) for r in rows}


def test_entities_created_from_fixture(tmp_path):
    rows = _rows()
    conn = open_db(tmp_path / "t.sqlite")
    ingest_hf_rows(conn, rows, {}, IngestStats())
    cl = {r["slug"]: r["season"] for r in conn.execute(
        "SELECT slug, season FROM caselists")}
    assert cl == {"hspf19": 2019, "hspf20": 2020, "hspf21": 2021, "hspf22": 2022}
    assert _counts(conn)["rounds"] == len({r["roundId"] for r in rows})
    assert _counts(conn)["teams"] == len({r["teamId"] for r in rows})
    sides = {r["side"] for r in conn.execute("SELECT DISTINCT side FROM rounds")}
    assert sides <= {"P", "C"}
    # PF rows have no docx: one synthetic document per round (hf_verify.md §2)
    assert _counts(conn)["documents"] == _counts(conn)["rounds"]
    # aggregates were recomputed by finish_batch
    assert conn.execute(
        "SELECT COUNT(*) FROM cards WHERE variant_count = 0").fetchone()[0] == 0
    # FTS rows exist for every card
    assert conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0] == \
        _counts(conn)["cards"]


# --- bucketId capture ------------------------------------------------------

def test_bucket_ids_captured(tmp_path):
    rows = _rows()
    conn = open_db(tmp_path / "t.sqlite")
    ingest_hf_rows(conn, rows, {}, IngestStats())
    n = conn.execute("SELECT COUNT(*) FROM hf_buckets").fetchone()[0]
    assert n > 0
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT bucket_id) FROM hf_buckets").fetchone()[0]
    assert distinct == len({r["bucketId"] for r in rows if r["bucketId"]})
    # rerun: no duplicate (card_id, bucket_id) pairs
    ingest_hf_rows(conn, rows, {}, IngestStats())
    assert conn.execute("SELECT COUNT(*) FROM hf_buckets").fetchone()[0] == n


def test_bucket_id_synthetic(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    ingest_hf_rows(conn, [_row(bucketId="bx-9")], {}, IngestStats())
    row = conn.execute("SELECT card_id, bucket_id FROM hf_buckets").fetchone()
    assert row["bucket_id"] == "bx-9"
    assert conn.execute("SELECT id FROM cards WHERE id = ?",
                        (row["card_id"],)).fetchone() is not None


# --- pf_only filter --------------------------------------------------------

def test_pf_only_filters_other_events(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    rows = [
        _row(id=1, roundId="r1", fulltext="pf body one"),
        _row(id=2, roundId="r2", event="cx", caselistName="ndtceda19",
             caselistDisplayName="NDT-CEDA 2019-20", fulltext="cx body"),
    ]
    s = IngestStats()
    ingest_hf_rows(conn, rows, {}, s, pf_only=True)
    assert s.parsed == 1
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    # the cx row was NOT ledger-stamped, so a pf_only=False pass ingests it
    s2 = IngestStats()
    ingest_hf_rows(conn, rows, {}, s2, pf_only=False)
    assert s2.parsed == 1 and s2.units_skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2


# --- unmappable rows -------------------------------------------------------

def test_unmappable_row_counted_failed_and_stamped(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    bad = _row(id=77, fulltext=None, tag=None)
    s = IngestStats()
    ingest_hf_rows(conn, [bad], {}, s)
    assert s.failed == 1 and s.parsed == 0
    # stamped: a rerun skips instead of retrying forever
    s2 = IngestStats()
    ingest_hf_rows(conn, [bad], {}, s2)
    assert s2.units_skipped == 1 and s2.failed == 0


# --- changed row content is reprocessed ------------------------------------

def test_changed_row_content_reprocessed(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    ingest_hf_rows(conn, [_row()], {}, IngestStats())
    changed = _row(fulltext="A different body entirely, re-shipped upstream.")
    s = IngestStats()
    ingest_hf_rows(conn, [changed], {}, s)
    assert s.units_skipped == 0 and s.parsed == 1  # sha differs -> reprocess
    assert s.new_cards == 1                        # new canonical body


# --- ingest_hf: optional dependency ----------------------------------------

def _datasets_available():
    try:
        import datasets  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(_datasets_available(),
                    reason="`datasets` installed; missing-dep path untestable")
def test_ingest_hf_missing_datasets_hint(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    with pytest.raises(RuntimeError) as ei:
        ingest_hf(conn, {}, IngestStats())
    msg = str(ei.value)
    assert "pip install datasets" in msg
    assert "28GB" in msg
