"""PDF card parser for open-source round files and private backfiles.

Spec §3.4's v1 said "PDFs uploaded as open source: log and skip"; the
owner has overridden that: PDFs must produce cards. PDF text carries NO
run formatting we can trust — no underline, bold, highlight, or font-size
extraction — so cards parsed from PDFs are text-only, on the honest-
fidelity model the schema already has: ``markup_html=None``,
``summary=None``, ``spoken=None``, ``highlight_ratio=None``, and
``fidelity='pdf'`` on the variant.

Extraction: pypdf reads text per page; pages are joined with a newline.
Whitespace is normalized conservatively — internal space runs collapse,
but LINE BREAKS ARE KEPT, because they are the only structure a PDF text
layer has. Lines that are pure page furniture ("2", "Page 3 of 12") are
dropped.

Segmentation heuristic (approximate by nature — see failure modes):

1. A line is a CITE ANCHOR when it matches the short-cite shape
   (docx_parser.SHORT_CITE_RE / its multi-author extension, reused via
   ``_cite_match``) at its start, or when it is fullcite-shaped: docx
   parser's ``_is_strong_fullcite`` (<= ~80 words with a URL, a
   19xx/20xx year, or an access-date marker), tightened for line
   granularity — the bare-year signal alone is too weak here (body
   sentences mention years constantly), so a line with no short-cite
   prefix also needs a URL, an access-date marker, or comma density
   (>= 2 commas) beside its year.
2. Consecutive fullcite-shaped lines after an anchor (up to 3) are the
   wrapped remainder of the same cite; together they form the cite block.
3. The nearest preceding short line block (<= ~40 words; the tag is bold
   in print but plain in extracted text) is the TAG: the last line of the
   segment before the cite block, extended upward over lines that end
   without punctuation (a wrapped tag), up to 3 lines / ~40 words.
4. The BODY of each card runs from the end of its cite block to the next
   card's tag (or end of document). Bodies span page breaks naturally.
5. Mirroring the docx parser's reconciled analytic rule: a block with a
   cite is evidence; a block with no cite but a substantial body
   (>= ~40 words) is still evidence; a short tag-only block is an
   analytic. A document with NO cite anchors at all falls back to
   treating its first short line as a tag and the rest as body
   (``used_fallback=True``); with neither tag nor cite it yields no
   cards, only a warning — exactly the docx parser's drop rule.

Known failure modes, honestly: a tagless card whose cite directly follows
the previous card's body steals that body's last line as its tag;
analytics between two evidence cards are absorbed into the preceding
body; body lines with cite-like density (a year plus two commas) can
open a spurious card; multi-line tags beyond three lines are truncated
into the preceding body; page headers/footers that are not bare page
numbers land inside bodies; text order in multi-column PDFs is whatever
pypdf yields. Cite/URL/date extraction reuses the docx parser's helpers,
so those formats stay in sync.

Failures raise PdfFailure, a ``docx_parser.ParseFailure`` subclass, so
every existing ``except ParseFailure`` caller records
``parse_status='failed'`` unchanged. Encrypted PDFs get one empty-
password decrypt attempt; scanned/image-only PDFs (no extractable text)
fail rather than OCR. Per-page extraction errors become warnings, never
failures, unless zero text was extracted overall.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .docx_parser import (ParsedDocument, ParseFailure, SHORT_CITE_RE,  # noqa: F401 (SHORT_CITE_RE re-exported: it is this module's cite shape too)
                          _ACCESS_RE, _DOA_RE, _URL_RE, _cite_match, _clean,
                          _is_strong_fullcite, _wc, extract_pub_date,
                          extract_source_url)
from .ingest import CardRecord


class PdfFailure(ParseFailure):
    """Unparseable PDF; message is the reason. Subclasses ParseFailure so
    callers' existing ``except ParseFailure`` paths record
    parse_status='failed' without new wiring."""


_TAG_MAX_WORDS = 40       # a tag block is at most ~40 words (§3.4 shape)
_TAG_MAX_LINES = 3
_CITE_CONT_MAX = 3        # wrapped fullcite continuation lines absorbed
_MIN_EVIDENCE_WORDS = 40  # no-cite body >= this many words -> evidence

# "2", "Page 3", "3 of 12", "Page 3 / 12" — page furniture, never content
_FURNITURE_RE = re.compile(
    r"^(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.IGNORECASE)

# ends mid-thought (no punctuation): candidate for a wrapped-tag line
_CLOSERS = "\"'’”)»′"


# --- text -> lines ---------------------------------------------------------

def _clean_lines(text: str) -> List[str]:
    """Conservative whitespace normalization: collapse internal space runs,
    keep line breaks, drop blank lines and page furniture."""
    out = []
    for raw in text.split("\n"):
        line = _clean(raw)
        if not line or _FURNITURE_RE.match(line):
            continue
        out.append(line)
    return out


# --- cite anchors ----------------------------------------------------------

def _is_fullcite_line(line: str) -> bool:
    """Fullcite-shaped at line granularity: docx_parser's
    _is_strong_fullcite, but a bare year alone is not enough — it must be
    joined by a URL, an access-date marker, or cite-ish comma density."""
    if not _is_strong_fullcite(line):
        return False
    if _URL_RE.search(line) or _ACCESS_RE.search(line) or _DOA_RE.search(line):
        return True
    return line.count(",") >= 2


def _is_cite_anchor(line: str) -> bool:
    return bool(_cite_match(line)) or _is_fullcite_line(line)


def _cite_blocks(lines: List[str]) -> List[Tuple[int, int]]:
    """[(start, end)) line spans of cite blocks: an anchor line plus up to
    _CITE_CONT_MAX following fullcite-shaped continuation lines."""
    blocks = []
    i = 0
    n = len(lines)
    while i < n:
        if not _is_cite_anchor(lines[i]):
            i += 1
            continue
        j = i + 1
        while j < n and (j - i) <= _CITE_CONT_MAX and _is_fullcite_line(lines[j]):
            j += 1
        blocks.append((i, j))
        i = j
    return blocks


def _split_cite_block(lines: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Cite block lines -> (cite, fullcite). The short-cite path takes
    precedence (same order as the docx parser's _split_cite)."""
    first = lines[0]
    rest = lines[1:]
    m = _cite_match(first)
    if m:
        cite = _clean(m.group(0)).rstrip(",")
        rem = _clean(first[m.end():].lstrip(" \t\n,;:-–—"))
        full = _clean(" ".join(x for x in [rem] + rest if x))
        return cite, (full or None)
    full = _clean(" ".join(lines))
    return None, (full or None)


