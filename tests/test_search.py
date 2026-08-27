"""Search + query-language tests. Spec §7 (grammar, ranking, filters,
sorts, latency), Appendix C query shapes, malformed-input degradation,
FTS injection safety, pagination, and the analytics default.

Corpus is built through the REAL ingest path (insert_card/attach_variant/
finish_batch) against a fixed today of 2026-09-15:

  card       cite                    teams (sides)          topic(s)   pub date
  grid       Kessler '26             AB(P), CD(P)           2026-SO    2026-07-14
  crypto     Diamond '13             XY(C)                  2025-SO    2013
  econ       Rodgers and Cooper 06   EF(C)                  2026-SO    2026-05-20
  framework  Smith et al. 24         CD(P), GH(P), XY(P)    2026-SO,   2025
                                                            2025-SO
  water      Nguyen '26              AB(C)                  2026-SO    2026 (bare)
  analytic   —  (block "A2: Moratorium")  AB(C)             2026-SO    —
"""
import sqlite3
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from carddb.db import open_db
from carddb.ingest import (CardRecord, IngestStats, attach_variant, finish_batch,
                           get_or_create_caselist, get_or_create_round,
                           get_or_create_school, get_or_create_team, insert_card)
from carddb.query import ParsedQuery, fts_quote, parse_query
from carddb.rawstore import record_document
from carddb.search import SearchHit, SearchResult, search

TODAY = date(2026, 9, 15)   # inside the 2026-SO window


# --- corpus ----------------------------------------------------------------

def _rec(**kw):
    return CardRecord(**kw)


CARDS = {
    "grid": _rec(
        tag="Moratorium collapses grid interconnection queues",
        cite="Kessler '26",
        fullcite=('Kessler, Sarah 7-14-2026 [energy analyst], "Queue chaos," '
                  'Grid Journal, https://example.test/queue'),
        body_text=("The interconnection queue has grown beyond any historical "
                   "precedent, and grid reliability now depends on rapid queue "
                   "reform across every regional transmission organization; "
                   "utilities and regulators alike concede the backlog is the "
                   "binding constraint on new generation."),
        source_url="https://example.test/queue",
        source_pub_date="2026-07-14",
        pocket="Case", hat="C1 Grid", block="Uniqueness",
        summary="The interconnection queue has grown; grid reliability depends on reform",
        spoken="queue has grown grid reliability depends on reform",
        highlight_ratio=0.2),
    "crypto": _rec(
        tag="Crypto mining strains rural power systems",
        cite="Diamond '13",
        fullcite=('Diamond, Lee 2013 [journalist], "Mining towns," Rural Wire, '
                  'https://example.test/mining'),
        body_text=("Bitcoin and crypto mining operations strain grid reliability "
                   "in rural cooperatives, forcing rate hikes; the "
                   "interconnection backlog and retail queue both grow."),
        source_pub_date="2013",
        pocket="Case", hat="Turns", block="Case Turn"),
    "econ": _rec(
        tag="Moratorium wrecks the economy",
        cite="Rodgers and Cooper 06",
        fullcite=('Rodgers, Ann and Cooper, Bo 2006 [economists], "Chill," '
                  'Econ Review, https://example.test/chill'),
        body_text=("A federal moratorium on construction would wreck the economy, "
                   "chilling investment for a decade, and the interconnection "
                   "queue would still stall projects."),
        source_pub_date="2026-05-20",
        pocket="Case", hat="Economy", block="Economy"),
    "framework": _rec(
        tag="Cost benefit analysis is the best framework",
        cite="Smith et al. 24",
        fullcite=('Smith, J., Doe, A., et al. 3-2-2024 [economists], '
                  '"Tradeoffs," Policy Review, https://example.test/cba'),
        body_text=("Cost benefit analysis remains the best framework for "
                   "evaluating public policy because it forces decision makers "
                   "to make tradeoffs explicit."),
        source_pub_date="2025",
        pocket="FW", hat="Framework", block="Framework"),
    "water": _rec(
        tag="Data centers drain local water supplies",
        cite="Nguyen '26",
        fullcite=('Nguyen, Ha 2026 [hydrologist], "Dry wells," Water Desk, '
                  'https://example.test/water'),
        body_text=("Hyperscale data centers consume millions of gallons of "
                   "water for cooling, draining local aquifers in drought "
                   "prone regions."),
        source_pub_date="2026",
        pocket="Case", hat="C2 Water", block="C2 Water"),
    "analytic": _rec(
        tag="No warrant for economic collapse claims",
        is_analytic=True,
        body_text=None,
        pocket="Blocks", hat="A2 Economy", block="A2: Moratorium"),
}

