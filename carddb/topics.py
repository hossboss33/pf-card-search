"""Topics: seed data, past/present/future status, round -> topic assignment.

Spec §6. The seed file (data/topics.json) is hand-maintained history of every
PF resolution back to 2013-14, with slot windows. Status (past/present/future)
is always computed from dates, never stored (§6.3). Assignment follows §6.2's
order exactly: season narrows -> round_date in a slot window -> tournament
overrides -> distinctive-keyword fallback -> NULL (never silently guess).
After assignment, cards.topic_ids is materialized as a sorted JSON array of
topic codes across each card's variants' rounds.

Slot codes: 'SO','ND','JAN','FEB','MA','NATS' plus the monthly-era slots
'SEP','OCT','NOV','DEC','APR'. PF's cadence changed over the years: Sep/Oct
has been a combined topic since 2013-14, November and December were separate
monthly topics through 2017-18 and combined ('ND') from 2018-19 on, and March
('MA') and April ('APR') remain separate topics. code = '<season>-<slot>'.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

VALID_SLOTS = {
    "SO", "ND", "JAN", "FEB", "MA", "NATS",   # modern cadence
    "SEP", "OCT", "NOV", "DEC", "APR",         # monthly-era extras
}

_OVERRIDES_DDL = """
CREATE TABLE IF NOT EXISTS topic_overrides (
  match TEXT PRIMARY KEY,   -- lowercased substring matched against rounds.tournament
  slot TEXT,                -- resolve within the round's season (e.g. 'NATS'), or
  code TEXT                 -- pin an exact topic code (e.g. '2024-NATS')
);
"""


# --- seed data ------------------------------------------------------------

def load_topics(conn: sqlite3.Connection, topics_json_path) -> int:
    """Upsert data/topics.json into the topics table. Returns rows upserted.

    The file is an object: {"_notes": ..., "topics": [...], "overrides": [...]}
    (a bare list of topic rows is also accepted). Overrides are stored in the
    module-owned topic_overrides table (same pattern as hf_buckets).
    """
    raw = json.loads(Path(topics_json_path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        rows, overrides = raw, []
    else:
        rows = raw.get("topics", [])
        overrides = raw.get("overrides", [])

    n = 0
    for row in rows:
        code = row.get("code")
        if not code:
            continue
        conn.execute(
            "INSERT INTO topics (season, slot, code, resolution, starts, ends, source_url) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET season=excluded.season, "
            " slot=excluded.slot, resolution=excluded.resolution, "
            " starts=excluded.starts, ends=excluded.ends, "
            " source_url=excluded.source_url",
            (row.get("season"), row.get("slot"), code, row.get("resolution"),
             row.get("starts"), row.get("ends"), row.get("source_url")),
        )
        n += 1

    conn.executescript(_OVERRIDES_DDL)
    conn.execute("DELETE FROM topic_overrides")
    for ov in overrides:
        m = (ov.get("match") or "").strip().lower()
        if not m:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO topic_overrides (match, slot, code) VALUES (?,?,?)",
            (m, ov.get("slot"), ov.get("code")),
        )
    conn.commit()
    return n


# --- status (§6.3: computed, never stored) --------------------------------

def _parse_iso(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def topic_status(topic_row, today: date) -> str:
    """'past' | 'present' | 'future' from the row's starts/ends windows."""
    starts = _parse_iso(topic_row["starts"])
    ends = _parse_iso(topic_row["ends"])
    if ends is not None and ends < today:
        return "past"
    if starts is not None and starts > today:
        return "future"
    if starts is None and ends is None:
        return "future"  # announced but no window yet
    return "present"


def resolve_topic_token(conn: sqlite3.Connection, token: str,
                        today: date) -> List[int]:
    """Resolve 'past'/'present'/'future' or an exact code to topic ids."""
    tok = (token or "").strip()
    low = tok.lower()
    if low in ("past", "present", "future"):
        ids = []
        for row in conn.execute("SELECT id, starts, ends FROM topics"):
            if topic_status(row, today) == low:
                ids.append(row["id"])
        return sorted(ids)
    row = conn.execute(
        "SELECT id FROM topics WHERE UPPER(code) = UPPER(?)", (tok,)
    ).fetchone()
    return [row["id"]] if row else []


