"""Tests for the local "Connect to openCaselist" flow (carddb.connect).

Nothing here touches api.opencaselist.com. ``api_sync._login``,
``api_sync._list_caselists`` and ``api_sync.sync`` are monkeypatched, so
the real service is never called and no real credentials exist anywhere
in this file. The fixture password below is obviously synthetic and is
used only to prove it never escapes: not into a response body, not into a
Set-Cookie header, not into a log record, not into the database file.

Covered, in the order the task lists them:
  - the form renders, with the honest disclosure next to the inputs
  - posting credentials reaches api_sync's own login code
  - the password never appears in output, logs, or the DB
  - the loopback guard returns 403 for a non-loopback bind
  - the sync endpoint launches and the status endpoint reports progress
  - the documented TABROOM_USERNAME / TABROOM_PASSWORD fallback works
"""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carddb import api_sync, connect as connect_mod
from carddb.config import load_config
from carddb.ingest import IngestStats
from carddb.server import create_app

ROOT = Path(__file__).resolve().parent.parent

# Obviously synthetic. Present only so the tests can assert it never leaks.
FIXTURE_USER = "fixture-owner@example.invalid"
FIXTURE_PASSWORD = "fixture-not-a-real-password-9Zq"
ENV_USER = "env-owner@example.invalid"
ENV_PASSWORD = "env-not-a-real-password-4Xk"

