"""Saved-session tests. The security properties here are the point: the
password must never be written, the file must never be group/world readable,
and an expired token must not be handed to the sync as if it were live."""
import json
import os
import stat

import pytest

from carddb import session


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    yield


def test_saves_token_and_never_the_password():
    p = session.save("tok-abc", expires="2099-01-01T00:00:00Z",
                     username="someone@example.com")
    raw = p.read_text()
    assert "tok-abc" in raw
    # The password is not a parameter and must not appear under any key.
    data = json.loads(raw)
    assert "password" not in data
    assert not any("password" in str(k).lower() for k in data)


def test_file_is_owner_only():
    p = session.save("tok-abc")
    mode = stat.S_IMODE(os.stat(str(p)).st_mode)
    assert mode == 0o600, oct(mode)


def test_expired_session_is_not_returned():
    session.save("stale", expires="2000-01-01T00:00:00Z")
    assert session.load() is None


def test_live_session_round_trips():
    session.save("live", expires="2099-01-01T00:00:00Z",
                 cookie_name="caselist_token")
    got = session.load()
    assert got["token"] == "live"
    assert got["cookie_name"] == "caselist_token"


def test_missing_and_corrupt_files_are_handled():
    assert session.load() is None            # nothing saved yet
    p = session.session_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert session.load() is None            # never raises


def test_unparseable_expiry_defers_to_the_server():
    session.save("tok", expires="whenever")
    assert session.load()["token"] == "tok"


def test_clear_removes_it():
    session.save("tok")
    assert session.clear() is True
    assert session.load() is None
    assert session.clear() is False          # idempotent
