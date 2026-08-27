"""Feature-module tests: highlight consensus (§9.1), miscut heuristics
(§9.6), cite health (§9.5). No real network — citehealth runs against
httpx.MockTransport."""
import httpx
import pytest

from carddb.citehealth import check_url, run_citehealth, wayback_url
from carddb.consensus import (consensus, consensus_summary_vector,
                              variant_mark_vector, variant_summary_vector)
from carddb.db import open_db
from carddb.heuristics import (BRACKET_DENSITY_PER_100, ELLIPSIS_MIN,
                               HIGHLIGHT_RATIO_HIGH, HIGHLIGHT_RATIO_LOW,
                               LONG_BODY_CHARS, Flag, miscut_flags)
from carddb.ingest import CardRecord, insert_card


# =========================================================================
# Consensus (§9.1)
# =========================================================================

BODY = ("The grid will fail without new capacity and prices will spike "
        "across the region")
# tokens: 0 The | 1 grid | 2 will | 3 fail | 4 without | 5 new | 6 capacity
#         7 and | 8 prices | 9 will | 10 spike | 11 across | 12 the | 13 region

VARIANT_A = ('<h4>Tag A</h4><p><mark>The grid will fail</mark> without new '
             'capacity and <mark>prices will spike</mark> across the region</p>')
VARIANT_B = ('<p>The <mark>grid will fail</mark> without <u>new capacity</u> '
             'and prices <mark>will spike across</mark> the region</p>')
VARIANT_C = ('<p><mark>The grid</mark> will fail without new capacity and '
             'prices will <mark>spike</mark> across <mark>the region</mark></p>')


def test_variant_mark_vector_exact():
    vec = variant_mark_vector(BODY, VARIANT_A)
    assert vec == [True, True, True, True, False, False, False, False,
                   True, True, True, False, False, False]


def test_variant_mark_vector_heading_text_excluded():
    # 'Tag' / 'A' from the <h4> must not appear as body tokens.
    assert len(variant_mark_vector(BODY, VARIANT_A)) == len(BODY.split())


def test_consensus_three_variant_counts():
    got = consensus(BODY, [VARIANT_A, VARIANT_B, VARIANT_C])
    expected = [
        ("The", 2), ("grid", 3), ("will", 2), ("fail", 2),
        ("without", 0), ("new", 0), ("capacity", 0), ("and", 0),
        ("prices", 1), ("will", 2), ("spike", 3), ("across", 1),
        ("the", 1), ("region", 1),
    ]
    assert got == expected


def test_trimmed_variant_alignment():
    # A trimmed variant (§4.3 'trim' merge) keeps only the middle of the
    # body; marks land on the right body tokens, the rest stays unmarked.
    trimmed = '<p>without <mark>new capacity</mark> and prices</p>'
    vec = variant_mark_vector(BODY, trimmed)
    assert vec == [False, False, False, False, False, True, True, False,
                   False, False, False, False, False, False]


def test_diverged_variant_inserted_word():
    # An inserted word the body lacks is simply not represented; marks on
    # shared tokens still land.
    diverged = ('<p>The grid will fail without new <mark>generation '
                'capacity</mark> and prices will spike across the region</p>')
    vec = variant_mark_vector(BODY, diverged)
    assert vec == [False] * 6 + [True] + [False] * 7


def test_mark_split_mid_token_counts_as_marked():
    body = "interconnection queues collapse"
    markup = '<p>inter<mark>connection queues</mark> collapse</p>'
    assert variant_mark_vector(body, markup) == [True, True, False]


def test_paragraph_boundaries_separate_tokens():
    body = "alpha beta"
    markup = '<p>alpha</p><p><mark>beta</mark></p>'
    assert variant_mark_vector(body, markup) == [False, True]


def test_summary_vector_u_and_strong():
    vec = variant_summary_vector(BODY, VARIANT_B)
    assert vec == [False, False, False, False, False, True, True, False,
                   False, False, False, False, False, False]
    strong = '<p><strong>The grid</strong> will fail without new capacity '\
             'and prices will spike across the region</p>'
    vec2 = variant_summary_vector(BODY, strong)
    assert vec2[:2] == [True, True] and not any(vec2[2:])


