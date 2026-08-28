"""scripts/build_site.py: the local index -> shipped site database.

The site is static (GitHub Pages cannot run Python), so the browser queries
a prebuilt SQLite file over HTTP Range requests with sql.js-httpvfs. That
makes the built file a contract, not an implementation detail: page size,
schema, FTS ranking, and the honesty of meta/subset_note are all asserted
here.

The fixture index is built through the REAL ingest path (insert_card /
attach_variant / finish_batch / topics.materialize_topic_ids), never by
hand-writing rows, so the builder is exercised against the same shapes the
production index produces.

  card         teams (reads)  topics              variants
  grid         AB, CD, EF (3) 2098-SO, 2097-SO    3, ratios .10 / .40 / .40
  queue        AB, CD (2)     2098-SO             2
  moratorium   AB (1)         2098-SO             1
  orphan       - (0)          -                   none (ships with NULL markup)
  analytic     AB (1)         2098-SO             1   is_analytic = 1

TODAY sits inside the 2098-SO window, so 2098-SO is 'present', 2098-ND is
announced-but-future (0 cards, must still ship, §6.3) and 2097-ND is a past
topic with no cards (must not ship).
"""
import hashlib
import importlib.util
import json
import random
import re
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from carddb.config import ROOT
from carddb.db import open_db
from carddb.ingest import (CardRecord, IngestStats, attach_variant, finish_batch,
                           get_or_create_caselist, get_or_create_round,
                           get_or_create_school, get_or_create_team, insert_card)
from carddb.rawstore import record_document
from carddb.topics import load_topics, materialize_topic_ids

TODAY = date(2098, 10, 15)          # inside the 2098-SO window
BUILT_AT = "2098-10-15T12:00:00Z"   # fixed so builds are byte-comparable

BUILD_SITE_PY = ROOT / "scripts" / "build_site.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_site", BUILD_SITE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bs = _load_builder()


# --- fixture corpus --------------------------------------------------------

VOCAB = ("evidence disclosure warrant impact uniqueness link turn framing "
         "reliability capacity siting permitting curtailment interconnection "
         "generation transmission demand economics analysis").split()


def _filler(seed, words=1400):
    """Deterministic bulk prose. Bodies have to be big enough that dropping
    cards visibly moves the file size, which is what the --max-bytes test
    measures."""
    rng = random.Random(seed)
    return " ".join(rng.choice(VOCAB) for _ in range(words))


TOPICS_JSON = {
    "_notes": "synthetic fixture",
    "topics": [
        {"code": "2097-SO", "season": 2097, "slot": "SO",
         "resolution": "Resolved: a past thing about zebrafish habitat.",
         "starts": "2097-09-01", "ends": "2097-10-31"},
        {"code": "2097-ND", "season": 2097, "slot": "ND",
         "resolution": "Resolved: a past thing nobody disclosed on.",
         "starts": "2097-11-01", "ends": "2097-12-31"},
        {"code": "2098-SO", "season": 2098, "slot": "SO",
         "resolution": "Resolved: The United States federal government should "
                       "enact a moratorium on hyperscale data center construction.",
         "starts": "2098-09-01", "ends": "2098-10-31"},
        {"code": "2098-ND", "season": 2098, "slot": "ND",
         "resolution": "Resolved: an announced topic with no cards yet.",
         "starts": "2098-11-01", "ends": "2098-12-31"},
    ],
    "overrides": [],
}

CARDS = {
    "grid": CardRecord(
        tag="Hyperscale zebrafish moratorium collapses interconnection queues",
        cite="Kessler '98",
        fullcite=('Kessler, Sarah 7-14-2098 [grid analyst], "Queue chaos," '
                  'Grid Journal, https://example.test/queue'),
        body_text="Grid reliability depends on queue reform. " + _filler(1),
        source_url="https://example.test/queue",
        source_pub_date="2098-07-14"),
    "queue": CardRecord(
        tag="Data centers strain rural power systems",
        cite="Diamond '96",
        fullcite='Diamond, R. 2096. "Rural load," Utility Review.',
        body_text="Rural feeders and zebrafish streams both suffer. " + _filler(2),
        source_url="https://example.test/rural",
        source_pub_date="2096-03-02"),
    "moratorium": CardRecord(
        tag="A construction pause buys planners time",
        cite="Rodgers and Cooper 94",
        fullcite='Rodgers, A. and Cooper, B. 2094. "Pause," Planning Quarterly.',
        body_text="Planners need slack in the siting pipeline. " + _filler(3),
        source_pub_date="2094-11-30"),
    "orphan": CardRecord(
        tag="An evidence card nobody has disclosed markup for",
        cite="Nguyen '97",
        body_text="This body exists with no variant attached. " + _filler(4)),
    "analytic": CardRecord(
        tag="No card here, just spillover assertion",
        is_analytic=True),
}

