"""Content-addressed raw store. Spec §2.3.

Everything fetched (API JSON, .docx bytes) lands at
data/raw/<sha256[0:2]>/<sha256> plus a `documents` row. Re-parsing must
never require re-downloading.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .keys import sha256_bytes


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_bytes(raw_root: Path, data: bytes) -> "tuple[str, Path]":
    sha = sha256_bytes(data)
    d = raw_root / sha[:2]
    d.mkdir(parents=True, exist_ok=True)
    path = d / sha
    if not path.exists():
        tmp = d / (sha + ".tmp")
        tmp.write_bytes(data)
        tmp.rename(path)  # atomic: readers never see partial files
    return sha, path


def record_document(conn: sqlite3.Connection, sha256: str, origin: str,
                    origin_url: Optional[str], orig_filename: Optional[str],
                    local_path: Optional[str]) -> int:
    """Insert (or find) the documents row for a stored blob. Returns id."""
    conn.execute(
        "INSERT INTO documents (sha256, origin, origin_url, orig_filename, local_path, fetched_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING",
        (sha256, origin, origin_url, orig_filename, local_path, now_iso()),
    )
    row = conn.execute("SELECT id FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
    return row["id"]