def test_consensus_summary_vector():
    got = consensus_summary_vector(BODY, [VARIANT_A, VARIANT_B])
    # only VARIANT_B underlines anything: tokens 5, 6
    assert [tok for tok, _ in got] == BODY.split()
    assert [n for _, n in got] == [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0]


def test_consensus_empty_inputs():
    assert consensus("", ["<p><mark>x</mark></p>"]) == []
    assert variant_mark_vector(BODY, "") == [False] * 14
    assert consensus(BODY, []) == [(t, 0) for t in BODY.split()]


# =========================================================================
# Miscut heuristics (§9.6)
# =========================================================================

def _long_body(n_words=100):
    return " ".join("evidence{}".format(i) for i in range(n_words))


def _codes(flags):
    return {f.code for f in flags}


def test_flag_is_dataclass_with_fields():
    f = Flag("x", "label", "detail")
    assert (f.code, f.label, f.detail) == ("x", "label", "detail")


def test_highlight_ratio_high_triggers_on_long_body():
    body = _long_body()
    assert len(body) >= LONG_BODY_CHARS
    card = {"body_text": body, "body_len": len(body), "tag": None}
    flags = miscut_flags(card, [{"highlight_ratio": 0.95}])
    assert "hl_ratio_high" in _codes(flags)


def test_highlight_ratio_high_not_on_short_body():
    body = "short body here"
    card = {"body_text": body, "body_len": len(body), "tag": None}
    assert _codes(miscut_flags(card, [{"highlight_ratio": 0.95}])) == set()


def test_highlight_ratio_normal_no_flag():
    body = _long_body()
    card = {"body_text": body, "body_len": len(body), "tag": None}
    flags = miscut_flags(card, [{"highlight_ratio": 0.25}])
    assert "hl_ratio_high" not in _codes(flags)
    assert "hl_ratio_low" not in _codes(flags)


def test_highlight_ratio_low_triggers_on_long_body():
    body = _long_body()
    card = {"body_text": body, "body_len": len(body), "tag": None}
    flags = miscut_flags(card, [{"highlight_ratio": 0.01}])
    assert "hl_ratio_low" in _codes(flags)


def test_highlight_ratio_any_variant_triggers():
    body = _long_body()
    card = {"body_text": body, "body_len": len(body), "tag": None}
    variants = [{"highlight_ratio": 0.3}, {"highlight_ratio": 0.9}]
    assert "hl_ratio_high" in _codes(miscut_flags(card, variants))


def test_bracket_density_triggers():
    # 25 words, 2 insertions -> 8 per 100 words > threshold
    words = ["w{}".format(i) for i in range(23)] + ["[they]", "[the state]"]
    body = " ".join(words)
    card = {"body_text": body, "body_len": len(body), "tag": None}
    assert "bracket_density" in _codes(miscut_flags(card, []))


def test_bracket_density_not_triggered_when_sparse():
    words = ["w{}".format(i) for i in range(99)] + ["[sic]"]
    body = " ".join(words)  # 1 per 100 words, under threshold
    card = {"body_text": body, "body_len": len(body), "tag": None}
    assert "bracket_density" not in _codes(miscut_flags(card, []))
    assert 1.0 <= BRACKET_DENSITY_PER_100


def test_bracket_density_skipped_on_tiny_body():
    body = "[a] [b] [c] tiny"
    card = {"body_text": body, "body_len": len(body), "tag": None}
    assert "bracket_density" not in _codes(miscut_flags(card, []))


def test_ellipsis_triggers_mixed_forms():
    body = "start … middle ... and then … the end " + _long_body(10)
    card = {"body_text": body, "body_len": len(body), "tag": None}
    flags = miscut_flags(card, [])
    assert "ellipsis" in _codes(flags)
    assert ELLIPSIS_MIN == 3


def test_ellipsis_two_is_fine_and_dot_runs_count_once():
    body = "start ... middle ...... end"
    card = {"body_text": body, "body_len": len(body), "tag": None}
    assert "ellipsis" not in _codes(miscut_flags(card, []))


def test_power_tag_triggers_on_disjoint_tag_and_spoken():
    card = {"body_text": _long_body(), "body_len": 600,
            "tag": "Economic collapse guarantees widespread famine"}
    variants = [{"highlight_ratio": 0.3,
                 "spoken": "penguins waddle along frozen antarctic "
                           "coastlines hunting silver fish"}]
    assert "power_tag" in _codes(miscut_flags(card, variants))