def current_topic(conn: sqlite3.Connection, today) -> Optional[sqlite3.Row]:
    """The present-slot topic (starts <= today <= ends), or None."""
    d = today or date.today()
    return conn.execute(
        "SELECT * FROM topics "
        "WHERE starts IS NOT NULL AND ends IS NOT NULL "
        " AND date(starts) <= date(?) AND date(?) <= date(ends) "
        "ORDER BY starts DESC LIMIT 1",
        (d.isoformat(), d.isoformat()),
    ).fetchone()


# --- assignment (§6.2) ----------------------------------------------------

@dataclass
class AssignStats:
    rounds: int = 0
    by_date: int = 0
    by_override: int = 0
    by_keyword: int = 0
    unassigned: int = 0
    cards_materialized: int = 0

    def __str__(self) -> str:
        return (f"rounds={self.rounds} by_date={self.by_date} "
                f"by_override={self.by_override} by_keyword={self.by_keyword} "
                f"unassigned={self.unassigned} "
                f"cards_with_topics={self.cards_materialized}")


_WORD = re.compile(r"[a-z0-9]+")

# Boilerplate that recurs across resolutions; within-season distinctiveness
# already strips season-shared words, this catches the generic rest.
_STOPWORDS = {
    "resolved", "the", "and", "for", "its", "that", "with", "should", "ought",
    "united", "states", "state", "federal", "government", "governments",
    "their", "more", "than", "from", "into", "over", "all", "are", "not",
    "has", "have", "had", "would", "will", "being", "been", "when", "was",
    "were", "this", "these", "those", "one", "two", "out", "our", "can",
    "could", "may", "might", "must", "shall", "benefits", "harms", "outweigh",
    "balance", "substantially", "increase", "decrease", "reduce", "between",
    "against", "about", "without", "within",
}


def _tokens(text: str) -> Set[str]:
    """Lowercased word set with a crude plural fold and stopword strip."""
    out = set()
    for w in _WORD.findall((text or "").lower()):
        if len(w) < 3 or w in _STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def _distinctive_keywords(topics: List[dict]) -> Dict[int, Set[str]]:
    """Per topic id: resolution tokens not shared with any other resolution
    in the same season. These are what the keyword fallback matches on."""
    toks = {t["id"]: _tokens(t["resolution"] or "") for t in topics}
    out = {}
    for t in topics:
        others: Set[str] = set()
        for u in topics:
            if u["id"] != t["id"]:
                others |= toks[u["id"]]
        out[t["id"]] = toks[t["id"]] - others
    return out


