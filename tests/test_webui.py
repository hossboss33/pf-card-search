"""Web UI tests (spec §8, §9 features 1/2/3/5/6/9/10/11).

Runs a FastAPI TestClient over a tmp seeded database. All fixture text is
clearly synthetic ("Fixture card body ...") so nobody mistakes it for real
disclosed evidence. Also runs scripts/style_lint.py as a test, and checks
the empty-database renders (the M0 done-check).
"""
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from carddb import server as server_mod
from carddb.db import open_db
from carddb.ingest import (CardRecord, IngestStats, attach_variant,
                           finish_batch, get_or_create_caselist,
                           get_or_create_round, get_or_create_school,
                           get_or_create_team, insert_card)
from carddb.rawstore import now_iso
from carddb.server import create_app

ROOT = Path(__file__).resolve().parent.parent
TODAY = date.today()

BODY1 = ("Fixture card body: the interconnection queue has grown beyond any "
         "historical precedent, and grid planners say reliability depends on reform.")
BODY2 = ("Fixture card body: queue statistics are inflated by duplicative "
         "speculative applications, so headline growth numbers mislead planners.")
BODY4 = ("Fixture card body: an older-topic passage about school meal funding "
         "that belongs to the previous fixture resolution.")

MARKUP1A = ('<p><u>Fixture card body</u>: the <mark>interconnection queue has '
            'grown</mark> beyond <span class="min">any historical precedent</span>, '
            'and grid planners say <strong><u>reliability depends on reform</u>'
            '</strong>.</p>')
MARKUP1B = ('<p>Fixture card body: the <mark>interconnection queue</mark> has '
            'grown beyond any historical precedent, and <mark>grid planners say '
            'reliability depends on reform</mark>.</p>')