# round -> (team, side, date, topic code, caselist)
ROUNDS = {
    "r1": ("ab", "Pro", "2098-09-20", "2098-SO"),
    "r2": ("cd", "Con", "2098-10-02", "2098-SO"),
    "r3": ("ef", "Pro", "2097-09-15", "2097-SO"),
    "r4": ("ab", "Con", "2098-10-05", "2098-SO"),
}

# card -> [(round, ordinal, highlight_ratio, spoken)]
PLACEMENTS = {
    "grid": [("r1", 1, 0.10, "queue reform"),
             ("r2", 1, 0.40, "grid reliability depends on queue reform"),
             ("r3", 1, 0.40, "reliability queue reform")],
    "queue": [("r1", 2, 0.25, "rural feeders suffer"),
              ("r2", 2, 0.15, "feeders suffer")],
    "moratorium": [("r4", 1, 0.30, "planners need slack")],
    "orphan": [],
    "analytic": [("r4", 2, None, None)],
}


def build_index(db_path, topics_path):
    """The local full index, built the way ingest builds it."""
    conn = open_db(db_path)
    topics_path.write_text(json.dumps(TOPICS_JSON), encoding="utf-8")
    load_topics(conn, topics_path)
    topic_id = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM topics")}

    cl97 = get_or_create_caselist(conn, "hspf97", season=2097, event="pf")
    cl98 = get_or_create_caselist(conn, "hspf98", season=2098, event="pf")
    team = {
        "ab": get_or_create_team(conn, get_or_create_school(conn, cl98, "Millburn"), "AB"),
        "cd": get_or_create_team(conn, get_or_create_school(conn, cl98, "Testville"), "CD"),
        "ef": get_or_create_team(conn, get_or_create_school(conn, cl97, "Oldtown"), "EF"),
    }

    rid, doc = {}, {}
    for ext, (tkey, side, rdate, tcode) in ROUNDS.items():
        r = get_or_create_round(conn, team[tkey], ext, side=side,
                                tournament="Testville Invitational", round_date=rdate)
        conn.execute("UPDATE rounds SET topic_id = ? WHERE id = ?", (topic_id[tcode], r))
        rid[ext] = r
        doc[ext] = record_document(conn, "sha-" + ext, "test", None, None, None)

    ids, variant_ids = {}, {}
    for name, rec in CARDS.items():
        card_id, _ = insert_card(conn, rec)
        ids[name] = card_id
        for ext, ordn, ratio, spoken in PLACEMENTS[name]:
            markup = ("<h4><strong>%s</strong></h4><p><u>%s</u> %s</p>"
                      % (rec.tag, spoken or "", (rec.body_text or "")[:200]))
            vid, _ = attach_variant(
                conn, card_id,
                replace(rec, ordinal=ordn, highlight_ratio=ratio, spoken=spoken,
                        summary=(rec.body_text or "")[:120] or None,
                        markup_html=markup if rec.body_text else None,
                        pocket="Case", hat="C1 Grid", block="Block %s" % ext),
                doc[ext], rid[ext])
            variant_ids[(name, ext)] = vid

    finish_batch(conn, IngestStats(touched_card_ids=set(ids.values())))
    materialize_topic_ids(conn)
    conn.commit()
    conn.close()          # checkpoint the WAL: the builder reads a static file
    return ids, variant_ids


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    d = tmp_path_factory.mktemp("siteidx")
    db = d / "carddb.sqlite"
    ids, variant_ids = build_index(db, d / "topics.json")
    return {"db": db, "ids": ids, "variant_ids": variant_ids, "dir": d}


def run_build(index, tmp_path, **kw):
    kw.setdefault("today", TODAY)
    kw.setdefault("built_at", BUILT_AT)
    logs = []
    kw.setdefault("log", logs.append)
    out = tmp_path / "site" / "db" / "cards.sqlite"
    result = bs.build_site(db=index["db"], out=out, **kw)
    result["log"] = logs
    return result


