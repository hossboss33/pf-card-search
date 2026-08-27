"""Topics tests: seed loading (§6.1), status computation (§6.3), token
resolution, and the §6.2 assignment ladder incl. the Unassigned bucket and
cards.topic_ids materialization. All 'today' values are fixed dates."""
import json
from datetime import date

import pytest

from carddb.config import ROOT
from carddb.db import open_db
from carddb.ingest import (CardRecord, attach_variant, get_or_create_caselist,
                           get_or_create_round, get_or_create_school,
                           get_or_create_team, insert_card)
from carddb.topics import (assign_topics, current_topic, load_topics,
                           materialize_topic_ids, resolve_topic_token,
                           topic_status)

REAL_TOPICS = ROOT / "data" / "topics.json"

MINI = {
    "_notes": "synthetic fixture",
    "topics": [
        {"code": "2098-SO", "season": 2098, "slot": "SO",
         "resolution": "Resolved: Test question about zebra migration corridors.",
         "starts": "2098-09-01", "ends": "2098-10-31", "source_url": "http://t"},
        {"code": "2098-ND", "season": 2098, "slot": "ND",
         "resolution": "Resolved: Test question about quantum kumquat tariffs.",
         "starts": "2098-11-01", "ends": "2098-12-31", "source_url": "http://t"},
        {"code": "2098-NATS", "season": 2098, "slot": "NATS",
         "resolution": "Resolved: Test question about lunar lighthouse construction.",
         "starts": "2099-06-01", "ends": "2099-06-30", "source_url": "http://t"},
    ],
    "overrides": [{"match": "nsda nationals", "slot": "NATS"}],
}


