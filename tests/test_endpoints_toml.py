"""Validate config/endpoints.toml (spec §2.2, §12 items 1-3).

The file was transcribed from the live OpenAPI spec at
https://api.opencaselist.com/v1/docs and the caselist repo's
server/v1/routes/paths.js. These tests keep it structurally sound and keep
the transcription honest (every endpoint carries a method, a path template
whose placeholders are declared, and an explicit verified flag). No network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomli

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS_PATH = ROOT / "config" / "endpoints.toml"

PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

# Endpoints spec §2.2 requires for the sync milestone.
REQUIRED = [
    "login",
    "caselists",
    "schools",
    "teams",
    "rounds",
    "cites",
    "download",
    "bulk_downloads",
]


@pytest.fixture(scope="module")
def cfg():
    with open(ENDPOINTS_PATH, "rb") as f:
        return tomli.load(f)


def test_file_exists_and_parses(cfg):
    assert isinstance(cfg, dict)
    assert "meta" in cfg
    assert "auth" in cfg
    assert "endpoints" in cfg


def test_meta(cfg):
    meta = cfg["meta"]
    assert meta["api_base"] == "https://api.opencaselist.com/v1"
    assert meta["spec_url"] == "https://api.opencaselist.com/v1/docs"
    assert meta["openapi_version"] == "3.0.2"


def test_auth_scheme_is_cookie(cfg):
    auth = cfg["auth"]
    assert auth["scheme"] == "apiKey"
    assert auth["location"] == "cookie"
    assert auth["cookie_name"] == "caselist_token"
    assert auth["verified"] is True


def test_required_endpoints_present(cfg):
    eps = cfg["endpoints"]
    missing = [name for name in REQUIRED if name not in eps]
    assert not missing, "endpoints.toml missing: %s" % missing


def test_every_endpoint_shape(cfg):
    for name, ep in cfg["endpoints"].items():
        assert ep.get("method") in ("GET", "POST"), name
        path = ep.get("path", "")
        assert path.startswith("/"), name
        # placeholders in the template must be declared in path_params, and
        # vice versa, in order
        placeholders = PLACEHOLDER_RE.findall(path)
        assert placeholders == ep.get("path_params", []), name
        assert isinstance(ep.get("verified"), bool), name
        assert isinstance(ep.get("auth_required"), bool), name


def test_core_paths_are_the_transcribed_ones(cfg):
    """Exact path templates as they appear in the OpenAPI spec + paths.js."""
    eps = cfg["endpoints"]
    assert eps["login"]["path"] == "/login"
    assert eps["caselists"]["path"] == "/caselists"
    assert eps["schools"]["path"] == "/caselists/{caselist}/schools"
    assert eps["teams"]["path"] == "/caselists/{caselist}/schools/{school}/teams"
    assert (
        eps["rounds"]["path"]
        == "/caselists/{caselist}/schools/{school}/teams/{team}/rounds"
    )
    assert (
        eps["cites"]["path"]
        == "/caselists/{caselist}/schools/{school}/teams/{team}/cites"
    )
    assert eps["download"]["path"] == "/download"
    assert eps["bulk_downloads"]["path"] == "/caselists/{caselist}/downloads"


def test_enumeration_chain_nests(cfg):
    """caselists -> schools -> teams -> rounds/cites nest as URL prefixes."""
    eps = cfg["endpoints"]
    chain = ["caselists", "schools", "teams", "rounds"]
    for parent, child in zip(chain, chain[1:]):
        parent_prefix = eps[parent]["path"]
        assert eps[child]["path"].startswith(parent_prefix + "/"), (parent, child)
    assert eps["cites"]["path"].startswith(eps["teams"]["path"] + "/")


def test_login_contract(cfg):
    login = cfg["endpoints"]["login"]
    assert login["method"] == "POST"
    assert login["auth_required"] is False
    assert login["body_params"] == ["username", "password", "remember"]
    assert login["verified"] is True


def test_download_takes_path_query(cfg):
    dl = cfg["endpoints"]["download"]
    assert dl["method"] == "GET"
    assert dl["query_params"] == ["path"]
    assert dl["auth_required"] is True
    assert dl["verified"] is True


def test_required_endpoints_all_verified(cfg):
    """Everything the sync milestone depends on was actually transcribed from
    the live spec; only the explicitly-uncertain extras may be unverified."""
    eps = cfg["endpoints"]
    for name in REQUIRED:
        assert eps[name]["verified"] is True, name
    unverified = sorted(n for n, ep in eps.items() if not ep["verified"])
    assert unverified == ["openev", "search"], unverified


def test_rounds_and_cites_take_side_filter(cfg):
    eps = cfg["endpoints"]
    assert eps["rounds"]["query_params"] == ["side"]
    assert eps["cites"]["query_params"] == ["side"]


def test_report_exists_and_covers_auth_flow():
    report = ROOT / "docs" / "api_verify.md"
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    # the report must document the Tabroom -> cookie auth flow and robots posture
    assert "caselist_token" in text
    assert "Tabroom" in text
    assert "robots.txt" in text
