"""config.toml loading. Spec §10.

Values live in config.toml at the repo root (or a path in CARDDB_CONFIG).
Credentials are named by env var, never stored (spec §0.3).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9/3.10
    import tomli as tomllib  # type: ignore

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "paths": {
        "db": "data/carddb.sqlite",
        "raw_store": "data/raw",
        "topics": "data/topics.json",
        "reports": "reports",
        "backups": "backups",
    },
    "sync": {
        "api_base": "https://api.opencaselist.com/v1",
        "rate_limit_rps": 1.0,          # spec §0.2: max 1 request/second
        "max_retries": 5,
        "user_agent": "pf-card-search (personal research index; contact: set contact_email)",
        "contact_email": "",            # set yours; it goes into the User-Agent
        "tabroom_username_env": "TABROOM_USERNAME",
        "tabroom_password_env": "TABROOM_PASSWORD",
        "endpoints_file": "config/endpoints.toml",
    },
    "hf": {
        "dataset": "Yusuf5/OpenCaselist",
        # PF caselist slugs are discovered from the data (`carddb ingest
        # --source hf` logs distinct values); override here only after
        # inspecting, never guess (spec §2.1).
        "pf_caselist_slugs": [],
    },
    "search": {
        "page_size": 30,
    },
    "features": {
        "semantic": False,
    },
    "export": {
        "wpm": 250,
    },
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None) -> Dict[str, Any]:
    cfg_path = Path(path or os.environ.get("CARDDB_CONFIG", ROOT / "config.toml"))
    cfg = DEFAULTS
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            cfg = _merge(DEFAULTS, tomllib.load(f))
    return cfg


def resolve_path(cfg: Dict[str, Any], key: str) -> Path:
    p = Path(cfg["paths"][key])
    return p if p.is_absolute() else ROOT / p
