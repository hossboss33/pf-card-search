"""Miscut heuristics (feature 9.6, spec §9.6).

Flags, never verdicts. Each signal is a hint that a card deserves a human
look, rendered as a small gray glyph with a tooltip on the card page only.
No numeric quality score — that would be false precision.

Works on plain dicts, sqlite3.Row, or attribute objects for both the card
row and its variant rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Set

# --- Thresholds -----------------------------------------------------------
# These are HEURISTICS, tuned by eyeballing real disclosures, not measured
# truths. Adjust freely; nothing downstream treats them as verdicts.
HIGHLIGHT_RATIO_HIGH = 0.8    # any variant highlighting > 80% of a long body
HIGHLIGHT_RATIO_LOW = 0.02    # ...or under 2% of it
LONG_BODY_CHARS = 500         # "body is long" cutoff for the ratio checks
BRACKET_DENSITY_PER_100 = 2.0  # [inserted] spans per 100 words
BRACKET_MIN_WORDS = 20        # skip the density check on tiny bodies
ELLIPSIS_MIN = 3              # '…' or '...' occurrences that earn a flag
TAG_SPOKEN_JACCARD = 0.05     # tag<->spoken content-word overlap below this
OVERLAP_MIN_CONTENT_WORDS = 3  # both sides must be non-trivial

_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
_ELLIPSIS_RE = re.compile(r"…|\.{3,}")  # one run of dots = one ellipsis
_WORD_RE = re.compile(r"[a-z0-9']+")

_STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here of in on
at to for from by with without as is are was were be been being am it its
not no nor so too very can will just do does did done has have had having
may might must shall should would could about into over under between
across after before during through more most less least own same other
some such only both each few all any when where which who whom why how
what while because until again once out off up down we they you he she i
""".split())


@dataclass(frozen=True)
class Flag:
    code: str
    label: str
    detail: str


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict, sqlite3.Row, or attribute object."""
    if row is None:
        return default
    try:
        v = row[key]
        if v is not None:
            return v
    except (KeyError, IndexError, TypeError):
        pass
    v = getattr(row, key, None)
    return default if v is None else v


def _content_words(text: str) -> Set[str]:
    words = _WORD_RE.findall((text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def miscut_flags(card_row: Any, variant_rows: Iterable[Any]) -> List[Flag]:
    """All heuristic flags for one canonical card and its variants.

    Card-level: a signal from any single variant flags the card once."""
    flags: List[Flag] = []
    variants = list(variant_rows or [])
    body = _get(card_row, "body_text", "") or ""
    body_len = int(_get(card_row, "body_len") or len(body))
    tag = _get(card_row, "tag", "") or ""
    words = body.split()

    # 1. Highlight-ratio outliers on long bodies. A short card fully
    #    highlighted is normal prep; a long one usually means pasted
    #    pre-highlighted text or a card nobody actually reads from.
    if body_len >= LONG_BODY_CHARS:
        ratios = [r for r in (_get(v, "highlight_ratio") for v in variants)
                  if r is not None]
        high = [r for r in ratios if r > HIGHLIGHT_RATIO_HIGH]
        low = [r for r in ratios if r < HIGHLIGHT_RATIO_LOW]
        if high:
            flags.append(Flag(
                "hl_ratio_high",
                "Nearly the whole card is highlighted",
                "a variant highlights {:.0f}% of a {}-character body "
                "(threshold {:.0f}%)".format(
                    100 * max(high), body_len, 100 * HIGHLIGHT_RATIO_HIGH),
            ))
        if low:
            flags.append(Flag(
                "hl_ratio_low",
                "Almost none of a long card is highlighted",
                "a variant highlights {:.1f}% of a {}-character body "
                "(threshold {:.0f}%)".format(
                    100 * min(low), body_len, 100 * HIGHLIGHT_RATIO_LOW),
            ))

    # 2. Bracket-insertion density: many [inserted] spans can change what
    #    the evidence says.
    if len(words) >= BRACKET_MIN_WORDS:
        n_brackets = len(_BRACKET_RE.findall(body))
        density = 100.0 * n_brackets / len(words)
        if density > BRACKET_DENSITY_PER_100:
            flags.append(Flag(
                "bracket_density",
                "Many bracketed insertions",
                "{} bracketed insertions in {} words "
                "({:.1f} per 100 words, threshold {:.1f})".format(
                    n_brackets, len(words), density, BRACKET_DENSITY_PER_100),
            ))

    # 3. Ellipsis count: repeated elisions can splice distant sentences.
    n_ellipses = len(_ELLIPSIS_RE.findall(body))
    if n_ellipses >= ELLIPSIS_MIN:
        flags.append(Flag(
            "ellipsis",
            "Multiple ellipses in the body",
            "{} ellipses (threshold {})".format(n_ellipses, ELLIPSIS_MIN),
        ))

    # 4. Tag <-> spoken lexical overlap wildly low: the words read aloud
    #    share almost nothing with the claim the tag makes — a possible
    #    power-tag. Spoken text is pooled across variants (charitable: any
    #    team's highlighting supporting the tag clears the card).
    tag_words = _content_words(tag)
    spoken_words: Set[str] = set()
    for v in variants:
        spoken_words |= _content_words(_get(v, "spoken", "") or "")
    if (len(tag_words) >= OVERLAP_MIN_CONTENT_WORDS
            and len(spoken_words) >= OVERLAP_MIN_CONTENT_WORDS):
        union = tag_words | spoken_words
        jaccard = len(tag_words & spoken_words) / len(union)
        if jaccard < TAG_SPOKEN_JACCARD:
            flags.append(Flag(
                "power_tag",
                "Tag shares almost no words with the spoken text",
                "tag/spoken content-word Jaccard {:.3f} "
                "(threshold {})".format(jaccard, TAG_SPOKEN_JACCARD),
            ))

    return flags