def _chunks(seq: Sequence, n: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def assign_topics(conn: sqlite3.Connection,
                  today: Optional[date] = None) -> AssignStats:
    """Assign rounds.topic_id per §6.2, then materialize cards.topic_ids.

    Deterministic full rebuild on every run (idempotent): every round is
    re-derived, every card's topic_ids list is rewritten.
    """
    stats = AssignStats()

    topics = [dict(r) for r in conn.execute(
        "SELECT id, code, season, slot, resolution, starts, ends FROM topics")]
    for t in topics:
        t["_starts"] = _parse_iso(t["starts"])
        t["_ends"] = _parse_iso(t["ends"])
    by_season: Dict[Any, List[dict]] = {}
    for t in topics:
        by_season.setdefault(t["season"], []).append(t)
    by_code = {(t["code"] or "").upper(): t["id"] for t in topics}
    by_season_slot = {(t["season"], t["slot"]): t["id"] for t in topics}
    keywords = {}
    for season_topics in by_season.values():
        keywords.update(_distinctive_keywords(season_topics))

    conn.executescript(_OVERRIDES_DDL)
    overrides = [dict(r) for r in conn.execute(
        "SELECT match, slot, code FROM topic_overrides")]
    overrides.sort(key=lambda o: -len(o["match"] or ""))  # most specific first

    rounds = conn.execute(
        "SELECT r.id, r.round_date, r.tournament, cl.season AS season "
        "FROM rounds r "
        "LEFT JOIN teams t ON t.id = r.team_id "
        "LEFT JOIN schools s ON s.id = t.school_id "
        "LEFT JOIN caselists cl ON cl.id = s.caselist_id"
    ).fetchall()
    stats.rounds = len(rounds)

    assigned: Dict[int, Optional[int]] = {}
    fallback_rounds: List[Tuple[int, Any]] = []  # (round_id, season)

    for r in rounds:
        season = r["season"]
        # 1. season narrows the candidate topics
        candidates = by_season.get(season, topics if season is None else [])

        # 2. round_date inside a slot window
        d = _parse_iso(r["round_date"])
        topic_id = None
        if d is not None:
            hits = [t["id"] for t in candidates
                    if t["_starts"] is not None and t["_ends"] is not None
                    and t["_starts"] <= d <= t["_ends"]]
            if len(hits) == 1:
                topic_id = hits[0]
                stats.by_date += 1
        # 3. tournament overrides
        if topic_id is None and r["tournament"]:
            tname = r["tournament"].lower()
            for ov in overrides:
                if ov["match"] and ov["match"] in tname:
                    if ov["code"]:
                        topic_id = by_code.get(ov["code"].upper())
                    elif ov["slot"] and season is not None:
                        topic_id = by_season_slot.get((season, ov["slot"]))
                    if topic_id is not None:
                        stats.by_override += 1
                        break
        # 4. keyword fallback needs the round's variants; defer it
        if topic_id is None and season is not None and season in by_season:
            fallback_rounds.append((r["id"], season))
            continue
        assigned[r["id"]] = topic_id

    # 4. distinctive-keyword fallback against that season's resolutions,
    #    matched on the pockets/hats/blocks of the round's variants.
    if fallback_rounds:
        texts: Dict[int, List[str]] = {rid: [] for rid, _ in fallback_rounds}
        ids = list(texts.keys())
        for chunk in _chunks(ids, 500):
            q = ",".join("?" * len(chunk))
            for v in conn.execute(
                f"SELECT round_id, pocket, hat, block FROM card_variants "
                f"WHERE round_id IN ({q})", tuple(chunk)):
                texts[v["round_id"]].extend(
                    x for x in (v["pocket"], v["hat"], v["block"]) if x)
        for rid, season in fallback_rounds:
            toks = _tokens(" ".join(texts[rid]))
            best_id, best_hits, tied = None, 0, False
            for t in by_season[season]:
                hits = len(keywords.get(t["id"], set()) & toks)
                if hits > best_hits:
                    best_id, best_hits, tied = t["id"], hits, False
                elif hits == best_hits and hits > 0:
                    tied = True
            if best_id is not None and best_hits > 0 and not tied:
                assigned[rid] = best_id
                stats.by_keyword += 1
            else:
                assigned[rid] = None  # 5. never silently guess

    stats.unassigned = sum(1 for v in assigned.values() if v is None)
    conn.executemany("UPDATE rounds SET topic_id = ? WHERE id = ?",
                     [(tid, rid) for rid, tid in assigned.items()])

    stats.cards_materialized = materialize_topic_ids(conn)
    conn.commit()
    return stats


def materialize_topic_ids(conn: sqlite3.Connection) -> int:
    """Rebuild cards.topic_ids for every card: sorted JSON array of the topic
    codes across the card's variants' rounds. Returns cards with >=1 topic."""
    codes: Dict[int, Set[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT v.card_id AS card_id, t.code AS code "
        "FROM card_variants v "
        "JOIN rounds r ON r.id = v.round_id "
        "JOIN topics t ON t.id = r.topic_id "
        "WHERE v.card_id IS NOT NULL AND t.code IS NOT NULL"):
        codes.setdefault(row["card_id"], set()).add(row["code"])
    conn.execute("UPDATE cards SET topic_ids = '[]'")
    conn.executemany(
        "UPDATE cards SET topic_ids = ? WHERE id = ?",
        [(json.dumps(sorted(v)), k) for k, v in codes.items()])
    return len(codes)