CASELIST_ROWS = [
    # the live shape: `name` holds the slug, `display_name` the label
    # (docs/api_access.md section 5a), archived as 0/1
    {"caselist_id": 1040, "name": "hspf26", "display_name": "HS PF 2026-27",
     "year": 2026, "event": "pf", "level": "hs", "team_size": 2, "archived": 0},
    {"caselist_id": 1039, "name": "hspf25", "display_name": "HS PF 2025-26",
     "year": 2025, "event": "pf", "level": "hs", "team_size": 2, "archived": 1},
    {"caselist_id": 1038, "name": "hspf24", "display_name": "HS PF 2024-25",
     "year": 2024, "event": "pf", "level": "hs", "team_size": 2, "archived": 1},
    # not PF: must not be offered
    {"caselist_id": 1050, "name": "hsld25", "display_name": "HS LD 2025-26",
     "year": 2025, "event": "ld", "level": "hs", "team_size": 1, "archived": 1},
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"] = dict(c["paths"])
    c["paths"]["db"] = str(tmp_path / "connect.sqlite")
    c["paths"]["raw_store"] = str(tmp_path / "raw")
    return c


@pytest.fixture
def db_path(cfg):
    return cfg["paths"]["db"]


@pytest.fixture(autouse=True)
def clean_state():
    """The connect state is module-level by design; reset it around tests."""
    def reset():
        client = connect_mod._STATE.clear()
        if client is not None:
            client.close()
        connect_mod._STATE.error = None
        connect_mod._STATE.notice = None
        connect_mod._STATE.job = None
    reset()
    yield
    reset()


@pytest.fixture
def client(cfg, db_path):
    app = create_app(db_path=db_path, cfg=cfg)
    # Declare the bind address the way a loopback deployment would.
    app.state.connect_bind_host = "127.0.0.1"
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_api(monkeypatch):
    """Stand in for openCaselist. Records the credentials login received."""
    seen = {"login_calls": 0, "creds": None, "listing_calls": 0,
            "cookie_set": False}

    def fake_login(conn, http, limiter, max_retries, api_base, endpoints, c):
        seen["login_calls"] += 1
        # api_sync's own credential reader: whatever connect.py supplied
        seen["creds"] = api_sync._credentials(c)
        http.cookies.set("caselist_token", "fixture-session-token")
        seen["cookie_set"] = True

    def fake_listing(conn, http, limiter, max_retries, api_base, endpoints,
                     raw_root):
        seen["listing_calls"] += 1
        return [dict(r) for r in CASELIST_ROWS]

    monkeypatch.setattr(api_sync, "_login", fake_login)
    monkeypatch.setattr(api_sync, "_list_caselists", fake_listing)
    return seen


def _connect_form(client, username=FIXTURE_USER, password=FIXTURE_PASSWORD):
    return client.post("/connect",
                       data={"username": username, "password": password})


def _all_text(resp):
    """Every byte a client could see: final body, redirect hops, headers."""
    chunks = [resp.text, json.dumps(dict(resp.headers))]
    for hop in resp.history:
        chunks.append(hop.text)
        chunks.append(json.dumps(dict(hop.headers)))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# The form renders
# ---------------------------------------------------------------------------

def test_form_renders(client):
    r = client.get("/connect")
    assert r.status_code == 200
    body = r.text
    assert 'type="password"' in body
    assert 'autocomplete="current-password"' in body
    assert 'name="username"' in body and 'name="password"' in body
    assert 'method="post"' in body and 'action="/connect"' in body
    # real labels, tied to the inputs
    assert 'for="tabroom-password"' in body and 'id="tabroom-password"' in body
    # never pre-filled: neither input carries a value attribute
    for anchor in ('id="tabroom-password"', 'id="tabroom-username"'):
        tag = body.split(anchor)[1].split(">")[0]
        assert "value" not in tag, tag


def test_disclosure_sits_next_to_the_inputs(client):
    body = client.get("/connect").text
    # the honest disclosure, not a footer footnote
    assert "your own" in body and "Tabroom" in body
    assert "held in memory in this server process" in body
    assert "never written to disk" in body
    assert "never put in the database" in body
    assert "localhost only" in body
    assert "api.opencaselist.com/v1/login" in body
    # and the local alternative is offered
    assert "TABROOM_USERNAME" in body and "TABROOM_PASSWORD" in body
    # section 8.5 copy rules
    assert "!" not in body.replace("<!doctype", "").replace("<!--", "")


# ---------------------------------------------------------------------------
# Posting credentials reaches api_sync's login
# ---------------------------------------------------------------------------

def test_post_credentials_calls_api_sync_login(client, fake_api):
    r = _connect_form(client)
    assert r.status_code == 200
    assert fake_api["login_calls"] == 1
    assert fake_api["creds"] == (FIXTURE_USER, FIXTURE_PASSWORD)
    assert fake_api["listing_calls"] == 1
    # PF caselists offered, newest first; non-PF filtered out
    body = r.text
    assert "hspf26" in body and "hspf25" in body and "hspf24" in body
    assert "hsld25" not in body
    assert body.index("hspf26") < body.index("hspf25") < body.index("hspf24")
    assert "Sync this season" in body


def test_credentials_override_is_removed_after_login(client, fake_api):
    original = api_sync._credentials
    _connect_form(client)
    assert api_sync._credentials is original


def test_status_reports_the_connection(client, fake_api):
    _connect_form(client)
    s = client.get("/connect/status").json()
    assert s["connected"] is True
    assert s["source"] == "form"
    assert [c["slug"] for c in s["caselists"]] == ["hspf26", "hspf25", "hspf24"]
    assert s["running"] is False
    assert s["done"] is False
    assert s["errors"] == []


def test_disconnect_drops_everything(client, fake_api):
    _connect_form(client)
    assert connect_mod._STATE.connected is True
    r = client.post("/connect/logout")
    assert r.status_code == 200
    assert connect_mod._STATE.connected is False
    assert connect_mod._STATE.creds is None
    assert connect_mod._STATE.client is None
    assert client.get("/connect/status").json()["connected"] is False


def test_login_failure_is_reported_without_the_password(client, monkeypatch):
    def boom(conn, http, limiter, max_retries, api_base, endpoints, c):
        # a hostile-shaped error: it quotes the secret back at us
        raise api_sync.SyncError(
            "login failed; sent password=%s" % FIXTURE_PASSWORD)

    monkeypatch.setattr(api_sync, "_login", boom)
    r = _connect_form(client)
    assert r.status_code == 200
    assert FIXTURE_PASSWORD not in _all_text(r)
    assert connect_mod.REDACTED in r.text
    assert "SyncError" in r.text
    assert connect_mod._STATE.connected is False


# ---------------------------------------------------------------------------
# The password never escapes
# ---------------------------------------------------------------------------

def test_password_never_appears_in_any_response(client, fake_api):
    r = _connect_form(client)
    assert FIXTURE_PASSWORD not in _all_text(r)
    assert FIXTURE_PASSWORD not in _all_text(client.get("/connect"))
    assert FIXTURE_PASSWORD not in _all_text(client.get("/connect/status"))
    # nothing of ours is stored in a cookie either
    assert not [v for v in client.cookies.values() if FIXTURE_PASSWORD in v]
    for hop in list(r.history) + [r]:
        assert "set-cookie" not in {k.lower() for k in hop.headers}


def test_password_never_reaches_a_log_record(client, fake_api, caplog):
    caplog.set_level(logging.DEBUG)
    _connect_form(client)
    client.get("/connect")
    client.get("/connect/status")
    for record in caplog.records:
        assert FIXTURE_PASSWORD not in record.getMessage()
        assert FIXTURE_PASSWORD not in str(record.args)
    assert FIXTURE_PASSWORD not in caplog.text


def test_password_never_reaches_the_database(client, fake_api, db_path):
    _connect_form(client)
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table in tables:
            rows = conn.execute("SELECT * FROM %s" % table).fetchall()
            for row in rows:
                assert FIXTURE_PASSWORD not in " ".join(
                    str(v) for v in row if v is not None)
    finally:
        conn.close()
    secret = FIXTURE_PASSWORD.encode()
    for path in Path(db_path).parent.rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes(), path


def test_password_is_not_kept_in_a_repr(client, fake_api):
    _connect_form(client)
    creds = connect_mod._STATE.creds
    assert creds is not None
    assert FIXTURE_PASSWORD not in repr(creds)
    assert FIXTURE_PASSWORD not in str(creds)
    assert connect_mod.REDACTED in repr(creds)


def test_redact_strips_live_secrets(client, fake_api):
    _connect_form(client)
    msg = connect_mod._redact("boom %s and token fixture-session-token"
                              % FIXTURE_PASSWORD)
    assert FIXTURE_PASSWORD not in msg
    assert "fixture-session-token" not in msg


# ---------------------------------------------------------------------------
# The loopback guard (spec section 0.4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::",
                                  "example.test"])
