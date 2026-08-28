"""Local "Connect to openCaselist" flow. Spec §0.2, §0.3, §0.4, §2.2, §8.

openCaselist requires a Tabroom account to read *any* disclosure
(docs/api_access.md §1: only ``/status`` and ``/login`` are public), so the
recent PF seasons — ``hspf23`` … ``hspf26``, which no public dataset
covers — can only be pulled with the owner's own Tabroom login. This
module is the local, loopback-only page that takes that login once and
hands it to :mod:`carddb.api_sync`.

Rules this module exists to enforce, in the order they matter:

1. **Loopback only** (spec §0.4). Every route here refuses with a 403 and
   an explanation unless the server is bound to a loopback address, the
   peer is loopback, and no proxy headers are present. A page that takes
   a password does not get served to a network.
2. **The credentials never land anywhere durable** (spec §0.3). They are
   read out of the POST body, handed to ``api_sync``'s existing login
   code, and kept in one module-level object in this process's memory —
   never in the database, a file, a cookie we set, a URL, or a log line.
   ``Disconnect`` clears them; so does stopping the server. They are held
   at all only because a long sync must be able to re-authenticate when
   its two-week openCaselist session lapses.
3. **No duplicated auth logic.** Login goes through
   ``api_sync._login`` / ``api_sync._list_caselists`` and the sync goes
   through ``api_sync.sync``; this module only *supplies* the credentials
   (by scoping an override over ``api_sync._credentials``, which normally
   reads the documented ``TABROOM_USERNAME`` / ``TABROOM_PASSWORD`` env
   vars — leaving the form blank falls back to exactly that).
4. **Politeness is not ours to relax** (spec §0.2). The background sync
   uses ``api_sync.sync``'s own 1 rps limiter, backoff, and checkpoints.
   Nothing here reconfigures them, and only one sync runs at a time.

Progress is observed, not invented: a logging handler scoped to the sync
thread mirrors ``carddb.*`` records into an in-memory job record that
``GET /connect/status`` serves as JSON for the page to poll.
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import sqlite3
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import api_sync
from .config import resolve_path
from .db import open_db
from .rawstore import now_iso

__all__ = ["register_connect"]

log = logging.getLogger("carddb.connect")

# Progress mirrors records from these loggers (api_sync logs the per-unit
# line; ingest/docx_parser log parse failures under the same root).
PROGRESS_LOGGER = "carddb"

REDACTED = "[redacted]"

# Headers that mean the request reached us through something other than a
# direct loopback connection. Their presence alone disqualifies the page.
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                  "x-real-ip", "forwarded")


# ---------------------------------------------------------------------------
# In-memory state. Nothing below is ever written to disk or to the database.
# ---------------------------------------------------------------------------

class _Credentials:
    """A Tabroom username/password pair, alive only in this process.

    ``__slots__`` so nothing can be stapled on, and ``__repr__`` /
    ``__str__`` are redacted so an accidental ``%r`` in a traceback or a
    log line cannot spill the password.
    """

    __slots__ = ("username", "password")

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "<Tabroom credentials for %s; password %s>" % (
            self.username, REDACTED)

    __str__ = __repr__


class _Job:
    """One background sync, and everything /connect/status reports."""

    def __init__(self, caselist: str):
        self.caselist = caselist
        self.started_at = now_iso()
        self.finished_at: Optional[str] = None
        self.thread_ident: Optional[int] = None
        self.current = "starting"
        self.units = 0
        self.errors: List[str] = []
        self.error: Optional[str] = None
        self.summary: Optional[str] = None
        self.done = False
        self.counts: Dict[str, int] = {
            "units": 0, "units_seen": 0, "units_skipped": 0,
            "parsed": 0, "failed": 0, "new_cards": 0, "new_variants": 0,
        }

    def payload(self) -> Dict[str, Any]:
        counts = dict(self.counts)
        counts["units"] = self.units
        return {
            "caselist": self.caselist,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current": self.current,
            "counts": counts,
            "errors": list(self.errors),
            "error": self.error,
            "summary": self.summary,
            "done": self.done,
        }


class _State:
    """Module-level, process-lifetime, memory-only."""

    def __init__(self):
        self.lock = threading.RLock()
        self.cfg: Dict[str, Any] = {}
        self.creds: Optional[_Credentials] = None
        self.client: Optional[httpx.Client] = None
        self.username: Optional[str] = None
        self.source: Optional[str] = None      # "form" | "environment"
        self.connected_at: Optional[str] = None
        self.caselists: List[Dict[str, Any]] = []
        self.error: Optional[str] = None       # last connect error, redacted
        self.notice: Optional[str] = None
        self.job: Optional[_Job] = None

    # -- connection lifecycle ------------------------------------------
    def set_connected(self, creds, client, username, source, caselists):
        with self.lock:
            self.creds = creds
            self.client = client
            self.username = username
            self.source = source
            self.connected_at = now_iso()
            self.caselists = caselists
            self.error = None

    def clear(self) -> Optional[httpx.Client]:
        """Drop every trace of the session. Returns the client to close."""
        with self.lock:
            creds, client = self.creds, self.client
            if creds is not None:
                # overwrite before dropping the reference; the interpreter
                # owns the actual bytes, but nothing of ours points at them
                creds.username = ""
                creds.password = ""
            self.creds = None
            self.client = None
            self.username = None
            self.source = None
            self.connected_at = None
            self.caselists = []
            self.job = None
            return client

    @property
    def connected(self) -> bool:
        return self.client is not None

    @property
    def running(self) -> bool:
        job = self.job
        return job is not None and not job.done


_STATE = _State()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _secret_values() -> List[str]:
    """Every string that must never appear in output, longest first."""
    out: List[str] = []
    creds = _STATE.creds
    if creds is not None and creds.password:
        out.append(creds.password)
    sync_cfg = (_STATE.cfg or {}).get("sync") or {}
    pass_env = sync_cfg.get("tabroom_password_env") or "TABROOM_PASSWORD"
    env_pw = os.environ.get(pass_env)
    if env_pw:
        out.append(env_pw)
    client = _STATE.client
    if client is not None:
        try:
            out.extend(v for v in dict(client.cookies).values() if v)
        except Exception:  # pragma: no cover - cookie jar shapes vary
            pass
    return sorted({s for s in out if s}, key=len, reverse=True)


def _redact(text: Any, extra: Optional[List[str]] = None) -> str:
    """Strip any live secret out of a message before it is shown or kept.

    ``extra`` carries secrets that are in flight but not yet (or no longer)
    in ``_STATE`` — a password from a login attempt that failed, say, which
    would otherwise reach the page inside the exception text.
    """
    s = "" if text is None else str(text)
    secrets = _secret_values()
    if extra:
        secrets = sorted({x for x in list(secrets) + list(extra) if x},
                         key=len, reverse=True)
    for secret in secrets:
        if secret in s:
            s = s.replace(secret, REDACTED)
    return s


# ---------------------------------------------------------------------------
# Credential supply: an override scoped over api_sync's own env-var reader
# ---------------------------------------------------------------------------

_OVERRIDE_LOCK = threading.RLock()


@contextlib.contextmanager
def _credentials_override(creds: Optional[_Credentials]):
    """Make ``api_sync._credentials`` return ``creds`` for the duration.

    ``creds is None`` means "use the documented env vars": the override is
    not installed at all, so ``api_sync`` reads ``TABROOM_USERNAME`` /
    ``TABROOM_PASSWORD`` and raises its own clear error if they are unset.

    The override is a closure that dies with the ``with`` block, and the
    lock keeps a login request and a running sync from interleaving
    patches. api_sync's login code is reused verbatim; only where the
    username and password come from changes.
    """
    if creds is None:
        yield
        return
    with _OVERRIDE_LOCK:
        original = api_sync._credentials
        api_sync._credentials = lambda cfg: (creds.username, creds.password)
        try:
            yield
        finally:
            api_sync._credentials = original


# ---------------------------------------------------------------------------
# Loopback guard (spec §0.4)
# ---------------------------------------------------------------------------

def _is_loopback_host(host: Any) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    h = h.split("%", 1)[0]          # scoped IPv6, e.g. ::1%lo0
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _declared_bind_host(app) -> Optional[str]:
    """The bind address, when whoever started the server declared it.

    ``app.state.connect_bind_host`` (or ``app.state.bind_host``, or
    ``$CARDDB_BIND_HOST``) is authoritative; a serving wrapper that binds
    somewhere else must say so. Absent a declaration we fall back to the
    connection's own local address, which is what ASGI reports.
    """
    for value in (getattr(app.state, "connect_bind_host", None),
                  getattr(app.state, "bind_host", None),
                  os.environ.get("CARDDB_BIND_HOST")):
        if value:
            return str(value)
    return None


def _argv_bind_host() -> Optional[str]:
    """The ``--host`` this process was started with, if any.

    ``python -m carddb serve --host 0.0.0.0`` and ``uvicorn --host 0.0.0.0``
    both put the bind address in argv, and ASGI does not otherwise report
    it: a request that arrives over loopback on a wide-open server looks
    identical to one on a loopback-only server. Reading argv closes that
    gap. It only ever tightens the guard.
    """
    argv = list(sys.argv[1:])
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return None


def _refusal_reason(request: Request) -> Optional[str]:
    """None when the request may proceed, else why it may not."""
    app = request.app
    declared = _declared_bind_host(app)
    argv_host = _argv_bind_host()
    for template, host in (
            ("this server declares its bind address as %s, which is not a "
             "loopback address", declared),
            ("this server was started with --host %s, which is not a "
             "loopback address", argv_host)):
        if host and not _is_loopback_host(host):
            return template % host
    if declared is None and argv_host is None:
        # Nothing declared the bind address, so fall back to the address
        # this connection actually landed on, which ASGI does report.
        server = request.scope.get("server") or ()
        local_host = server[0] if server else ""
        if not _is_loopback_host(local_host):
            return ("this request did not arrive on a loopback address "
                    "(local address %s)" % (local_host or "unknown"))
    for header in _PROXY_HEADERS:
        if header in request.headers:
            return ("the request carries a %s header, so it was forwarded "
                    "from somewhere else" % header)
    client = request.client
    peer = client.host if client is not None else ""
    if peer:
        try:
            ip = ipaddress.ip_address(str(peer).split("%", 1)[0])
        except ValueError:
            ip = None
        if ip is not None and not ip.is_loopback:
            return "the request came from %s, which is not loopback" % peer
    return None


REFUSAL_TITLE = "This page is available on localhost only"

REFUSAL_BODY = (
    "The connect page takes a Tabroom password, so it runs only when this "
    "server is reachable from this machine alone (spec section 0.4 keeps the "
    "deployment private). Restart with --host 127.0.0.1, or set the "
    "TABROOM_USERNAME and TABROOM_PASSWORD environment variables and sync "
    "from the command line instead: python -m carddb sync --caselist hspf25")


# ---------------------------------------------------------------------------
# Form parsing (urlencoded; the project does not depend on python-multipart)
# ---------------------------------------------------------------------------

async def _read_form(request: Request) -> Dict[str, str]:
    body = await request.body()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {}
    parsed = parse_qs(text, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


# ---------------------------------------------------------------------------
# Login + caselist listing (reuses api_sync)
# ---------------------------------------------------------------------------

def _sync_settings(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], str, int, float]:
    sync_cfg = cfg.get("sync") or {}
    endpoints = api_sync.load_endpoints(cfg)
    api_base = (sync_cfg.get("api_base")
                or (endpoints.get("meta") or {}).get("api_base")
                or api_sync.DEFAULT_API_BASE)
    max_retries = int(sync_cfg.get("max_retries", 5))
    rps = float(sync_cfg.get("rate_limit_rps", 1.0))
    return endpoints, api_base, max_retries, rps


def _caselist_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PF caselists only, newest season first.

    The live ``/caselists`` payload is the raw DB row: ``name`` holds the
    slug and ``display_name`` the label (docs/api_access.md §5a), so read
    both shapes.
    """
    out = []
    for row in rows:
        if not isinstance(row, dict) or not api_sync._is_pf(row):
            continue
        slug = str(row.get("slug") or row.get("name") or "").strip()
        if not slug:
            continue
        year = row.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        out.append({
            "slug": slug,
            "display_name": str(row.get("display_name") or row.get("name")
                                or slug),
            "year": year,
            "archived": bool(row.get("archived")),
        })
    out.sort(key=lambda r: (r["year"] if r["year"] is not None else -1,
                            r["slug"]), reverse=True)
    return out


