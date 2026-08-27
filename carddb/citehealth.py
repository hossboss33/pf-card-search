"""Cite health checks (feature 9.5, spec §9.5, §0.2).

Nightly job samples cards with a source_url and classifies each link:

- alive:      2xx, final host is the original host
- redirected: kept a 2xx after redirects but landed on a different host
- paywalled:  401/402/403, or a common paywall marker in the first 4KB
- dead:       other 4xx/5xx, or a network error

Non-alive results always carry a Wayback Machine lookup URL. Politeness per
spec §0.2: 1 request/second pacing, a 10s timeout, and the configured
User-Agent when this module constructs its own client.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import httpx

from .config import load_config
from .rawstore import now_iso

WAYBACK_PREFIX = "https://web.archive.org/web/*/"
PAYWALL_STATUSES = (401, 402, 403)
HEAD_BYTES = 4096  # how much of a 2xx body we scan for paywall markers

# Lowercase substrings that mark metered/paywalled article pages. Heuristic;
# checked only against the first HEAD_BYTES of the response.
PAYWALL_MARKERS = (
    "paywall",
    "subscribe to continue",
    "subscribe to read",
    "subscription required",
    "subscribers only",
    "sign in to continue",
    "sign in to read",
    "log in to continue",
    "register to continue",
    "create a free account to continue",
    "purchase this article",
    "to continue reading",
    "metered-content",
    "piano.io",
    "tinypass.com",
)

DEFAULT_RPS = 1.0  # spec §0.2: max 1 request/second


def wayback_url(url: str) -> str:
    """Wayback Machine lookup URL for a source link."""
    return WAYBACK_PREFIX + url


def _host(url: str) -> str:
    """Lowercased host with any leading 'www.' dropped, so the ubiquitous
    example.com -> www.example.com redirect still counts as alive."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def check_url(client: httpx.Client, url: str) -> Dict[str, Any]:
    """Classify one source URL. Never raises.

    Returns {'status', 'http_status', 'final_url', 'wayback_url'};
    wayback_url is filled for every non-alive status."""
    try:
        with client.stream("GET", url, follow_redirects=True) as resp:
            http_status = resp.status_code
            final_url = str(resp.url)
            head = b""
            if 200 <= http_status < 300:
                for chunk in resp.iter_bytes():
                    head += chunk
                    if len(head) >= HEAD_BYTES:
                        break
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError):
        return {"status": "dead", "http_status": None, "final_url": None,
                "wayback_url": wayback_url(url)}

    if http_status in PAYWALL_STATUSES:
        status = "paywalled"
    elif 200 <= http_status < 300:
        text = head[:HEAD_BYTES].decode("utf-8", "ignore").lower()
        if any(marker in text for marker in PAYWALL_MARKERS):
            status = "paywalled"
        elif _host(final_url) != _host(url):
            status = "redirected"
        else:
            status = "alive"
    else:
        status = "dead"

    return {
        "status": status,
        "http_status": http_status,
        "final_url": final_url,
        "wayback_url": None if status == "alive" else wayback_url(url),
    }


def _pick_cards(conn: sqlite3.Connection, limit: int):
    """Cards with a source_url: never-checked first, then oldest checked_at."""
    return conn.execute(
        """
        SELECT c.id AS card_id, c.source_url
        FROM cards c
        LEFT JOIN cite_health h ON h.card_id = c.id
        WHERE c.source_url IS NOT NULL AND c.source_url != ''
        ORDER BY (h.checked_at IS NULL) DESC, h.checked_at ASC, c.id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _upsert(conn: sqlite3.Connection, card_id: int, result: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO cite_health (card_id, status, http_status, final_url,
                                 wayback_url, checked_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(card_id) DO UPDATE SET
          status = excluded.status,
          http_status = excluded.http_status,
          final_url = excluded.final_url,
          wayback_url = excluded.wayback_url,
          checked_at = excluded.checked_at
        """,
        (card_id, result["status"], result["http_status"],
         result["final_url"], result["wayback_url"], now_iso()),
    )


def run_citehealth(conn: sqlite3.Connection, limit: int = 200,
                   timeout: float = 10.0,
                   client: Optional[httpx.Client] = None,
                   rps: float = DEFAULT_RPS,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> int:
    """Check up to `limit` cards' source URLs and upsert cite_health rows.

    Uses the passed-in httpx.Client (tests inject MockTransport-backed
    clients) or constructs one with the 10s timeout and the configured
    User-Agent. Paces requests at `rps` (1/s per spec §0.2). Returns the
    number of cards checked."""
    rows = _pick_cards(conn, limit)
    if not rows:
        return 0

    own_client = client is None
    if own_client:
        cfg = load_config()
        client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": cfg["sync"]["user_agent"]},
        )

    interval = (1.0 / rps) if rps > 0 else 0.0
    last: Optional[float] = None
    checked = 0
    try:
        for row in rows:
            if last is not None and interval > 0:
                remaining = interval - (clock() - last)
                if remaining > 0:
                    sleep(remaining)
            last = clock()
            result = check_url(client, row["source_url"])
            _upsert(conn, row["card_id"], result)
            checked += 1
        conn.commit()
    finally:
        if own_client:
            client.close()
    return checked
