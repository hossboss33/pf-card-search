"""Season backfill from openCaselist's own weekly zip archives.

Crawling a season round by round means thousands of requests held to one per
second (spec §0.2), which is slow for you and rude to a volunteer-run service.
openCaselist publishes the same content as a weekly zip per caselist, and
their own docs call this the archive offering (spec §12.3). One authenticated
listing call plus one file transfer replaces the entire crawl.

The split, from `docs/api_access.md`: the *listing*
(`GET /caselists/{caselist}/downloads`) needs the session cookie; the zip URLs
it returns are plain Backblaze links with no signature or expiry, on a
different host, and openCaselist's own front end fetches them with no
credentials. So this asks the API for the listing — never guessing a URL — and
then downloads what the API told it about.

Provenance comes from the paths inside the zip: entries look like
``<school>/<team>/<file>.docx``, which is how the wiki organises uploads. When
a path does not carry that shape the file is still ingested, with the school
and team left unknown rather than invented.
"""
from __future__ import annotations

import io
import logging
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .db import ledger_put, ledger_seen
from .ingest import (IngestStats, attach_variant, finish_batch,
                     get_or_create_caselist, get_or_create_round,
                     get_or_create_school, get_or_create_team, insert_card)
from .keys import sha256_bytes
from .rawstore import now_iso, record_document, store_bytes

logger = logging.getLogger("carddb.bulk_sync")

BULK_SOURCE = "bulk-zip"
PARSEABLE = (".docx", ".doc", ".pdf")

# Archive names look like <caselist>-all-<date>.zip / <caselist>-weekly-<date>.zip.
# "all" is the full season; "weekly" is only that week's changes.
_ALL_RE = re.compile(r"-all-(\d{4}-?\d{2}-?\d{2})", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4}-?\d{2}-?\d{2})")


