"""Near-duplicate detection and merging. Spec §4.3–4.4, Appendix A.

Layer 3 of dedup: a batch job, never inline with ingest. Candidate
generation is MinHash (num_perm=128) over 5-token shingles of
normalize(body_text), bucketed by LSH with bands b=8, rows r=16
(similarity knee ≈ 0.88). Verification requires BOTH:

  1. true Jaccard >= 0.90, or containment >= 0.95 in one direction
     (the trimmed-card case: the LONGER text survives and the shorter
     is linked with relation='trim'), and
  2. compatible cites: the 2-digit cite years are equal (never merge
     across different years — two cards quoting different articles can
     share long boilerplate) and the author token sets overlap.

Merging repoints card_variants at the survivor, records the merge in
card_merges (reversible: the absorbed canonical_key is kept; older rows
whose survivor is now absorbed are path-compressed to the new survivor),
moves hf_buckets rows when that table exists, moves box memberships and
cite-health onto the survivor, deletes the absorbed cards row and its
card_fts row, then refreshes FTS + aggregates + topic_ids for survivors.
Re-running after convergence merges nothing: absorbed cards are gone
and surviving pairs all failed verification.

Analytics (is_analytic=1) and empty/too-short bodies are excluded from
near-dup entirely — an analytic has no body to shingle, and two empty
shingle sets would look identical to MinHash.

Cross-check (spec §4.3): when the hf_buckets(card_id, bucket_id) table
exists and is non-empty, report_dir/dedup_disagreements.tsv lists
(a) pairs sharing a bucket_id that we did NOT merge, and (b) pairs we
merged whose bucket_ids differ. Tune thresholds from a hand audit of
that file, not from vibes.
"""
from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from datasketch import MinHash, MinHashLSH

from .db import fts_upsert_cards, recompute_aggregates
from .normalize import normalize
from .rawstore import now_iso

# Appendix A parameters, exact. Do not tune without re-reading §4.3.
NUM_PERM = 128
SHINGLE_K = 5
LSH_BANDS = 8
LSH_ROWS = 16
JACCARD_MIN = 0.90
CONTAINMENT_MIN = 0.95

REPORT_NAME = "dedup_disagreements.tsv"


@dataclass
class DedupStats:
    candidates: int = 0      # distinct LSH candidate pairs examined
    merged: int = 0          # merges performed (trims included)
    trims: int = 0           # merges with relation='trim'
    disagreements: int = 0   # rows written to the disagreement report


# --- Short-cite parsing ----------------------------------------------------
# Short cites look like: "Diamond '13", "Rodgers and Cooper 06",
# "Smith et al. 24", "Kessler '26", occasionally a 4-digit "Kessler 2026".

_YEAR_TOKEN = re.compile(r"(?<![0-9A-Za-z])['’]?([0-9]{4}|[0-9]{2})(?![0-9A-Za-z])")
_AUTHOR_STOP = {"and", "et", "al"}


def cite_year(cite: Optional[str]) -> Optional[str]:
    """The 2-digit year from a short cite, or None.

    Scans right-to-left: the year is conventionally the last token.
    4-digit years are accepted when they look like years (19xx/20xx)
    and reduced to their last two digits.
    """
    if not cite:
        return None
    for m in reversed(list(_YEAR_TOKEN.finditer(cite))):
        tok = m.group(1)
        if len(tok) == 2:
            return tok
        if tok[:2] in ("19", "20"):
            return tok[2:]
    return None


def author_tokens(cite: Optional[str]) -> Set[str]:
    """Normalized author-name tokens from a short cite.

    Uses the frozen §3.5 normalizer, then drops connective words
    ('and', 'et', 'al') and purely numeric tokens (years).
    """
    if not cite:
        return set()
    out: Set[str] = set()
    for tok in normalize(cite).split():
        if tok in _AUTHOR_STOP or tok.isdigit():
            continue
        out.add(tok)
    return out


def _cites_compatible(cite_a: Optional[str], cite_b: Optional[str]) -> bool:
    """Both conditions of §4.3 rule 2. Missing years are incompatible:
    if we cannot prove the years match, we never merge."""
    ya, yb = cite_year(cite_a), cite_year(cite_b)
    if ya is None or yb is None or ya != yb:
        return False
    return bool(author_tokens(cite_a) & author_tokens(cite_b))


# --- Shingling -------------------------------------------------------------

def _shingle_hashes(norm_text: str) -> FrozenSet[bytes]:
    """5-token shingles of already-normalized text, as stable 8-byte
    digests (cheap to store, deterministic across processes). Fewer than
    5 tokens yields the empty set and the card is excluded upstream."""
    toks = norm_text.split()
    if len(toks) < SHINGLE_K:
        return frozenset()
    out = set()
    for i in range(len(toks) - SHINGLE_K + 1):
        sh = " ".join(toks[i:i + SHINGLE_K])
        out.add(hashlib.blake2b(sh.encode("utf-8"), digest_size=8).digest())
    return frozenset(out)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