def _connect(cfg: Dict[str, Any], db_path: str,
             creds: Optional[_Credentials]) -> List[Dict[str, Any]]:
    """Log in through api_sync and list the PF caselists. Two GETs."""
    endpoints, api_base, max_retries, rps = _sync_settings(cfg)
    limiter = api_sync.RateLimiter(rps)
    raw_root = resolve_path(cfg, "raw_store")
    client = httpx.Client(timeout=30.0, follow_redirects=True)
    client.headers["User-Agent"] = api_sync.build_user_agent(cfg)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = open_db(db_path)
        with _credentials_override(creds):
            api_sync._login(conn, client, limiter, max_retries, api_base,
                            endpoints, cfg)
        rows = api_sync._list_caselists(conn, client, limiter, max_retries,
                                        api_base, endpoints, raw_root)
        conn.commit()
    except BaseException:
        client.close()
        raise
    finally:
        if conn is not None:
            conn.close()
    caselists = _caselist_rows(rows if isinstance(rows, list) else [])
    previous = _STATE.clear()
    if previous is not None and previous is not client:
        previous.close()
    _STATE.set_connected(
        creds, client,
        username=(creds.username if creds is not None
                  else os.environ.get(
                      ((cfg.get("sync") or {}).get("tabroom_username_env")
                       or "TABROOM_USERNAME"), "")),
        source=("form" if creds is not None else "environment"),
        caselists=caselists)
    return caselists


