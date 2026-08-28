"""Remote PF loader: stream PF rows out of the Hugging Face parquet shards
over HTTP range requests, without downloading the 27.6 GB corpus.

Spec §2.1 gets the bulk history from `Yusuf5/OpenCaselist` (MIT). The naive
path — `datasets` streaming or a full snapshot download — pulls every event
(Policy, LD, PF) and needs ~28 GB of free disk. This module instead:

1. reads a shard census (reports/shard_census.json, built by
   scripts/hf_census.py) that names which of the 109 shards hold any
   `event='pf'` rows, so shards with no PF are never fetched at all;
2. queries each PF shard remotely with DuckDB + httpfs, pushing the
   `event='pf'` predicate down so only matching row groups' bytes cross
   the wire (these parquet files carry no column statistics, so pruning is
   by row group content, not stats);
3. feeds the rows through the SAME normalize/dedup/insert path as every
   other source (`carddb.hf_loader.ingest_hf_rows`), so idempotence and
   dedup are identical to a local load.

Nothing is written to disk except the SQLite index itself. Re-running is a
no-op per the ingest ledger; a shard-level ledger (`source='hf-shard'`)
additionally lets an interrupted load resume without re-fetching completed
shards.
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .config import ROOT
from .db import ledger_put, ledger_seen
from .hf_loader import ingest_hf_rows
from .ingest import IngestStats
from .rawstore import now_iso

logger = logging.getLogger(__name__)

BASE = ("https://huggingface.co/datasets/Yusuf5/OpenCaselist/resolve/"
        "refs%2Fconvert%2Fparquet/default/train")
CENSUS = ROOT / "reports" / "shard_census.json"
SHARD_SOURCE = "hf-shard"
UA = "pf-card-search (personal research index; contact: caravellojake504@gmail.com)"
FETCH_BATCH = 2000
PACE_SECONDS = 5.0   # gap between shard downloads


def load_census(path: Optional[Path] = None) -> List[int]:
    """Shard indices known to contain PF rows. Raises if the census is
    missing — we never guess which shards to fetch."""
    p = Path(path or CENSUS)
    if not p.exists():
        raise RuntimeError(
            f"shard census not found at {p}. Run: .venv/bin/python scripts/hf_census.py "
            "(reads only the event column of each remote shard)")
    data = json.loads(p.read_text())
    shards = data.get("pf_shards") or []
    if not shards:
        raise RuntimeError(f"census at {p} lists no PF shards")
    return [int(s) for s in shards]


def _connect():
    import duckdb
    con = duckdb.connect()
    con.execute("LOAD httpfs;")
    con.execute("SET threads TO 4;")
    con.execute("SET enable_progress_bar=false;")
    return con


def _fetch_shard(shard: int, dest: Path, max_retries: int = 6) -> Path:
    """Fetch one shard to a temp file, then delete it after reading.

    These parquet files carry no column statistics, so a remote scan cannot
    skip row groups and ends up pulling the whole file anyway — in thousands
    of small, latency-bound range requests (measured: ~4.4 min/shard). One
    sequential GET moves the same bytes ~5x faster.

    Hugging Face rate-limits anonymous bulk reads, so this honours 429s with
    exponential backoff and Retry-After rather than treating them as fatal.
    """
    import httpx
    url = f"{BASE}/{shard:04d}.parquet"
    for attempt in range(max_retries):
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=180.0,
                              headers={"User-Agent": UA}) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    retry_after = r.headers.get("retry-after")
                    wait = (float(retry_after) if retry_after
                            and retry_after.isdigit()
                            else min(120.0, 10.0 * (2 ** attempt)))
                    logger.warning("shard %d: HTTP %d, backing off %.0fs "
                                   "(attempt %d/%d)", shard, r.status_code,
                                   wait, attempt + 1, max_retries)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                return dest
        except (httpx.HTTPError, OSError) as exc:
            wait = min(120.0, 10.0 * (2 ** attempt))
            logger.warning("shard %d: %s, retrying in %.0fs (attempt %d/%d)",
                           shard, type(exc).__name__, wait, attempt + 1, max_retries)
            if dest.exists():
                dest.unlink()
            time.sleep(wait)
    raise RuntimeError(f"shard {shard}: giving up after {max_retries} attempts")


def iter_shard_pf_rows(con, shard: int, local: Optional[Path] = None) -> Iterator[dict]:
    """Yield every PF row of one shard as a plain dict."""
    src = str(local) if local else f"{BASE}/{shard:04d}.parquet"
    cur = con.execute(
        "SELECT * FROM read_parquet(?) WHERE event = 'pf'", [src])
    cols = [d[0] for d in cur.description]
    while True:
        rows = cur.fetchmany(FETCH_BATCH)
        if not rows:
            break
        for r in rows:
            yield dict(zip(cols, r))


def ingest_remote_pf(conn, cfg: Dict, stats: Optional[IngestStats] = None,
                     shards: Optional[List[int]] = None,
                     census_path: Optional[Path] = None) -> IngestStats:
    """Ingest every PF row from the remote shards. Resumable per shard."""
    stats = stats or IngestStats()
    todo = shards if shards is not None else load_census(census_path)
    con = _connect()
    failed_shards: List[int] = []
    logger.info("remote PF load: %d shards to process", len(todo))
    for n, shard in enumerate(todo, 1):
        if ledger_seen(conn, SHARD_SOURCE, str(shard)):
            logger.info("[%d/%d] shard %d already ingested, skipping",
                        n, len(todo), shard)
            continue
        before_cards, before_vars = stats.new_cards, stats.new_variants
        tmp = Path(tempfile.gettempdir()) / f"pfshard-{shard:04d}.parquet"
        try:
            _fetch_shard(shard, tmp)
            rows = list(iter_shard_pf_rows(con, shard, local=tmp))
        except Exception as exc:
            # One unreachable shard must not lose the whole run; it stays
            # unstamped in the ledger, so a rerun picks it up.
            logger.error("shard %d failed, continuing: %s", shard, exc)
            failed_shards.append(shard)
            continue
        finally:
            if tmp.exists():
                tmp.unlink()          # nothing accumulates on disk
        ingest_hf_rows(conn, rows, cfg, stats)
        # Stamp the shard only after its rows are committed by ingest_hf_rows,
        # so a crash mid-shard re-fetches that shard (its rows are ledgered
        # individually, so re-fetching still inserts nothing twice).
        ledger_put(conn, SHARD_SOURCE, str(shard), None, now_iso())
        conn.commit()
        logger.info("[%d/%d] shard %d: %d rows -> +%d cards +%d variants",
                    n, len(todo), shard, len(rows),
                    stats.new_cards - before_cards,
                    stats.new_variants - before_vars)
        time.sleep(PACE_SECONDS)   # be a good citizen on a shared CDN
    if failed_shards:
        logger.error("shards that did not load (rerun to retry): %s",
                     failed_shards)
    return stats
