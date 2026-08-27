"""Search execution over the FTS index + SQL filters. Spec §7.

Ranking (§7.1): bm25(card_fts, 5.0, 3.0, 2.0, 1.0) — tag ≫ cite > block >
body. One result row per canonical card.

Analytics policy (§1.3): analytics (Heading-4 blocks with no evidence body)
are EXCLUDED from results by default. The `is:analytic` operator flips the
search to analytics-ONLY — there is no mode that mixes the two, matching
the spec's "exclude them from card counts by default".

Fielded filters are SQL predicates (EXISTS subqueries over
card_variants/rounds/teams/schools/caselists), never FTS text:

  - topic:  tokens ('present'|'past'|'future'|code) resolve to topic ids via
    carddb.topics.resolve_topic_token when that module is available; a
    built-in fallback with the same contract semantics is used otherwise so
    search works before the topics module lands. An unresolvable token
    matches nothing (0 results) rather than erroring.
  - year:   compares the 2-digit year parsed from cards.cite via a
    registered SQL function (pf_cite_year), so LIMIT/OFFSET paginate
    correctly with the filter applied in the WHERE clause.
  - before:/after: ISO string compare on cards.source_pub_date. Bare-year
    (and year-month) values are handled sanely: a card's partial date is
    padded to its earliest possible day for `before` and its latest
    possible day for `after`, i.e. a card qualifies when its possible date
    range could satisfy the constraint. Cards with no pub date never match
    a before:/after: filter.
  - min_reads: cards.team_count (materialized by recompute_aggregates).
  - status: applied only when a prep_status(card_id, status) table exists
    (feature 9.20); silently inert otherwise.

Sorts: relevance = bm25 (falls back to reads for pure filtered listings,
where there is no MATCH to rank); reads = team_count DESC; recent =
max(round_date) over the card's disclosures DESC; length = body_len DESC.
All sorts tie-break on card id so pagination is deterministic.

Queries with no FTS terms but with filters (e.g. 'topic:present sort:reads')
run as pure filtered listings without MATCH. Exclusion-only queries
('-crypto') apply the exclusions as a NOT IN subquery over card_fts.

Snippets: snippet(card_fts, 3, '<b>', '</b>', '…', 24) on the body column.
Snippet, tag, and cite are returned RAW — HTML-escaping is the server
layer's job (it must escape text while preserving the <b> markers), not
this module's.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .query import ParsedQuery, parse_query

BM25 = "bm25(card_fts, 5.0, 3.0, 2.0, 1.0)"
SNIPPET_SQL = "snippet(card_fts, 3, '<b>', '</b>', '…', 24)"

# Pad a stored partial pub date to the edge of its possible range.
_PAD_MIN = ("CASE length(c.source_pub_date) "
            "WHEN 4 THEN c.source_pub_date || '-01-01' "
            "WHEN 7 THEN c.source_pub_date || '-01' "
            "ELSE c.source_pub_date END")
_PAD_MAX = ("CASE length(c.source_pub_date) "
            "WHEN 4 THEN c.source_pub_date || '-12-31' "
            "WHEN 7 THEN c.source_pub_date || '-31' "
            "ELSE c.source_pub_date END")


@dataclass
class SearchHit:
    card_id: int
    tag: str
    cite: str
    snippet_html: str
    body_len: int
    is_analytic: bool
    team_count: int
    school_count: int
    topic_codes: List[str] = field(default_factory=list)
    source_pub_date: Optional[str] = None


@dataclass
class SearchResult:
    hits: List[SearchHit]
    total: int
    elapsed_ms: float
    query: ParsedQuery


# --- cite year (year: operator) -------------------------------------------

_DIGITS = re.compile(r"\d+")


def _cite_year(cite: Optional[str]) -> Optional[str]:
    """2-digit year from a short cite: "Kessler '26" -> '26',
    "Rodgers and Cooper 06" -> '06', "Smith et al. 24" -> '24',
    "Diamond 2013" -> '13'. Scans number tokens from the end."""
    if not cite:
        return None
    for tok in reversed(_DIGITS.findall(cite)):
        if len(tok) == 4:
            return tok[2:]
        if len(tok) <= 2:
            return tok.zfill(2)
    return None


def _sql_cite_year(cite):
    try:
        return _cite_year(cite)
    except Exception:      # a SQL function must never raise mid-query
        return None


def _ensure_cite_year_fn(conn: sqlite3.Connection) -> None:
    try:
        conn.create_function("pf_cite_year", 1, _sql_cite_year, deterministic=True)
    except (TypeError, sqlite3.NotSupportedError):
        conn.create_function("pf_cite_year", 1, _sql_cite_year)


# --- topic resolution ------------------------------------------------------

def _fallback_resolve_topic(conn: sqlite3.Connection, token: str,
                            today: date) -> List[int]:
    """Same contract as carddb.topics.resolve_topic_token (§6.3):
    'present'|'past'|'future' by date window, anything else is a code."""
    t = (token or "").strip()
    iso = today.isoformat()
    tl = t.lower()
    if tl == "present":
        rows = conn.execute(
            "SELECT id FROM topics WHERE starts <= ? AND ends >= ?", (iso, iso))
    elif tl == "past":
        rows = conn.execute("SELECT id FROM topics WHERE ends < ?", (iso,))
    elif tl == "future":
        rows = conn.execute("SELECT id FROM topics WHERE starts > ?", (iso,))
    else:
        rows = conn.execute(
            "SELECT id FROM topics WHERE UPPER(code) = UPPER(?)", (t,))
    return [r["id"] for r in rows.fetchall()]


def _resolve_topic_ids(conn: sqlite3.Connection, token: str,
                       today: date) -> List[int]:
    try:
        from .topics import resolve_topic_token  # another module; may not exist yet
    except Exception:
        return _fallback_resolve_topic(conn, token, today)
    try:
        return list(resolve_topic_token(conn, token, today))
    except Exception:
        return _fallback_resolve_topic(conn, token, today)


# --- predicate assembly ----------------------------------------------------

def _like_pattern(value: str) -> str:
    """Substring LIKE pattern with user %/_ neutralized (ESCAPE '\\')."""
    v = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + v + "%"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,)).fetchone() is not None


def _pad_bound_min(d: str) -> str:
    if len(d) == 4:
        return d + "-01-01"
    if len(d) == 7:
        return d + "-01"
    return d


def _predicates(conn: sqlite3.Connection, f: Dict[str, Any],
                today: date) -> Tuple[List[str], List[Any]]:
    preds: List[str] = []
    params: List[Any] = []

    # Analytics excluded by default; is:analytic flips to analytics-only.
    preds.append("c.is_analytic = ?")
    params.append(1 if f.get("is_analytic") else 0)

    if "topic" in f:
        ids = _resolve_topic_ids(conn, f["topic"], today)
        if ids:
            qs = ",".join("?" * len(ids))
            preds.append(
                "EXISTS (SELECT 1 FROM card_variants v "
                "JOIN rounds r ON r.id = v.round_id "
                f"WHERE v.card_id = c.id AND r.topic_id IN ({qs}))")
            params.extend(ids)
        else:
            preds.append("0 = 1")   # unresolvable topic token matches nothing

    if "season" in f:
        preds.append(
            "EXISTS (SELECT 1 FROM card_variants v "
            "JOIN rounds r ON r.id = v.round_id "
            "JOIN teams t ON t.id = r.team_id "
            "JOIN schools s ON s.id = t.school_id "
            "JOIN caselists cl ON cl.id = s.caselist_id "
            "WHERE v.card_id = c.id AND cl.season = ?)")
        params.append(f["season"])

    if "side" in f:
        preds.append(
            "EXISTS (SELECT 1 FROM card_variants v "
            "JOIN rounds r ON r.id = v.round_id "
            "WHERE v.card_id = c.id AND r.side = ?)")
        params.append(f["side"])

    if "school" in f:
        pat = _like_pattern(f["school"])
        preds.append(
            "EXISTS (SELECT 1 FROM card_variants v "
            "JOIN rounds r ON r.id = v.round_id "
            "JOIN teams t ON t.id = r.team_id "
            "JOIN schools s ON s.id = t.school_id "
            "WHERE v.card_id = c.id AND "
            "(s.name LIKE ? ESCAPE '\\' OR s.display_name LIKE ? ESCAPE '\\'))")
        params.extend([pat, pat])

    if "team" in f:
        pat = _like_pattern(f["team"])
        preds.append(
            "EXISTS (SELECT 1 FROM card_variants v "
            "JOIN rounds r ON r.id = v.round_id "
            "JOIN teams t ON t.id = r.team_id "
            "WHERE v.card_id = c.id AND "
            "(t.name LIKE ? ESCAPE '\\' OR t.display_name LIKE ? ESCAPE '\\'))")
        params.extend([pat, pat])

    if "cite" in f:
        pat = _like_pattern(f["cite"])
        preds.append("(c.cite LIKE ? ESCAPE '\\' OR c.fullcite LIKE ? ESCAPE '\\')")
        params.extend([pat, pat])

    if "year" in f:
        _ensure_cite_year_fn(conn)
        preds.append("pf_cite_year(c.cite) = ?")
        params.append(f["year"])

    if "before" in f:
        preds.append("(c.source_pub_date IS NOT NULL AND c.source_pub_date != '' "
                     f"AND {_PAD_MIN} < ?)")
        params.append(_pad_bound_min(f["before"]))

    if "after" in f:
        preds.append("(c.source_pub_date IS NOT NULL AND c.source_pub_date != '' "
                     f"AND {_PAD_MAX} >= ?)")
        params.append(_pad_bound_min(f["after"]))

    if "min_reads" in f:
        preds.append("c.team_count >= ?")
        params.append(int(f["min_reads"]))

    if "status" in f and _table_exists(conn, "prep_status"):
        preds.append("EXISTS (SELECT 1 FROM prep_status ps "
                     "WHERE ps.card_id = c.id AND ps.status = ?)")
        params.append(f["status"])

    if "exclude" in f:      # exclusion-only query: no MATCH to hang NOT off
        preds.append("c.id NOT IN (SELECT rowid FROM card_fts WHERE card_fts MATCH ?)")
        params.append(" OR ".join(f["exclude"]))

    return preds, params


def _order_sql(sort: str, has_fts: bool) -> str:
    if sort == "reads":
        return "c.team_count DESC, c.id"
    if sort == "recent":
        return ("(SELECT MAX(r.round_date) FROM card_variants v "
                "JOIN rounds r ON r.id = v.round_id "
                "WHERE v.card_id = c.id) DESC, c.id")
    if sort == "length":
        return "c.body_len DESC, c.id"
    # relevance
    if has_fts:
        return BM25 + ", c.id"
    return "c.team_count DESC, c.id"    # no MATCH to rank: most-read first


# --- hit assembly ----------------------------------------------------------

def _topic_codes_for(conn: sqlite3.Connection,
                     rows: List[sqlite3.Row]) -> Dict[int, List[str]]:
    """Topic codes per card: prefer the materialized cards.topic_ids JSON
    (codes, or ids mapped through topics); derive live from
    card_variants ⋈ rounds ⋈ topics when not materialized yet."""
    out: Dict[int, List[str]] = {}
    live: List[int] = []
    by_int_ids: Dict[int, List[int]] = {}
    for r in rows:
        cid = r["id"]
        raw = r["topic_ids"]
        arr = None
        if raw:
            try:
                arr = json.loads(raw)
            except Exception:
                arr = None
        if isinstance(arr, list):
            strs = [x for x in arr if isinstance(x, str)]
            ints = [x for x in arr if isinstance(x, int)]
            if ints and not strs:
                by_int_ids[cid] = ints
            else:
                out[cid] = sorted(strs)
            continue
        live.append(cid)
    if by_int_ids:
        all_ids = sorted({i for v in by_int_ids.values() for i in v})
        qs = ",".join("?" * len(all_ids))
        code_of = {row["id"]: row["code"] for row in conn.execute(
            f"SELECT id, code FROM topics WHERE id IN ({qs})", all_ids)}
        for cid, idlist in by_int_ids.items():
            out[cid] = sorted(code_of[i] for i in idlist if i in code_of)
    if live:
        qs = ",".join("?" * len(live))
        for row in conn.execute(
                "SELECT DISTINCT v.card_id AS cid, t.code AS code "
                "FROM card_variants v "
                "JOIN rounds r ON r.id = v.round_id "
                "JOIN topics t ON t.id = r.topic_id "
                f"WHERE v.card_id IN ({qs})", live):
            out.setdefault(row["cid"], []).append(row["code"])
        for cid in live:
            out[cid] = sorted(out.get(cid, []))
    return out


def _fallback_snippet(body: Optional[str], max_words: int = 24) -> str:
    """Plain body-prefix snippet for listings that run without MATCH."""
    if not body:
        return ""
    words = body.split()
    if len(words) <= max_words:
        return body
    return " ".join(words[:max_words]) + "…"


# --- entry point -----------------------------------------------------------

def search(conn: sqlite3.Connection, q: str, limit: int = 30, offset: int = 0,
           today: Optional[date] = None) -> SearchResult:
    """Run one search. See the module docstring for semantics."""
    t0 = time.perf_counter()
    pq = parse_query(q if q is not None else "")
    if today is None:
        today = date.today()

    preds, params = _predicates(conn, pq.filters, today)
    where = " AND ".join(preds)
    order = _order_sql(pq.sort, pq.fts is not None)

    if pq.fts is not None:
        base_from = "FROM card_fts JOIN cards c ON c.id = card_fts.rowid"
        base_where = "card_fts MATCH ? AND " + where
        head: List[Any] = [pq.fts]
        snip_col = SNIPPET_SQL + " AS snip"
        body_col = "NULL AS body_text"
    else:
        base_from = "FROM cards c"
        base_where = where
        head = []
        snip_col = "NULL AS snip"
        body_col = "c.body_text AS body_text"

    sel = ("SELECT c.id, c.tag, c.cite, c.body_len, c.is_analytic, "
           "c.team_count, c.school_count, c.source_pub_date, c.topic_ids, "
           f"{body_col}, {snip_col} {base_from} WHERE {base_where} "
           f"ORDER BY {order} LIMIT ? OFFSET ?")
    cnt = f"SELECT COUNT(*) {base_from} WHERE {base_where}"

    try:
        rows = conn.execute(sel, head + params + [int(limit), int(offset)]).fetchall()
        total = conn.execute(cnt, head + params).fetchone()[0]
    except sqlite3.OperationalError as e:
        # The quoting in carddb.query makes FTS syntax errors unreachable in
        # principle; this guard keeps §7.2's "never error on a query" true
        # even against an unforeseen FTS5 corner case.
        msg = str(e).lower()
        if "fts5" in msg or "match" in msg:
            rows, total = [], 0
        else:
            raise

    codes = _topic_codes_for(conn, rows)
    hits: List[SearchHit] = []
    for r in rows:
        snip = r["snip"] if r["snip"] is not None else _fallback_snippet(r["body_text"])
        hits.append(SearchHit(
            card_id=r["id"],
            tag=r["tag"] or "",
            cite=r["cite"] or "",
            snippet_html=snip,
            body_len=r["body_len"] or 0,
            is_analytic=bool(r["is_analytic"]),
            team_count=r["team_count"] or 0,
            school_count=r["school_count"] or 0,
            topic_codes=codes.get(r["id"], []),
            source_pub_date=r["source_pub_date"],
        ))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return SearchResult(hits=hits, total=int(total), elapsed_ms=elapsed_ms, query=pq)
