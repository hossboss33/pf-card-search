"""Saved openCaselist session, so you sign in once instead of every sync.

What is stored is the SESSION TOKEN, never the password. openCaselist issues
that token for two weeks (postLogin.js inserts the session with
`DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 2 WEEK)`), so one sign-in covers a
fortnight of syncing. The file is written 0600 and lives outside the repo
tree; the password is never written anywhere, and `getpass` keeps it out of
the terminal and out of shell history.

A stolen token is still a live session, which is why the file is
owner-read-only and why `carddb logout` exists.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def session_path() -> Path:
    """~/.config/pf-card-search/session.json, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "pf-card-search" / "session.json"


def save(token: str, cookie_name: str = "caselist_token",
         expires: Optional[str] = None, username: Optional[str] = None) -> Path:
    p = session_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookie_name": cookie_name,
        "token": token,
        "expires": expires,
        # Stored only so the CLI can say who is signed in. Never a password.
        "username": username,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Create with 0600 from the start: never briefly world-readable.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=1)
    os.chmod(str(p), stat.S_IRUSR | stat.S_IWUSR)
    return p


def load() -> Optional[dict]:
    """Return the saved session, or None if absent, unreadable, or expired."""
    p = session_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return None
    if not data.get("token"):
        return None
    exp = data.get("expires")
    if exp:
        try:
            # Compare as UTC; openCaselist sends an ISO timestamp.
            when = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if when <= datetime.now(timezone.utc):
                return None
        except ValueError:
            pass          # unparseable expiry: let the server decide
    return data


def clear() -> bool:
    p = session_path()
    if p.exists():
        p.unlink()
        return True
    return False