def test_power_tag_not_triggered_when_tag_supported():
    card = {"body_text": _long_body(), "body_len": 600,
            "tag": "Economic collapse guarantees widespread famine"}
    variants = [{"highlight_ratio": 0.3,
                 "spoken": "economic collapse guarantees widespread famine "
                           "within months"}]
    assert "power_tag" not in _codes(miscut_flags(card, variants))


def test_power_tag_skipped_when_trivial():
    card = {"body_text": _long_body(), "body_len": 600, "tag": "Extend it"}
    variants = [{"highlight_ratio": 0.3, "spoken": "yes"}]
    assert "power_tag" not in _codes(miscut_flags(card, variants))


def test_analytic_card_no_flags():
    card = {"body_text": None, "body_len": 0, "tag": "We outweigh on scope",
            "is_analytic": 1}
    assert miscut_flags(card, []) == []


def test_sqlite_row_inputs(tmp_path):
    conn = open_db(tmp_path / "h.sqlite")
    body = _long_body()
    rec = CardRecord(tag="Grid fails", body_text=body)
    card_id, _ = insert_card(conn, rec)
    conn.execute(
        "INSERT INTO card_variants (card_id, document_id, ordinal, "
        " highlight_ratio, spoken) VALUES (?,?,?,?,?)",
        (card_id, None, 0, 0.95, "grid fails badly"),
    )
    card = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    variants = conn.execute(
        "SELECT * FROM card_variants WHERE card_id = ?", (card_id,)).fetchall()
    assert "hl_ratio_high" in _codes(miscut_flags(card, variants))
    conn.close()


# =========================================================================
# Cite health (§9.5) — httpx.MockTransport, no real network
# =========================================================================

URL_ALIVE = "https://ok.example.com/article"
URL_REDIR = "https://moved.example.com/a"
URL_WALL_403 = "https://wall403.example.com/a"
URL_WALL_BODY = "https://wall200.example.com/a"
URL_DEAD = "https://gone.example.com/a"
URL_ERROR = "https://boom.example.com/a"
URL_WWW = "https://plain.example.com/a"


def _handler(request):
    host = request.url.host
    if host == "ok.example.com":
        return httpx.Response(200, text="<html>the full article text</html>")
    if host == "moved.example.com":
        return httpx.Response(
            302, headers={"Location": "https://final.example.org/x"})
    if host == "final.example.org":
        return httpx.Response(200, text="<html>arrived elsewhere</html>")
    if host == "wall403.example.com":
        return httpx.Response(403, text="forbidden")
    if host == "wall200.example.com":
        return httpx.Response(
            200, text="<html>Please subscribe to continue reading.</html>")
    if host == "gone.example.com":
        return httpx.Response(404, text="not found")
    if host == "boom.example.com":
        raise httpx.ConnectError("no route to host", request=request)
    if host == "plain.example.com":
        return httpx.Response(
            301, headers={"Location": "https://www.plain.example.com/a"})
    if host == "www.plain.example.com":
        return httpx.Response(200, text="<html>same site, www added</html>")
    return httpx.Response(500, text="unexpected host")


@pytest.fixture
def mock_client():
    with httpx.Client(transport=httpx.MockTransport(_handler)) as c:
        yield c


def test_check_url_alive(mock_client):
    r = check_url(mock_client, URL_ALIVE)
    assert r["status"] == "alive"
    assert r["http_status"] == 200
    assert r["final_url"] == URL_ALIVE
    assert r["wayback_url"] is None


def test_check_url_redirected(mock_client):
    r = check_url(mock_client, URL_REDIR)
    assert r["status"] == "redirected"
    assert r["http_status"] == 200
    assert r["final_url"] == "https://final.example.org/x"
    assert r["wayback_url"] == "https://web.archive.org/web/*/" + URL_REDIR


def test_check_url_www_redirect_is_alive(mock_client):
    r = check_url(mock_client, URL_WWW)
    assert r["status"] == "alive"


def test_check_url_paywalled_by_status(mock_client):
    r = check_url(mock_client, URL_WALL_403)
    assert r["status"] == "paywalled"
    assert r["http_status"] == 403
    assert r["wayback_url"] == wayback_url(URL_WALL_403)