def _mini(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    p = tmp_path / "topics.json"
    p.write_text(json.dumps(MINI), encoding="utf-8")
    n = load_topics(conn, p)
    assert n == 3
    return conn, p


def _code_of(conn, round_id):
    row = conn.execute(
        "SELECT t.code AS code FROM rounds r LEFT JOIN topics t ON t.id = r.topic_id "
        "WHERE r.id = ?", (round_id,)).fetchone()
    return row["code"]


# --- load_topics (upsert) --------------------------------------------------

def test_load_topics_upsert_idempotent(tmp_path):
    conn, p = _mini(tmp_path)
    assert load_topics(conn, p) == 3  # second load: same rows, no dupes
    assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM topic_overrides").fetchone()[0] == 1

    changed = json.loads(json.dumps(MINI))
    changed["topics"][0]["resolution"] = "Resolved: Edited."
    p.write_text(json.dumps(changed), encoding="utf-8")
    load_topics(conn, p)
    row = conn.execute("SELECT resolution FROM topics WHERE code='2098-SO'").fetchone()
    assert row["resolution"] == "Resolved: Edited."


# --- topic_status (§6.3: computed, never stored) ---------------------------

def test_topic_status_boundaries(tmp_path):
    conn, _ = _mini(tmp_path)
    so = conn.execute("SELECT * FROM topics WHERE code='2098-SO'").fetchone()
    assert topic_status(so, date(2098, 8, 31)) == "future"
    assert topic_status(so, date(2098, 9, 1)) == "present"    # starts inclusive
    assert topic_status(so, date(2098, 10, 31)) == "present"  # ends inclusive
    assert topic_status(so, date(2098, 11, 1)) == "past"


# --- resolve_topic_token ---------------------------------------------------

def test_resolve_topic_token(tmp_path):
    conn, _ = _mini(tmp_path)
    ids = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM topics")}
    today = date(2098, 11, 15)  # inside the ND window
    assert resolve_topic_token(conn, "present", today) == [ids["2098-ND"]]
    assert resolve_topic_token(conn, "past", today) == [ids["2098-SO"]]
    assert resolve_topic_token(conn, "future", today) == [ids["2098-NATS"]]
    assert resolve_topic_token(conn, "2098-so", today) == [ids["2098-SO"]]  # case-insensitive
    assert resolve_topic_token(conn, "2098-NATS", today) == [ids["2098-NATS"]]
    assert resolve_topic_token(conn, "bogus", today) == []
    # between windows nothing is present
    assert resolve_topic_token(conn, "present", date(2099, 1, 5)) == []


# --- current_topic ---------------------------------------------------------

def test_current_topic(tmp_path):
    conn, _ = _mini(tmp_path)
    row = current_topic(conn, date(2098, 9, 15))
    assert row is not None and row["code"] == "2098-SO"
    assert current_topic(conn, date(2099, 1, 5)) is None


# --- assign_topics: every §6.2 branch --------------------------------------

def _mini_rounds(conn):
    """Six rounds exercising each assignment branch, with variants."""
    cl = get_or_create_caselist(conn, "hspf98", season=2098, event="pf")
    sc = get_or_create_school(conn, cl, "Testville")
    tm = get_or_create_team(conn, sc, "TeVi")
    r = {
        # branch 2: date inside the SO window
        "date": get_or_create_round(conn, tm, "r-date", round_date="2098-09-15"),
        # branch 2 beats branch 3: dated NSDA Nationals round in the ND window
        "precedence": get_or_create_round(conn, tm, "r-prec",
                                          round_date="2098-11-20",
                                          tournament="NSDA Nationals"),
        # branch 3: no date, tournament override -> season's NATS topic
        "override": get_or_create_round(conn, tm, "r-ovr",
                                        tournament="NSDA Nationals 2099"),
        # branch 4: no date, distinctive keywords in the variants' block text
        "keyword": get_or_create_round(conn, tm, "r-kw",
                                       tournament="Lakeville Invite"),
        # branch 5: nothing matches -> Unassigned bucket
        "none": get_or_create_round(conn, tm, "r-none",
                                    tournament="Mystery Open",
                                    round_date="not-a-date"),
        # branch 4 tie: equally matches two topics -> never silently guess
        "tie": get_or_create_round(conn, tm, "r-tie"),
    }
    conn.execute("INSERT INTO documents (sha256, origin) VALUES ('d1', 'test')")

    def rec(body, ordinal, **kw):
        return CardRecord(tag=f"tag {ordinal}", body_text=body,
                          ordinal=ordinal, **kw)

    # card X: read in two rounds that land on two different topics
    x1 = rec("Body of card X, quite unique text.", 0)
    cx, _ = insert_card(conn, x1)
    attach_variant(conn, cx, x1, 1, r["date"])
    x2 = rec("Body of card X, quite unique text.", 1)
    attach_variant(conn, cx, x2, 1, r["override"])
    # card Y: only in the unassigned round
    y = rec("Body of card Y, different text entirely.", 2, hat="Framework")
    cy, _ = insert_card(conn, y)
    attach_variant(conn, cy, y, 1, r["none"])
    # card Z: keyword round (zebra block) + tie round (ambiguous text)
    z1 = rec("Body of card Z, its own words here.", 3,
             block="A2: Zebra Migration Bad")
    cz, _ = insert_card(conn, z1)
    attach_variant(conn, cz, z1, 1, r["keyword"])
    z2 = rec("Body of card Z, its own words here.", 4,
             hat="Zebra kumquat mashup")
    attach_variant(conn, cz, z2, 1, r["tie"])
    return r, {"x": cx, "y": cy, "z": cz}


def test_assign_topics_all_branches(tmp_path):
    conn, _ = _mini(tmp_path)
    r, cards = _mini_rounds(conn)
    st = assign_topics(conn, today=date(2098, 12, 1))

    assert st.rounds == 6
    assert st.by_date == 2          # r-date, r-prec
    assert st.by_override == 1      # r-ovr
    assert st.by_keyword == 1       # r-kw
    assert st.unassigned == 2       # r-none, r-tie

    assert _code_of(conn, r["date"]) == "2098-SO"
    assert _code_of(conn, r["precedence"]) == "2098-ND"   # date beats override
    assert _code_of(conn, r["override"]) == "2098-NATS"
    assert _code_of(conn, r["keyword"]) == "2098-SO"
    assert _code_of(conn, r["none"]) is None              # Unassigned bucket
    assert _code_of(conn, r["tie"]) is None               # ambiguity -> NULL

    # topic_ids materialization: sorted JSON array of codes across rounds
    def codes(cid):
        return json.loads(conn.execute(
            "SELECT topic_ids FROM cards WHERE id = ?", (cid,)).fetchone()[0])
    assert codes(cards["x"]) == ["2098-NATS", "2098-SO"]
    assert codes(cards["y"]) == []
    assert codes(cards["z"]) == ["2098-SO"]

    # re-run: identical outcome (idempotent, derived not incremented)
    st2 = assign_topics(conn, today=date(2098, 12, 1))
    assert (st2.by_date, st2.by_override, st2.by_keyword, st2.unassigned) == \
           (st.by_date, st.by_override, st.by_keyword, st.unassigned)
    assert codes(cards["x"]) == ["2098-NATS", "2098-SO"]


def test_assign_no_season_uses_global_date_window(tmp_path):
    conn, _ = _mini(tmp_path)
    # a round with no team (so no caselist season) but a usable date
    conn.execute("INSERT INTO rounds (round_date, external_id) VALUES ('2098-12-05', 'r-ns')")
    st = assign_topics(conn)
    rid = conn.execute("SELECT id FROM rounds WHERE external_id='r-ns'").fetchone()["id"]
    assert _code_of(conn, rid) == "2098-ND"
    assert st.by_date == 1


def test_assign_unknown_season_lands_unassigned(tmp_path):
    conn, _ = _mini(tmp_path)
    cl = get_or_create_caselist(conn, "hspf99", season=1999, event="pf")
    sc = get_or_create_school(conn, cl, "Oldtown")
    tm = get_or_create_team(conn, sc, "OlTo")
    rid = get_or_create_round(conn, tm, "r-old", round_date="1999-10-05")
    st = assign_topics(conn)
    assert _code_of(conn, rid) is None
    assert st.unassigned == 1


# --- the real seed file (data/topics.json, spec §6.1) ----------------------

MONTHLY_SLOTS = {"SO", "NOV", "DEC", "JAN", "FEB", "MA", "APR", "NATS"}
MODERN_SLOTS = {"SO", "ND", "JAN", "FEB", "MA", "APR", "NATS"}


@pytest.fixture(scope="module")
def real():
    assert REAL_TOPICS.exists(), "data/topics.json missing"
    return json.loads(REAL_TOPICS.read_text(encoding="utf-8"))


def test_real_file_shape(real):
    assert isinstance(real["_notes"], str) and real["_notes"]
    assert isinstance(real["topics"], list) and isinstance(real["overrides"], list)
    codes = [t["code"] for t in real["topics"]]
    assert len(codes) == len(set(codes))
    for t in real["topics"]:
        assert t["code"] == f"{t['season']}-{t['slot']}"
        assert t["resolution"].startswith("Resolved: ")
        assert t["source_url"].startswith("http")
        assert date.fromisoformat(t["starts"]) < date.fromisoformat(t["ends"])
        assert not t.get("unverified"), f"unverified row shipped: {t['code']}"


def test_real_file_covers_every_season_with_correct_cadence(real):
    by_season = {}
    for t in real["topics"]:
        by_season.setdefault(t["season"], set()).add(t["slot"])
    # dataset era through last completed season
    for season in range(2013, 2018):        # monthly era (Nov/Dec separate)
        assert by_season[season] == MONTHLY_SLOTS, season
    for season in range(2018, 2026):        # Nov/Dec combined era
        assert by_season[season] == MODERN_SLOTS, season
    # 2026-27: only announced slots (SO as of 2026-08); never invent future rows
    assert "SO" in by_season[2026]
    assert by_season[2026] <= MODERN_SLOTS


def test_real_windows_disjoint_within_season(real):
    by_season = {}
    for t in real["topics"]:
        by_season.setdefault(t["season"], []).append(
            (t["starts"], t["ends"], t["code"]))
    for season, rows in by_season.items():
        rows.sort()
        for (s1, e1, c1), (s2, e2, c2) in zip(rows, rows[1:]):
            assert e1 < s2, f"windows overlap: {c1} vs {c2}"


def test_real_file_has_spec_verified_2026_so_row(real):
    row = [t for t in real["topics"] if t["code"] == "2026-SO"][0]
    assert row["season"] == 2026 and row["slot"] == "SO"
    assert row["resolution"] == (
        "Resolved: The United States federal government should enact a "
        "moratorium on hyperscale data center construction.")
    assert row["starts"] == "2026-09-01"
    assert row["ends"] == "2026-10-31"
    assert row["source_url"] == "https://www.speechanddebate.org/topics/"


def test_real_overrides_only_verified_entries(real):
    assert real["overrides"], "expected the verified NSDA Nationals override"
    for ov in real["overrides"]:
        assert ov["match"] == ov["match"].lower()
        assert ov.get("slot") == "NATS"  # only the Nationals mapping is verified


def test_real_file_loads_and_resolves(tmp_path, real):
    conn = open_db(tmp_path / "t.sqlite")
    n = load_topics(conn, REAL_TOPICS)
    assert n == len(real["topics"])
    # fixed 'today' inside the 2026 Sept/Oct window
    row = current_topic(conn, date(2026, 9, 15))
    assert row is not None and row["code"] == "2026-SO"
    # status math on real rows at a fixed today
    today = date(2026, 8, 27)
    t13 = conn.execute("SELECT * FROM topics WHERE code='2013-SO'").fetchone()
    t26 = conn.execute("SELECT * FROM topics WHERE code='2026-SO'").fetchone()
    assert topic_status(t13, today) == "past"
    assert topic_status(t26, today) == "future"   # announced, zero cards yet
    assert resolve_topic_token(conn, "2013-NOV", today)
    assert resolve_topic_token(conn, "future", today) == \
           resolve_topic_token(conn, "2026-SO", today)

    # a dated 2024-25 round lands on the real ND topic; an undated NSDA
    # Nationals round lands on the season's NATS topic via the override
    cl = get_or_create_caselist(conn, "hspf24", season=2024, event="pf")
    sc = get_or_create_school(conn, cl, "Realville")
    tm = get_or_create_team(conn, sc, "ReVi")
    r_nd = get_or_create_round(conn, tm, "r-nd", round_date="2024-11-20")
    r_nats = get_or_create_round(conn, tm, "r-nats", tournament="NSDA Nationals")
    assign_topics(conn, today=today)
    assert _code_of(conn, r_nd) == "2024-ND"
    assert _code_of(conn, r_nats) == "2024-NATS"


def test_materialize_runs_standalone(tmp_path):
    conn, _ = _mini(tmp_path)
    _mini_rounds(conn)
    assign_topics(conn)
    # calling the materializer directly is safe and idempotent
    n = materialize_topic_ids(conn)
    assert n == 2  # cards x and z carry topics; card y is empty