# --- Merge mechanics -------------------------------------------------------

def _pick_survivor(a: int, b: int, token_counts: Dict[int, int],
                   body_lens: Dict[int, int]) -> "Tuple[int, int]":
    """(survivor, absorbed). The longer text survives (mandatory for
    trims, harmless for dups); ties break to raw body length, then to
    the smaller (older) card id for determinism."""
    ka = (token_counts[a], body_lens[a], -a)
    kb = (token_counts[b], body_lens[b], -b)
    return (a, b) if ka >= kb else (b, a)


def _merge(conn: sqlite3.Connection, survivor_id: int, absorbed_id: int,
           relation: str, move_hf: bool) -> None:
    row = conn.execute(
        "SELECT canonical_key FROM cards WHERE id = ?", (absorbed_id,)
    ).fetchone()
    absorbed_key = row["canonical_key"] if row else None
    conn.execute(
        "UPDATE card_variants SET card_id = ? WHERE card_id = ?",
        (survivor_id, absorbed_id),
    )
    conn.execute(
        "INSERT INTO card_merges (survivor_id, absorbed_key, relation, merged_at) "
        "VALUES (?,?,?,?)",
        (survivor_id, absorbed_key, relation, now_iso()),
    )
    # Path compression: earlier merges that pointed at the card we are now
    # absorbing must be repointed at the new survivor, so every absorbed_key
    # in card_merges always resolves to a LIVE card in one hop (ingest
    # consults this table to keep absorbed keys from resurrecting).
    conn.execute(
        "UPDATE card_merges SET survivor_id = ? WHERE survivor_id = ?",
        (survivor_id, absorbed_id),
    )
    if move_hf:
        # The survivor may already hold the same bucket_id (the routine
        # agreement case); idx_hf_buckets(card_id, bucket_id) is UNIQUE, so
        # repoint what we can and drop the now-duplicate leftovers.
        conn.execute(
            "UPDATE OR IGNORE hf_buckets SET card_id = ? WHERE card_id = ?",
            (survivor_id, absorbed_id),
        )
        conn.execute("DELETE FROM hf_buckets WHERE card_id = ?", (absorbed_id,))
    # Rows referencing the absorbed card (FK to cards.id) move to the
    # survivor before the DELETE, or PRAGMA foreign_keys=ON rejects it.
    # Box memberships: keep one membership per (box, card).
    conn.execute(
        "INSERT OR IGNORE INTO card_box_members (box_id, card_id, note, added_at) "
        "SELECT box_id, ?, note, added_at FROM card_box_members WHERE card_id = ?",
        (survivor_id, absorbed_id),
    )
    conn.execute(
        "DELETE FROM card_box_members WHERE card_id = ?", (absorbed_id,)
    )
    # Cite health (PRIMARY KEY card_id): keep the survivor's own row when it
    # has one, otherwise inherit the absorbed card's latest check.
    conn.execute(
        "UPDATE OR IGNORE cite_health SET card_id = ? WHERE card_id = ?",
        (survivor_id, absorbed_id),
    )
    conn.execute("DELETE FROM cite_health WHERE card_id = ?", (absorbed_id,))
    conn.execute("DELETE FROM cards WHERE id = ?", (absorbed_id,))
    conn.execute("DELETE FROM card_fts WHERE rowid = ?", (absorbed_id,))


# --- The batch job ---------------------------------------------------------