def _seed(conn):
    ids = {}
    cl = get_or_create_caselist(conn, "hspf25", season=2025, event="pf")
    s1 = get_or_create_school(conn, cl, "Testville High")
    s2 = get_or_create_school(conn, cl, "Otherton Academy")
    t1 = get_or_create_team(conn, s1, "TeVi", display_name="Testville VI")
    t2 = get_or_create_team(conn, s2, "OtTo", display_name="Otherton TO")
    ids.update(school1=s1, school2=s2, team1=t1, team2=t2)

    topics = [
        ("T-PAST", 2025, "FEB", "Fixture resolution: the previous synthetic topic.",
         (TODAY - timedelta(days=120)).isoformat(), (TODAY - timedelta(days=60)).isoformat()),
        ("T-PRES", 2025, "SO", "Fixture resolution: the present synthetic topic.",
         (TODAY - timedelta(days=10)).isoformat(), (TODAY + timedelta(days=20)).isoformat()),
        ("T-FUT", 2025, "ND", "Fixture resolution: an announced future synthetic topic.",
         (TODAY + timedelta(days=40)).isoformat(), (TODAY + timedelta(days=70)).isoformat()),
    ]
    for code, season, slot, res, starts, ends in topics:
        conn.execute(
            "INSERT INTO topics (code, season, slot, resolution, starts, ends) "
            "VALUES (?,?,?,?,?,?)", (code, season, slot, res, starts, ends))

    r1 = get_or_create_round(conn, t1, "r-1", side="A", tournament="Test Invitational",
                             round_label="R1", report="Fixture round report text.",
                             round_date=(TODAY - timedelta(days=5)).isoformat())
    r2 = get_or_create_round(conn, t2, "r-2", side="N", tournament="Sample Classic",
                             round_label="R3",
                             round_date=(TODAY - timedelta(days=3)).isoformat())
    r3 = get_or_create_round(conn, t1, "r-3", side="A", tournament="Old Open",
                             round_label="R2",
                             round_date=(TODAY - timedelta(days=90)).isoformat())
    ids.update(round1=r1, round2=r2, round3=r3)
    conn.execute("UPDATE rounds SET topic_id = (SELECT id FROM topics WHERE code='T-PRES') "
                 "WHERE id IN (?, ?)", (r1, r2))
    conn.execute("UPDATE rounds SET topic_id = (SELECT id FROM topics WHERE code='T-PAST') "
                 "WHERE id = ?", (r3,))

    for n in (1, 2, 3):
        conn.execute("INSERT INTO documents (sha256, origin) VALUES (?, 'test')",
                     ("fixture-doc-%d" % n,))
    d1, d2, d3 = [r[0] for r in conn.execute(
        "SELECT id FROM documents ORDER BY id").fetchall()]

    stats = IngestStats()

    rec1a = CardRecord(
        tag="Fixture: grid reliability requires queue reform",
        cite="Kessler '26",
        fullcite=('Kessler, Sarah [energy systems analyst at Fixture Institute], '
                  '7-14-2026, "Fixture article," Fixture Journal, '
                  'https://example.org/fixture-article'),
        body_text=BODY1, source_url="https://example.org/fixture-article",
        source_pub_date="2026-07-14", pocket="Case", hat="C1 Grid",
        block="Grid Reliability", markup_html=MARKUP1A,
        summary="Fixture card body reliability depends on reform",
        spoken="interconnection queue has grown", highlight_ratio=0.2, ordinal=0)
    c1, _ = insert_card(conn, rec1a)
    attach_variant(conn, c1, rec1a, d1, r1)

    rec1b = CardRecord(
        tag="Fixture: grid reliability requires queue reform",
        cite="Kessler '26", body_text=BODY1, pocket="Case", hat="Grid",
        block="Grid Reliability", markup_html=MARKUP1B,
        summary="Fixture card body", spoken="interconnection queue grid planners "
        "say reliability depends on reform", highlight_ratio=0.5, ordinal=0)
    c1b, created = insert_card(conn, rec1b)
    assert c1b == c1 and not created  # same body, one canonical card
    attach_variant(conn, c1, rec1b, d2, r2)

    rec2 = CardRecord(
        tag="Fixture answer: queue growth is overstated",
        cite="Rivera '25",
        fullcite='Rivera, Jo [fixture fellow], 2025, "Fixture reply," https://example.net/reply',
        body_text=BODY2, source_url="https://example.net/reply",
        pocket="Answers", hat="Grid", block="A2: Grid Reliability",
        markup_html="<p><mark>queue statistics are inflated</mark></p>",
        summary="queue statistics are inflated",
        spoken="queue statistics are inflated", highlight_ratio=0.3, ordinal=1)
    c2, _ = insert_card(conn, rec2)
    attach_variant(conn, c2, rec2, d2, r2)

    rec3 = CardRecord(tag="Fixture analytic: asserted without evidence",
                      is_analytic=True, block="Grid Reliability", ordinal=2)
    c3, _ = insert_card(conn, rec3)
    attach_variant(conn, c3, rec3, d2, r2)

    rec4 = CardRecord(
        tag="Fixture: older topic card about meal funding",
        cite="Chen '24", body_text=BODY4, block="Funding",
        markup_html="<p><mark>school meal funding</mark></p>",
        spoken="school meal funding", highlight_ratio=0.2, ordinal=0)
    c4, _ = insert_card(conn, rec4)
    attach_variant(conn, c4, rec4, d3, r3)

    # a card whose stored markup is hostile: the server must re-sanitize
    # markup_html at render time before |safe (spec §8 security rule)
    rec5 = CardRecord(
        tag="Fixture: stored markup is hostile",
        cite="Mallory '25",
        body_text="Fixture card body: hostile markup fixture text.",
        block="Grid Reliability",
        markup_html='<script>alert(1)</script><p onclick="x">hostile but visible</p>',
        spoken="hostile", ordinal=1)
    c5, _ = insert_card(conn, rec5)
    attach_variant(conn, c5, rec5, d3, r3)

    for cid, codes in ((c1, ["T-PRES"]), (c2, ["T-PRES"]), (c4, ["T-PAST"]),
                       (c5, ["T-PAST"])):
        conn.execute("UPDATE cards SET topic_ids = ? WHERE id = ?",
                     (json.dumps(codes), cid))
    stats.touched_card_ids.update([c1, c2, c3, c4, c5])
    finish_batch(conn, stats)

    conn.execute(
        "INSERT INTO cite_health (card_id, status, http_status, wayback_url, checked_at) "
        "VALUES (?, 'dead', 404, ?, ?)",
        (c1, "https://web.archive.org/web/2026/https://example.org/fixture-article",
         now_iso()))
    conn.execute("INSERT INTO card_boxes (name, created_at) VALUES ('Fixture box', ?)",
                 (now_iso(),))
    box_id = conn.execute("SELECT id FROM card_boxes WHERE name='Fixture box'").fetchone()[0]
    for cid in (c1, c2):
        conn.execute(
            "INSERT INTO card_box_members (box_id, card_id, added_at) VALUES (?,?,?)",
            (box_id, cid, now_iso()))
    conn.commit()
    ids.update(card1=c1, card2=c2, card3=c3, card4=c4, card5=c5, box=box_id)
    return ids


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    dbp = tmp_path_factory.mktemp("webui") / "seeded.sqlite"
    conn = open_db(dbp)
    ids = _seed(conn)
    conn.close()
    app = create_app(db_path=dbp)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, ids=ids, db=dbp)


