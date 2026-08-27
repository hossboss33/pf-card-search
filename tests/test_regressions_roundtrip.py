"""§9.4 round-trip regressions (reviewer repro).

PF dataset rows mostly ship cite='' with everything in fullcite (mapped to
cite=None by the HF loader). The export writes a fullcite-only cite
paragraph; the parser used to recognize only short-cite-shaped lines,
found no cite, and — per its old no-cite rule — marked the whole block an
analytic. The re-parsed card then took the analytic tag-keyed canonical
key, so re-ingesting exported prep (private backfile import,
evidence-exchange docs) silently minted duplicate canonical cards.

These tests drive four card shapes through the real ingest path
(insert_card + attach_variant), export them via BOTH presets, re-parse the
export, and require identical canonical_key / is_analytic / spoken /
summary — then re-ingest the exported file and require exactly 0 new
canonical cards:

(a) an HF-shaped card: cite=None, realistic fullcite, markup with the
    fullcite paragraph ahead of the body (mirrors carddb.hf_loader
    .map_hf_row output for a typical PF row, whose cite='' -> None);
(b) a no-cite card: cite=None, fullcite=None, substantial body only;
(c) normal short-cite cards (build_verbatim — the shape the 34 existing
    export tests already cover, which must not regress);
(d) a true analytic (tag-only Heading 4, also from build_verbatim).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.docx_builders import build_verbatim, docx_bytes

from carddb.db import open_db
from carddb.docx_parser import parse_docx, parse_docx_bytes
from carddb.export_docx import export_cards
from carddb.ingest import CardRecord, attach_variant, insert_card
from carddb.keys import sha256_bytes
from carddb.rawstore import record_document
from carddb.sanitize import sanitize_markup


@pytest.fixture()
def conn(tmp_path):
    c = open_db(tmp_path / "cards.sqlite")
    yield c
    c.close()


# --- (a) HF-shaped card: cite='' -> None, everything in fullcite -----------

HF_FULLCITE = ("Author Name, 7-28-2019. Title, Reuters. https://x.test/a. "
               "Accessed 1-6-2020. //TP")
HF_TAG = "Moratorium collapses the grid"
HF_BODY = ("The moratorium freezes interconnection approvals for years and "
           "grid operators warn that blackouts follow.")
HF_MARKUP = sanitize_markup(
    "<h4>" + HF_TAG + "</h4>"
    "<p>" + HF_FULLCITE + "</p>"
    "<p>The moratorium <u>freezes interconnection approvals for years </u>"
    "and grid operators warn that <mark>blackouts follow</mark>.</p>")


def hf_style_record():
    """Mirrors map_hf_row output for a typical PF dataset row: the row's
    cite column is '' (mapped to None), the whole citation lives in
    fullcite, and the markup carries the fullcite paragraph ahead of the
    body. spoken/summary are set to the parser's own projections of the
    markup so the round-trip equality below is exact."""
    return CardRecord(
        tag=HF_TAG,
        cite=None,                      # PF rows ship cite=''; _s() -> None
        fullcite=HF_FULLCITE,
        body_text=HF_BODY,
        is_analytic=False,
        source_url="https://x.test/a",
        source_pub_date="2019-07-28",
        markup_html=HF_MARKUP,
        summary="freezes interconnection approvals for years blackouts follow",
        spoken="blackouts follow",
        highlight_ratio=len("blackouts follow") / len(HF_BODY),
        ordinal=0,
    )


# --- (b) no cite at all: body-only evidence card ---------------------------

NOCITE_BODY = (
    "Prefer probability over magnitude because judges can only weigh what "
    "is likely, speculative impacts invite infinite regress, both teams "
    "get better clash when links are compared honestly, and every coach "
    "teaches that a coherent story beats a pile of unlikely apocalypse "
    "claims in front of any panel."
    "\n"
    "That framing also rewards research over trickery and keeps rounds "
    "educational for novices and varsity alike.")


def body_only_record():
    return CardRecord(
        tag="Framework: prefer probability over magnitude",
        cite=None,
        fullcite=None,
        body_text=NOCITE_BODY,
        is_analytic=False,
        markup_html=None,               # cites_only-style: plain body export
        summary="",
        spoken="",
        ordinal=1,
    )


# --- ingest helpers (the real insert_card + attach_variant path) -----------

def ingest_records(conn, recs, doc_key):
    doc_id = record_document(conn, sha256_bytes(doc_key.encode("utf-8")),
                             "test", None, doc_key, None)
    out = []
    for rec in recs:
        cid, _created = insert_card(conn, rec)
        attach_variant(conn, cid, rec, doc_id, None)
        out.append((cid, rec))
    conn.commit()
    return out


def ingest_docx(conn, data, name):
    parsed = parse_docx_bytes(data, name)
    doc_id = record_document(conn, sha256_bytes(data), "test", None, name, None)
    out = []
    for rec in parsed.cards:
        cid, _created = insert_card(conn, rec)
        attach_variant(conn, cid, rec, doc_id, None)
        out.append((cid, rec))
    conn.commit()
    return out


# --- THE regression: all four shapes, both presets -------------------------

@pytest.mark.parametrize("preset", ["house", "verbatim"])
def test_round_trip_all_shapes_and_reingest_zero_new_canonicals(
        conn, tmp_path, preset):
    hf_rows = ingest_records(conn, [hf_style_record(), body_only_record()],
                             "hf-shaped.docx")
    vb_rows = ingest_docx(conn, docx_bytes(build_verbatim()), "verbatim.docx")
    rows = hf_rows + vb_rows        # (a), (b), then (c) x2 and (d)
    assert len(rows) == 5
    assert sum(1 for _cid, rec in rows if rec.is_analytic) == 1

    out = export_cards(conn, [cid for cid, _rec in rows],
                       tmp_path / ("rt-%s.docx" % preset), preset=preset)
    reparsed = parse_docx(out)
    assert len(reparsed.cards) == len(rows)

    for (cid, rec), got in zip(rows, reparsed.cards):
        assert got.key() == rec.key(), rec.tag        # identical canonical_key
        assert got.is_analytic == rec.is_analytic, rec.tag
        assert got.spoken == rec.spoken, rec.tag
        assert got.summary == rec.summary, rec.tag

    # re-ingesting the exported file through the real path: 0 new canonicals
    n_before = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    doc_id = record_document(conn, sha256_bytes(out.read_bytes()), "test",
                             None, out.name, None)
    new_canonicals = 0
    for got in reparsed.cards:
        cid2, created = insert_card(conn, got)
        new_canonicals += int(created)
        attach_variant(conn, cid2, got, doc_id, None)
    conn.commit()
    assert new_canonicals == 0
    n_after = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    assert n_after == n_before


# --- focused repro of the original failure (case (a) alone) ----------------

@pytest.mark.parametrize("preset", ["house", "verbatim"])
def test_hf_fullcite_only_card_not_reparsed_as_analytic(conn, tmp_path, preset):
    """The reviewer's exact failure: the exported fullcite-only cite
    paragraph used to make the re-parsed card an analytic with a tag-keyed
    canonical key."""
    (cid, rec), = ingest_records(conn, [hf_style_record()], "hf-one.docx")
    out = export_cards(conn, [cid], tmp_path / ("one-%s.docx" % preset),
                       preset=preset)
    reparsed = parse_docx(out)
    assert len(reparsed.cards) == 1
    got = reparsed.cards[0]
    assert not got.is_analytic
    assert got.cite is None                    # no short cite to find
    assert got.fullcite == HF_FULLCITE         # cite line survives verbatim
    assert got.source_url == "https://x.test/a"
    assert got.key() == rec.key()
    cid2, created = insert_card(conn, got)
    assert not created
    assert cid2 == cid


def test_body_only_card_round_trips_as_evidence(conn, tmp_path):
    """Case (b): no cite paragraph at all, substantial body — an evidence
    card with cite=None on both sides of the trip (§1.3 reconciliation)."""
    (cid, rec), = ingest_records(conn, [body_only_record()], "nocite.docx")
    out = export_cards(conn, [cid], tmp_path / "nocite.docx")
    got = parse_docx(out).cards[0]
    assert not got.is_analytic
    assert got.cite is None and got.fullcite is None
    assert got.key() == rec.key()
    assert got.spoken == "" and got.summary == ""
    cid2, created = insert_card(conn, got)
    assert not created and cid2 == cid