# card -> [(round_ext, ordinal)]
PLACEMENTS = {
    "grid": [("r1", 0), ("r2", 0)],
    "crypto": [("r3", 0)],
    "econ": [("r4", 0)],
    "framework": [("r2", 1), ("r5", 0), ("r6", 0)],
    "water": [("r7", 0)],
    "analytic": [("r7", 1)],
}

# ext -> (team_key, side, tournament, round_date, topic_code)
ROUNDS = {
    "r1": ("ab", "Pro", "Blake", "2026-09-13", "2026-SO"),
    "r2": ("cd", "Pro", "Glenbrooks", "2026-09-20", "2026-SO"),
    "r3": ("xy", "Con", "Apple Valley", "2025-09-20", "2025-SO"),
    "r4": ("ef", "Con", "Blake", "2026-09-14", "2026-SO"),
    "r5": ("gh", "Pro", "Blake", "2026-09-10", "2026-SO"),
    "r6": ("xy", "Pro", "Ridge", "2025-10-01", "2025-SO"),
    "r7": ("ab", "Con", "Glenbrooks", "2026-09-21", "2026-SO"),
}

TOPICS = [
    (2025, "SO", "2025-SO", "Resolved: a past thing.", "2025-09-01", "2025-10-31"),
    (2026, "SO", "2026-SO",
     "Resolved: The United States federal government should enact a moratorium "
     "on hyperscale data center construction.", "2026-09-01", "2026-10-31"),
    (2026, "ND", "2026-ND", "Resolved: a future thing.", "2026-11-01", "2026-12-31"),
]


def build_corpus(db_path):
    conn = open_db(db_path)
    topic_id = {}
    for season, slot, code, res, s, e in TOPICS:
        cur = conn.execute(
            "INSERT INTO topics (season, slot, code, resolution, starts, ends) "
            "VALUES (?,?,?,?,?,?)", (season, slot, code, res, s, e))
        topic_id[code] = cur.lastrowid

    cl25 = get_or_create_caselist(conn, "hspf25", season=2025, event="pf")
    cl26 = get_or_create_caselist(conn, "hspf26", season=2026, event="pf")
    millburn = get_or_create_school(conn, cl26, "Millburn")
    testville = get_or_create_school(conn, cl26, "Testville")
    riverdale = get_or_create_school(conn, cl26, "Riverdale")
    oldtown = get_or_create_school(conn, cl25, "Oldtown")
    team = {
        "ab": get_or_create_team(conn, millburn, "AB"),
        "cd": get_or_create_team(conn, testville, "CD"),
        "gh": get_or_create_team(conn, testville, "GH"),
        "ef": get_or_create_team(conn, riverdale, "EF"),
        "xy": get_or_create_team(conn, oldtown, "XY"),
    }

    rid, doc = {}, {}
    for ext, (tkey, side, tourn, rdate, tcode) in ROUNDS.items():
        r = get_or_create_round(conn, team[tkey], ext, side=side,
                                tournament=tourn, round_date=rdate)
        conn.execute("UPDATE rounds SET topic_id = ? WHERE id = ?",
                     (topic_id[tcode], r))
        rid[ext] = r
        doc[ext] = record_document(conn, "sha-" + ext, "test", None, None, None)

    ids = {}
    for name in ["grid", "crypto", "econ", "framework", "water", "analytic"]:
        rec = CARDS[name]
        card_id, _ = insert_card(conn, rec)
        ids[name] = card_id
        for ext, ordn in PLACEMENTS[name]:
            attach_variant(conn, card_id, replace(rec, ordinal=ordn),
                           doc[ext], rid[ext])

    finish_batch(conn, IngestStats(touched_card_ids=set(ids.values())))
    conn.commit()
    return conn, ids


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    db = tmp_path_factory.mktemp("searchdb") / "t.sqlite"
    conn, ids = build_corpus(db)
    yield SimpleNamespace(conn=conn, ids=ids)
    conn.close()