# --- tag extraction --------------------------------------------------------

def _take_tag(segment: List[str]) -> Tuple[List[str], Optional[str]]:
    """Split the lines between two cite blocks into (previous card's body
    lines, this card's tag). The tag is the segment's last line when short
    enough, extended upward over wrapped-tag lines — lines that end without
    punctuation (body excerpts end their sentences; tag wraps do not)."""
    if not segment:
        return [], None
    idx = len(segment) - 1
    if _wc(segment[idx]) > _TAG_MAX_WORDS:
        return segment, None
    words = _wc(segment[idx])
    while idx > 0 and (len(segment) - idx) < _TAG_MAX_LINES:
        prev = segment[idx - 1].rstrip(_CLOSERS)
        if not prev or prev[-1] in ".!?,;:":
            break
        if words + _wc(prev) > _TAG_MAX_WORDS:
            break
        idx -= 1
        words += _wc(prev)
    return segment[:idx], _clean(" ".join(segment[idx:]))


# --- card assembly ---------------------------------------------------------

def _make_record(tag: Optional[str], cite: Optional[str],
                 fullcite: Optional[str], body_lines: List[str],
                 warnings: List[str]) -> Optional[CardRecord]:
    body_text = "\n".join(body_lines)
    analytic = cite is None and fullcite is None
    if analytic and not tag:
        warnings.append("dropped a card with neither tag nor cite")
        return None
    if analytic and _wc(body_text) >= _MIN_EVIDENCE_WORDS:
        # reconciled analytic rule (docx parser): a substantial body under
        # the tag means evidence, even with no recognized cite
        analytic = False
    if not analytic and not body_text:
        warnings.append(
            "card %r has a cite but no body; treated as analytic"
            % (tag or "")[:40])
        analytic = True
    return CardRecord(
        tag=tag or None,
        cite=cite,
        fullcite=fullcite,
        body_text=body_text,
        is_analytic=analytic,
        source_url=extract_source_url(fullcite),
        source_pub_date=extract_pub_date(fullcite),
        # text-only, honest fidelity: no markup, no projections, no ratio
        markup_html=None, summary=None, spoken=None, highlight_ratio=None,
        fidelity="pdf",
    )