def _logout(cfg: Dict[str, Any]) -> None:
    """Drop the session. Calls a logout route only if one is transcribed.

    ``config/endpoints.toml`` currently records no logout operation, and
    endpoints are transcribed, never invented (spec §2.2) — so when there
    is none we simply drop the cookie jar, which is what ends the session
    for this process.
    """
    client = _STATE.client
    if client is not None:
        try:
            endpoints, api_base, max_retries, rps = _sync_settings(cfg)
        except Exception:
            endpoints = None
        if endpoints is not None and (endpoints.get("endpoints")
                                      or {}).get("logout"):
            try:
                api_sync.request_with_backoff(
                    client, "POST",
                    api_sync._url(api_base, endpoints, "logout"),
                    limiter=api_sync.RateLimiter(rps),
                    max_retries=max_retries)
            except Exception as e:      # a failed logout must not wedge us
                log.warning("openCaselist logout call failed (%s); dropping "
                            "the session locally anyway", _redact(e))
    dropped = _STATE.clear()
    if dropped is not None:
        dropped.close()


# ---------------------------------------------------------------------------
# Background sync + progress
# ---------------------------------------------------------------------------

class _ProgressHandler(logging.Handler):
    """Mirror this sync thread's log records into the job record.

    Filtering on the thread id keeps a concurrent web request's logging
    out of the progress feed, and every message goes through ``_redact``
    before it is stored.
    """

    def __init__(self, job: _Job):
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.job.thread_ident:
            return
        try:
            message = _redact(record.getMessage())
        except Exception:  # pragma: no cover - never break the sync
            return
        with _STATE.lock:
            if record.levelno >= logging.WARNING:
                if len(self.job.errors) < 200:
                    self.job.errors.append(message)
            else:
                self.job.current = message
                if message.startswith("unit "):
                    self.job.units += 1


