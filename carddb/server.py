"""Web UI. Spec §8, features §9.1/2/3/5/6/9/10/11.

FastAPI + Jinja2, server-rendered, one CSS file, vanilla JS. Connections
are opened per request; every page renders sanely against an empty
database (the M0 done-check). Card markup_html is re-sanitized with
carddb.sanitize.sanitize_markup at render time before it is marked safe.

Sibling feature modules (search, topics, consensus, heuristics,
export_docx) may be mid-build; each import has a working local fallback
so the app stays up and tests pass without them.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from .a2 import a2_display, a2_target, argument_key
from .config import ROOT, load_config, resolve_path
from .connect import register_connect
from .db import open_db
from .rawstore import now_iso
from .sanitize import sanitize_markup

# --- optional sibling modules (graceful while mid-build) ------------------

try:
    from .search import search as _search_fn
except Exception:  # module not written yet
    _search_fn = None

try:
    from .topics import topic_status as _topic_status_fn
except Exception:
    _topic_status_fn = None

try:
    from .consensus import consensus as _consensus_fn
except Exception:
    _consensus_fn = None

try:
    from .heuristics import miscut_flags as _miscut_flags_fn
except Exception:
    _miscut_flags_fn = None

try:
    from .export_docx import export_cards as _export_cards_fn
    from .export_docx import read_time_str as _read_time_str
    from .export_docx import spoken_word_count as _spoken_word_count
except Exception:
    _export_cards_fn = None

    def _spoken_word_count(spoken):  # type: ignore
        return len((spoken or "").split())

    def _read_time_str(words, wpm=250):  # type: ignore
        secs = int(round(words * 60.0 / max(float(wpm), 1.0)))
        return "{0}:{1:02d}".format(secs // 60, secs % 60)


# --- constants ------------------------------------------------------------

# Word's base highlighter palette; the restriction is load-bearing (§8.3).
HL_COLORS = [
    ("green", "Bright green", "#00FF00"),
    ("yellow", "Yellow", "#FFFF00"),
    ("blue", "Blue", "#0000FF"),
    ("turquoise", "Turquoise", "#00FFFF"),
]
HL_BY_NAME = {name: hexv for name, _, hexv in HL_COLORS}

# NSDA's record shows March and April were separate PF topics in every season
# since 2013-14, so MA is March (data/topics.json carries APR rows separately);
# monthly-era slots (2013-17) get their own labels.
SLOT_LABELS = {"SO": "Sept/Oct", "ND": "Nov/Dec", "JAN": "January",
               "FEB": "February", "MA": "March", "APR": "April",
               "SEP": "September", "OCT": "October", "NOV": "November",
               "DEC": "December", "NATS": "Nationals"}


def _side_display(side) -> str:
    return {"P": "Pro", "C": "Con"}.get(side or "", "")


def _hl_name(raw: Optional[str]) -> str:
    return raw if raw in HL_BY_NAME else "green"


def _topic_status(row, today: date) -> str:
    if _topic_status_fn is not None:
        try:
            return _topic_status_fn(row, today)
        except Exception:
            pass
    starts, ends = row["starts"], row["ends"]
    iso = today.isoformat()
    if starts and iso < starts:
        return "future"
    if ends and ends < iso:
        return "past"
    return "present"


def _topic_ids_for_token(conn, token: str, today: date) -> List[int]:
    token = (token or "").strip().lower()
    rows = conn.execute("SELECT * FROM topics").fetchall()
    if token in ("past", "present", "future"):
        return [r["id"] for r in rows if _topic_status(r, today) == token]
    return [r["id"] for r in rows if (r["code"] or "").lower() == token]


# --- fallback search (used only until carddb.search lands) ----------------

_FIELD_TOKEN = re.compile(r'(\w+):("[^"]*"|\S+)')


def _fallback_search(conn, q: str, limit: int, offset: int,
                     today: Optional[date] = None):
    today = today or date.today()
    t0 = time.perf_counter()
    filters: Dict[str, str] = {}

    def _grab(m):
        filters[m.group(1).lower()] = m.group(2).strip('"')
        return " "

    rest = _FIELD_TOKEN.sub(_grab, q or "")
    terms = []
    for tok in re.findall(r'"[^"]+"|\S+', rest):
        if tok.startswith("-"):
            continue
        tok = re.sub(r"[^\w\s]", " ", tok.strip('"')).strip()
        if tok:
            terms.append('"%s"' % tok)
    match = " AND ".join(terms) if terms else None

    where, params = [], []  # type: List[str], List[Any]
    tok = filters.get("topic")
    if tok:
        ids = _topic_ids_for_token(conn, tok, today)
        if ids:
            where.append(
                "c.id IN (SELECT v.card_id FROM card_variants v "
                "JOIN rounds r ON r.id = v.round_id WHERE r.topic_id IN (%s))"
                % ",".join("?" * len(ids)))
            params.extend(ids)
        else:
            where.append("0")
    hits: List[SimpleNamespace] = []
    total = 0
    try:
        if match:
            base = ("FROM card_fts JOIN cards c ON c.id = card_fts.rowid "
                    "WHERE card_fts MATCH ?")
            qparams = [match] + params
            wh = (" AND " + " AND ".join(where)) if where else ""
            total = conn.execute("SELECT COUNT(*) " + base + wh, qparams).fetchone()[0]
            rows = conn.execute(
                "SELECT c.*, snippet(card_fts, 3, char(2), char(3), ' ... ', 24) AS snip "
                + base + wh +
                " ORDER BY bm25(card_fts, 5.0, 3.0, 2.0, 1.0) LIMIT ? OFFSET ?",
                qparams + [limit, offset]).fetchall()
        else:
            wh = (" WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute("SELECT COUNT(*) FROM cards c" + wh, params).fetchone()[0]
            rows = conn.execute(
                "SELECT c.*, substr(c.body_text, 1, 200) AS snip FROM cards c"
                + wh + " ORDER BY c.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        snip = _html.escape(r["snip"] or "")
        snip = snip.replace("\x02", "<b>").replace("\x03", "</b>")
        hits.append(SimpleNamespace(
            card_id=r["id"], tag=r["tag"], cite=r["cite"], snippet_html=snip,
            body_len=r["body_len"] or 0, is_analytic=bool(r["is_analytic"]),
            team_count=r["team_count"] or 0, school_count=r["school_count"] or 0,
            topic_codes=json.loads(r["topic_ids"] or "[]"),
            source_pub_date=r["source_pub_date"]))
    elapsed = (time.perf_counter() - t0) * 1000.0
    return SimpleNamespace(hits=hits, total=total, elapsed_ms=elapsed, query=None)


def _run_search(conn, q: str, limit: int, offset: int):
    if _search_fn is not None:
        try:
            return _search_fn(conn, q, limit=limit, offset=offset)
        except Exception:
            pass
    return _fallback_search(conn, q, limit, offset)


# --- fallback consensus (feature 9.1) -------------------------------------

_WORD = re.compile(r"\W+", re.UNICODE)


def _fallback_consensus(body_text: str, markup_htmls: List[str]) -> List[Tuple[str, int]]:
    tokens = (body_text or "").split()
    norm = [_WORD.sub("", t).lower() for t in tokens]
    counts = [0] * len(tokens)
    for mh in markup_htmls:
        marked = " ".join(re.findall(r"<mark>(.*?)</mark>", mh or "", re.S))
        marked = _html.unescape(re.sub(r"<[^>]+>", " ", marked))
        bag: Dict[str, int] = {}
        for w in marked.split():
            w = _WORD.sub("", w).lower()
            if w:
                bag[w] = bag.get(w, 0) + 1
        used: Dict[str, int] = {}
        for i, w in enumerate(norm):
            if w and bag.get(w, 0) > used.get(w, 0):
                used[w] = used.get(w, 0) + 1
                counts[i] += 1
    return list(zip(tokens, counts))


def _consensus_tokens(body_text: str, markup_htmls: List[str]) -> List[Tuple[str, int]]:
    if _consensus_fn is not None:
        try:
            return _consensus_fn(body_text, markup_htmls)
        except Exception:
            pass
    return _fallback_consensus(body_text, markup_htmls)


def _miscut_flags(card_row, variant_rows):
    if _miscut_flags_fn is None:
        return []
    try:
        return _miscut_flags_fn(card_row, variant_rows) or []
    except Exception:
        return []


# --- inline SVG sparkline (no chart library) ------------------------------

def _sparkline_svg(values: List[int], width: int = 200, height: int = 26,
                   label: str = "disclosures over time") -> str:
    if not values:
        values = [0]
    mx = max(values) or 1
    n = len(values)
    if n == 1:
        pts = "1,{0} {1},{0}".format(height - 2, width - 1)
    else:
        step = (width - 2) / float(n - 1)
        pts = " ".join(
            "{0:.1f},{1:.1f}".format(1 + i * step,
                                     height - 2 - (v / float(mx)) * (height - 4))
            for i, v in enumerate(values))
    return ('<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            'role="img" aria-label="{label}"><polyline points="{pts}" fill="none" '
            'stroke="currentColor" stroke-width="1"/></svg>').format(
                w=width, h=height, pts=pts, label=_html.escape(label, quote=True))


class _Form:
    """Minimal urlencoded form access (no python-multipart dependency;
    every form in the UI posts application/x-www-form-urlencoded)."""

    def __init__(self, data: Dict[str, List[str]]):
        self._data = data

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        vals = self._data.get(key)
        return vals[0] if vals else default

    def getlist(self, key: str) -> List[str]:
        return self._data.get(key, [])


async def _read_form(request: Request) -> _Form:
    body = await request.body()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    return _Form(parse_qs(text, keep_blank_values=True))


# --- misc helpers ---------------------------------------------------------

def _corpus_stats(conn) -> Dict[str, Any]:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    seasons = conn.execute(
        "SELECT MIN(season), MAX(season) FROM caselists WHERE season IS NOT NULL"
    ).fetchone()
    lo, hi = seasons[0], seasons[1]
    if lo is None:
        covered = "none yet"
    elif lo == hi:
        covered = "{0}-{1:02d}".format(lo, (lo + 1) % 100)
    else:
        covered = "{0}-{1:02d} through {2}-{3:02d}".format(
            lo, (lo + 1) % 100, hi, (hi + 1) % 100)
    return {
        "cards": one("SELECT COUNT(*) FROM cards WHERE is_analytic = 0"),
        "analytics": one("SELECT COUNT(*) FROM cards WHERE is_analytic = 1"),
        "variants": one("SELECT COUNT(*) FROM card_variants"),
        "teams": one("SELECT COUNT(*) FROM teams"),
        "schools": one("SELECT COUNT(*) FROM schools"),
        "rounds": one("SELECT COUNT(*) FROM rounds"),
        "topics": one("SELECT COUNT(*) FROM topics"),
        "seasons_covered": covered,
    }


def _topic_groups(conn, today: date) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.*, (SELECT COUNT(DISTINCT v.card_id) FROM rounds r "
        " JOIN card_variants v ON v.round_id = r.id "
        " JOIN cards c ON c.id = v.card_id AND c.is_analytic = 0 "
        " WHERE r.topic_id = t.id) "
        " AS card_count FROM topics t ORDER BY t.starts DESC, t.code DESC"
    ).fetchall()
    groups = {"present": [], "future": [], "past": []}
    for r in rows:
        d = dict(r)
        d["status"] = _topic_status(r, today)
        d["slot_label"] = SLOT_LABELS.get(r["slot"] or "", r["slot"] or "")
        groups[d["status"]].append(d)
    out = []
    for key, label in (("present", "Present"), ("future", "Future"), ("past", "Past")):
        if groups[key]:
            out.append({"key": key, "label": label, "topics": groups[key]})
    return out


def _current_topic(conn, today: date):
    for r in conn.execute("SELECT * FROM topics").fetchall():
        if _topic_status(r, today) == "present":
            return r
    return None


def _domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def _month_series(dates: Iterable[Optional[str]]) -> Tuple[List[str], List[int]]:
    c = Counter(d[:7] for d in dates if d)
    months = sorted(c)
    return months, [c[m] for m in months]


def _card_topic_codes(row) -> List[str]:
    try:
        return json.loads(row["topic_ids"] or "[]")
    except (ValueError, TypeError):
        return []


def _hit_dict(h) -> Dict[str, Any]:
    return {k: getattr(h, k, None) for k in (
        "card_id", "tag", "cite", "snippet_html", "body_len", "is_analytic",
        "team_count", "school_count", "topic_codes", "source_pub_date")}


def _flags_for_hits(conn, hits) -> Dict[int, list]:
    out: Dict[int, list] = {}
    if _miscut_flags_fn is None:
        return out
    for h in hits:
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (h.card_id,)).fetchone()
        variants = conn.execute(
            "SELECT * FROM card_variants WHERE card_id = ?", (h.card_id,)).fetchall()
        if card is not None:
            out[h.card_id] = _miscut_flags(card, variants)
    return out


# --- app factory ----------------------------------------------------------

def create_app(db_path=None, cfg: Optional[dict] = None) -> FastAPI:
    cfg = cfg or load_config()
    if db_path is None:
        db_path = resolve_path(cfg, "db")
    db_path = str(db_path)
    open_db(db_path).close()  # ensure schema exists; empty DB renders fine

    app = FastAPI(title="PF card search", docs_url=None, redoc_url=None)
    app.state.db_path = db_path
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    def _nlabel(n, word):
        n = n or 0
        return "{0} {1}{2}".format(n, word, "" if n == 1 else "s")

    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.filters["side"] = _side_display
    templates.env.filters["nlabel"] = _nlabel
    templates.env.globals["hl_colors"] = HL_COLORS
    page_size = int(cfg.get("search", {}).get("page_size", 30))
    wpm = int(cfg.get("export", {}).get("wpm", 250))

    def get_conn():
        # Per-request connection. check_same_thread=False because sync
        # dependencies run in a threadpool while async handlers run on the
        # event loop; each connection still lives for exactly one request.
        conn = sqlite3.connect(app.state.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, ctx: Dict[str, Any], status_code: int = 200):
        return templates.TemplateResponse(request, name, dict(ctx),
                                          status_code=status_code)

    def not_found(request: Request, what: str):
        return render(request, "notfound.html", {"what": what}, status_code=404)

    def _search_context(conn, q: str, topic: str, pageno: int) -> Dict[str, Any]:
        today = date.today()
        effective_q = q.strip()
        if topic:
            effective_q = (effective_q + " topic:" + topic).strip()
        result = None
        hit_flags: Dict[int, list] = {}
        if effective_q:
            offset = max(pageno - 1, 0) * page_size
            result = _run_search(conn, effective_q, page_size, offset)
            hit_flags = _flags_for_hits(conn, result.hits)
        return {
            "q": q, "topic": topic, "page_no": pageno, "page_size": page_size,
            "result": result, "hit_flags": hit_flags,
            "topic_groups": _topic_groups(conn, today),
            "stats": _corpus_stats(conn),
            "current_topic": _current_topic(conn, today),
        }

    # --- search -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = "", topic: str = "", page: int = 1,
              conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, "index.html", _search_context(conn, q, topic, page))

    @app.get("/search")
    def search_route(request: Request, q: str = "", topic: str = "",
                     page: int = 1, format: str = "",
                     conn: sqlite3.Connection = Depends(get_conn)):
        ctx = _search_context(conn, q, topic, page)
        if format == "json":
            result = ctx["result"]
            hits = []
            if result is not None:
                for h in result.hits:
                    d = _hit_dict(h)
                    d["flags"] = [
                        {"code": getattr(f, "code", ""),
                         "label": getattr(f, "label", ""),
                         "detail": getattr(f, "detail", "")}
                        for f in ctx["hit_flags"].get(h.card_id, [])]
                    hits.append(d)
            return JSONResponse({
                "hits": hits,
                "total": result.total if result is not None else 0,
                "elapsed_ms": result.elapsed_ms if result is not None else 0.0,
            })
        return render(request, "index.html", ctx)

    # --- card page --------------------------------------------------------

    @app.get("/card/{card_id}", response_class=HTMLResponse)
    def card_page(request: Request, card_id: int, hl: str = "",
                  conn: sqlite3.Connection = Depends(get_conn)):
        card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        if card is None:
            return not_found(request, "card")
        variants = conn.execute(
            "SELECT v.*, r.id AS rid, r.tournament, r.side, r.round_label, "
            " r.round_date, r.opponent, t.id AS team_id, "
            " t.display_name AS team_name, s.id AS school_id, "
            " s.display_name AS school_name "
            "FROM card_variants v "
            "LEFT JOIN rounds r ON r.id = v.round_id "
            "LEFT JOIN teams t ON t.id = r.team_id "
            "LEFT JOIN schools s ON s.id = t.school_id "
            "WHERE v.card_id = ? "
            "ORDER BY r.round_date IS NULL, r.round_date, v.id",
            (card_id,)).fetchall()
        vlist = []
        for v in variants:
            d = dict(v)
            # re-sanitize at render time, before |safe in the template
            d["markup_safe"] = sanitize_markup(v["markup_html"] or "")
            label = v["team_name"] or "unknown team"
            if v["school_name"]:
                label = "{0} ({1})".format(label, v["school_name"])
            d["label"] = label
            vlist.append(d)
        markups = [v["markup_html"] or "" for v in variants]
        consensus_tokens = []
        if not card["is_analytic"] and card["body_text"] and len(markups) >= 1:
            raw = _consensus_tokens(card["body_text"], markups)
            mx = max([c for _, c in raw] or [0]) or 1
            consensus_tokens = [
                {"token": t, "count": c, "weight": round(c / float(mx), 2)}
                for t, c in raw]
        flags = _miscut_flags(card, variants)
        health = conn.execute(
            "SELECT * FROM cite_health WHERE card_id = ?", (card_id,)).fetchone()

        # A2 cross-index (§9.3): answers other teams have disclosed to this
        # card's argument (matched on normalized block titles).
        blocks = sorted({v["block"] for v in variants if v["block"]})
        answers, seen_answer_ids = [], set()
        for b in blocks:
            k = argument_key(b)
            if not k:
                continue
            for row in conn.execute(
                    "SELECT DISTINCT c.id, c.tag, c.cite FROM card_variants v "
                    "JOIN cards c ON c.id = v.card_id "
                    "WHERE v.a2_target = ? AND c.id != ? LIMIT 25",
                    (k, card_id)).fetchall():
                if row["id"] not in seen_answer_ids:
                    seen_answer_ids.add(row["id"])
                    answers.append(row)
        # Display the ORIGINAL block titles, not the normalized a2_target —
        # normalize() output is never display text (§3.5). The normalized
        # value is still what dedups/matches (one entry per distinct target).
        answers_to_by_target: Dict[str, str] = {}
        for v in variants:
            if v["a2_target"] and v["a2_target"] not in answers_to_by_target:
                answers_to_by_target[v["a2_target"]] = (
                    a2_display(v["block"]) or v["a2_target"])
        answers_to = sorted(answers_to_by_target.values())

        # lineage (§9.2)
        dates = [v["round_date"] for v in variants]
        first_seen = min([d for d in dates if d] or [None]) if any(dates) else None
        months, series = _month_series(dates)
        spark = _sparkline_svg(series, label="disclosures per month") if months else ""

        spoken = ""
        for v in variants:
            if v["spoken"]:
                spoken = v["spoken"]
                break
        boxes = conn.execute("SELECT * FROM card_boxes ORDER BY name").fetchall()
        return render(request, "card.html", {
            "card": card, "variants": vlist,
            "topic_codes": _card_topic_codes(card),
            "consensus_tokens": consensus_tokens,
            "flags": flags, "health": health,
            "answers": answers, "answers_to": answers_to,
            "first_seen": first_seen, "spark": spark, "months": months,
            "spoken": spoken, "boxes": boxes,
            "hl": _hl_name(hl),
        })

    @app.get("/card/{card_id}/spoken.txt", response_class=PlainTextResponse)
    def card_spoken(card_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute(
            "SELECT spoken FROM card_variants WHERE card_id = ? AND spoken IS NOT NULL "
            "AND spoken != '' ORDER BY id LIMIT 1", (card_id,)).fetchone()
        return PlainTextResponse(row["spoken"] if row else "")

    # --- topic pages ------------------------------------------------------

    @app.get("/topic/{code}", response_class=HTMLResponse)
    def topic_page(request: Request, code: str,
                   conn: sqlite3.Connection = Depends(get_conn)):
        topic = conn.execute("SELECT * FROM topics WHERE code = ?", (code,)).fetchone()
        if topic is None:
            return not_found(request, "topic")
        today = date.today()
        status = _topic_status(topic, today)
        # analytics are excluded from card counts by default (spec §1.3)
        card_count = conn.execute(
            "SELECT COUNT(DISTINCT v.card_id) FROM rounds r "
            "JOIN card_variants v ON v.round_id = r.id "
            "JOIN cards c ON c.id = v.card_id AND c.is_analytic = 0 "
            "WHERE r.topic_id = ?", (topic["id"],)).fetchone()[0]
        day_rows = conn.execute(
            "SELECT r.round_date AS d, COUNT(*) AS n FROM rounds r "
            "JOIN card_variants v ON v.round_id = r.id "
            "WHERE r.topic_id = ? AND r.round_date IS NOT NULL "
            "GROUP BY r.round_date ORDER BY r.round_date", (topic["id"],)).fetchall()
        spark = _sparkline_svg([r["n"] for r in day_rows],
                               label="cards per day") if day_rows else ""
        sides = {"P": 0, "C": 0}
        for r in conn.execute(
                "SELECT r.side AS s, COUNT(*) AS n FROM rounds r "
                "JOIN card_variants v ON v.round_id = r.id "
                "WHERE r.topic_id = ? GROUP BY r.side", (topic["id"],)).fetchall():
            if r["s"] in sides:
                sides[r["s"]] = r["n"]
        side_total = (sides["P"] + sides["C"]) or 1
        most_read = conn.execute(
            "SELECT c.* FROM cards c WHERE c.is_analytic = 0 AND c.id IN "
            " (SELECT v.card_id FROM rounds r JOIN card_variants v "
            "  ON v.round_id = r.id WHERE r.topic_id = ?) "
            "ORDER BY c.team_count DESC, c.id LIMIT 10",
            (topic["id"],)).fetchall()
        newest = conn.execute(
            "SELECT c.*, MAX(r.round_date) AS latest FROM cards c "
            "JOIN card_variants v ON v.card_id = c.id "
            "JOIN rounds r ON r.id = v.round_id "
            "WHERE r.topic_id = ? AND c.is_analytic = 0 "
            "GROUP BY c.id ORDER BY latest DESC LIMIT 10", (topic["id"],)).fetchall()
        authors = conn.execute(
            "SELECT c.cite AS cite, COUNT(DISTINCT c.id) AS n FROM cards c "
            "WHERE c.cite IS NOT NULL AND c.cite != '' AND c.id IN "
            " (SELECT v.card_id FROM rounds r JOIN card_variants v "
            "  ON v.round_id = r.id WHERE r.topic_id = ?) "
            "GROUP BY c.cite ORDER BY n DESC LIMIT 10", (topic["id"],)).fetchall()
        urls = conn.execute(
            "SELECT DISTINCT c.id, c.source_url FROM cards c WHERE c.id IN "
            " (SELECT v.card_id FROM rounds r JOIN card_variants v "
            "  ON v.round_id = r.id WHERE r.topic_id = ?) "
            "AND c.source_url IS NOT NULL", (topic["id"],)).fetchall()
        domains = Counter(d for d in (_domain(u["source_url"]) for u in urls) if d)
        return render(request, "topic.html", {
            "topic": topic, "status": status, "card_count": card_count,
            "slot_label": SLOT_LABELS.get(topic["slot"] or "", topic["slot"] or ""),
            "spark": spark, "sides": sides,
            "pro_pct": round(100.0 * sides["P"] / side_total),
            "con_pct": round(100.0 * sides["C"] / side_total),
            "most_read": most_read, "newest": newest,
            "authors": authors, "domains": domains.most_common(10),
        })

    @app.get("/feed/topic/{code}.rss")
    def topic_feed(request: Request, code: str,
                   conn: sqlite3.Connection = Depends(get_conn)):
        topic = conn.execute("SELECT * FROM topics WHERE code = ?", (code,)).fetchone()
        if topic is None:
            return PlainTextResponse("no such topic", status_code=404)
        base = str(request.base_url).rstrip("/")
        rows = conn.execute(
            "SELECT c.id, c.tag, c.cite, c.canonical_key, MAX(r.round_date) AS latest "
            "FROM cards c JOIN card_variants v ON v.card_id = c.id "
            "JOIN rounds r ON r.id = v.round_id "
            "WHERE r.topic_id = ? AND c.is_analytic = 0 "
            "GROUP BY c.id ORDER BY latest DESC LIMIT 50", (topic["id"],)).fetchall()
        rss = ET.Element("rss", version="2.0")
        ch = ET.SubElement(rss, "channel")
        ET.SubElement(ch, "title").text = "PF card search: {0}".format(topic["code"])
        ET.SubElement(ch, "link").text = "{0}/topic/{1}".format(base, topic["code"])
        ET.SubElement(ch, "description").text = topic["resolution"] or topic["code"]
        for r in rows:
            item = ET.SubElement(ch, "item")
            title = r["tag"] or "(untagged card)"
            if r["cite"]:
                title = "{0} - {1}".format(title, r["cite"])
            ET.SubElement(item, "title").text = title
            ET.SubElement(item, "link").text = "{0}/card/{1}".format(base, r["id"])
            guid = ET.SubElement(item, "guid", isPermaLink="false")
            guid.text = r["canonical_key"]
            if r["latest"]:
                ET.SubElement(item, "pubDate").text = r["latest"]
        body = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
            rss, encoding="unicode")
        return Response(content=body, media_type="application/rss+xml")

    # --- school / team / round -------------------------------------------

    def _cards_by_topic(conn, where_sql: str, params: tuple):
        rows = conn.execute(
            "SELECT DISTINCT c.id, c.tag, c.cite, c.team_count, c.school_count, "
            " c.source_pub_date, tp.code AS topic_code "
            "FROM card_variants v JOIN cards c ON c.id = v.card_id "
            "JOIN rounds r ON r.id = v.round_id "
            "LEFT JOIN topics tp ON tp.id = r.topic_id "
            "LEFT JOIN teams t ON t.id = r.team_id "
            "WHERE " + where_sql + " ORDER BY tp.code DESC, c.tag", params).fetchall()
        groups: Dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["topic_code"] or "Unassigned", []).append(r)
        return groups

    @app.get("/school/{school_id}", response_class=HTMLResponse)
    def school_page(request: Request, school_id: int,
                    conn: sqlite3.Connection = Depends(get_conn)):
        school = conn.execute(
            "SELECT s.*, cl.display_name AS caselist_name FROM schools s "
            "LEFT JOIN caselists cl ON cl.id = s.caselist_id WHERE s.id = ?",
            (school_id,)).fetchone()
        if school is None:
            return not_found(request, "school")
        teams = conn.execute(
            "SELECT * FROM teams WHERE school_id = ? ORDER BY name", (school_id,)).fetchall()
        groups = _cards_by_topic(conn, "t.school_id = ?", (school_id,))
        return render(request, "school.html",
                    {"school": school, "teams": teams, "groups": groups})

    @app.get("/team/{team_id}", response_class=HTMLResponse)
    def team_page(request: Request, team_id: int,
                  conn: sqlite3.Connection = Depends(get_conn)):
        team = conn.execute(
            "SELECT t.*, s.display_name AS school_name, s.id AS school_id_ "
            "FROM teams t LEFT JOIN schools s ON s.id = t.school_id "
            "WHERE t.id = ?", (team_id,)).fetchone()
        if team is None:
            return not_found(request, "team")
        rounds = conn.execute(
            "SELECT * FROM rounds WHERE team_id = ? ORDER BY round_date", (team_id,)).fetchall()
        groups = _cards_by_topic(conn, "r.team_id = ?", (team_id,))
        return render(request, "team.html",
                    {"team": team, "rounds": rounds, "groups": groups})

    @app.get("/round/{round_id}", response_class=HTMLResponse)
    def round_page(request: Request, round_id: int,
                   conn: sqlite3.Connection = Depends(get_conn)):
        rnd = conn.execute(
            "SELECT r.*, t.display_name AS team_name, t.id AS team_id_, "
            " s.display_name AS school_name, s.id AS school_id_ "
            "FROM rounds r LEFT JOIN teams t ON t.id = r.team_id "
            "LEFT JOIN schools s ON s.id = t.school_id WHERE r.id = ?",
            (round_id,)).fetchone()
        if rnd is None:
            return not_found(request, "round")
        doc_ids = [r["document_id"] for r in conn.execute(
            "SELECT DISTINCT document_id FROM card_variants "
            "WHERE round_id = ? AND document_id IS NOT NULL", (round_id,)).fetchall()]
        doc_cards = []
        if doc_ids:
            doc_cards = conn.execute(
                "SELECT v.*, c.tag, c.cite, c.fullcite, c.is_analytic "
                "FROM card_variants v JOIN cards c ON c.id = v.card_id "
                "WHERE v.document_id IN (%s) ORDER BY v.document_id, v.ordinal"
                % ",".join("?" * len(doc_ids)), doc_ids).fetchall()
        cards = []
        for v in doc_cards:
            d = dict(v)
            d["markup_safe"] = sanitize_markup(v["markup_html"] or "")
            cards.append(d)
        return render(request, "round.html", {"round": rnd, "cards": cards})

    # --- authors (§9.7) ---------------------------------------------------

    @app.get("/authors", response_class=HTMLResponse)
    def authors_page(request: Request,
                     conn: sqlite3.Connection = Depends(get_conn)):
        authors = conn.execute(
            "SELECT cite, COUNT(*) AS n, MAX(fullcite) AS sample_fullcite "
            "FROM cards WHERE cite IS NOT NULL AND cite != '' "
            "GROUP BY cite ORDER BY n DESC, cite LIMIT 200").fetchall()
        rows = []
        for a in authors:
            d = dict(a)
            fc = a["sample_fullcite"] or ""
            m = re.search(r"\[([^\]]{3,240})\]", fc)
            d["quals"] = m.group(1) if m else (fc[:160] if fc else "")
            rows.append(d)
        urls = conn.execute(
            "SELECT source_url FROM cards WHERE source_url IS NOT NULL").fetchall()
        domains = Counter(d for d in (_domain(u["source_url"]) for u in urls) if d)
        return render(request, "authors.html",
                    {"authors": rows, "domains": domains.most_common(100)})

    # --- card boxes (§9.10) -----------------------------------------------

    def _box_or_none(conn, box_id: int):
        return conn.execute(
            "SELECT * FROM card_boxes WHERE id = ?", (box_id,)).fetchone()

    @app.get("/boxes", response_class=HTMLResponse)
    def boxes_page(request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)):
        boxes = conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM card_box_members m "
            " WHERE m.box_id = b.id) AS member_count "
            "FROM card_boxes b ORDER BY b.name").fetchall()
        return render(request, "boxes.html", {"boxes": boxes})

    @app.post("/boxes")
    async def create_box(request: Request,
                         conn: sqlite3.Connection = Depends(get_conn)):
        form = await _read_form(request)
        name = (form.get("name") or "").strip()
        if name:
            conn.execute(
                "INSERT INTO card_boxes (name, created_at) VALUES (?, ?) "
                "ON CONFLICT(name) DO NOTHING", (name, now_iso()))
            conn.commit()
        return RedirectResponse("/boxes", status_code=303)

    @app.post("/boxes/import")
    async def import_box(request: Request,
                         conn: sqlite3.Connection = Depends(get_conn)):
        form = await _read_form(request)
        try:
            payload = json.loads(form.get("payload") or "{}")
        except ValueError:
            return RedirectResponse("/boxes", status_code=303)
        name = (payload.get("name") or "imported box").strip() or "imported box"
        conn.execute(
            "INSERT INTO card_boxes (name, created_at) VALUES (?, ?) "
            "ON CONFLICT(name) DO NOTHING", (name, now_iso()))
        box = conn.execute(
            "SELECT id FROM card_boxes WHERE name = ?", (name,)).fetchone()
        for entry in payload.get("cards", []):
            key = entry.get("canonical_key") if isinstance(entry, dict) else None
            if not key:
                continue
            card = conn.execute(
                "SELECT id FROM cards WHERE canonical_key = ?", (key,)).fetchone()
            if card is not None:
                conn.execute(
                    "INSERT INTO card_box_members (box_id, card_id, added_at) "
                    "VALUES (?,?,?) ON CONFLICT(box_id, card_id) DO NOTHING",
                    (box["id"], card["id"], now_iso()))
        conn.commit()
        return RedirectResponse("/boxes/{0}".format(box["id"]), status_code=303)

    @app.get("/boxes/{box_id}", response_class=HTMLResponse)
    def box_page(request: Request, box_id: int,
                 conn: sqlite3.Connection = Depends(get_conn)):
        box = _box_or_none(conn, box_id)
        if box is None:
            return not_found(request, "card box")
        members = conn.execute(
            "SELECT c.id, c.tag, c.cite, c.team_count, c.school_count, m.note, "
            " (SELECT v.spoken FROM card_variants v WHERE v.card_id = c.id "
            "  AND v.spoken IS NOT NULL AND v.spoken != '' ORDER BY v.id LIMIT 1) "
            " AS spoken "
            "FROM card_box_members m JOIN cards c ON c.id = m.card_id "
            "WHERE m.box_id = ? ORDER BY m.added_at", (box_id,)).fetchall()
        total_words = sum(_spoken_word_count(m["spoken"] or "") for m in members)
        return render(request, "box.html", {
            "box": box, "members": members,
            "total_words": total_words,
            "read_time": _read_time_str(total_words, wpm),
            "wpm": wpm,
            "card_ids": ",".join(str(m["id"]) for m in members),
        })

    @app.post("/boxes/{box_id}/add")
    async def box_add(request: Request, box_id: int,
                      conn: sqlite3.Connection = Depends(get_conn)):
        form = await _read_form(request)
        box = _box_or_none(conn, box_id)
        try:
            card_id = int(form.get("card_id") or 0)
        except ValueError:
            card_id = 0
        # Validate the card still exists (it may have been merged away by
        # dedup while the page was open); a stale id redirects back without
        # inserting instead of 500ing on the FK constraint.
        card_exists = card_id and conn.execute(
            "SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone() is not None
        if box is not None and card_exists:
            conn.execute(
                "INSERT INTO card_box_members (box_id, card_id, note, added_at) "
                "VALUES (?,?,?,?) ON CONFLICT(box_id, card_id) DO NOTHING",
                (box_id, card_id, form.get("note"), now_iso()))
            conn.commit()
        back = form.get("back") or "/boxes/{0}".format(box_id)
        return RedirectResponse(back, status_code=303)

    @app.post("/boxes/{box_id}/remove")
    async def box_remove(request: Request, box_id: int,
                         conn: sqlite3.Connection = Depends(get_conn)):
        form = await _read_form(request)
        try:
            card_id = int(form.get("card_id") or 0)
        except ValueError:
            card_id = 0
        conn.execute("DELETE FROM card_box_members WHERE box_id = ? AND card_id = ?",
                     (box_id, card_id))
        conn.commit()
        return RedirectResponse("/boxes/{0}".format(box_id), status_code=303)

    @app.get("/boxes/{box_id}/export.json")
    def box_export(box_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        box = _box_or_none(conn, box_id)
        if box is None:
            return JSONResponse({"error": "no such box"}, status_code=404)
        members = conn.execute(
            "SELECT c.canonical_key, c.tag, c.cite, m.note "
            "FROM card_box_members m JOIN cards c ON c.id = m.card_id "
            "WHERE m.box_id = ? ORDER BY m.added_at", (box_id,)).fetchall()
        return JSONResponse({
            "name": box["name"],
            "exported_at": now_iso(),
            "cards": [dict(m) for m in members],
        })

    # --- export (§9.4) ----------------------------------------------------

    @app.post("/export/docx")
    async def export_docx_route(request: Request,
                                conn: sqlite3.Connection = Depends(get_conn)):
        if _export_cards_fn is None:
            return PlainTextResponse(
                "The .docx export module is not available yet.", status_code=503)
        form = await _read_form(request)
        raw_ids = []
        for chunk in form.getlist("ids"):
            raw_ids.extend(p for p in str(chunk).split(",") if p.strip())
        try:
            ids = [int(p) for p in raw_ids]
        except ValueError:
            return PlainTextResponse("bad card ids", status_code=400)
        if not ids:
            return PlainTextResponse("no cards selected", status_code=400)
        preset = form.get("preset") or "house"
        if preset not in ("house", "verbatim"):
            preset = "house"
        hl = _hl_name(form.get("hl") or request.query_params.get("hl"))
        fd, tmp = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        try:
            out = _export_cards_fn(conn, ids, Path(tmp), preset=preset, highlight=hl)
        except Exception as e:
            os.unlink(tmp)
            return PlainTextResponse("export failed: {0}".format(e), status_code=500)
        return FileResponse(
            str(out), filename="cards.docx",
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
            background=BackgroundTask(os.unlink, str(out)))

    # --- stats / about ----------------------------------------------------

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)):
        stats = _corpus_stats(conn)
        # analytics are excluded from card counts by default (spec §1.3),
        # matching _corpus_stats/_topic_groups; variants still count every
        # disclosure row.
        per_season = conn.execute(
            "SELECT cl.season AS season, "
            " COUNT(DISTINCT CASE WHEN c.is_analytic = 0 THEN v.card_id END) "
            "  AS cards, "
            " COUNT(*) AS variants "
            "FROM card_variants v JOIN cards c ON c.id = v.card_id "
            "JOIN rounds r ON r.id = v.round_id "
            "JOIN teams t ON t.id = r.team_id "
            "JOIN schools s ON s.id = t.school_id "
            "JOIN caselists cl ON cl.id = s.caselist_id "
            "WHERE cl.season IS NOT NULL GROUP BY cl.season ORDER BY cl.season"
        ).fetchall()
        per_topic = conn.execute(
            "SELECT tp.code AS code, COUNT(DISTINCT c.id) AS cards "
            "FROM topics tp LEFT JOIN rounds r ON r.topic_id = tp.id "
            "LEFT JOIN card_variants v ON v.round_id = r.id "
            "LEFT JOIN cards c ON c.id = v.card_id AND c.is_analytic = 0 "
            "GROUP BY tp.id ORDER BY tp.starts").fetchall()
        unassigned = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE topic_id IS NULL").fetchone()[0]
        docs = conn.execute(
            "SELECT COALESCE(parse_status, 'unparsed') AS status, COUNT(*) AS n "
            "FROM documents GROUP BY parse_status").fetchall()
        merges = conn.execute("SELECT COUNT(*) FROM card_merges").fetchone()[0]
        return render(request, "stats.html", {
            "stats": stats, "per_season": per_season, "per_topic": per_topic,
            "unassigned": unassigned, "docs": docs, "merges": merges,
        })

    @app.get("/about", response_class=HTMLResponse)
    def about_page(request: Request,
                   conn: sqlite3.Connection = Depends(get_conn)):
        return render(request, "about.html", {"stats": _corpus_stats(conn)})

    register_connect(app, templates)   # /connect, loopback-only (spec §0.4)

    return app