def run_dedup(conn: sqlite3.Connection, report_dir: Path, seed: int = 0) -> DedupStats:
    """One near-dup pass over all canonical evidence cards. Idempotent:
    a second run after convergence merges 0."""
    stats = DedupStats()

    # Universe: evidence cards with a shingleable body. Analytics and
    # empty bodies are excluded from near-dup entirely (spec §4.3).
    shingles: Dict[int, FrozenSet[bytes]] = {}
    token_counts: Dict[int, int] = {}
    body_lens: Dict[int, int] = {}
    cites: Dict[int, Optional[str]] = {}
    for r in conn.execute(
        "SELECT id, cite, body_text FROM cards "
        "WHERE is_analytic = 0 AND body_text IS NOT NULL AND body_text != ''"
    ):
        norm = normalize(r["body_text"])
        sh = _shingle_hashes(norm)
        if not sh:
            continue
        cid = r["id"]
        shingles[cid] = sh
        token_counts[cid] = len(norm.split())
        body_lens[cid] = len(r["body_text"])
        cites[cid] = r["cite"]

    # Pre-merge snapshot of the HF bucket assignments for the cross-check.
    has_hf = _table_exists(conn, "hf_buckets")
    bucket_map: Dict[int, Set[str]] = {}
    if has_hf:
        for r in conn.execute("SELECT card_id, bucket_id FROM hf_buckets"):
            if r["card_id"] is not None and r["bucket_id"] is not None:
                bucket_map.setdefault(r["card_id"], set()).add(str(r["bucket_id"]))

    # Candidate generation: MinHash + LSH, deterministic via fixed seed
    # and sorted insertion/query order.
    lsh = MinHashLSH(num_perm=NUM_PERM, params=(LSH_BANDS, LSH_ROWS))
    minhashes: Dict[int, MinHash] = {}
    for cid in sorted(shingles):
        m = MinHash(num_perm=NUM_PERM, seed=seed)
        m.update_batch(list(shingles[cid]))
        minhashes[cid] = m
        lsh.insert(cid, m)

    pairs: Set[Tuple[int, int]] = set()
    for cid in sorted(shingles):
        for key in lsh.query(minhashes[cid]):
            if key != cid:
                pairs.add((min(cid, key), max(cid, key)))
    stats.candidates = len(pairs)

    # Verification + merging. Chains resolve through a union-find so a
    # card absorbed earlier in the pass never resurfaces; thresholds are
    # re-checked against the resolved representatives' shingle sets.
    parent: Dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while root in parent:
            root = parent[root]
        while x != root:            # path compression
            parent[x], x = root, parent[x]
        return root

    merge_events: List[Tuple[int, int, str]] = []  # (survivor, absorbed, relation)
    for a, b in sorted(pairs):
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        sa, sb = shingles[ra], shingles[rb]
        inter = len(sa & sb)
        if inter == 0:
            continue
        union = len(sa) + len(sb) - inter
        jaccard = inter / union
        containment = inter / min(len(sa), len(sb))
        if jaccard >= JACCARD_MIN:
            relation = "dup"
        elif containment >= CONTAINMENT_MIN:
            relation = "trim"
        else:
            continue
        if not _cites_compatible(cites.get(ra), cites.get(rb)):
            continue
        survivor, absorbed = _pick_survivor(ra, rb, token_counts, body_lens)
        _merge(conn, survivor, absorbed, relation, has_hf)
        parent[absorbed] = survivor
        merge_events.append((survivor, absorbed, relation))
        stats.merged += 1
        if relation == "trim":
            stats.trims += 1

    if merge_events:
        final_survivors = {find(s) for s, _, _ in merge_events}
        fts_upsert_cards(conn, final_survivors)
        recompute_aggregates(conn, final_survivors)
        # Survivors inherited variants (and thus rounds/topics) from the
        # cards they absorbed; refresh their materialized topic_ids now
        # rather than waiting for the next `carddb topics assign`. Lazy
        # import keeps dedup free of a topics dependency at module load.
        if _table_exists(conn, "topics"):
            from .topics import materialize_topic_ids
            materialize_topic_ids(conn, final_survivors)
    conn.commit()

    # Disagreement report (spec §4.3 cross-check), only when the HF
    # bucket signal is actually present.
    if has_hf and bucket_map:
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        rows_out: List[Tuple[str, int, int, str, str]] = []

        # (a) pairs sharing a bucket_id that we did NOT merge.
        by_bucket: Dict[str, Set[int]] = {}
        for cid, buckets in bucket_map.items():
            for bkt in buckets:
                by_bucket.setdefault(bkt, set()).add(cid)
        emitted: Set[Tuple[int, int]] = set()
        for bkt in sorted(by_bucket):
            members = sorted(by_bucket[bkt])
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    x, y = members[i], members[j]
                    if find(x) == find(y):
                        continue  # we agreed; both landed on one card
                    if (x, y) in emitted:
                        continue
                    emitted.add((x, y))
                    rows_out.append(("bucket_shared_not_merged", x, y, bkt, bkt))

        # (b) pairs we merged whose bucket_ids differ.
        for survivor, absorbed, _relation in merge_events:
            bs = bucket_map.get(survivor, set())
            ba = bucket_map.get(absorbed, set())
            if bs and ba and not (bs & ba):
                rows_out.append((
                    "merged_different_buckets", survivor, absorbed,
                    ";".join(sorted(bs)), ";".join(sorted(ba)),
                ))

        with open(report_dir / REPORT_NAME, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t", lineterminator="\n")
            w.writerow(["kind", "card_id_a", "card_id_b", "buckets_a", "buckets_b"])
            for row in rows_out:
                w.writerow(row)
        stats.disagreements = len(rows_out)

    return stats