def opened(path):
    conn = sqlite3.connect("file:%s?mode=ro" % Path(path).resolve(), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def built(index, tmp_path_factory):
    """Default build (analytics excluded, no caps), shared by most tests."""
    out_dir = tmp_path_factory.mktemp("sitebuild")
    logs = []
    result = bs.build_site(db=index["db"], out=out_dir / "db" / "cards.sqlite",
                           today=TODAY, built_at=BUILT_AT, log=logs.append)
    conn = opened(result["out"])
    yield {"result": result, "conn": conn, "ids": index["ids"], "log": logs}
    conn.close()


def meta_of(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


def card_ids(conn, where="1"):
    return [r["id"] for r in conn.execute("SELECT id FROM cards WHERE %s" % where)]


# --- schema contract -------------------------------------------------------

CARDS_COLUMNS = [
    ("id", "INTEGER", 1), ("tag", "TEXT", 0), ("cite", "TEXT", 0),
    ("fullcite", "TEXT", 0), ("body_text", "TEXT", 0), ("markup_html", "TEXT", 0),
    ("summary", "TEXT", 0), ("spoken", "TEXT", 0), ("source_url", "TEXT", 0),
    ("source_pub_date", "TEXT", 0), ("is_analytic", "INTEGER", 0),
    ("team_count", "INTEGER", 0), ("school_count", "INTEGER", 0),
    ("topic_codes", "TEXT", 0), ("pocket", "TEXT", 0), ("hat", "TEXT", 0),
    ("block", "TEXT", 0),
]
TOPICS_COLUMNS = [
    ("code", "TEXT", 1), ("season", "INTEGER", 0), ("slot", "TEXT", 0),
    ("resolution", "TEXT", 0), ("starts", "TEXT", 0), ("ends", "TEXT", 0),
    ("card_count", "INTEGER", 0),
]


def _table_info(conn, table):
    return [(r["name"], r["type"], r["pk"])
            for r in conn.execute("PRAGMA table_info(%s)" % table)]


def test_schema_matches_the_contract(built):
    conn = built["conn"]
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    user_tables = {t for t in tables
                   if not t.startswith("sqlite_") and not t.startswith("card_fts_")}
    assert user_tables == {"cards", "card_fts", "topics", "meta"}
    assert _table_info(conn, "cards") == CARDS_COLUMNS
    assert _table_info(conn, "topics") == TOPICS_COLUMNS
    assert _table_info(conn, "meta") == [("key", "TEXT", 1), ("value", "TEXT", 0)]


def test_no_builder_scratch_tables_survive(built):
    names = {r["name"] for r in built["conn"].execute(
        "SELECT name FROM sqlite_master")}
    assert not [n for n in names if n.startswith("_")]


def test_fts_table_is_a_real_fts5_with_the_spec_tokenizer(built):
    sql = built["conn"].execute(
        "SELECT sql FROM sqlite_master WHERE name = 'card_fts'").fetchone()["sql"]
    assert "fts5" in sql.lower()
    assert "porter unicode61 remove_diacritics 2" in sql
    assert [c[0] for c in _table_info(built["conn"], "card_fts")] == [
        "tag", "cite", "block", "body"]
    # not contentless: snippet() needs the stored text
    assert "content=" not in sql.replace(" ", "")
    row = built["conn"].execute(
        "SELECT COUNT(*) AS n FROM card_fts_content").fetchone()
    assert row["n"] == built["conn"].execute(
        "SELECT COUNT(*) AS n FROM cards").fetchone()["n"]


def test_page_size_and_journal_mode_suit_range_requests(built):
    conn = built["conn"]
    assert conn.execute("PRAGMA page_size").fetchone()[0] == bs.PAGE_SIZE
    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "delete"
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0  # VACUUMed


def test_ships_as_a_single_static_file(built):
    out = Path(built["result"]["out"])
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(out) + suffix).exists()


def test_analyze_ran(built):
    assert built["conn"].execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'"
    ).fetchone()[0] == 1


# --- search: the whole point ----------------------------------------------