def choose_archive(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The newest full-season archive, else the newest archive of any kind.

    Prefers ``-all-`` over ``-weekly-``: a weekly zip holds one week of
    changes, so backfilling from it would silently miss the season.
    """
    if not files:
        return None
    def key(f):
        name = str(f.get("name") or "")
        m = _DATE_RE.search(name)
        return (m.group(1).replace("-", "") if m else "")
    full = [f for f in files if _ALL_RE.search(str(f.get("name") or ""))]
    pool = full or list(files)
    return sorted(pool, key=key, reverse=True)[0]


def school_team_from_path(name: str) -> Tuple[Optional[str], Optional[str]]:
    """(school, team) from a zip entry path, or (None, None) if it lacks them.

    Never invents: a flat archive yields unknowns rather than a guessed school.
    """
    parts = [p for p in posixpath.normpath(name).split("/") if p not in ("", ".", "..")]
    if len(parts) >= 3:
        return parts[-3], parts[-2]
    if len(parts) == 2:
        return parts[0], None
    return None, None


def list_archives(client, limiter, max_retries, api_base, endpoints,
                  caselist: str) -> List[Dict[str, Any]]:
    """Ask the API which archives exist. Never construct these URLs."""
    from .api_sync import _get_json, _url
    url = _url(api_base, endpoints, "bulk_downloads", {"caselist": caselist})
    rows, _blob, _final = _get_json(client, limiter, max_retries, url)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("url")]


def fetch_archive(client, limiter, url: str, dest: Path,
                  max_retries: int = 5) -> Path:
    """Stream one archive to disk. The zip host is not the API host and has no
    per-minute limit, but this still goes through the limiter so a season
    backfill cannot turn into a burst."""
    from .ratelimit import request_with_backoff
    limiter.wait()
    with client.stream("GET", url, follow_redirects=True, timeout=300.0) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    return dest


def ingest_archive(conn, cfg: Dict[str, Any], caselist: str, zip_path: Path,
                   stats: IngestStats, raw_root: Path,
                   season: Optional[int] = None,
                   display_name: Optional[str] = None) -> IngestStats:
    """Parse every card file in the archive and ingest it.

    One file at a time, and a file that will not parse is recorded and skipped
    — a season must not be lost to one corrupt document (spec §3.4).
    """
    from .docx_parser import ParseFailure, parse_docx_bytes
    try:
        from .pdf_parser import parse_pdf_bytes
    except Exception:          # pragma: no cover - pdf support is optional
        parse_pdf_bytes = None

    caselist_id = get_or_create_caselist(
        conn, caselist, display_name=display_name or caselist,
        season=season, event="pf")

    with zipfile.ZipFile(str(zip_path)) as zf:
        names = [n for n in zf.namelist()
                 if n.lower().endswith(PARSEABLE) and not n.endswith("/")]
        logger.info("archive %s: %d card files", zip_path.name, len(names))
        for n, name in enumerate(names, 1):
            try:
                data = zf.read(name)
            except Exception as exc:
                logger.warning("%s: unreadable in archive (%s)", name, exc)
                stats.failed += 1
                continue

            sha = sha256_bytes(data)
            stats.units_seen += 1
            if ledger_seen(conn, BULK_SOURCE, sha, sha):
                stats.units_skipped += 1
                continue

            sha_stored, local = store_bytes(raw_root, data)
            doc_id = record_document(conn, sha_stored, "bulk-zip",
                                     origin_url=None,
                                     orig_filename=posixpath.basename(name),
                                     local_path=str(local))

            school_name, team_name = school_team_from_path(name)
            round_id = None
            if school_name:
                school_id = get_or_create_school(conn, caselist_id, school_name)
                if team_name:
                    team_id = get_or_create_team(conn, school_id, team_name)
                    # The archive carries files, not round metadata; one
                    # synthetic round per file keeps provenance attached
                    # without inventing tournaments or sides.
                    round_id = get_or_create_round(
                        conn, team_id, "zip-%s" % sha_stored[:16])

            lower = name.lower()
            try:
                if lower.endswith(".pdf"):
                    if parse_pdf_bytes is None:
                        raise ParseFailure("pdf support unavailable")
                    parsed = parse_pdf_bytes(data, filename=name)
                    fidelity = "pdf"
                else:
                    parsed = parse_docx_bytes(data, filename=name)
                    fidelity = "opensource"
            except ParseFailure as exc:
                conn.execute(
                    "UPDATE documents SET parse_status='failed', "
                    "parse_error=?, parsed_at=? WHERE id=?",
                    (str(exc)[:400], now_iso(), doc_id))
                stats.failed += 1
                ledger_put(conn, BULK_SOURCE, sha, sha, now_iso())
                conn.commit()
                continue
            except Exception as exc:     # never lose the season to one file
                conn.execute(
                    "UPDATE documents SET parse_status='failed', "
                    "parse_error=?, parsed_at=? WHERE id=?",
                    ("%s: %s" % (type(exc).__name__, exc), now_iso(), doc_id))
                stats.failed += 1
                ledger_put(conn, BULK_SOURCE, sha, sha, now_iso())
                conn.commit()
                continue

            for rec in parsed.cards:
                rec.fidelity = fidelity
                card_id, created = insert_card(conn, rec)
                _vid, vcreated = attach_variant(conn, card_id, rec, doc_id,
                                                round_id)
                stats.new_cards += int(created)
                stats.new_variants += int(vcreated)
                stats.touched_card_ids.add(card_id)

            conn.execute(
                "UPDATE documents SET parse_status='ok', parsed_at=? WHERE id=?",
                (now_iso(), doc_id))
            ledger_put(conn, BULK_SOURCE, sha, sha, now_iso())
            stats.parsed += 1
            if n % 50 == 0:
                finish_batch(conn, stats)
                logger.info("archive %s: %d/%d files, %s",
                            zip_path.name, n, len(names), stats.summary())

    finish_batch(conn, stats)
    return stats


def sync_caselist_bulk(conn, cfg: Dict[str, Any], client, limiter,
                       max_retries: int, api_base: str,
                       endpoints: Dict[str, Any], caselist: str,
                       stats: IngestStats, raw_root: Path,
                       season: Optional[int] = None,
                       display_name: Optional[str] = None) -> bool:
    """Backfill one caselist from its newest full archive.

    Returns False when the API lists no archive for it, so the caller can fall
    back to the per-round crawl.
    """
    files = list_archives(client, limiter, max_retries, api_base, endpoints,
                          caselist)
    chosen = choose_archive(files)
    if not chosen:
        logger.info("%s: no bulk archive listed; falling back to crawling",
                    caselist)
        return False
    logger.info("%s: downloading archive %s", caselist, chosen.get("name"))
    tmp = Path(tempfile.gettempdir()) / ("carddb-%s.zip" % caselist)
    try:
        fetch_archive(client, limiter, str(chosen["url"]), tmp,
                      max_retries=max_retries)
        if not zipfile.is_zipfile(str(tmp)):
            logger.warning("%s: downloaded archive is not a zip; falling back",
                           caselist)
            return False
        ingest_archive(conn, cfg, caselist, tmp, stats, raw_root,
                       season=season, display_name=display_name)
    finally:
        if tmp.exists():
            tmp.unlink()      # nothing accumulates on disk
    return True