def _segment(lines: List[str], warnings: List[str]
             ) -> Tuple[List[CardRecord], bool]:
    blocks = _cite_blocks(lines)
    cards: List[CardRecord] = []

    if not blocks:
        # fallback: no cite anchors anywhere — first short line is the tag,
        # everything after it is the body (evidence when substantial,
        # analytic when trivial; nothing at all without a tag)
        body, tag = lines[1:], None
        if _wc(lines[0]) <= _TAG_MAX_WORDS:
            tag = lines[0]
        else:
            body = lines
        rec = _make_record(tag, None, None, body, warnings)
        if rec is not None:
            cards.append(rec)
        return cards, True

    # tag for each block + the body span each card owns
    tags: List[Optional[str]] = []
    prev_end = 0
    prev_body_extra: List[List[str]] = []  # per-card leftover before its tag
    for (s, _e) in blocks:
        leftover, tag = _take_tag(lines[prev_end:s])
        prev_body_extra.append(leftover)
        tags.append(tag)
        prev_end = _e
    # leftover before the FIRST card's tag is preamble (title pages, round
    # labels): dropped, exactly like text before the first docx heading
    if prev_body_extra[0]:
        warnings.append("dropped %d leading line(s) before the first card"
                        % len(prev_body_extra[0]))

    for k, (s, e) in enumerate(blocks):
        cite, fullcite = _split_cite_block(lines[s:e])
        if k + 1 < len(blocks):
            # this card's body = the segment before the NEXT card's cite
            # block, minus the tag lines _take_tag split off above
            body = prev_body_extra[k + 1]
        else:
            body = lines[e:]
        rec = _make_record(tags[k], cite, fullcite, body, warnings)
        if rec is not None:
            cards.append(rec)
    return cards, False


# --- top level -------------------------------------------------------------

def parse_pdf_bytes(data: bytes, filename: str = "") -> ParsedDocument:
    """Parse in-memory PDF bytes into text-only cards. Raises PdfFailure."""
    from pypdf import PdfReader

    label = filename or "<bytes>"
    if not data:
        raise PdfFailure("empty file: %s has no bytes" % label)
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise PdfFailure("not a readable PDF (%s): %s" % (label, exc))

    if reader.is_encrypted:
        try:
            result = reader.decrypt("")     # one empty-password attempt
        except Exception as exc:
            raise PdfFailure("encrypted: %s (%s)" % (exc, label))
        if not result:                       # PasswordType.NOT_DECRYPTED
            raise PdfFailure("encrypted: %s needs a password" % label)

    warnings: List[str] = []
    page_texts: List[str] = []
    try:
        pages = list(reader.pages)
    except Exception as exc:
        raise PdfFailure("unreadable page tree (%s): %s" % (label, exc))
    for i, page in enumerate(pages):
        try:
            page_texts.append(page.extract_text() or "")
        except Exception as exc:  # a bad page is a warning, not a failure
            warnings.append("page %d: text extraction failed: %r"
                            % (i + 1, exc))
            page_texts.append("")

    lines = _clean_lines("\n".join(page_texts))
    if not lines:
        # zero text overall: scanned/image-only or truly empty — do NOT OCR
        raise PdfFailure("no extractable text in %s "
                         "(scanned/image-only PDF?)" % label)

    try:
        cards, used_fallback = _segment(lines, warnings)
    except Exception as exc:  # never crash on weird content — report it
        raise PdfFailure("parser error in %s: %r" % (label, exc))
    if not cards:
        warnings.append("no cards found in PDF text")
    for i, rec in enumerate(cards):
        rec.ordinal = i
    return ParsedDocument(cards=cards, warnings=warnings,
                          used_fallback=used_fallback)


def parse_pdf(path) -> ParsedDocument:
    """Parse a .pdf file from disk. Raises PdfFailure."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise PdfFailure("cannot read %s: %s" % (p, exc))
    return parse_pdf_bytes(data, filename=p.name)