def test_non_loopback_bind_is_refused(client, host):
    client.app.state.connect_bind_host = host
    r = client.get("/connect")
    assert r.status_code == 403
    assert connect_mod.REFUSAL_TITLE in r.text
    assert host in r.text
    assert "127.0.0.1" in r.text          # says how to fix it
    # the form itself is not served at all
    assert 'type="password"' not in r.text

    assert client.post("/connect", data={"username": "a", "password": "b"}
                       ).status_code == 403
    assert client.post("/connect/sync/hspf25").status_code == 403
    assert client.post("/connect/logout").status_code == 403

    s = client.get("/connect/status")
    assert s.status_code == 403
    assert s.json()["error"] == connect_mod.REFUSAL_TITLE


def test_non_loopback_bind_never_calls_the_api(client, fake_api):
    client.app.state.connect_bind_host = "0.0.0.0"
    client.post("/connect", data={"username": FIXTURE_USER,
                                  "password": FIXTURE_PASSWORD})
    assert fake_api["login_calls"] == 0
    assert connect_mod._STATE.connected is False


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_binds_are_allowed(client, host):
    client.app.state.connect_bind_host = host
    assert client.get("/connect").status_code == 200


@pytest.mark.parametrize("argv,host", [
    (["serve", "--host", "0.0.0.0"], "0.0.0.0"),
    (["serve", "--host=192.168.1.20"], "192.168.1.20"),
])
def test_command_line_host_is_a_bind_declaration(client, monkeypatch, argv,
                                                 host):
    """ASGI cannot tell a loopback request to a wide-open server from a
    loopback request to a loopback-only one; argv can."""
    monkeypatch.setattr(sys, "argv", ["carddb"] + argv)
    # declare loopback anyway: argv must still veto it
    client.app.state.connect_bind_host = "127.0.0.1"
    r = client.get("/connect")
    assert r.status_code == 403
    assert ("this server was started with --host %s" % host) in r.text


def test_loopback_command_line_host_is_allowed(client, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["carddb", "serve", "--host", "127.0.0.1"])
    monkeypatch.delattr(client.app.state, "connect_bind_host", raising=False)
    assert client.get("/connect").status_code == 200


