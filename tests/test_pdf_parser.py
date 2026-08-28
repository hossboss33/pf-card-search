"""pdf_parser tests. Owner override of spec §3.4's "log and skip": PDFs
must produce cards — text-only (no trustworthy run formatting), so
markup_html/summary/spoken/highlight_ratio stay None and the variant's
fidelity is 'pdf'.

Fixtures are built IN CODE: a hand-written minimal raw PDF (one text
stream per page, correct xref) is fully deterministic and needs no
layout library. Encrypted fixtures are produced by pypdf's PdfWriter.
"""
import io
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import carddb.pdf_parser as pp
from carddb.docx_parser import ParseFailure
from carddb.docx_parser import SHORT_CITE_RE as DOCX_SHORT_CITE_RE
from carddb.pdf_parser import (PdfFailure, SHORT_CITE_RE, parse_pdf,
                               parse_pdf_bytes)


# ---------------------------------------------------------------------------
# Raw PDF builder: ~40 lines, deterministic, no layout engine
# ---------------------------------------------------------------------------

def _esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def raw_pdf(pages):
    """pages: list of pages, each a list of text lines -> PDF bytes.
    Each line becomes one Tj at its own y position, which pypdf extracts
    with the line breaks intact."""
    objs = []
    n_pages = len(pages)
    kids = " ".join("%d 0 R" % (3 + 2 * i) for i in range(n_pages))
    objs.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append((2, ("<< /Type /Pages /Kids [%s] /Count %d >>"
                     % (kids, n_pages)).encode()))
    font_num = 3 + 2 * n_pages
    for i, lines in enumerate(pages):
        page_num = 3 + 2 * i
        objs.append((page_num, (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Contents %d 0 R /Resources << /Font << /F1 %d 0 R >> >> >>"
            % (page_num + 1, font_num)).encode()))
        ops = ["BT", "/F1 11 Tf", "72 720 Td"]
        for j, line in enumerate(lines):
            if j:
                ops.append("0 -14 Td")
            ops.append("(%s) Tj" % _esc(line))
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        objs.append((page_num + 1,
                     b"<< /Length %d >>\nstream\n%s\nendstream"
                     % (len(stream), stream)))
    objs.append((font_num,
                 b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for num, body in sorted(objs):
        offsets[num] = out.tell()
        out.write(b"%d 0 obj\n" % num)
        out.write(body)
        out.write(b"\nendobj\n")
    xref_at = out.tell()
    count = len(objs) + 1
    out.write(b"xref\n0 %d\n" % count)
    out.write(b"0000000000 65535 f \n")
    for num in sorted(offsets):
        out.write(b"%010d 00000 n \n" % offsets[num])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (count, xref_at))
    return out.getvalue()


def encrypted_pdf(data, user_password, owner_password="owner-secret"):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for page in PdfReader(io.BytesIO(data)).pages:
        w.add_page(page)
    w.encrypt(user_password=user_password, owner_password=owner_password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixture documents
# ---------------------------------------------------------------------------

KESSLER_CITE = ("Kessler '26, Jake Kessler, energy analyst at the Grid "
                "Institute, 7-14-2026,")
KESSLER_CITE_2 = ('"The interconnection queue," Wired, '
                  "https://example.com/grid-queue, accessed 8-1-2026")
DIAMOND_CITE = ("Diamond '13, Jared Diamond, professor of geography at "
                "UCLA, Science, March 12, 2013")

TWO_CARD_LINES = [
    "Fracking bans spike energy prices",
    KESSLER_CITE,
    KESSLER_CITE_2,
    "Restricting supply while demand rises pushes prices upward across every",
    "regional market, and the model shows a persistent gap through 2030.",
    "Renewables cannot fill the gap",
    DIAMOND_CITE,
    "Intermittency remains the binding constraint on grid decarbonization in",
    "every serious model of the next decade.",
]
TWO_CARD_PDF = raw_pdf([TWO_CARD_LINES])

FULLCITE_ONLY_PDF = raw_pdf([[
    "Data centers strain the grid",
    "Jake Kessler, energy analyst at the Grid Institute, 7-14-2026, "
    '"The interconnection queue," Wired, https://example.com/grid-queue',
    "Interconnection requests now exceed installed capacity in three",
    "regional markets and the backlog keeps growing.",
]])

ANALYTIC_PDF = raw_pdf([[
    "No impact - their evidence predates the 2019 reforms",
]])

NO_CITE_BODY_PDF = raw_pdf([[
    "Framework: prioritize probability over magnitude",
    "Judges should weigh arguments by how likely they are to happen rather",
    "than by how large their terminal impacts sound. A tiny chance of an",
    "enormous harm should not outweigh a near certain moderate one. This",
    "is the only way to keep the round from collapsing into competing",
    "hypotheticals that neither team can meaningfully compare or resolve.",
]])

MULTI_PAGE_PDF = raw_pdf([
    [
        "Fracking bans spike energy prices",
        KESSLER_CITE,
        KESSLER_CITE_2,
        "Restricting supply while demand rises pushes prices upward across",
        "every regional market that analysts have modeled so far and the",
    ],
    [
        "2",   # page-number furniture: must not land in the body
        "effect compounds every winter as heating demand peaks while new",
        "supply stays locked behind the permitting moratorium.",
    ],
])


# ---------------------------------------------------------------------------
# Regex/helper reuse (parser-sync: one cite shape, defined once)
# ---------------------------------------------------------------------------

def test_short_cite_re_is_docx_parsers():
    assert SHORT_CITE_RE is DOCX_SHORT_CITE_RE


def test_pdf_failure_is_a_parse_failure():
    # callers' existing `except ParseFailure` must catch PDF failures
    assert issubclass(PdfFailure, ParseFailure)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def test_two_card_page_with_short_cites():
    pd = parse_pdf_bytes(TWO_CARD_PDF, "two.pdf")
    assert not pd.used_fallback
    assert len(pd.cards) == 2
    a, b = pd.cards
    assert a.ordinal == 0 and b.ordinal == 1

    assert a.tag == "Fracking bans spike energy prices"
    # pypdf renders the Type1 apostrophe as ’ — the regex accepts both
    assert a.cite in ("Kessler '26", "Kessler ’26")
    assert "Jake Kessler" in a.fullcite and "Wired" in a.fullcite
    assert a.source_url == "https://example.com/grid-queue"
    assert a.source_pub_date == "2026-07-14"
    assert not a.is_analytic
    assert a.body_text.startswith("Restricting supply")
    assert "persistent gap through 2030." in a.body_text
    assert "\n" in a.body_text          # line breaks kept

    assert b.tag == "Renewables cannot fill the gap"
    assert b.cite in ("Diamond '13", "Diamond ’13")
    assert b.source_pub_date == "2013-03-12"
    assert b.source_url is None
    assert b.body_text.startswith("Intermittency remains")
    # the tag line never leaks into the first card's body
    assert "Renewables" not in a.body_text


def test_pdf_cards_are_text_only_with_pdf_fidelity():
    for rec in parse_pdf_bytes(TWO_CARD_PDF, "two.pdf").cards:
        assert rec.markup_html is None
        assert rec.summary is None
        assert rec.spoken is None
        assert rec.highlight_ratio is None
        assert rec.fidelity == "pdf"
        assert rec.pocket is None and rec.hat is None and rec.block is None


def test_fullcite_only_card():
    pd = parse_pdf_bytes(FULLCITE_ONLY_PDF, "full.pdf")
    assert len(pd.cards) == 1
    c = pd.cards[0]
    assert c.tag == "Data centers strain the grid"
    assert c.cite is None                      # no short-cite prefix
    assert c.fullcite.startswith("Jake Kessler")
    assert c.source_url == "https://example.com/grid-queue"
    assert c.source_pub_date == "2026-07-14"
    assert not c.is_analytic
    assert c.body_text.startswith("Interconnection requests")


def test_analytic_short_tag_only():
    pd = parse_pdf_bytes(ANALYTIC_PDF, "a2.pdf")
    assert pd.used_fallback                    # no cite anchors anywhere
    assert len(pd.cards) == 1
    c = pd.cards[0]
    assert c.is_analytic
    assert c.tag.startswith("No impact")
    assert c.cite is None and c.fullcite is None
    assert c.body_text == ""


def test_no_cite_substantial_body_is_evidence():
    pd = parse_pdf_bytes(NO_CITE_BODY_PDF, "framework.pdf")
    assert pd.used_fallback
    assert len(pd.cards) == 1
    c = pd.cards[0]
    assert not c.is_analytic                   # >= ~40 words: evidence
    assert c.tag.startswith("Framework:")
    assert c.cite is None and c.fullcite is None
    assert "probability" in c.body_text.lower() or "likely" in c.body_text


def test_multi_page_card_spans_page_break():
    pd = parse_pdf_bytes(MULTI_PAGE_PDF, "multi.pdf")
    assert len(pd.cards) == 1
    c = pd.cards[0]
    assert c.tag == "Fracking bans spike energy prices"
    assert "modeled so far and the" in c.body_text          # page 1 body
    assert "permitting moratorium." in c.body_text          # page 2 body
    lines = c.body_text.split("\n")
    assert "2" not in lines                    # furniture dropped


def test_year_in_body_does_not_open_a_card():
    # "gap through 2030." carries a year but no URL/access marker and only
    # one comma: it must stay body, not become a spurious cite anchor
    pd = parse_pdf_bytes(TWO_CARD_PDF, "two.pdf")
    assert len(pd.cards) == 2


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_empty_bytes_raise():
    with pytest.raises(PdfFailure, match="empty"):
        parse_pdf_bytes(b"", "empty.pdf")


def test_non_pdf_bytes_raise():
    with pytest.raises(PdfFailure, match="not a readable PDF"):
        parse_pdf_bytes(b"this is not a pdf, just some prose\n" * 10,
                        "prose.pdf")


def test_no_extractable_text_raises_without_ocr():
    blank = raw_pdf([[]])                      # one page, no text at all
    with pytest.raises(PdfFailure, match="no extractable text"):
        parse_pdf_bytes(blank, "scan.pdf")


def test_zero_page_pdf_raises():
    from pypdf import PdfWriter
    buf = io.BytesIO()
    PdfWriter().write(buf)
    with pytest.raises(PdfFailure, match="no extractable text"):
        parse_pdf_bytes(buf.getvalue(), "zero.pdf")


def test_encrypted_pdf_raises():
    enc = encrypted_pdf(TWO_CARD_PDF, user_password="secret")
    with pytest.raises(PdfFailure, match="encrypted"):
        parse_pdf_bytes(enc, "locked.pdf")


def test_encrypted_with_empty_user_password_parses():
    # pypdf's empty-password decrypt succeeds here — cards come through
    enc = encrypted_pdf(TWO_CARD_PDF, user_password="")
    pd = parse_pdf_bytes(enc, "open.pdf")
    assert len(pd.cards) == 2
    assert pd.cards[0].tag == "Fracking bans spike energy prices"


def test_per_page_extraction_error_is_a_warning(monkeypatch):
    from pypdf import PageObject
    orig = PageObject.extract_text
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom on page 1")
        return orig(self, *a, **kw)

    monkeypatch.setattr(PageObject, "extract_text", flaky)
    # page 1 dies, page 2 alone still forms a full card
    data = raw_pdf([
        ["Preamble page that will fail to extract"],
        ["Renewables cannot fill the gap",
         DIAMOND_CITE,
         "Intermittency remains the binding constraint on decarbonization."],
    ])
    pd = parse_pdf_bytes(data, "flaky.pdf")
    assert any("page 1" in w and "extraction failed" in w
               for w in pd.warnings)
    assert len(pd.cards) == 1
    assert pd.cards[0].tag == "Renewables cannot fill the gap"


def test_parse_pdf_path_matches_bytes(tmp_path):
    p = tmp_path / "two.pdf"
    p.write_bytes(TWO_CARD_PDF)
    from_path = parse_pdf(p)
    from_bytes = parse_pdf_bytes(TWO_CARD_PDF, "two.pdf")
    assert [c.key() for c in from_path.cards] == \
        [c.key() for c in from_bytes.cards]


def test_parse_pdf_missing_path_raises(tmp_path):
    with pytest.raises(PdfFailure, match="cannot read"):
        parse_pdf(tmp_path / "nope.pdf")


# ---------------------------------------------------------------------------
# CLI wiring: `ingest --source private` routes .pdf through parse_pdf
# ---------------------------------------------------------------------------

def _cli_cfg(tmp_path):
    return {"paths": {"db": str(tmp_path / "carddb.sqlite"),
                      "raw_store": str(tmp_path / "raw"),
                      "topics": str(tmp_path / "topics.json"),
                      "reports": str(tmp_path / "reports"),
                      "backups": str(tmp_path / "backups")}}


def _ingest_private(tmp_path, paths):
    from carddb.cli import cmd_ingest
    args = SimpleNamespace(source="private", paths=[str(p) for p in paths],
                           caselist=None, since=None, limit=None)
    return cmd_ingest(args, _cli_cfg(tmp_path))


def _counts(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {"cards": q("SELECT COUNT(*) FROM cards"),
                "variants": q("SELECT COUNT(*) FROM card_variants"),
                "documents": q("SELECT COUNT(*) FROM documents")}
    finally:
        conn.close()


def test_cli_private_ingests_pdf_with_pdf_fidelity(tmp_path):
    pdf = tmp_path / "backfile.pdf"
    pdf.write_bytes(TWO_CARD_PDF)
    assert _ingest_private(tmp_path, [pdf]) == 0

    conn = sqlite3.connect(str(tmp_path / "carddb.sqlite"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT fidelity, markup_html, summary, spoken, highlight_ratio "
        "FROM card_variants").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["fidelity"] == "pdf"
        assert r["markup_html"] is None
        assert r["summary"] is None
        assert r["spoken"] is None
        assert r["highlight_ratio"] is None
    st = conn.execute("SELECT parse_status FROM documents").fetchone()
    assert st["parse_status"] == "ok"
    # FTS rows exist for the ingested cards (finish_batch ran)
    assert conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0] == 2
    conn.close()


def test_cli_private_pdf_rerun_is_idempotent(tmp_path):
    pdf = tmp_path / "backfile.pdf"
    pdf.write_bytes(TWO_CARD_PDF)
    db = tmp_path / "carddb.sqlite"
    assert _ingest_private(tmp_path, [pdf]) == 0
    first = _counts(db)
    assert _ingest_private(tmp_path, [pdf]) == 0   # ledger skips the sha
    assert _counts(db) == first


def test_cli_private_encrypted_pdf_records_parse_failed(tmp_path):
    pdf = tmp_path / "locked.pdf"
    pdf.write_bytes(encrypted_pdf(TWO_CARD_PDF, user_password="secret"))
    assert _ingest_private(tmp_path, [pdf]) == 0   # batch never aborts

    conn = sqlite3.connect(str(tmp_path / "carddb.sqlite"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT parse_status, parse_error FROM documents").fetchone()
    assert row["parse_status"] == "failed"
    assert "encrypted" in row["parse_error"]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# api_sync wiring: a .pdf open-source file goes through parse_pdf_bytes
# ---------------------------------------------------------------------------

PDF_PATH = "hspf25/Northview/NoAB/case.pdf"


def _pdf_only_api():
    from test_api_sync import ROUNDS, MockAPI
    rounds = {"NoAB": [dict(ROUNDS["NoAB"][0], opensource=PDF_PATH)],
              "MiCD": [], "MiEF": []}
    cites = {"NoAB": [], "MiCD": [], "MiEF": []}
    return MockAPI(rounds=rounds, cites=cites,
                   downloads={PDF_PATH: TWO_CARD_PDF})


def test_api_sync_pdf_download_routed_through_parse_pdf_bytes(
        tmp_path, monkeypatch):
    from carddb.db import open_db
    from test_api_sync import make_cfg, run_sync
    monkeypatch.setenv("TEST_TABROOM_USER", "owner@example.com")
    monkeypatch.setenv("TEST_TABROOM_PASS", "not-a-real-password")

    seen = {}
    real = pp.parse_pdf_bytes

    def spy(data, filename=""):
        seen["filename"] = filename
        seen["data"] = data
        return real(data, filename=filename)

    monkeypatch.setattr(pp, "parse_pdf_bytes", spy)

    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    st = run_sync(conn, cfg, _pdf_only_api())

    # the .pdf filename went through parse_pdf_bytes with the raw bytes
    assert seen["filename"] == "case.pdf"
    assert seen["data"] == TWO_CARD_PDF
    # not recorded as failed: it parsed into cards on the round
    assert st.parsed == 1 and st.failed == 0
    assert st.new_cards == 2 and st.new_variants == 2
    rows = conn.execute(
        "SELECT v.fidelity, v.markup_html, v.summary, v.spoken, "
        " v.highlight_ratio FROM card_variants v "
        "JOIN rounds r ON r.id = v.round_id "
        "WHERE r.external_id = 'api-101'").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["fidelity"] == "pdf"
        assert r["markup_html"] is None and r["summary"] is None
        assert r["spoken"] is None and r["highlight_ratio"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE parse_status = 'failed'"
    ).fetchone()[0] == 0
    conn.close()