def test_check_url_paywalled_by_body_marker(mock_client):
    r = check_url(mock_client, URL_WALL_BODY)
    assert r["status"] == "paywalled"
    assert r["http_status"] == 200


def test_check_url_dead_http(mock_client):
    r = check_url(mock_client, URL_DEAD)
    assert r["status"] == "dead"
    assert r["http_status"] == 404
    assert r["wayback_url"] == wayback_url(URL_DEAD)


def test_check_url_dead_network_error(mock_client):
    r = check_url(mock_client, URL_ERROR)
    assert r["status"] == "dead"
    assert r["http_status"] is None
    assert r["final_url"] is None
    assert r["wayback_url"] == wayback_url(URL_ERROR)


def _seed_cards(conn):
    urls = [URL_ALIVE, URL_REDIR, URL_WALL_403, URL_DEAD]
    ids = []
    for i, url in enumerate(urls):
        rec = CardRecord(tag="t{}".format(i),
                         body_text="body text number {} for the card".format(i),
                         source_url=url)
        cid, _ = insert_card(conn, rec)
        ids.append(cid)
    # one card without a URL: must never be sampled
    no_url, _ = insert_card(conn, CardRecord(tag="nourl", body_text="no url body"))
    conn.commit()
    return ids, no_url


def test_run_citehealth_classifies_and_upserts(tmp_path, mock_client):
    conn = open_db(tmp_path / "c.sqlite")
    ids, no_url = _seed_cards(conn)
    n = run_citehealth(conn, limit=10, client=mock_client,
                       sleep=lambda s: None)
    assert n == 4
    got = {row["card_id"]: row["status"] for row in
           conn.execute("SELECT card_id, status FROM cite_health")}
    assert got == {ids[0]: "alive", ids[1]: "redirected",
                   ids[2]: "paywalled", ids[3]: "dead"}
    assert no_url not in got
    wb = conn.execute(
        "SELECT wayback_url FROM cite_health WHERE card_id = ?",
        (ids[3],)).fetchone()["wayback_url"]
    assert wb == "https://web.archive.org/web/*/" + URL_DEAD
    # rerun: upsert, not duplicate insert
    n2 = run_citehealth(conn, limit=10, client=mock_client,
                        sleep=lambda s: None)
    assert n2 == 4
    count = conn.execute("SELECT COUNT(*) AS c FROM cite_health").fetchone()["c"]
    assert count == 4
    conn.close()


def test_run_citehealth_never_checked_first(tmp_path, mock_client):
    conn = open_db(tmp_path / "c2.sqlite")
    ids, _ = _seed_cards(conn)
    assert run_citehealth(conn, limit=2, client=mock_client,
                          sleep=lambda s: None) == 2
    first = {r["card_id"] for r in
             conn.execute("SELECT card_id FROM cite_health")}
    assert len(first) == 2
    # second pass must pick the two still-unchecked cards, not recheck
    assert run_citehealth(conn, limit=2, client=mock_client,
                          sleep=lambda s: None) == 2
    now = {r["card_id"] for r in
           conn.execute("SELECT card_id FROM cite_health")}
    assert now == set(ids)
    conn.close()


def test_run_citehealth_respects_limit(tmp_path, mock_client):
    conn = open_db(tmp_path / "c3.sqlite")
    _seed_cards(conn)
    assert run_citehealth(conn, limit=1, client=mock_client,
                          sleep=lambda s: None) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM cite_health").fetchone()["c"] == 1
    conn.close()


def test_run_citehealth_paces_at_one_rps(tmp_path, mock_client):
    conn = open_db(tmp_path / "c4.sqlite")
    _seed_cards(conn)
    sleeps = []
    # a frozen clock makes every gap demand the full 1.0s interval
    run_citehealth(conn, limit=10, client=mock_client,
                   sleep=sleeps.append, clock=lambda: 0.0)
    assert sleeps == [pytest.approx(1.0)] * 3  # 4 requests -> 3 waits
    conn.close()


def test_run_citehealth_empty_db(tmp_path, mock_client):
    conn = open_db(tmp_path / "c5.sqlite")
    assert run_citehealth(conn, limit=10, client=mock_client,
                          sleep=lambda s: None) == 0
    conn.close()
