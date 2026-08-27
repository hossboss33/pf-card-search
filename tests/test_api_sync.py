"""Tests for carddb.ratelimit + carddb.api_sync (spec §0.2, §2.2, §11 M4).

No real network and no real sleeping anywhere: HTTP goes through
httpx.MockTransport and time goes through an injected fake clock/sleep
pair. Mock payload shapes mirror what config/endpoints.toml documents
(caselist rows with slug/event/year, school rows with name/displayName,
round rows carrying `opensource`, cite rows {cite_id, round_id, title,
cites}, cookie auth via caselist_token).
"""
from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import tomli

from carddb.api_sync import (build_user_agent, discover_endpoints, sync)
from carddb.db import open_db
from carddb.ratelimit import (RateLimiter, SyncError, parse_retry_after,
                              request_with_backoff)
from fixtures.docx_builders import build_loose_pf, docx_bytes

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS_SRC = ROOT / "config" / "endpoints.toml"

LOOSE_PF_BYTES = docx_bytes(build_loose_pf())   # parses to 2 evidence cards


# ---------------------------------------------------------------------------
# Fake time
# ---------------------------------------------------------------------------

class FakeTime:
    """Injectable clock + sleep; sleep() advances the clock."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def make_limiter(rps=1.0):
    ft = FakeTime()
    return RateLimiter(rps, sleep=ft.sleep, clock=ft.clock), ft


# ---------------------------------------------------------------------------
# (a) RateLimiter caps at 1 rps, proven with a fake clock
# ---------------------------------------------------------------------------

def test_limiter_caps_at_one_rps():
    rl, ft = make_limiter(1.0)
    starts = []
    for _ in range(5):
        rl.wait()
        starts.append(ft.now)
        ft.now += 0.25            # each request itself takes 250 ms
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 1.0 - 1e-9 for g in gaps), gaps
    # exactly the make-up sleeps, never more: 4 x 750 ms
    assert ft.sleeps == pytest.approx([0.75, 0.75, 0.75, 0.75])
    # 5 requests span >= 4 seconds of fake time
    assert starts[-1] - starts[0] >= 4.0 - 1e-9


def test_limiter_first_call_never_sleeps():
    rl, ft = make_limiter(1.0)
    rl.wait()
    assert ft.sleeps == []


def test_limiter_no_sleep_when_caller_is_already_slow():
    rl, ft = make_limiter(1.0)
    rl.wait()
    ft.now += 2.0                 # slower than 1 rps on its own
    rl.wait()
    assert ft.sleeps == []


def test_limiter_does_not_burst_with_nonadvancing_sleep():
    """Even a broken sleep that never advances the clock cannot let two
    requests claim the same slot."""
    t = {"now": 0.0}
    sleeps = []
    rl = RateLimiter(1.0, sleep=sleeps.append, clock=lambda: t["now"])
    rl.wait()
    rl.wait()
    rl.wait()
    # the schedule advances even though the clock does not: the second wait
    # is scheduled for t=1.0, the third for t=2.0 (asked to sleep 2.0 since
    # the stuck clock still reads 0)
    assert sleeps == pytest.approx([1.0, 2.0])


def test_limiter_disabled_for_nonpositive_rps():
    rl, ft = make_limiter(0.0)
    for _ in range(10):
        rl.wait()
    assert ft.sleeps == []


# ---------------------------------------------------------------------------
# (b) request_with_backoff: 429/5xx backoff, Retry-After, exhaustion
# ---------------------------------------------------------------------------

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_backoff_honors_retry_after():
    rl, ft = make_limiter(1.0)
    calls = []

    def handler(request):
        calls.append(ft.now)
        if len(calls) <= 2:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as client:
        resp = request_with_backoff(client, "GET", "https://api.test/v1/x",
                                    limiter=rl, max_retries=5)
    assert resp.status_code == 200
    assert len(calls) == 3
    # both backoff waits are exactly the server's Retry-After (7 s beats the
    # 1 s rate slot, so no extra limiter sleeps appear)
    assert ft.sleeps == pytest.approx([7.0, 7.0])


def test_backoff_retry_after_http_date():
    ref = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    value = format_datetime(ref + timedelta(seconds=42), usegmt=True)
    assert parse_retry_after(value, now=ref) == pytest.approx(42.0)
    # a date in the past clamps to 0, never negative
    past = format_datetime(ref - timedelta(seconds=42), usegmt=True)
    assert parse_retry_after(past, now=ref) == 0.0
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not a date") is None
    assert parse_retry_after("") is None


def test_backoff_exponential_sequence_on_5xx():
    rl, ft = make_limiter(1.0)
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        if n["calls"] <= 3:
            return httpx.Response(503)      # no Retry-After header
        return httpx.Response(200, json=[])

    with _client(handler) as client:
        resp = request_with_backoff(client, "GET", "https://api.test/v1/x",
                                    limiter=rl, max_retries=5,
                                    rng=lambda: 1.0)   # deterministic jitter
    assert resp.status_code == 200
    assert ft.sleeps == pytest.approx([1.0, 2.0, 4.0])   # base * 2^(k-1)


def test_backoff_jitter_low_end_and_cap():
    rl, ft = make_limiter(1.0)
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        if n["calls"] <= 4:
            return httpx.Response(500)
        return httpx.Response(200, json=[])

    with _client(handler) as client:
        request_with_backoff(client, "GET", "https://api.test/v1/x",
                             limiter=rl, max_retries=5, backoff_cap=3.0,
                             rng=lambda: 1.0)
    assert ft.sleeps == pytest.approx([1.0, 2.0, 3.0, 3.0])  # capped

    rl2, ft2 = make_limiter(1.0)
    n["calls"] = 0
    with _client(handler) as client:
        request_with_backoff(client, "GET", "https://api.test/v1/x",
                             limiter=rl2, max_retries=5, rng=lambda: 0.0)
    # rng=0 -> the 0.5x low end of the jitter window; the first backoff
    # sleep (0.5 s) is shorter than the 1 rps slot, so the limiter tops it
    # up with its own 0.5 s — jitter can never break the rate cap
    assert ft2.sleeps == pytest.approx([0.5, 0.5, 1.0, 2.0, 4.0])


def test_backoff_exhaustion_raises_clear_syncerror():
    rl, ft = make_limiter(1.0)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "1"})

    with _client(handler) as client:
        with pytest.raises(SyncError) as ei:
            request_with_backoff(client, "GET", "https://api.test/v1/x",
                                 limiter=rl, max_retries=3)
    msg = str(ei.value)
    assert "429" in msg
    assert "https://api.test/v1/x" in msg
    assert "3" in msg
    assert len(calls) == 4          # initial attempt + max_retries


def test_backoff_does_not_retry_plain_4xx():
    rl, ft = make_limiter(1.0)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, json={"message": "not found"})

    with _client(handler) as client:
        resp = request_with_backoff(client, "GET", "https://api.test/v1/x",
                                    limiter=rl, max_retries=5)
    assert resp.status_code == 404
    assert len(calls) == 1
    assert ft.sleeps == []


# ---------------------------------------------------------------------------
# discover_endpoints: regenerate by merging, checked-in file as fallback
# ---------------------------------------------------------------------------

def _op(query=(), security=None):
    o = {}
    if query:
        o["parameters"] = [{"name": n, "in": "query"} for n in query]
    if security is not None:
        o["security"] = security
    return o


MINI_SPEC = {
    "openapi": "3.0.2",
    "info": {"title": "Caselist API v1"},
    "security": [{"cookie": []}],
    "components": {"securitySchemes": {
        "cookie": {"type": "apiKey", "in": "cookie",
                   "name": "caselist_token"}}},
    "paths": {
        "/login": {"post": _op(security=[])},
        "/status": {"get": _op(security=[])},
        "/caselists": {"get": _op(["archived"])},
        "/caselists/{caselist}": {"get": _op()},
        "/caselists/{caselist}/schools": {"get": _op()},
        "/caselists/{caselist}/schools/{school}": {"get": _op()},
        "/caselists/{caselist}/schools/{school}/teams": {"get": _op()},
        "/caselists/{caselist}/schools/{school}/teams/{team}": {"get": _op()},
        "/caselists/{caselist}/schools/{school}/teams/{team}/rounds":
            {"get": _op(["side"])},
        "/caselists/{caselist}/schools/{school}/teams/{team}/rounds/{round}":
            {"get": _op()},
        "/caselists/{caselist}/schools/{school}/teams/{team}/cites":
            {"get": _op(["side"])},
        "/download": {"get": _op(["path"])},
        "/caselists/{caselist}/downloads": {"get": _op()},
        # NOTE: /caselists/{caselist}/recent deliberately missing
        "/search": {"get": _op(["q", "shard"])},
        "/openev": {"get": _op(["year"])},
        "/caselists/{caselist}/tournaments": {"get": _op()},   # brand new
    },
}


def _spec_client():
    def handler(request):
        if request.url.path == "/v1/docs":
            return httpx.Response(200, json=MINI_SPEC)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discover_endpoints_merges_not_clobbers(tmp_path):
    out = tmp_path / "endpoints.toml"
    shutil.copy(ENDPOINTS_SRC, out)
    with _spec_client() as client:
        merged = discover_endpoints("https://api.test/v1", out, client=client)
    with open(out, "rb") as f:
        reloaded = tomli.load(f)
    assert reloaded == merged      # what we returned is what we wrote

    eps = reloaded["endpoints"]
    # every §2.2-required endpoint survives with its transcribed path
    assert eps["caselists"]["path"] == "/caselists"
    assert eps["rounds"]["path"] == \
        "/caselists/{caselist}/schools/{school}/teams/{team}/rounds"
    assert eps["download"]["query_params"] == ["path"]
    assert eps["bulk_downloads"]["path"] == "/caselists/{caselist}/downloads"
    for name in ("login", "caselists", "schools", "teams", "rounds", "cites",
                 "download", "bulk_downloads"):
        assert eps[name]["verified"] is True, name
    # login keeps its transcribed extras
    assert eps["login"]["body_params"] == ["username", "password", "remember"]
    assert eps["login"]["auth_required"] is False
    # deliberately-unverified entries stay unverified even though the paths
    # exist in the live spec (merge preserves the research pass's judgment)
    assert eps["search"]["verified"] is False
    assert eps["openev"]["verified"] is False
    # an endpoint that vanished from the live spec is downgraded honestly
    assert eps["recent"]["verified"] is False
    # a new path in the live spec is appended, transcribed not invented
    assert eps["tournaments"]["path"] == "/caselists/{caselist}/tournaments"
    assert eps["tournaments"]["path_params"] == ["caselist"]
    assert eps["tournaments"]["auth_required"] is True
    assert eps["tournaments"]["verified"] is True

    meta = reloaded["meta"]
    assert meta["api_base"] == "https://api.test/v1"
    assert meta["spec_url"] == "https://api.test/v1/docs"
    assert meta["fetched"] == date.today().isoformat()
    assert reloaded["auth"]["cookie_name"] == "caselist_token"
    assert reloaded["auth"]["verified"] is True

    # structural invariant the checked-in file's tests pin: placeholders in
    # every path template match declared path_params exactly
    for name, ep in eps.items():
        assert re.findall(r"\{([a-z_]+)\}", ep["path"]) == \
            ep.get("path_params", []), name
        assert isinstance(ep["verified"], bool), name
        assert isinstance(ep["auth_required"], bool), name


def test_discover_endpoints_fetch_failure_keeps_checked_in_file(tmp_path):
    out = tmp_path / "endpoints.toml"
    shutil.copy(ENDPOINTS_SRC, out)
    before = out.read_bytes()

    def handler(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = discover_endpoints("https://api.test/v1", out, client=client)
    assert out.read_bytes() == before                    # untouched
    assert result["endpoints"]["caselists"]["path"] == "/caselists"


def test_discover_endpoints_failure_without_fallback_raises(tmp_path):
    def handler(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SyncError):
            discover_endpoints("https://api.test/v1",
                               tmp_path / "missing.toml", client=client)


def test_build_user_agent_includes_contact_email():
    cfg = {"sync": {"user_agent": "pf-card-search (research index)",
                    "contact_email": "owner@example.com"}}
    ua = build_user_agent(cfg)
    assert ua.startswith("pf-card-search")
    assert "owner@example.com" in ua
    # no email configured -> UA unchanged
    assert build_user_agent({"sync": {"user_agent": "pf-card-search"}}) == \
        "pf-card-search"
    # already embedded -> not duplicated
    cfg2 = {"sync": {"user_agent": "x (contact: a@b.c)",
                     "contact_email": "a@b.c"}}
    assert build_user_agent(cfg2).count("a@b.c") == 1


# ---------------------------------------------------------------------------
# The scripted mock caselist (payload shapes per endpoints.toml)
# ---------------------------------------------------------------------------

PF_CASELIST = {"caselist_id": 1, "slug": "hspf25", "name": "hspf25",
               "display_name": "2025-26 HS Public Forum", "event": "pf",
               "year": 2025, "archived": False}
CX_CASELIST = {"caselist_id": 2, "slug": "ndtceda25", "name": "ndtceda25",
               "display_name": "2025-26 NDT/CEDA", "event": "cx",
               "year": 2025, "archived": False}

SCHOOLS = [{"name": "Northview", "displayName": "Northview HS", "state": "GA"},
           {"name": "Millburn", "displayName": "Millburn HS", "state": "NJ"}]

TEAMS = {
    "Northview": [{"name": "NoAB", "display_name": "Northview AB",
                   "team_id": 11, "notes": None}],
    "Millburn": [{"name": "MiCD", "display_name": "Millburn CD",
                  "team_id": 12, "notes": None},
                 {"name": "MiEF", "display_name": "Millburn EF",
                  "team_id": 13, "notes": None}],
}

NOAB_DOC = "hspf25/Northview/NoAB/round1.docx"
MIEF_DOC = "hspf25/Millburn/MiEF/round3.docx"   # different path, SAME bytes

ROUNDS = {
    "NoAB": [{"round_id": 101, "side": "A", "tournament": "Blue Key",
              "round": "1", "opponent": "Lakeville XY", "judge": "J. Smith",
              "report": "read the case", "opensource": NOAB_DOC,
              "tourn_id": 9, "external_id": None}],
    "MiCD": [{"round_id": 102, "side": "N", "tournament": "Blue Key",
              "round": "2", "opponent": "Dover PQ", "judge": "K. Lee",
              "report": "", "opensource": "", "tourn_id": 9,
              "external_id": None}],
    "MiEF": [{"round_id": 103, "side": "P", "tournament": "Glenbrooks",
              "round": "3", "opponent": "Ridge MN", "judge": "L. Cho",
              "report": "", "opensource": MIEF_DOC, "tourn_id": 10,
              "external_id": None}],
}

CITES = {
    "NoAB": [{"cite_id": 899, "round_id": 101, "title": "Case cites",
              "cites": "# Case\nLoris '19 - fracking bans spike prices."}],
    "MiCD": [{"cite_id": 900, "round_id": 102,
              "title": "AT: Data centers good",
              "cites": "# AT: Data centers good\n\nKessler '26 — the "
                       "moratorium collapses interconnection queues; first "
                       "and last sentence pasted here."}],
    "MiEF": [],
}

DOWNLOADS = {NOAB_DOC: LOOSE_PF_BYTES, MIEF_DOC: LOOSE_PF_BYTES}


class MockAPI:
    """Scripted openCaselist. Records every request; optionally raises
    mid-run to simulate a crash."""

    def __init__(self, crash_on=None, downloads=None):
        self.crash_on = crash_on
        self.downloads = DOWNLOADS if downloads is None else downloads
        self.log = []

    # -- helpers used by tests ------------------------------------------
    def paths(self):
        return [e["path"] for e in self.log]

    def full_urls(self):
        return ["%s?%s" % (e["path"], e["query"]) for e in self.log]

    # -- transport handler ----------------------------------------------
    def __call__(self, request):
        path = request.url.path
        query = str(request.url.params)
        self.log.append({
            "method": request.method,
            "path": path,
            "query": query,
            "ua": request.headers.get("user-agent", ""),
            "cookie": request.headers.get("cookie", ""),
        })
        if self.crash_on and self.crash_on in path:
            raise RuntimeError("simulated crash at %s" % path)
        return self.route(request)

    def route(self, request):
        path = request.url.path
        params = request.url.params
        if path == "/v1/login" and request.method == "POST":
            return httpx.Response(
                201,
                json={"message": "Successfully logged in", "token": "tok123",
                      "expires": "2026-09-10T00:00:00Z", "trusted": True,
                      "userId": 1, "admin": False},
                headers={"Set-Cookie":
                         "caselist_token=tok123; Path=/; SameSite=Lax"})
        if path == "/v1/caselists":
            if params.get("archived") == "true":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[PF_CASELIST, CX_CASELIST])
        if path == "/v1/caselists/hspf25/schools":
            return httpx.Response(200, json=SCHOOLS)
        m = re.match(r"^/v1/caselists/hspf25/schools/([^/]+)/teams$", path)
        if m:
            return httpx.Response(200, json=TEAMS.get(m.group(1), []))
        m = re.match(
            r"^/v1/caselists/hspf25/schools/[^/]+/teams/([^/]+)/rounds$", path)
        if m:
            return httpx.Response(200, json=ROUNDS.get(m.group(1), []))
        m = re.match(
            r"^/v1/caselists/hspf25/schools/[^/]+/teams/([^/]+)/cites$", path)
        if m:
            return httpx.Response(200, json=CITES.get(m.group(1), []))
        if path == "/v1/download":
            fp = params.get("path")
            if fp in self.downloads:
                return httpx.Response(
                    200, content=self.downloads[fp],
                    headers={"Content-Type":
                             "application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document"})
            return httpx.Response(404, json={"message": "file not found"})
        return httpx.Response(404, json={"message": "no such route " + path})


def make_cfg(tmp_path):
    return {
        "paths": {"db": str(tmp_path / "carddb.sqlite"),
                  "raw_store": str(tmp_path / "raw"),
                  "topics": str(tmp_path / "topics.json"),
                  "reports": str(tmp_path / "reports"),
                  "backups": str(tmp_path / "backups")},
        "sync": {"api_base": "https://api.test/v1",
                 "rate_limit_rps": 1.0,
                 "max_retries": 2,
                 "user_agent": "pf-card-search-test",
                 "contact_email": "owner@example.com",
                 "tabroom_username_env": "TEST_TABROOM_USER",
                 "tabroom_password_env": "TEST_TABROOM_PASS",
                 "endpoints_file": str(ENDPOINTS_SRC)},
    }


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("TEST_TABROOM_USER", "owner@example.com")
    monkeypatch.setenv("TEST_TABROOM_PASS", "not-a-real-password")


def run_sync(conn, cfg, api, **kw):
    ft = FakeTime()
    limiter = RateLimiter(1.0, sleep=ft.sleep, clock=ft.clock)
    with httpx.Client(transport=httpx.MockTransport(api)) as client:
        return sync(conn, cfg, client=client, limiter=limiter, **kw)


def counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {"cards": q("SELECT COUNT(*) FROM cards"),
            "variants": q("SELECT COUNT(*) FROM card_variants"),
            "rounds": q("SELECT COUNT(*) FROM rounds"),
            "fts": q("SELECT COUNT(*) FROM card_fts")}


# ---------------------------------------------------------------------------
# Full sync end-to-end
# ---------------------------------------------------------------------------

def test_full_sync_end_to_end(tmp_path, creds):
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    api = MockAPI()
    st = run_sync(conn, cfg, api)

    # units: NoAB, MiCD, MiEF
    assert st.units_seen == 3
    assert st.units_skipped == 0
    # one unique docx parsed once (MiEF's file is byte-identical -> layer-1
    # sha dedup short-circuits the second parse)
    assert st.parsed == 1
    assert st.failed == 0
    # 2 cards from the docx + 1 cites-only fallback card
    assert st.new_cards == 3
    assert st.new_variants == 3
    c = counts(conn)
    assert c["cards"] == 3 and c["variants"] == 3 and c["fts"] == 3

    # sides normalized at ingest: A -> P, N -> C (spec §1.4)
    sides = {r["external_id"]: r["side"]
             for r in conn.execute("SELECT external_id, side FROM rounds")}
    assert sides == {"api-101": "P", "api-102": "C", "api-103": "P"}

    # fidelity: the no-doc round's cites came in as cites_only
    fids = sorted(r["fidelity"] for r in
                  conn.execute("SELECT fidelity FROM card_variants"))
    assert fids == ["cites_only", "opensource", "opensource"]
    cites_var = conn.execute(
        "SELECT v.round_id, c.tag FROM card_variants v "
        "JOIN cards c ON c.id = v.card_id "
        "WHERE v.fidelity = 'cites_only'").fetchone()
    round_102 = conn.execute(
        "SELECT id FROM rounds WHERE external_id = 'api-102'").fetchone()
    assert cites_var["round_id"] == round_102["id"]
    assert cites_var["tag"] == "AT: Data centers good"

    # checkpoints: one 'done' row per completed (caselist, school, team)
    cps = {(r["caselist"], r["school"], r["team"]): r["state"]
           for r in conn.execute("SELECT * FROM sync_checkpoints")}
    assert cps == {("hspf25", "Northview", "NoAB"): "done",
                   ("hspf25", "Millburn", "MiCD"): "done",
                   ("hspf25", "Millburn", "MiEF"): "done"}

    # PF filter is client-side: the cx caselist was never enumerated
    assert not any("ndtceda25" in u for u in api.full_urls())
    # cites are fetched lazily: only the team with a doc-less round
    cite_paths = [p for p in api.paths() if p.endswith("/cites")]
    assert cite_paths == \
        ["/v1/caselists/hspf25/schools/Millburn/teams/MiCD/cites"]
    # both opensource paths downloaded (distinct paths, same bytes)
    downloads = [e for e in api.log if e["path"] == "/v1/download"]
    assert len(downloads) == 2
    assert any("NoAB" in d["query"] for d in downloads)
    assert any("MiEF" in d["query"] for d in downloads)

    # politeness headers on every request; cookie present after login
    assert all("owner@example.com" in e["ua"] for e in api.log)
    data_requests = [e for e in api.log if e["path"] != "/v1/login"]
    assert all("caselist_token=tok123" in e["cookie"] for e in data_requests)

    # raw store: every documents row points at a real file, origin='api'
    docs = conn.execute(
        "SELECT origin, local_path, sha256 FROM documents").fetchall()
    assert docs and all(d["origin"] == "api" for d in docs)
    for d in docs:
        p = Path(d["local_path"])
        assert p.exists()
        assert p.name == d["sha256"] and p.parent.name == d["sha256"][:2]
    # the docx blob is among them exactly once (content-addressed)
    from carddb.keys import sha256_bytes
    docx_sha = sha256_bytes(LOOSE_PF_BYTES)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE sha256 = ?",
                        (docx_sha,)).fetchone()[0] == 1
    assert conn.execute(
        "SELECT parse_status FROM documents WHERE sha256 = ?",
        (docx_sha,)).fetchone()["parse_status"] == "ok"

    # FTS actually searches the synced cards
    hit = conn.execute(
        "SELECT COUNT(*) FROM card_fts WHERE card_fts MATCH 'fracking'"
    ).fetchone()[0]
    assert hit >= 1
    conn.close()


# ---------------------------------------------------------------------------
# (c) crash-resume: completed units issue ZERO new requests
# ---------------------------------------------------------------------------

def test_crash_resume_skips_completed_units_without_requests(tmp_path, creds):
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])

    # run 1: the mock raises while fetching unit 3's rounds
    api1 = MockAPI(crash_on="MiEF/rounds")
    with pytest.raises(RuntimeError):
        run_sync(conn, cfg, api1)

    cps = {(r["caselist"], r["school"], r["team"]): r["state"]
           for r in conn.execute("SELECT * FROM sync_checkpoints")}
    assert cps == {("hspf25", "Northview", "NoAB"): "done",
                   ("hspf25", "Millburn", "MiCD"): "done"}
    before = counts(conn)
    assert before["cards"] == 3          # units 1+2 landed and committed
    assert before["rounds"] == 2         # unit 3's round was rolled back

    # run 2: resume. Completed units must issue ZERO HTTP requests.
    api2 = MockAPI()
    st2 = run_sync(conn, cfg, api2)
    assert st2.units_seen == 3
    assert st2.units_skipped == 2
    urls2 = api2.full_urls()
    assert not any("NoAB" in u for u in urls2), urls2     # unit 1: nothing
    assert not any("MiCD" in u for u in urls2), urls2     # unit 2: nothing
    # unit 3 did complete this time
    cps = {(r["caselist"], r["school"], r["team"]): r["state"]
           for r in conn.execute("SELECT * FROM sync_checkpoints")}
    assert cps[("hspf25", "Millburn", "MiEF")] == "done"
    # MiEF's file is byte-identical to NoAB's -> no new cards or variants,
    # but its round (rolled back in run 1) now exists
    assert st2.new_cards == 0 and st2.new_variants == 0
    after_resume = dict(before, rounds=3)
    assert counts(conn) == after_resume

    # run 3: fully synced. Every unit skips; zero unit-level requests.
    api3 = MockAPI()
    st3 = run_sync(conn, cfg, api3)
    assert st3.units_seen == 3 and st3.units_skipped == 3
    assert st3.new_cards == 0 and st3.new_variants == 0
    paths3 = api3.paths()
    assert not any(p.endswith("/rounds") or p.endswith("/cites")
                   or p == "/v1/download" for p in paths3), paths3
    # only login + enumeration listings remain
    assert set(paths3) == {
        "/v1/login", "/v1/caselists", "/v1/caselists/hspf25/schools",
        "/v1/caselists/hspf25/schools/Northview/teams",
        "/v1/caselists/hspf25/schools/Millburn/teams"}
    assert counts(conn) == after_resume
    conn.close()


def test_rerun_without_checkpoints_is_still_idempotent(tmp_path, creds):
    """Layer 1+2 idempotence holds even when checkpoints are wiped: the
    rerun re-requests everything but inserts nothing new (spec §0.5)."""
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    run_sync(conn, cfg, MockAPI())
    before = counts(conn)
    conn.execute("DELETE FROM sync_checkpoints")
    conn.commit()

    st = run_sync(conn, cfg, MockAPI())
    assert st.units_skipped == 0         # nothing skipped: real re-requests
    assert st.new_cards == 0 and st.new_variants == 0
    assert counts(conn) == before
    conn.close()


# ---------------------------------------------------------------------------
# (d) missing credentials -> helpful error, zero requests
# ---------------------------------------------------------------------------

def test_missing_credentials_error_names_env_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_TABROOM_USER", raising=False)
    monkeypatch.delenv("TEST_TABROOM_PASS", raising=False)
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    api = MockAPI()
    with pytest.raises(SyncError) as ei:
        run_sync(conn, cfg, api)
    msg = str(ei.value)
    assert "TEST_TABROOM_USER" in msg
    assert "TEST_TABROOM_PASS" in msg
    assert api.log == []                 # not a single request went out
    conn.close()


def test_missing_only_password_named_specifically(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TABROOM_USER", "owner@example.com")
    monkeypatch.delenv("TEST_TABROOM_PASS", raising=False)
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    with pytest.raises(SyncError) as ei:
        run_sync(conn, cfg, MockAPI())
    msg = str(ei.value)
    assert "TEST_TABROOM_PASS" in msg
    assert "TEST_TABROOM_USER" not in msg
    # and never the value of anything
    assert "owner@example.com" not in msg
    conn.close()


# ---------------------------------------------------------------------------
# Edges: unknown caselist, download failure fallback, since filter
# ---------------------------------------------------------------------------

def test_unknown_caselist_raises_with_available_slugs(tmp_path, creds):
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    with pytest.raises(SyncError) as ei:
        run_sync(conn, cfg, MockAPI(), caselist="hspf99")
    assert "hspf25" in str(ei.value)
    conn.close()


def test_download_failure_falls_back_to_cites(tmp_path, creds):
    """A round whose opensource file 404s is treated as doc-less: the sync
    keeps going and ingests that team's pasted cites instead of crashing."""
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    api = MockAPI(downloads={})          # every download 404s
    st = run_sync(conn, cfg, api)
    assert st.units_seen == 3 and st.parsed == 0
    # NoAB's doc failed -> its cites were fetched as the fallback
    assert "/v1/caselists/hspf25/schools/Northview/teams/NoAB/cites" \
        in api.paths()
    fids = sorted(r["fidelity"] for r in
                  conn.execute("SELECT fidelity FROM card_variants"))
    assert fids == ["cites_only", "cites_only"]   # NoAB cite + MiCD cite
    # all units still checkpoint as done
    assert conn.execute(
        "SELECT COUNT(*) FROM sync_checkpoints WHERE state='done'"
    ).fetchone()[0] == 3
    conn.close()


def test_since_filters_out_older_seasons(tmp_path, creds):
    cfg = make_cfg(tmp_path)
    conn = open_db(cfg["paths"]["db"])
    api = MockAPI()
    st = run_sync(conn, cfg, api, since="2026-01-01")
    # hspf25 is the 2025 season -> filtered; nothing below caselists runs
    assert st.units_seen == 0
    assert not any("/schools" in p for p in api.paths())
    conn.close()