# --- every page renders with sentinel content ------------------------------

def test_index_page(env):
    r = env.client.get("/")
    assert r.status_code == 200
    assert "PF card search" in r.text
    assert "T-PRES" in r.text                      # topic picker
    assert "Present" in r.text and "Past" in r.text and "Future" in r.text
    assert "Seasons covered" in r.text             # empty-state corpus stats
    assert "the present synthetic topic" in r.text  # current topic shown
    assert "copy spoken text" in r.text            # keyboard map in footer


def test_search_html(env):
    r = env.client.get("/search", params={"q": "interconnection"})
    assert r.status_code == 200
    assert "Fixture: grid reliability requires queue reform" in r.text
    assert "read by 2 teams" in r.text
    assert "2 schools" in r.text


def test_search_json_shape(env):
    r = env.client.get("/search", params={"q": "interconnection", "format": "json"})
    assert r.status_code == 200
    data = r.json()
    assert set(["hits", "total", "elapsed_ms"]) <= set(data)
    assert data["total"] >= 1
    assert isinstance(data["elapsed_ms"], (int, float))
    hit = data["hits"][0]
    for field in ("card_id", "tag", "cite", "snippet_html", "team_count",
                  "school_count", "topic_codes", "source_pub_date", "is_analytic"):
        assert field in hit
    assert hit["tag"].startswith("Fixture:")


def test_search_topic_filter(env):
    r = env.client.get("/search", params={"q": "fixture", "topic": "T-PAST",
                                          "format": "json"})
    assert r.status_code == 200
    tags = [h["tag"] for h in r.json()["hits"]]
    assert any("older topic" in t for t in tags)
    assert not any("queue reform" in t for t in tags)


def test_card_page(env):
    r = env.client.get("/card/%d" % env.ids["card1"])
    assert r.status_code == 200
    text = r.text
    assert "Fixture: grid reliability requires queue reform" in text
    assert "Kessler" in text
    assert "energy systems analyst" in text            # fullcite shown
    assert "The parts of a cut card" in text           # legend (§8.2)
    assert "<mark>" in text                            # highlighting rendered
    assert "Consensus" in text                         # §9.1 mode offered
    assert 'data-count=' in text                       # consensus token spans
    assert "Download .docx" in text and "Copy spoken text" in text
    assert "Test Invitational" in text                 # provenance table
    assert "Sample Classic" in text
    assert "Pro" in text and "Con" in text             # sides displayed Pro/Con
    assert "Wayback snapshot" in text                  # cite health, dead link
    assert "Fixture answer: queue growth is overstated" in text  # A2 index (§9.3)
    assert "read by 2 teams" in text                   # lineage line (§9.2)
    assert "<svg" in text                              # disclosure sparkline