def s(c, q, **kw):
    kw.setdefault("today", TODAY)
    return search(c.conn, q, **kw)


def hit_ids(res):
    return [h.card_id for h in res.hits]


def hit_set(res):
    return set(hit_ids(res))


def want(c, *names):
    return {c.ids[n] for n in names}


# ==========================================================================
# parse_query
# ==========================================================================

def test_parse_bare_words_and():
    pq = parse_query("grid reliability")
    assert pq.fts == '"grid" AND "reliability"'
    assert pq.filters == {}
    assert pq.sort == "relevance"


def test_parse_phrase():
    pq = parse_query('"interconnection queue"')
    assert pq.fts == '"interconnection queue"'


def test_parse_exclude_with_positive():
    pq = parse_query("grid -crypto")
    assert pq.fts == '("grid") NOT ("crypto")'
    assert "exclude" not in pq.filters


def test_parse_exclude_phrase():
    pq = parse_query('grid -"rate hikes"')
    assert pq.fts == '("grid") NOT ("rate hikes")'


def test_parse_exclude_only():
    pq = parse_query("-crypto")
    assert pq.fts is None
    assert pq.filters["exclude"] == ['"crypto"']


def test_parse_every_fielded_operator():
    pq = parse_query('topic:2026-SO season:2025 side:pro school:"Millburn" '
                     'team:AB cite:kessler year:26 before:2026-01-01 '
                     'after:2025 is:analytic min_reads:5 sort:reads')
    f = pq.filters
    assert f["topic"] == "2026-SO"
    assert f["season"] == 2025
    assert f["side"] == "P"
    assert f["school"] == "Millburn"
    assert f["team"] == "AB"
    assert f["cite"] == "kessler"
    assert f["year"] == "26"
    assert f["before"] == "2026-01-01"
    assert f["after"] == "2025"
    assert f["is_analytic"] is True
    assert f["min_reads"] == 5
    assert pq.sort == "reads"
    assert pq.fts is None   # pure filter query


def test_parse_author_is_cite_alias():
    assert parse_query("author:kessler").filters["cite"] == "kessler"


def test_parse_side_values():
    assert parse_query("side:pro").filters["side"] == "P"
    assert parse_query("side:con").filters["side"] == "C"
    assert parse_query("side:CON").filters["side"] == "C"


def test_parse_year_four_digit_collapses():
    assert parse_query("year:2026").filters["year"] == "26"


def test_parse_sort_values():
    for v in ("relevance", "reads", "recent", "length"):
        assert parse_query("sort:" + v).sort == v
    assert parse_query("grid").sort == "relevance"


def test_parse_block_becomes_scoped_fts():
    pq = parse_query('block:"A2: Moratorium"')
    assert pq.fts == 'block:"A2: Moratorium"'
    assert pq.filters == {}


def test_parse_status_reserved_filter():
    assert parse_query("status:Answered").filters["status"] == "answered"


@pytest.mark.parametrize("q,degraded_term,absent_key", [
    ("year:", '"year:"', "year"),
    ("year:5", '"year:5"', "year"),
    ("year:abc", '"year:abc"', "year"),
    ("side:maybe", '"side:maybe"', "side"),
    ("season:25", '"season:25"', "season"),
    ("min_reads:soon", '"min_reads:soon"', "min_reads"),
    ("before:tomorrow", '"before:tomorrow"', "before"),
    ("is:winning", '"is:winning"', "is_analytic"),
    ("foo:bar", '"foo:bar"', "foo"),
])
def test_parse_malformed_operator_degrades_to_term(q, degraded_term, absent_key):
    pq = parse_query(q)
    assert pq.fts == degraded_term
    assert absent_key not in pq.filters