def _run_sync(db_path: str, cfg: Dict[str, Any], job: _Job,
              creds: Optional[_Credentials],
              client: Optional[httpx.Client]) -> None:
    job.thread_ident = threading.get_ident()
    logger = logging.getLogger(PROGRESS_LOGGER)
    handler = _ProgressHandler(job)
    previous_level = logger.level
    logger.addHandler(handler)
    if previous_level > logging.INFO or previous_level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = open_db(db_path)
        # api_sync.sync() owns the rate limiter (1 rps), the backoff, and
        # the checkpoints. Passing the logged-in client reuses this
        # session instead of opening another.
        with _credentials_override(creds):
            stats = api_sync.sync(conn, cfg, caselist=job.caselist,
                                  client=client)
        with _STATE.lock:
            job.counts.update({
                "units_seen": stats.units_seen,
                "units_skipped": stats.units_skipped,
                "parsed": stats.parsed,
                "failed": stats.failed,
                "new_cards": stats.new_cards,
                "new_variants": stats.new_variants,
            })
            job.summary = stats.summary()
            job.current = "finished"
    except BaseException as e:
        with _STATE.lock:
            job.error = _redact("%s: %s" % (type(e).__name__, e))
            job.current = "stopped"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        with _STATE.lock:
            job.finished_at = now_iso()
            job.done = True