def test_card_markup_resanitized(env):
    r = env.client.get("/card/%d" % env.ids["card5"])
    assert r.status_code == 200
    assert "<script" not in r.text.replace('<script src="/static/app.js">', "")
    assert "onclick" not in r.text
    assert "hostile but visible" in r.text


def test_card_spoken_txt(env):
    r = env.client.get("/card/%d/spoken.txt" % env.ids["card1"])
    assert r.status_code == 200
    assert "interconnection queue has grown" in r.text


def test_topic_pages(env):
    r = env.client.get("/topic/T-PRES")
    assert r.status_code == 200
    assert "the present synthetic topic" in r.text
    assert "RSS feed" in r.text
    assert "Pro" in r.text and "Con" in r.text          # side volume (§9.8)
    assert "Most-read cards" in r.text
    assert "Top cited authors" in r.text
    assert "example.org" in r.text                      # source domains
    assert "<svg" in r.text                             # cards/day sparkline

    r = env.client.get("/topic/T-FUT")
    assert r.status_code == 200
    assert "Announced. 0 cards disclosed yet." in r.text  # §6.3

    assert env.client.get("/topic/NOPE").status_code == 404


def test_school_and_team_pages(env):
    r = env.client.get("/school/%d" % env.ids["school1"])
    assert r.status_code == 200
    assert "Testville High" in r.text
    assert "T-PRES" in r.text                    # grouped by topic
    assert "queue reform" in r.text

    r = env.client.get("/team/%d" % env.ids["team2"])
    assert r.status_code == 200
    assert "Otherton TO" in r.text
    assert "Sample Classic" in r.text
    assert "Fixture answer: queue growth is overstated" in r.text


def test_round_page(env):
    r = env.client.get("/round/%d" % env.ids["round2"])
    assert r.status_code == 200
    assert "Sample Classic" in r.text
    assert "Con" in r.text
    # the whole parsed doc in ordinal order with jump anchors (§9.11)
    assert 'id="doc-card-0"' in r.text and 'id="doc-card-1"' in r.text
    assert r.text.index("queue reform") < r.text.index("queue growth is overstated")

    r = env.client.get("/round/%d" % env.ids["round1"])
    assert "Fixture round report text." in r.text


def test_authors_page(env):
    r = env.client.get("/authors")
    assert r.status_code == 200
    assert "Kessler" in r.text  # Jinja escapes the apostrophe in the short cite
    assert "energy systems analyst at Fixture Institute" in r.text  # quals mined
    assert "example.org" in r.text                                  # domains table


def test_boxes_pages(env):
    r = env.client.get("/boxes")
    assert r.status_code == 200
    assert "Fixture box" in r.text

    r = env.client.get("/boxes/%d" % env.ids["box"])
    assert r.status_code == 200
    assert "spoken words" in r.text and "wpm" in r.text   # §9.22-style math
    assert "Download .docx" in r.text
    assert "queue reform" in r.text

    r = env.client.get("/boxes/%d/export.json" % env.ids["box"])
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Fixture box"
    assert len(data["cards"]) == 2
    assert all("canonical_key" in c for c in data["cards"])


def test_boxes_create_add_remove_import(env):
    c = env.client
    r = c.post("/boxes", data={"name": "Fixture second box"})
    assert r.status_code == 200 and "Fixture second box" in r.text
    conn = open_db(env.db)
    bid = conn.execute("SELECT id FROM card_boxes WHERE name='Fixture second box'"
                       ).fetchone()[0]
    key = conn.execute("SELECT canonical_key FROM cards WHERE id=?",
                       (env.ids["card1"],)).fetchone()[0]
    conn.close()

    r = c.post("/boxes/%d/add" % bid, data={"card_id": env.ids["card1"]})
    assert r.status_code == 200 and "queue reform" in r.text
    r = c.post("/boxes/%d/remove" % bid, data={"card_id": env.ids["card1"]})
    assert r.status_code == 200 and "This box is empty" in r.text

    payload = json.dumps({"name": "Fixture imported box",
                          "cards": [{"canonical_key": key}]})
    r = c.post("/boxes/import", data={"payload": payload})
    assert r.status_code == 200
    assert "Fixture imported box" in r.text and "queue reform" in r.text