def test_parse_bad_sort_degrades_and_keeps_default():
    pq = parse_query("grid sort:banana")
    assert pq.sort == "relevance"
    assert pq.fts == '"grid" AND "sort:banana"'


def test_parse_unclosed_quote_consumes_rest():
    pq = parse_query('topic:"unclosed')
    assert pq.filters["topic"] == "unclosed"
    pq2 = parse_query('"unclosed phrase')
    assert pq2.fts == '"unclosed phrase"'


def test_parse_stray_punctuation_dropped():
    for q in (":", "::: -", "-", '"', "''", "..."):
        pq = parse_query(q)
        assert pq.fts is None
        assert "exclude" not in pq.filters


def test_parse_embedded_quotes_doubled():
    assert parse_query('a"b').fts == '"a""b"'
    assert fts_quote('say "hi"') == '"say ""hi"""'


def test_parse_never_raises():
    nasty = ["", None, "   ", '"""', "-:-:-", "a -", '-"', "x:" * 50,
             "topic:", "\x00weird", "term " * 500, '"a" -"b" c:d -e:f']
    for q in nasty:
        pq = parse_query(q)
        assert isinstance(pq, ParsedQuery)


# ==========================================================================
# search: FTS terms, phrases, exclusion, relevance
# ==========================================================================

def test_bare_words_are_anded(corpus):
    res = s(corpus, "grid reliability")
    assert hit_set(res) == want(corpus, "grid", "crypto")
    assert res.total == 2


def test_relevance_ranks_tag_hits_first(corpus):
    # "grid" is in the grid card's TAG (weight 5.0) but only crypto's BODY.
    res = s(corpus, "grid reliability")
    assert res.hits[0].card_id == corpus.ids["grid"]


def test_phrase_vs_bare_words(corpus):
    # crypto has "interconnection ... queue" non-adjacent: words match,
    # the exact phrase must not.
    words = s(corpus, "interconnection queue")
    phrase = s(corpus, '"interconnection queue"')
    assert hit_set(words) == want(corpus, "grid", "crypto", "econ")
    assert hit_set(phrase) == want(corpus, "grid", "econ")


def test_exclusion_with_positive_terms(corpus):
    res = s(corpus, "grid reliability -crypto")
    assert hit_set(res) == want(corpus, "grid")


def test_exclusion_only_query(corpus):
    res = s(corpus, "-crypto")
    assert hit_set(res) == want(corpus, "grid", "econ", "framework", "water")


def test_snippet_bolds_match_terms(corpus):
    res = s(corpus, "reliability")
    assert res.total == 2
    for h in res.hits:
        assert "<b>" in h.snippet_html and "</b>" in h.snippet_html
        assert "reliab" in h.snippet_html.lower()


# ==========================================================================
# search: fielded filters
# ==========================================================================

def test_topic_present(corpus):
    res = s(corpus, "topic:present")
    assert hit_set(res) == want(corpus, "grid", "econ", "framework", "water")


def test_topic_past(corpus):
    res = s(corpus, "topic:past")
    assert hit_set(res) == want(corpus, "crypto", "framework")


def test_topic_future_has_no_cards(corpus):
    assert s(corpus, "topic:future").total == 0


def test_topic_code(corpus):
    res = s(corpus, "topic:2026-SO")
    assert hit_set(res) == want(corpus, "grid", "econ", "framework", "water")


def test_topic_unknown_code_matches_nothing(corpus):
    res = s(corpus, "topic:9999-ZZ")
    assert res.total == 0 and res.hits == []


def test_season(corpus):
    assert hit_set(s(corpus, "season:2025")) == want(corpus, "crypto", "framework")
    assert hit_set(s(corpus, "season:2026")) == want(
        corpus, "grid", "econ", "framework", "water")


def test_side(corpus):
    assert hit_set(s(corpus, "side:pro")) == want(corpus, "grid", "framework")
    assert hit_set(s(corpus, "side:con")) == want(corpus, "crypto", "econ", "water")


def test_school_quoted_and_case_insensitive(corpus):
    assert hit_set(s(corpus, 'school:"Millburn"')) == want(corpus, "grid", "water")
    assert hit_set(s(corpus, "school:millburn")) == want(corpus, "grid", "water")
    assert hit_set(s(corpus, 'school:"Testville"')) == want(
        corpus, "grid", "framework")


