"""Highlight consensus (feature 9.1, spec §9.1).

Variants of one canonical card share body_text by construction (§4.2), so
per-team highlighting can be projected onto the canonical body's whitespace
tokens and summed: "what do good teams actually read from this card?"

Alignment: when a variant's visible text tokenizes identically to the body
(the overwhelmingly common case), marks map 1:1. When the variant's text
diverges slightly — trimmed variants after 'trim' merges (§4.3), stray
inserted words, OCR drift — we align token lists with
difflib.SequenceMatcher; marks land on matching body tokens and regions the
variant does not contain are simply unmarked. Heading text (h1–h4: pocket /
hat / block / tag) is not part of the body and is excluded before alignment.
"""
from __future__ import annotations

import difflib
from html.parser import HTMLParser
from typing import List, Tuple

# Tags whose text content is NOT body text (tag/hat/block headings).
_HEADINGS = {"h1", "h2", "h3", "h4"}
# Block-ish tags that must separate tokens on either side of them.
_BREAKS = {"p", "br", "h1", "h2", "h3", "h4"}
# Highlighted = read aloud in round (spec §1.2 layer 4).
_MARK_TAGS = {"mark"}
# Underlined / bold = the extractive summary layers (spec §1.2 layers 2–3).
_SUMMARY_TAGS = {"u", "strong"}


class _MarkupWalker(HTMLParser):
    """Walk sanitized markup_html, emitting (text, marked, summarized)
    segments for non-heading text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: List[Tuple[str, bool, bool]] = []
        self._mark = 0
        self._summary = 0
        self._heading = 0

    def _break(self) -> None:
        # Block boundaries separate tokens so '<p>a</p><p>b</p>' != 'ab'.
        self.segments.append((" ", False, False))

    def handle_starttag(self, tag, attrs):
        if tag in _BREAKS:
            self._break()
        if tag in _HEADINGS:
            self._heading += 1
        elif tag in _MARK_TAGS:
            self._mark += 1
        elif tag in _SUMMARY_TAGS:
            self._summary += 1

    def handle_endtag(self, tag):
        if tag in _BREAKS:
            self._break()
        if tag in _HEADINGS:
            self._heading = max(0, self._heading - 1)
        elif tag in _MARK_TAGS:
            self._mark = max(0, self._mark - 1)
        elif tag in _SUMMARY_TAGS:
            self._summary = max(0, self._summary - 1)

    def handle_data(self, data):
        if self._heading or not data:
            return
        self.segments.append((data, self._mark > 0, self._summary > 0))


def _variant_tokens(markup_html: str) -> List[Tuple[str, bool, bool]]:
    """Whitespace tokens of the variant's body text with per-token flags.

    A token is flagged if any of its characters fall inside the layer
    (highlight fragments can start or end mid-token)."""
    walker = _MarkupWalker()
    walker.feed(markup_html or "")
    walker.close()
    tokens: List[Tuple[str, bool, bool]] = []
    cur: List[str] = []
    marked = summarized = False
    for text, m, s in walker.segments:
        for ch in text:
            if ch.isspace():
                if cur:
                    tokens.append(("".join(cur), marked, summarized))
                    cur, marked, summarized = [], False, False
            else:
                cur.append(ch)
                marked = marked or m
                summarized = summarized or s
    if cur:
        tokens.append(("".join(cur), marked, summarized))
    return tokens


def _project(body_tokens: List[str],
             variant_tokens: List[Tuple[str, bool, bool]],
             flag_index: int) -> List[bool]:
    """Project one flag column of the variant's tokens onto the body tokens."""
    flags = [False] * len(body_tokens)
    if not body_tokens or not variant_tokens:
        return flags
    vtoks = [t[0] for t in variant_tokens]
    if vtoks == body_tokens:  # fast path: identical tokenization
        for i, tok in enumerate(variant_tokens):
            flags[i] = tok[flag_index]
        return flags
    # Divergent text (trimmed variant, inserted words): align token lists.
    # autojunk=False — "popular" tokens (the, of, ...) are exactly the ones
    # we need aligned in long bodies.
    sm = difflib.SequenceMatcher(a=body_tokens, b=vtoks, autojunk=False)
    for op, a1, a2, b1, _b2 in sm.get_opcodes():
        if op == "equal":
            for off in range(a2 - a1):
                flags[a1 + off] = variant_tokens[b1 + off][flag_index]
    return flags


def body_tokens(body_text: str) -> List[str]:
    """The canonical tokenization every vector in this module is aligned to."""
    return (body_text or "").split()


def variant_mark_vector(body_text: str, markup_html: str) -> List[bool]:
    """Per-body-token booleans: does this variant highlight (<mark>) the token?

    len(result) == len(body_text.split()). Tokens the variant's text does not
    contain (trimmed regions) are False."""
    return _project(body_tokens(body_text), _variant_tokens(markup_html), 1)


def variant_summary_vector(body_text: str, markup_html: str) -> List[bool]:
    """Same projection for the summary layers (<u> / <strong>)."""
    return _project(body_tokens(body_text), _variant_tokens(markup_html), 2)


def consensus(body_text: str, markup_htmls: List[str]) -> List[Tuple[str, int]]:
    """Sum highlight vectors across variants.

    Returns [(token, highlight_count)] aligned to the canonical body's
    tokens; count is how many variants highlight that token."""
    toks = body_tokens(body_text)
    counts = [0] * len(toks)
    for markup in markup_htmls or []:
        vec = _project(toks, _variant_tokens(markup), 1)
        for i, hit in enumerate(vec):
            if hit:
                counts[i] += 1
    return list(zip(toks, counts))


def consensus_summary_vector(body_text: str,
                             markup_htmls: List[str]) -> List[Tuple[str, int]]:
    """Summary-layer consensus: [(token, underline_or_bold_count)] aligned to
    the body tokens. Powers the summary-consensus toggle."""
    toks = body_tokens(body_text)
    counts = [0] * len(toks)
    for markup in markup_htmls or []:
        vec = _project(toks, _variant_tokens(markup), 2)
        for i, hit in enumerate(vec):
            if hit:
                counts[i] += 1
    return list(zip(toks, counts))
