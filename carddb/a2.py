"""A2 / AT block-title parsing for the answers cross-index. Spec §9.3.

An answer block is titled "A2: X" or "AT: X" (case-insensitive, colon
optional, "A/2" and "AT-" variants appear in the wild). The normalized
target string is what links an answer block to the argument it answers,
so both sides normalize the same way: strip the prefix, then run the
frozen §3.5 normalizer.
"""
from __future__ import annotations

import re
from typing import Optional

from .normalize import normalize

_A2_PREFIX = re.compile(r"^\s*(?:a2|at|a/2|a-2)\s*[:\-–—]?\s+", re.IGNORECASE)
_A2_PREFIX_TIGHT = re.compile(r"^\s*(?:a2|at)[:\-–—]\s*", re.IGNORECASE)
_LEADING_NUMBERING = re.compile(r"^\s*(?:\(?\d+[\).]|\(?[a-z][\).]|[ivx]+[\).])\s+", re.IGNORECASE)


def a2_target(block_title: Optional[str]) -> Optional[str]:
    """Return the normalized answered-argument string, or None if the
    block is not an answer block."""
    if not block_title:
        return None
    s = _LEADING_NUMBERING.sub("", block_title)
    m = _A2_PREFIX.match(s) or _A2_PREFIX_TIGHT.match(s)
    if not m:
        return None
    target = s[m.end():]
    norm = normalize(target)
    return norm or None


def argument_key(block_title: Optional[str]) -> Optional[str]:
    """Normalized form of any block title (answer prefix stripped), used
    to match an argument's title against disclosed A2 targets."""
    if not block_title:
        return None
    s = _LEADING_NUMBERING.sub("", block_title)
    m = _A2_PREFIX.match(s) or _A2_PREFIX_TIGHT.match(s)
    if m:
        s = s[m.end():]
    norm = normalize(s)
    return norm or None