def test_team(corpus):
    assert hit_set(s(corpus, "team:XY")) == want(corpus, "crypto", "framework")
    assert hit_set(s(corpus, "team:ab")) == want(corpus, "grid", "water")


def test_cite_and_author_alias(corpus):
    assert hit_set(s(corpus, "cite:kessler")) == want(corpus, "grid")
    assert hit_set(s(corpus, "author:KESSLER")) == want(corpus, "grid")


def test_year_two_digit_cite_year(corpus):
    assert hit_set(s(corpus, "year:26")) == want(corpus, "grid", "water")
    assert hit_set(s(corpus, "year:13")) == want(corpus, "crypto")
    assert hit_set(s(corpus, "year:06")) == want(corpus, "econ")   # leading zero
    assert hit_set(s(corpus, "year:24")) == want(corpus, "framework")
    assert hit_set(s(corpus, "year:2026")) == want(corpus, "grid", "water")


def test_before_after_iso_and_bare_year(corpus):
    # before: earliest possible pub date strictly before the bound
    assert hit_set(s(corpus, "before:2026-01-01")) == want(
        corpus, "crypto", "framework")
    # after: bare-year "2026" could fall after 2026-06-01, so water qualifies
    assert hit_set(s(corpus, "after:2026-06-01")) == want(corpus, "grid", "water")
    # bare-year bound: after:2026 = on/after 2026 began, so econ (May 2026)
    # qualifies alongside grid and bare-year water
    assert hit_set(s(corpus, "after:2026")) == want(corpus, "grid", "econ", "water")
    assert hit_set(s(corpus, "before:2014")) == want(corpus, "crypto")


def test_min_reads(corpus):
    assert hit_set(s(corpus, "min_reads:2")) == want(corpus, "grid", "framework")
    assert hit_set(s(corpus, "min_reads:3")) == want(corpus, "framework")
    assert s(corpus, "min_reads:99").total == 0


def test_block_scoped_search(corpus):
    assert hit_set(s(corpus, 'block:"Framework"')) == want(corpus, "framework")
    assert s(corpus, 'block:"No Such Block"').total == 0


# ==========================================================================
# search: analytics default and flip
# ==========================================================================

def test_analytics_excluded_by_default(corpus):
    # "collapse" stems to match both the grid tag ("collapses") and the
    # analytic tag ("collapse"); only the evidence card may appear.
    res = s(corpus, "collapse")
    assert hit_set(res) == want(corpus, "grid")


def test_is_analytic_flips_to_analytics_only(corpus):
    res = s(corpus, "is:analytic collapse")
    assert hit_set(res) == want(corpus, "analytic")
    assert res.hits[0].is_analytic is True
    listing = s(corpus, "is:analytic")
    assert hit_set(listing) == want(corpus, "analytic")


# ==========================================================================
# search: sorts
# ==========================================================================

def test_sort_reads(corpus):
    res = s(corpus, "sort:reads")
    assert res.hits[0].card_id == corpus.ids["framework"]
    assert res.hits[0].team_count == 3
    assert res.hits[1].card_id == corpus.ids["grid"]
    counts = [h.team_count for h in res.hits]
    assert counts == sorted(counts, reverse=True)


def test_sort_recent(corpus):
    res = s(corpus, "sort:recent")
    # max round_date: water 09-21 > grid/framework 09-20 (id tiebreak)
    # > econ 09-14 > crypto 2025.
    assert hit_ids(res) == [corpus.ids[n] for n in
                            ("water", "grid", "framework", "econ", "crypto")]


def test_sort_length(corpus):
    res = s(corpus, "sort:length")
    expected = [r["id"] for r in corpus.conn.execute(
        "SELECT id FROM cards WHERE is_analytic = 0 "
        "ORDER BY body_len DESC, id")]
    assert hit_ids(res) == expected


def test_relevance_without_fts_falls_back_to_reads(corpus):
    res = s(corpus, "")
    assert res.hits[0].card_id == corpus.ids["framework"]
    assert res.total == 5