def _status_payload() -> Dict[str, Any]:
    with _STATE.lock:
        job = _STATE.job
        job_payload = job.payload() if job is not None else None
        payload: Dict[str, Any] = {
            "connected": _STATE.connected,
            "source": _STATE.source,
            "connected_at": _STATE.connected_at,
            "caselists": [dict(c) for c in _STATE.caselists],
            "running": _STATE.running,
            "job": job_payload,
        }
    if job_payload is None:
        payload.update({
            "current": "", "counts": {}, "errors": [], "done": False,
            "caselist": None, "error": None,
        })
    else:
        payload.update({
            "current": job_payload["current"],
            "counts": job_payload["counts"],
            "errors": job_payload["errors"],
            "done": job_payload["done"],
            "caselist": job_payload["caselist"],
            "error": job_payload["error"],
        })
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register_connect(app, templates) -> None:
    """Mount the connect flow on ``app``. Called once from create_app."""

    cfg = getattr(app.state, "cfg", None) or {}
    _STATE.cfg = cfg

    def _db_path() -> str:
        return str(getattr(app.state, "db_path", ""))

    def _page(request: Request, status_code: int = 200,
              blocked_reason: Optional[str] = None):
        with _STATE.lock:
            ctx = {
                "connected": _STATE.connected,
                "username": _STATE.username,
                "source": _STATE.source,
                "connected_at": _STATE.connected_at,
                "caselists": [dict(c) for c in _STATE.caselists],
                "error": _STATE.error,
                "notice": _STATE.notice,
                "running": _STATE.running,
                "job": _STATE.job.payload() if _STATE.job is not None else None,
                "blocked_reason": blocked_reason,
                "refusal_title": REFUSAL_TITLE,
                "refusal_body": REFUSAL_BODY,
                "username_env": ((cfg.get("sync") or {})
                                 .get("tabroom_username_env")
                                 or "TABROOM_USERNAME"),
                "password_env": ((cfg.get("sync") or {})
                                 .get("tabroom_password_env")
                                 or "TABROOM_PASSWORD"),
                "api_base": ((cfg.get("sync") or {}).get("api_base")
                             or api_sync.DEFAULT_API_BASE),
            }
            # one-shot flash messages: shown once, then gone
            _STATE.notice = None
            _STATE.error = None
        return templates.TemplateResponse(request, "connect.html", ctx,
                                          status_code=status_code)

    def _blocked_json(reason: str):
        return JSONResponse(
            {"error": REFUSAL_TITLE, "reason": reason,
             "detail": REFUSAL_BODY},
            status_code=403)

    @app.get("/connect")
    def connect_page(request: Request):
        reason = _refusal_reason(request)
        if reason:
            return _page(request, status_code=403, blocked_reason=reason)
        return _page(request)

    @app.post("/connect")
    async def connect_submit(request: Request):
        reason = _refusal_reason(request)
        if reason:
            return _page(request, status_code=403, blocked_reason=reason)
        form = await _read_form(request)
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        form.clear()                    # drop the parsed body immediately
        if _STATE.running:
            with _STATE.lock:
                _STATE.error = ("a sync is running; wait for it to finish "
                                "before reconnecting")
            return RedirectResponse("/connect", status_code=303)
        # Blank form means the documented env-var path (docs/api_access.md
        # §6 step 2): api_sync reads TABROOM_USERNAME / TABROOM_PASSWORD
        # and raises its own named error if they are missing.
        creds = _Credentials(username, password) if (username or password) else None
        in_flight = [password] if password else None
        password = ""
        try:
            _connect(cfg, _db_path(), creds)
        except Exception as e:
            with _STATE.lock:
                # the password is not in api_sync's error text, but an error
                # from anywhere else might quote it; redact unconditionally
                _STATE.error = _redact("%s: %s" % (type(e).__name__, e),
                                       extra=in_flight)
                _STATE.notice = None
        else:
            with _STATE.lock:
                _STATE.notice = "Connected to openCaselist"
        finally:
            creds = None
            in_flight = None
        return RedirectResponse("/connect", status_code=303)

    @app.post("/connect/logout")
    def connect_logout(request: Request):
        reason = _refusal_reason(request)
        if reason:
            return _page(request, status_code=403, blocked_reason=reason)
        _logout(cfg)
        with _STATE.lock:
            _STATE.notice = "Disconnected. The login and session are gone."
        return RedirectResponse("/connect", status_code=303)

    @app.post("/connect/sync/{caselist}")
    def connect_sync(request: Request, caselist: str):
        reason = _refusal_reason(request)
        if reason:
            return _page(request, status_code=403, blocked_reason=reason)
        with _STATE.lock:
            if not _STATE.connected:
                _STATE.error = "not connected to openCaselist yet"
                return RedirectResponse("/connect", status_code=303)
            if _STATE.running:
                _STATE.error = ("a sync is already running; one at a time "
                                "keeps the request rate at one per second")
                return RedirectResponse("/connect", status_code=303)
            known = {c["slug"] for c in _STATE.caselists}
            if caselist not in known:
                _STATE.error = ("%s is not in the PF caselists this login "
                                "can see" % caselist)
                return RedirectResponse("/connect", status_code=303)
            job = _Job(caselist)
            _STATE.job = job
            _STATE.error = None
            _STATE.notice = "Sync started for %s" % caselist
            creds, client = _STATE.creds, _STATE.client
        thread = threading.Thread(
            target=_run_sync,
            args=(_db_path(), cfg, job, creds, client),
            name="carddb-connect-sync", daemon=True)
        thread.start()
        return RedirectResponse("/connect", status_code=303)

    @app.get("/connect/status")
    def connect_status(request: Request):
        reason = _refusal_reason(request)
        if reason:
            return _blocked_json(reason)
        return JSONResponse(_status_payload())
