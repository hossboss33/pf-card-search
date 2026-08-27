"""Canonical card keys. Spec §4.2 and Appendix A.

The key hashes the FULL BODY TEXT, never the highlighted/underlined
projections — markup differs team to team; the body is what's stable.
Analytics (no body) key on the normalized tag instead, in a separate
namespace so an analytic can never collide with an evidence card.
"""
from __future__ import annotations

import hashlib

from .normalize import NORM_V, normalize


def canonical_key(body_text: str, tag: str, is_analytic: bool) -> str:
    base = (
        f"{NORM_V}:analytic:{normalize(tag or '')}"
        if is_analytic
        else f"{NORM_V}:{normalize(body_text or '')}"
    )
    return hashlib.sha256(base.encode()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