def test_sort_with_fts_terms(corpus):
    res = s(corpus, "interconnection sort:length")
    lens = [h.body_len for h in res.hits]
    assert lens == sorted(lens, reverse=True)


# ==========================================================================
# pure filtered listings (no MATCH)
# ==========================================================================

def test_pure_filter_listing_without_match(corpus):
    res = s(corpus, "topic:present sort:reads")
    assert res.query.fts is None
    assert res.total == 4
    assert res.hits[0].card_id == corpus.ids["framework"]
    # listing snippets come from the body, unbolded
    assert res.hits[0].snippet_html
    assert "<b>" not in res.hits[0].snippet_html


def test_empty_query_lists_evidence_cards(corpus):
    res = s(corpus, "")
    assert res.total == 5
    assert len(res.hits) == 5
    assert all(not h.is_analytic for h in res.hits)


# ==========================================================================
# Appendix C query shapes, verbatim
# ==========================================================================

def test_appendix_c_q1(corpus):
    res = s(corpus, 'topic:present side:con "interconnection queue"')
    assert hit_set(res) == want(corpus, "econ")


def test_appendix_c_q2(corpus):
    res = s(corpus, "cite:kessler year:26 sort:recent")
    assert hit_set(res) == want(corpus, "grid")
    assert res.query.sort == "recent"


def test_appendix_c_q3(corpus):
    res = s(corpus, "grid reliability -crypto after:2026-06-01 min_reads:5")
    assert res.total == 0    # grid matches everything except min_reads:5
    eased = s(corpus, "grid reliability -crypto after:2026-06-01 min_reads:2")
    assert hit_set(eased) == want(corpus, "grid")


def test_appendix_c_q4(corpus):
    res = s(corpus, 'topic:2026-SO is:analytic block:"A2: Moratorium"')
    assert hit_set(res) == want(corpus, "analytic")


# ==========================================================================
# pagination
# ==========================================================================

def test_pagination_pages_are_disjoint_and_complete(corpus):
    pages = [s(corpus, "sort:reads", limit=2, offset=o) for o in (0, 2, 4)]
    assert [p.total for p in pages] == [5, 5, 5]
    ids = [hit_ids(p) for p in pages]
    assert [len(x) for x in ids] == [2, 2, 1]
    flat = [i for page in ids for i in page]
    assert len(set(flat)) == 5


def test_pagination_with_python_year_filter(corpus):
    # year: uses a registered SQL function; LIMIT/OFFSET must still page.
    p0 = s(corpus, "year:26", limit=1, offset=0)
    p1 = s(corpus, "year:26", limit=1, offset=1)
    p2 = s(corpus, "year:26", limit=1, offset=2)
    assert p0.total == p1.total == 2
    assert len(p0.hits) == len(p1.hits) == 1
    assert p0.hits[0].card_id != p1.hits[0].card_id
    assert {p0.hits[0].card_id, p1.hits[0].card_id} == want(corpus, "grid", "water")
    assert p2.hits == []


def test_limit_caps_hits_not_total(corpus):
    res = s(corpus, "", limit=2)
    assert len(res.hits) == 2 and res.total == 5


# ==========================================================================
# malformed input and injection safety
# ==========================================================================

def test_malformed_queries_never_raise(corpus):
    for q in ("year:", 'topic:"unclosed', ":", "foo:bar", "-", '""', '"',
              "sort:", "min_reads:-3", "side:", "is:", "before:whenever",
              "::::", 'school:"'):
        res = s(corpus, q)
        assert isinstance(res, SearchResult)
        assert res.total >= 0


def test_year_colon_degrades_to_term(corpus):
    res = s(corpus, "year:")
    assert "year" not in res.query.filters
    assert res.total == 0    # no card contains the token "year"


def test_unclosed_topic_quote_matches_nothing(corpus):
    res = s(corpus, 'topic:"unclosed')
    assert res.query.filters["topic"] == "unclosed"
    assert res.total == 0


def test_stray_colon_is_a_plain_listing(corpus):
    res = s(corpus, ":")
    assert res.query.fts is None
    assert res.total == 5