def test_forwarded_request_is_refused(client):
    r = client.get("/connect", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403
    assert "x-forwarded-for" in r.text


def test_is_loopback_host_unit():
    ok = ["127.0.0.1", "127.1.2.3", "::1", "[::1]", "::1%lo0", "localhost"]
    bad = ["", None, "0.0.0.0", "192.168.0.1", "10.0.0.1", "::",
           "example.com", "testserver", "1.2.3.4"]
    assert all(connect_mod._is_loopback_host(h) for h in ok)
    assert not any(connect_mod._is_loopback_host(h) for h in bad)


def test_undeclared_bind_falls_back_to_the_connection_address(cfg, db_path):
    """With no declared bind host, an ASGI server address of "testserver"
    is not provably loopback, so the page fails closed."""
    app = create_app(db_path=db_path, cfg=cfg)
    with TestClient(app) as c:
        r = c.get("/connect")
        assert r.status_code == 403
        assert connect_mod.REFUSAL_TITLE in r.text


# ---------------------------------------------------------------------------
# The background sync and the status endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_sync(monkeypatch):
    """A sync that logs one unit line, then blocks until the test releases."""
    gate = threading.Event()
    started = threading.Event()
    seen = {"calls": 0, "caselist": None, "client": None, "kwargs": None}

    def _sync(conn, c, caselist=None, since=None, *, client=None,
              limiter=None):
        seen["calls"] += 1
        seen["caselist"] = caselist
        seen["client"] = client
        seen["kwargs"] = {"since": since, "limiter": limiter}
        logging.getLogger("carddb.sync").info(
            "unit %s/Fixture School/FiSc: rounds=2 opensource_docs=1 "
            "cites_fallback=0", caselist)
        logging.getLogger("carddb.sync").warning(
            "parse failed for fixture/path.docx (synthetic fixture failure)")
        started.set()
        gate.wait(10)
        stats = IngestStats()
        stats.units_seen = 1
        stats.parsed = 2
        stats.failed = 1
        stats.new_cards = 2
        stats.new_variants = 3
        return stats

    monkeypatch.setattr(api_sync, "sync", _sync)
    _sync.gate = gate
    _sync.started = started
    _sync.seen = seen
    return _sync


def _poll_until(client, predicate, timeout=10.0):
    deadline = time.time() + timeout
    payload = None
    while time.time() < deadline:
        payload = client.get("/connect/status").json()
        if predicate(payload):
            return payload
        time.sleep(0.02)
    raise AssertionError("status never satisfied the predicate: %r" % (payload,))


def test_sync_launches_and_status_reports_progress(client, fake_api, fake_sync):
    _connect_form(client)
    r = client.post("/connect/sync/hspf25")
    assert r.status_code == 200

    assert fake_sync.started.wait(10)
    running = _poll_until(client, lambda s: s["current"].startswith("unit "))
    assert running["running"] is True
    assert running["done"] is False
    assert running["caselist"] == "hspf25"
    assert "Fixture School/FiSc" in running["current"]
    assert running["counts"]["units"] == 1
    assert any("parse failed" in e for e in running["errors"])

    fake_sync.gate.set()
    done = _poll_until(client, lambda s: s["done"] is True)
    assert done["running"] is False
    assert done["error"] is None
    assert done["counts"]["parsed"] == 2
    assert done["counts"]["failed"] == 1
    assert done["counts"]["new_cards"] == 2
    assert done["counts"]["new_variants"] == 3
    assert done["counts"]["units_seen"] == 1

    # the sync got the season it was asked for, on the logged-in session,
    # and was handed no limiter override (api_sync builds its own 1 rps one)
    assert fake_sync.seen["caselist"] == "hspf25"
    assert fake_sync.seen["client"] is not None
    assert fake_sync.seen["kwargs"]["limiter"] is None
    assert fake_sync.seen["kwargs"]["since"] is None


def test_only_one_sync_runs_at_a_time(client, fake_api, fake_sync):
    _connect_form(client)
    client.post("/connect/sync/hspf25")
    assert fake_sync.started.wait(10)
    second = client.post("/connect/sync/hspf24")
    assert second.status_code == 200
    assert "already running" in second.text
    fake_sync.gate.set()
    _poll_until(client, lambda s: s["done"] is True)
    assert fake_sync.seen["calls"] == 1


def test_sync_refuses_a_caselist_the_login_cannot_see(client, fake_api,
                                                      fake_sync):
    _connect_form(client)
    r = client.post("/connect/sync/hsld25")
    assert r.status_code == 200
    assert "not in the PF caselists" in r.text
    assert fake_sync.seen["calls"] == 0


def test_sync_requires_a_connection(client, fake_sync):
    r = client.post("/connect/sync/hspf25")
    assert r.status_code == 200
    assert "not connected" in r.text
    assert fake_sync.seen["calls"] == 0


def test_sync_failure_is_recorded_and_redacted(client, fake_api, monkeypatch):
    def boom(conn, c, caselist=None, since=None, *, client=None, limiter=None):
        raise api_sync.SyncError("exploded with password=%s" % FIXTURE_PASSWORD)

    monkeypatch.setattr(api_sync, "sync", boom)
    _connect_form(client)
    client.post("/connect/sync/hspf25")
    done = _poll_until(client, lambda s: s["done"] is True)
    assert done["error"] is not None
    assert FIXTURE_PASSWORD not in json.dumps(done)
    assert connect_mod.REDACTED in done["error"]
    assert FIXTURE_PASSWORD not in _all_text(client.get("/connect"))


def test_progress_ignores_other_threads(client, fake_api, fake_sync):
    _connect_form(client)
    client.post("/connect/sync/hspf25")
    assert fake_sync.started.wait(10)
    _poll_until(client, lambda s: s["current"].startswith("unit "))
    logging.getLogger("carddb.sync").info("unit bogus/from/another-thread")
    time.sleep(0.05)
    payload = client.get("/connect/status").json()
    assert "bogus" not in payload["current"]
    assert payload["counts"]["units"] == 1
    fake_sync.gate.set()
    _poll_until(client, lambda s: s["done"] is True)


def test_logger_level_is_restored_after_a_sync(client, fake_api, fake_sync):
    before = logging.getLogger(connect_mod.PROGRESS_LOGGER).level
    handlers_before = list(logging.getLogger(
        connect_mod.PROGRESS_LOGGER).handlers)
    _connect_form(client)
    client.post("/connect/sync/hspf25")
    assert fake_sync.started.wait(10)
    fake_sync.gate.set()
    _poll_until(client, lambda s: s["done"] is True)
    time.sleep(0.05)
    log = logging.getLogger(connect_mod.PROGRESS_LOGGER)
    assert log.level == before
    assert log.handlers == handlers_before


# ---------------------------------------------------------------------------
# Env-var fallback (docs/api_access.md section 6, step 2)
# ---------------------------------------------------------------------------

def test_env_var_fallback(client, fake_api, monkeypatch):
    monkeypatch.setenv("TABROOM_USERNAME", ENV_USER)
    monkeypatch.setenv("TABROOM_PASSWORD", ENV_PASSWORD)
    r = client.post("/connect", data={"username": "", "password": ""})
    assert r.status_code == 200
    assert fake_api["creds"] == (ENV_USER, ENV_PASSWORD)
    s = client.get("/connect/status").json()
    assert s["connected"] is True
    assert s["source"] == "environment"
    assert ENV_PASSWORD not in _all_text(r)


def test_env_var_fallback_syncs_too(client, fake_api, fake_sync, monkeypatch):
    monkeypatch.setenv("TABROOM_USERNAME", ENV_USER)
    monkeypatch.setenv("TABROOM_PASSWORD", ENV_PASSWORD)
    client.post("/connect", data={"username": "", "password": ""})
    client.post("/connect/sync/hspf26")
    assert fake_sync.started.wait(10)
    fake_sync.gate.set()
    done = _poll_until(client, lambda s: s["done"] is True)
    assert done["error"] is None
    assert fake_sync.seen["caselist"] == "hspf26"


def test_missing_env_vars_produce_a_named_error(client, monkeypatch):
    monkeypatch.delenv("TABROOM_USERNAME", raising=False)
    monkeypatch.delenv("TABROOM_PASSWORD", raising=False)
    r = client.post("/connect", data={"username": "", "password": ""})
    assert r.status_code == 200
    assert "TABROOM_USERNAME" in r.text and "TABROOM_PASSWORD" in r.text
    assert connect_mod._STATE.connected is False


# ---------------------------------------------------------------------------
# Caselist row shaping
# ---------------------------------------------------------------------------

def test_caselist_rows_handle_the_live_shape():
    rows = connect_mod._caselist_rows(CASELIST_ROWS)
    assert [r["slug"] for r in rows] == ["hspf26", "hspf25", "hspf24"]
    assert rows[0]["display_name"] == "HS PF 2026-27"
    assert rows[0]["archived"] is False
    assert rows[1]["archived"] is True


def test_caselist_rows_tolerate_the_declared_schema_shape():
    rows = connect_mod._caselist_rows([
        {"caselist_id": 1, "slug": "hspf25", "name": "HS PF 2025-26",
         "year": 2025, "event": "pf", "archived": True},
        {"caselist_id": 2, "slug": "ndtceda25", "name": "NDT/CEDA 2025-26",
         "year": 2025, "event": "cx", "archived": True},
        {"nonsense": True},
    ])
    assert [r["slug"] for r in rows] == ["hspf25"]
    assert rows[0]["display_name"] == "HS PF 2025-26"