def bm25_search(conn, match, limit=10):
    sql = ("SELECT c.id AS id, bm25(card_fts, 5.0, 3.0, 2.0, 1.0) AS score "
           "FROM card_fts JOIN cards c ON c.id = card_fts.rowid "
           "WHERE card_fts MATCH ? ORDER BY score LIMIT ?")
    return [r["id"] for r in conn.execute(sql, (match, limit))]


def test_fts_match_with_bm25_returns_the_expected_card(built):
    ids = built["ids"]
    hits = bm25_search(built["conn"], '"interconnection"')
    assert ids["grid"] in hits


def test_bm25_weights_rank_a_tag_hit_over_a_body_hit(built):
    ids = built["ids"]
    # 'zebrafish' is in grid's tag and queue's body; tag weighs 5.0, body 1.0
    hits = bm25_search(built["conn"], '"zebrafish"')
    assert hits[:2] == [ids["grid"], ids["queue"]]


def test_fts_rowid_is_the_card_id(built):
    conn = built["conn"]
    mismatched = conn.execute(
        "SELECT COUNT(*) FROM card_fts f JOIN cards c ON c.id = f.rowid "
        "WHERE f.body <> COALESCE(c.body_text, '')").fetchone()[0]
    assert mismatched == 0
    assert conn.execute("SELECT COUNT(*) FROM card_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def test_snippet_works_for_the_browser(built):
    row = built["conn"].execute(
        "SELECT snippet(card_fts, 3, '<mark>', '</mark>', '...', 10) AS s "
        "FROM card_fts WHERE card_fts MATCH ? LIMIT 1",
        ('"interconnection"',)).fetchone()
    assert "<mark>" in row["s"]


def test_fts_cite_column_covers_short_and_full_cite(built):
    ids = built["ids"]
    assert ids["grid"] in bm25_search(built["conn"], 'cite : "Kessler"')
    assert ids["grid"] in bm25_search(built["conn"], 'cite : "Grid Journal"')


def test_fts_block_column_covers_every_block_the_card_was_filed_under(built):
    # grid's three variants sit under three different block titles
    hits = bm25_search(built["conn"], 'block : "r3"')
    assert built["ids"]["grid"] in hits


# --- representative variant ------------------------------------------------

def test_representative_variant_is_the_most_highlighted(built, index):
    row = built["conn"].execute(
        "SELECT markup_html, spoken, block FROM cards WHERE id = ?",
        (built["ids"]["grid"],)).fetchone()
    # ratios .10 / .40 / .40 -> the first of the two .40s (lowest variant id)
    assert row["spoken"] == "grid reliability depends on queue reform"
    assert row["block"] == "Block r2"
    assert "<u>" in row["markup_html"]


def test_card_with_no_variant_still_ships_with_null_markup(built):
    row = built["conn"].execute(
        "SELECT markup_html, summary, spoken, body_text, topic_codes "
        "FROM cards WHERE id = ?", (built["ids"]["orphan"],)).fetchone()
    assert row is not None
    assert row["markup_html"] is None
    assert row["summary"] is None and row["spoken"] is None
    assert row["body_text"]           # the page falls back to plain body text
    assert row["topic_codes"] == "[]"


def test_aggregate_counts_come_across(built):
    row = built["conn"].execute(
        "SELECT team_count, school_count FROM cards WHERE id = ?",
        (built["ids"]["grid"],)).fetchone()
    assert row["team_count"] == 3
    assert row["school_count"] == 3


# --- topic codes -----------------------------------------------------------

def test_topic_codes_is_always_valid_json(built):
    for row in built["conn"].execute("SELECT id, topic_codes FROM cards"):
        assert row["topic_codes"] is not None
        codes = json.loads(row["topic_codes"])
        assert isinstance(codes, list)
        assert all(isinstance(c, str) for c in codes)


def test_topic_codes_carry_every_topic_a_card_was_read_on(built):
    row = built["conn"].execute(
        "SELECT topic_codes FROM cards WHERE id = ?",
        (built["ids"]["grid"],)).fetchone()
    assert json.loads(row["topic_codes"]) == ["2097-SO", "2098-SO"]
    # minified, and quoted the way the front end's LIKE '%"code"%' expects
    assert row["topic_codes"] == '["2097-SO","2098-SO"]'
    assert '"2098-SO"' in row["topic_codes"]


def test_topic_codes_are_derived_when_the_index_never_materialized_them(
        index, tmp_path):
    """An index where `carddb topics assign` has not run still ships correct
    topic codes: the builder falls back to variants -> rounds -> topics."""
    raw = Path(index["db"]).read_bytes()
    stale = tmp_path / "stale.sqlite"
    stale.write_bytes(raw)
    conn = sqlite3.connect(str(stale))
    conn.execute("UPDATE cards SET topic_ids = NULL")
    conn.commit()
    conn.close()

    out = tmp_path / "db" / "cards.sqlite"
    res = bs.build_site(db=stale, out=out, today=TODAY, built_at=BUILT_AT,
                        log=lambda *a: None)
    out = Path(res["out"])          # content-versioned filename
    conn = opened(out)
    try:
        got = {r["id"]: json.loads(r["topic_codes"])
               for r in conn.execute("SELECT id, topic_codes FROM cards")}
    finally:
        conn.close()
    ids = index["ids"]
    assert got[ids["grid"]] == ["2097-SO", "2098-SO"]   # same as materialized
    assert got[ids["queue"]] == ["2098-SO"]
    assert got[ids["orphan"]] == []


# --- topics table ----------------------------------------------------------

def test_topics_table_counts_cards_and_keeps_announced_topics(built):
    rows = {r["code"]: r for r in built["conn"].execute("SELECT * FROM topics")}
    assert set(rows) == {"2097-SO", "2098-SO", "2098-ND"}
    assert rows["2098-SO"]["card_count"] == 3      # grid, queue, moratorium
    assert rows["2097-SO"]["card_count"] == 1      # grid
    assert rows["2098-ND"]["card_count"] == 0      # announced, no cards yet
    assert rows["2098-ND"]["resolution"].startswith("Resolved:")
    assert rows["2098-SO"]["season"] == 2098 and rows["2098-SO"]["slot"] == "SO"


def test_past_topics_with_no_cards_are_not_shipped(built):
    codes = {r["code"] for r in built["conn"].execute("SELECT code FROM topics")}
    assert "2097-ND" not in codes


def test_topic_card_count_excludes_analytics(index, tmp_path):
    result = run_build(index, tmp_path, include_analytics=True)
    conn = opened(result["out"])
    try:
        row = conn.execute(
            "SELECT card_count FROM topics WHERE code = '2098-SO'").fetchone()
        analytics = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE is_analytic = 1").fetchone()[0]
    finally:
        conn.close()
    assert analytics == 1
    assert row["card_count"] == 3          # unchanged by the shipped analytic


# --- meta ------------------------------------------------------------------

REQUIRED_META = ("built_at", "card_count", "analytic_count", "team_count",
                 "school_count", "seasons_covered", "coverage_note",
                 "source_note")


def test_the_module_declares_the_contract_meta_keys():
    assert set(bs.META_KEYS) == set(REQUIRED_META)


def test_every_meta_key_in_the_contract_is_present(built):
    meta = meta_of(built["conn"])
    for key in REQUIRED_META:
        assert key in meta, key
        assert meta[key] != "", key


def test_meta_counts_describe_the_shipped_corpus(built):
    meta = meta_of(built["conn"])
    assert meta["card_count"] == "4"        # grid, queue, moratorium, orphan
    assert meta["analytic_count"] == "0"
    assert meta["team_count"] == "3"
    assert meta["school_count"] == "3"
    assert meta["seasons_covered"] == "2097-2098"
    assert meta["built_at"] == BUILT_AT


def test_coverage_note_is_the_honest_statement(built):
    note = meta_of(built["conn"])["coverage_note"].lower()
    assert "opencaselist" in note
    assert "not every card ever cut" in note
    assert "disclose" in note
    for oversell in ("every pf card", "complete", "exhaustive"):
        assert oversell not in note


def test_source_note_credits_the_dataset_and_its_license(built):
    note = meta_of(built["conn"])["source_note"]
    assert "Yusuf5/OpenCaselist" in note
    assert "MIT" in note
    assert "openCaselist" in note


def test_default_build_has_no_subset_note(built):
    assert "subset_note" not in meta_of(built["conn"])
    assert built["result"]["subset_note"] is None


# --- analytics -------------------------------------------------------------

def test_analytics_are_excluded_by_default(built):
    conn = built["conn"]
    assert conn.execute(
        "SELECT COUNT(*) FROM cards WHERE is_analytic = 1").fetchone()[0] == 0
    assert built["ids"]["analytic"] not in card_ids(conn)
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 4


def test_analytics_ship_with_the_flag(index, tmp_path):
    result = run_build(index, tmp_path, include_analytics=True)
    conn = opened(result["out"])
    try:
        assert index["ids"]["analytic"] in card_ids(conn)
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 5
        meta = meta_of(conn)
    finally:
        conn.close()
    assert meta["card_count"] == "4"
    assert meta["analytic_count"] == "1"


# --- selection flags -------------------------------------------------------

def test_max_cards_keeps_the_highest_value_cards(index, tmp_path):
    result = run_build(index, tmp_path, max_cards=2)
    conn = opened(result["out"])
    try:
        ids = card_ids(conn)
    finally:
        conn.close()
    assert len(ids) == 2
    assert set(ids) == {index["ids"]["grid"], index["ids"]["queue"]}


def test_min_reads_drops_rarely_read_cards(index, tmp_path):
    result = run_build(index, tmp_path, min_reads=2)
    conn = opened(result["out"])
    try:
        ids = card_ids(conn)
        assert min(r["team_count"] for r in
                   conn.execute("SELECT team_count FROM cards")) >= 2
    finally:
        conn.close()
    assert set(ids) == {index["ids"]["grid"], index["ids"]["queue"]}


def test_min_reads_and_max_cards_record_a_subset_note(index, tmp_path):
    result = run_build(index, tmp_path, min_reads=2)
    conn = opened(result["out"])
    try:
        meta = meta_of(conn)
    finally:
        conn.close()
    assert "subset_note" in meta
    assert "--min-reads 2" in meta["subset_note"]
    assert "4 canonical cards" in meta["subset_note"]  # the real denominator


# --- size control ----------------------------------------------------------

def test_byte_cap_shrinks_loudly_and_records_what_was_dropped(index, tmp_path):
    full = run_build(index, tmp_path / "full")
    cap = int(full["bytes"] * 0.6)

    result = run_build(index, tmp_path / "capped", max_bytes=cap)
    assert result["shrunk"] is True
    assert 0 < result["card_count"] < full["card_count"]
    # A database has a floor: the header plus at least one page per table, so
    # with a large page_size a small corpus cannot always reach an arbitrary
    # cap. The contract is that the builder shrinks as far as it can and then
    # reports honestly rather than pretending — never that any cap is meetable.
    if result["fits"]:
        assert result["bytes"] <= cap
    else:
        assert result["bytes"] > cap
        assert "CANNOT FIT" in "\n".join(result["log"])

    conn = opened(result["out"])
    try:
        meta = meta_of(conn)
        shipped = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    finally:
        conn.close()
    note = meta["subset_note"]
    assert shipped == result["card_count"]
    assert "%d of the 4 canonical cards" % shipped in note
    assert str(cap) in note                      # the exact cap that forced it
    assert "team_count" in note                  # and the rule used to choose
    assert int(meta["card_count"]) == shipped

    loud = "\n".join(result["log"])
    assert "SIZE CAP EXCEEDED" in loud
    # It either got under the cap, or said plainly that it could not.
    assert ("SHRUNK TO FIT" in loud) if result["fits"] else ("CANNOT FIT" in loud)


def test_byte_cap_drops_analytics_before_cards(index, tmp_path):
    full = run_build(index, tmp_path / "full", include_analytics=True)
    result = run_build(index, tmp_path / "capped", include_analytics=True,
                       max_bytes=int(full["bytes"] * 0.7))
    log = result["log"]
    dropped_analytics = [i for i, line in enumerate(log)
                         if "dropping analytics" in line]
    trimmed_cards = [i for i, line in enumerate(log) if "trying the top" in line]
    assert dropped_analytics, log
    assert trimmed_cards, log
    assert dropped_analytics[0] < trimmed_cards[0]   # analytics go first
    assert result["analytic_count"] == 0

    conn = opened(result["out"])
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM cards WHERE is_analytic = 1").fetchone()[0] == 0
        assert "analytics" in meta_of(conn)["subset_note"].lower()
    finally:
        conn.close()


def test_a_cap_that_cannot_be_met_fails_loudly_instead_of_lying(index, tmp_path):
    result = run_build(index, tmp_path, max_bytes=1024)
    assert result["fits"] is False
    assert "CANNOT FIT" in "\n".join(result["log"])
    # and the note still tells the truth about the full index
    conn = opened(result["out"])
    try:
        assert "of the 4 canonical cards" in meta_of(conn)["subset_note"]
    finally:
        conn.close()


def test_no_cap_ships_everything(built, index):
    assert built["result"]["shrunk"] is False
    assert built["result"]["card_count"] == 4


# --- front-end config ------------------------------------------------------

def test_config_json_describes_the_database(built):
    cfg = json.loads(Path(built["result"]["config"]).read_text(encoding="utf-8"))
    assert cfg["serverMode"] == "full"
    # The filename is content-versioned so cached chunks from a previous
    # deploy can never be mixed with a new build's.
    assert re.match(r"^db/cards-[0-9a-f]{10}\.sqlite$", cfg["url"]), cfg["url"]
    assert cfg["url"] == "db/" + Path(built["result"]["out"]).name
    assert cfg["requestChunkSize"] == bs.REQUEST_CHUNK_SIZE
    # every page the browser fetches must fit whole reads
    assert bs.REQUEST_CHUNK_SIZE % bs.PAGE_SIZE == 0
    # requestChunkSize need not equal page_size, but it must be a whole
    # multiple of it so a fetched block never straddles a page boundary.
    assert cfg["requestChunkSize"] % \
        built["conn"].execute("PRAGMA page_size").fetchone()[0] == 0
    assert cfg["databaseLengthBytes"] == Path(built["result"]["out"]).stat().st_size
    assert cfg["card_count"] == 4
    assert cfg["analytic_count"] == 0
    assert cfg["seasons_covered"] == "2097-2098"
    assert cfg["meta"] == meta_of(built["conn"])
    assert cfg["bm25Weights"] == [5.0, 3.0, 2.0, 1.0]


def test_config_json_sits_next_to_the_database(built):
    assert Path(built["result"]["config"]).parent == Path(built["result"]["out"]).parent


# --- safety ----------------------------------------------------------------

def test_the_source_index_is_never_written_to(index, tmp_path):
    before = hashlib.sha256(Path(index["db"]).read_bytes()).hexdigest()
    run_build(index, tmp_path, max_cards=1)
    after = hashlib.sha256(Path(index["db"]).read_bytes()).hexdigest()
    assert before == after
    # a read-only open of a WAL index may recreate the sidecars, but nothing
    # of ours is ever pending in them
    wal = Path(str(index["db"]) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_rebuilding_the_same_index_gives_the_same_bytes(index, tmp_path):
    a = run_build(index, tmp_path / "a")
    b = run_build(index, tmp_path / "b")
    assert Path(a["out"]).read_bytes() == Path(b["out"]).read_bytes()


def test_verify_rejects_a_wrong_page_size(tmp_path):
    bad = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(str(bad))
    conn.execute("PRAGMA page_size = 512")   # deliberately not PAGE_SIZE
    conn.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(bs.BuildError):
        bs.verify(bad, log=lambda *a: None)


def test_missing_source_is_a_clear_error(tmp_path):
    with pytest.raises(bs.BuildError):
        bs.build_site(db=tmp_path / "nope.sqlite", out=tmp_path / "out.sqlite",
                      log=lambda *a: None)


# --- CLI -------------------------------------------------------------------

def test_cli_builds_and_prints_size_and_count(index, tmp_path):
    out = tmp_path / "site" / "db" / "cards.sqlite"
    proc = subprocess.run(
        [sys.executable, str(BUILD_SITE_PY), "--db", str(index["db"]),
         "--out", str(out), "--max-cards", "3", "--today", "2098-10-15"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    # The built file is content-versioned; find it rather than assuming a name.
    built_files = sorted(out.parent.glob("cards-*.sqlite"))
    assert len(built_files) == 1, built_files
    out = built_files[0]
    assert out.exists()
    assert (out.parent / "config.json").exists()
    assert "3 cards" in proc.stdout
    assert "bytes" in proc.stdout
    conn = opened(out)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 3
    finally:
        conn.close()


def test_cli_rejects_a_bad_today(index, tmp_path):
    proc = subprocess.run(
        [sys.executable, str(BUILD_SITE_PY), "--db", str(index["db"]),
         "--out", str(tmp_path / "x.sqlite"), "--today", "not-a-date"],
        capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 2