def test_fts_operators_are_neutralized(corpus):
    # OR / NOT as bare words must be literal tokens, not FTS operators:
    # no fixture text contains "or"/"not", so an AND of the literals is 0,
    # while a real OR would match and a real NOT would raise or filter.
    assert s(corpus, "grid OR crypto").total == 0
    assert s(corpus, "grid NOT reliability").total == 0


def test_injection_strings_never_raise(corpus):
    nasty = [
        "'; DROP TABLE cards; --",
        'a" OR "b',
        "(((",
        "NEAR(a b)",
        "grid* cry*",
        "^grid",
        '"unclosed OR *',
        "tag:^{}",
        "body:grid",     # unknown-to-grammar field name degrades
        "-{-}-",
        'x AND y OR z NOT "w',
    ]
    for q in nasty:
        res = s(corpus, q)
        assert isinstance(res, SearchResult)
    # the table survived
    assert corpus.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 6


# ==========================================================================
# hit shape, topic codes, totals, timing
# ==========================================================================

def test_hit_fields(corpus):
    res = s(corpus, "cite:kessler")
    h = res.hits[0]
    assert h.card_id == corpus.ids["grid"]
    assert h.tag == "Moratorium collapses grid interconnection queues"
    assert h.cite == "Kessler '26"
    assert h.body_len == len(CARDS["grid"].body_text)
    assert h.is_analytic is False
    assert h.team_count == 2 and h.school_count == 2
    assert h.source_pub_date == "2026-07-14"
    assert h.topic_codes == ["2026-SO"]
    assert res.query.filters["cite"] == "kessler"


def test_topic_codes_span_topics(corpus):
    res = s(corpus, "framework")
    assert hit_set(res) == want(corpus, "framework")
    assert res.hits[0].topic_codes == ["2025-SO", "2026-SO"]


def test_materialized_topic_ids_preferred(corpus):
    conn = corpus.conn
    gid = corpus.ids["grid"]
    try:
        conn.execute("UPDATE cards SET topic_ids = ? WHERE id = ?",
                     ('["9999-XX"]', gid))
        res = s(corpus, "cite:kessler")
        assert res.hits[0].topic_codes == ["9999-XX"]
        # integer-id form maps through the topics table
        tid = conn.execute("SELECT id FROM topics WHERE code = '2026-SO'").fetchone()[0]
        conn.execute("UPDATE cards SET topic_ids = ? WHERE id = ?",
                     ("[%d]" % tid, gid))
        res = s(corpus, "cite:kessler")
        assert res.hits[0].topic_codes == ["2026-SO"]
    finally:
        conn.execute("UPDATE cards SET topic_ids = NULL WHERE id = ?", (gid,))
        conn.commit()


def test_total_is_exact_and_elapsed_measured(corpus):
    res = s(corpus, "grid reliability", limit=1)
    assert res.total == 2 and len(res.hits) == 1
    assert res.elapsed_ms >= 0.0
    assert isinstance(res.query, ParsedQuery)


def test_status_filter_inert_without_prep_status_table(corpus):
    res = s(corpus, "status:answered")
    assert res.query.filters["status"] == "answered"
    assert res.total == 5    # no prep_status table -> filter is inert


# ==========================================================================
# latency sanity (generous p95-style bound on the fixture corpus)
# ==========================================================================

def test_latency_p95_bound(corpus):
    queries = [
        "grid reliability",
        '"interconnection queue"',
        "grid reliability -crypto after:2026-06-01 min_reads:5",
        'topic:present side:con "interconnection queue"',
        "cite:kessler year:26 sort:recent",
        'topic:2026-SO is:analytic block:"A2: Moratorium"',
        "topic:present sort:reads",
        "sort:recent", "sort:length", "-crypto",
        "school:millburn team:ab", "year:26", "before:2026-01-01",
        "", "min_reads:2 side:pro",
    ]
    samples = []
    for _ in range(3):
        for q in queries:
            samples.append(s(corpus, q).elapsed_ms)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * len(samples))))]
    assert p95 < 250.0, "p95 latency %.1fms exceeds bound" % p95
