"""Query-language parser. Spec §7.2, Appendix C.

Grammar:
  - Bare words are AND'd across all FTS columns.
  - "exact phrase" matches adjacent tokens.
  - -term / -"phrase" excludes (FTS NOT).
  - Fielded operators become SQL-side filters, never FTS text:
      topic:2026-SO | topic:present|past|future
      season:2025
      side:pro|con              (normalized to 'P'/'C')
      school:"Millburn"  team:XY
      cite:kessler  author:kessler   (author is an alias of cite)
      year:26 | year:2026            (2-digit cite year)
      before:2026-01-01  after:2026  (source pub date, ISO or bare year)
      is:analytic                    (flip to analytics-only)
      min_reads:5
      sort:relevance|reads|recent|length
      status:answered                (reserved for feature 9.20; parsed here,
                                      applied only if a prep_status table exists)
  - block:"A2: Moratorium" scopes an FTS phrase to the block column.

Safety and robustness (spec §7.2: "never error on a query"):
  - parse_query() NEVER raises; a last-resort guard degrades any unexpected
    parser failure to a single quoted phrase.
  - Unknown operators (foo:bar) and malformed values (year:, side:maybe,
    sort:banana, season:25, min_reads:soon) degrade to plain search terms.
  - An unclosed quote consumes the rest of the query as the phrase/value.
  - Every term and phrase is individually double-quoted for FTS5 with
    embedded quotes doubled, so user input can never inject FTS syntax
    (AND/OR/NOT/NEAR/*/^/column filters are all neutralized).
  - Tokens with no alphanumeric content are dropped (an FTS phrase that
    tokenizes to nothing is useless at best).
  - A negated fielded operator (-cite:kessler) is not part of the grammar
    and degrades to an excluded plain term.
  - Repeated scalar operators: last one wins.

ParsedQuery.fts is a complete FTS5 MATCH expression (positives AND'd, then
NOT (negatives OR'd)), or None when the query has no positive full-text
terms. A query with only exclusions puts the individually-quoted excluded
expressions in filters["exclude"]; search() applies them as a NOT IN
subquery so pure filtered listings still honor them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .ingest import normalize_side

SORTS = ("relevance", "reads", "recent", "length")

_FIELD_RE = re.compile(r"([A-Za-z_]+):")
_SEASON_RE = re.compile(r"\d{4}$")
_YEAR2_RE = re.compile(r"\d{2}$")
_YEAR4_RE = re.compile(r"\d{4}$")
_DATE_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?$")
_INT_RE = re.compile(r"\d+$")


@dataclass
class ParsedQuery:
    fts: Optional[str]          # FTS5 MATCH expression or None
    filters: Dict[str, Any] = field(default_factory=dict)
    sort: str = "relevance"     # 'relevance' | 'reads' | 'recent' | 'length'


def fts_quote(text: str) -> str:
    """Quote text as one FTS5 string (term or phrase). Embedded double
    quotes are doubled, so the result is always a single literal phrase —
    no user input can become FTS5 syntax."""
    return '"' + str(text).replace('"', '""') + '"'


def _scan(q: str) -> List[Tuple[bool, Optional[str], str, bool, str]]:
    """Tokenize into (neg, fieldname, value, quoted, raw_token) tuples."""
    out: List[Tuple[bool, Optional[str], str, bool, str]] = []
    i, n = 0, len(q)
    while i < n:
        while i < n and q[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        neg = False
        if q[i] == "-" and i + 1 < n and not q[i + 1].isspace():
            neg = True
            i += 1
        m = _FIELD_RE.match(q, i)
        fieldname = None
        if m:
            fieldname = m.group(1).lower()
            i = m.end()
        if i < n and q[i] == '"':
            j = q.find('"', i + 1)
            if j == -1:                       # unclosed quote: rest of string
                value = q[i + 1:]
                i = n
            else:
                value = q[i + 1:j]
                i = j + 1
            quoted = True
        else:
            j = i
            while j < n and not q[j].isspace():
                j += 1
            value = q[i:j]
            i = j
            quoted = False
        out.append((neg, fieldname, value, quoted, q[start:i]))
    return out


def _parse(q: str) -> ParsedQuery:
    filters: Dict[str, Any] = {}
    sort = "relevance"
    pos: List[str] = []
    negs: List[str] = []

    for neg, fieldname, value, quoted, raw in _scan(q):
        if fieldname is not None and not neg:
            consumed = True
            v = value.strip()
            if v == "":
                consumed = False
            elif fieldname == "topic":
                filters["topic"] = v
            elif fieldname == "season":
                if _SEASON_RE.fullmatch(v):
                    filters["season"] = int(v)
                else:
                    consumed = False
            elif fieldname == "side":
                side = normalize_side(v)
                if side:
                    filters["side"] = side
                else:
                    consumed = False
            elif fieldname == "school":
                filters["school"] = v
            elif fieldname == "team":
                filters["team"] = v
            elif fieldname in ("cite", "author"):
                filters["cite"] = v
            elif fieldname == "year":
                if _YEAR2_RE.fullmatch(v):
                    filters["year"] = v
                elif _YEAR4_RE.fullmatch(v):
                    filters["year"] = v[2:]
                else:
                    consumed = False
            elif fieldname in ("before", "after"):
                if _DATE_RE.fullmatch(v):
                    filters[fieldname] = v
                else:
                    consumed = False
            elif fieldname == "is":
                if v.lower() in ("analytic", "analytics"):
                    filters["is_analytic"] = True
                else:
                    consumed = False
            elif fieldname == "min_reads":
                if _INT_RE.fullmatch(v):
                    filters["min_reads"] = int(v)
                else:
                    consumed = False
            elif fieldname == "sort":
                if v.lower() in SORTS:
                    sort = v.lower()
                else:
                    consumed = False
            elif fieldname == "status":
                filters["status"] = v.lower()
            elif fieldname == "block":
                pos.append("block:" + fts_quote(v))
            else:
                consumed = False            # unknown operator
            if consumed:
                continue

        # Plain (or degraded) term / phrase.
        if fieldname is None:
            term = value                    # phrase text without its quotes
        elif neg and raw.startswith("-"):
            term = raw[1:]                  # degraded negated operator
        else:
            term = raw                      # degraded operator, verbatim
        if not any(ch.isalnum() for ch in term):
            continue
        (negs if neg else pos).append(fts_quote(term))

    if pos and negs:
        fts: Optional[str] = ("(" + " AND ".join(pos) + ") NOT ("
                              + " OR ".join(negs) + ")")
    elif pos:
        fts = " AND ".join(pos)
    else:
        fts = None
        if negs:
            filters["exclude"] = negs
    return ParsedQuery(fts=fts, filters=filters, sort=sort)


def parse_query(q: str) -> ParsedQuery:
    """Parse a user query. Never raises: any unexpected failure degrades
    the whole input to a single quoted phrase."""
    try:
        return _parse(str(q) if q is not None else "")
    except Exception:
        text = str(q or "")
        fts = fts_quote(text) if any(c.isalnum() for c in text) else None
        return ParsedQuery(fts=fts, filters={}, sort="relevance")