def test_rss_feed(env):
    r = env.client.get("/feed/topic/T-PRES.rss")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    assert r.text.startswith('<?xml version="1.0"')
    assert "queue reform" in r.text
    assert "<rss" in r.text and "</rss>" in r.text
    assert env.client.get("/feed/topic/NOPE.rss").status_code == 404


def test_stats_page(env):
    r = env.client.get("/stats")
    assert r.status_code == 200
    assert "Canonical cards" in r.text
    assert "unassigned" in r.text.lower()   # §6.2: never silently guess topics


def test_about_page(env):
    r = env.client.get("/about")
    assert r.status_code == 200
    assert "Public Forum evidence disclosed on openCaselist" in r.text  # §2.4
    assert "Yusuf5/OpenCaselist" in r.text
    assert "ashtarcommunications/caselist" in r.text
    assert "Verbatim" in r.text
    assert "Comparisons intentionally omitted" in r.text   # §12.8
    assert "!" not in r.text.split("<body>")[-1].replace("!=", "").replace(
        "!important", "")


def test_export_docx_route(env):
    r = env.client.post("/export/docx",
                        data={"ids": str(env.ids["card1"]), "preset": "house",
                              "hl": "green"})
    if server_mod._export_cards_fn is None:
        assert r.status_code == 503
        pytest.skip("carddb.export_docx not built yet; route degrades to 503")
    assert r.status_code == 200
    assert "wordprocessingml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"  # a real zip container

    r = env.client.post("/export/docx", data={"ids": ""})
    assert r.status_code == 400


def test_hl_setting_wiring(env):
    # swatches for the four Word base colors, wired to --hl + data-hl
    r = env.client.get("/")
    for hexv in ("#00FF00", "#FFFF00", "#0000FF", "#00FFFF"):
        assert hexv in r.text
    css = (ROOT / "static" / "style.css").read_text()
    assert "--hl: #00FF00" in css                    # default bright green
    assert '[data-hl="blue"]' in css and "#ffffff" in css  # blue: white text
    js = (ROOT / "static" / "app.js").read_text()
    assert "localStorage" in js and "--hl" in js


# --- design constraints ----------------------------------------------------

def test_css_tokens_and_print(env):
    css = (ROOT / "static" / "style.css").read_text()
    for token in ("--font: Calibri, Carlito", "--ink: #1a1a1a", "--paper: #ffffff",
                  "--rule: #d9d9d9", "--meta: #5f5f5f", "--accent: #1f4e79",
                  "--min-size: 9px"):
        assert token in css, token
    assert "@media print" in css
    assert "@font-face" in css and "Carlito-Regular.ttf" in css
    assert "#55606e" in css      # visited links stay visible, without purple


def test_app_js_under_300_lines(env):
    lines = (ROOT / "static" / "app.js").read_text().splitlines()
    assert len(lines) < 300


def test_style_lint_passes(env):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "style_lint.py")],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the M0 done-check: everything renders on an empty database ------------

def test_empty_db_renders(tmp_path):
    app = create_app(db_path=tmp_path / "empty.sqlite")
    with TestClient(app) as client:
        for path in ("/", "/search?q=grid", "/authors", "/boxes", "/stats",
                     "/about"):
            r = client.get(path)
            assert r.status_code == 200, path
        r = client.get("/search", params={"q": "anything", "format": "json"})
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert client.get("/card/1").status_code == 404
        assert client.get("/topic/2026-SO").status_code == 404
