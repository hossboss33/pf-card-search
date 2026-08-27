"""Text normalization for hashing. Spec §3.5.

FROZEN. NORM_V is stored alongside every hash; changing this function in any
way requires bumping NORM_V and re-keying the corpus. Do not "improve" it.
Used for hashing and near-dup shingling and nothing else — display text is
never normalized.
"""
from __future__ import annotations

import re
import unicodedata

NORM_V = "1"

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
}
_DASHES = {"–": "-", "—": "-", "―": "-", "−": "-"}

_KEEP = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    """§3.5, exactly: NFKC → lowercase → straighten quotes/dashes →
    strip everything outside [a-z0-9 ] → collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    for k, v in _QUOTES.items():
        s = s.replace(k, v)
    for k, v in _DASHES.items():
        s = s.replace(k, v)
    # Whitespace variants become spaces before the strip so words don't fuse.
    s = re.sub(r"\s", " ", s)
    s = _KEEP.sub("", s)
    s = _WS.sub(" ", s).strip()
    return s
