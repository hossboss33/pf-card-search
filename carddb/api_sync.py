"""openCaselist live sync. Spec §0.2, §2.2, §11 M4.

The 2024→today gap is filled through the documented API, never HTML
scraping. Everything here is built on three rules:

1. **Transcribed endpoints.** URLs come from ``config/endpoints.toml``,
   which was transcribed from the live OpenAPI spec (see
   docs/api_verify.md). ``discover_endpoints()`` re-fetches the spec at
   runtime and regenerates that file, merging into (never clobbering) the
   checked-in structure; if the fetch fails the checked-in file stands.
2. **Politeness.** Every request goes through ``RateLimiter`` (1 rps cap
   from config) + ``request_with_backoff`` (429/5xx backoff honoring
   Retry-After). The User-Agent names the project and includes the contact
   email when configured. Auth is the owner's own Tabroom credentials read
   from the env vars *named* in config — values are never stored, logged,
   or echoed.
3. **Resumability.** One checkpoint row per (caselist, school, team) in
   ``sync_checkpoints``, written transactionally only after that unit
   fully completes. Checkpoints are scoped to a *run* (module-owned
   ``sync_runs`` table, like hf_loader's hf_buckets): an unfinished run
   for the same scope is resumed and its completed units are skipped
   without a single HTTP request; a finished prior run is never resumed —
   a new invocation re-processes every unit (ingest is idempotent and the
   rate limiter keeps it polite), so weekly in-season syncs pick up new
   rounds instead of freezing the corpus. Every fetched JSON body and
   .docx lands in the content-addressed raw store with a ``documents``
   row before parsing; a file path already fetched is served from that
   store with zero HTTP forever (spec §0.2).

Enumeration walk (client-side PF filter — the API has no server-side event
filter): caselists → schools → teams → rounds (+ per-round open-source
.docx via ``/download?path=``). Rounds with no usable open-source doc fall
back to the team's pasted cites, ingested with ``fidelity='cites_only'``.
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import httpx

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9/3.10
    import tomli as tomllib  # type: ignore

from .config import ROOT, resolve_path
from .db import fts_upsert_cards, ledger_seen, recompute_aggregates
from .ingest import (CardRecord, IngestStats, attach_variant,
                     get_or_create_caselist, get_or_create_round,
                     get_or_create_school, get_or_create_team, insert_card,
                     ledger_stamp)
from .keys import sha256_bytes
from .ratelimit import RateLimiter, SyncError, request_with_backoff
from .rawstore import now_iso, record_document, store_bytes

__all__ = ["discover_endpoints", "sync", "SyncError", "build_user_agent",
           "load_endpoints"]

log = logging.getLogger("carddb.sync")

DEFAULT_API_BASE = "https://api.opencaselist.com/v1"
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

# PF event values seen or plausible in /caselists rows. The exact live value
# is confirmed at M4 by inspecting the listing (docs/api_verify.md §5);
# matching stays conservative — never enumerate a caselist that isn't PF.
_PF_EVENTS = {"pf", "pfd", "hspf", "publicforum", "public forum",
              "public-forum"}


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------

def load_endpoints(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Read the transcribed endpoints file named in config."""
    p = Path((cfg.get("sync") or {}).get("endpoints_file")
             or "config/endpoints.toml")
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise SyncError(
            "endpoints file not found: %s — restore config/endpoints.toml "
            "or run carddb.api_sync.discover_endpoints() first (spec §2.2: "
            "endpoints are transcribed from the OpenAPI spec, never "
            "invented)." % p)
    with open(p, "rb") as f:
        return tomllib.load(f)


def build_user_agent(cfg: Dict[str, Any]) -> str:
    """Project-naming User-Agent; includes the contact email when set
    (spec §0.2)."""
    sync_cfg = cfg.get("sync") or {}
    ua = (sync_cfg.get("user_agent") or "").strip() or "pf-card-search"
    email = (sync_cfg.get("contact_email") or "").strip()
    if email and email not in ua:
        ua = "%s (contact: %s)" % (ua, email)
    return ua


def _credentials(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Read Tabroom credentials from the env vars *named* in config.

    Raises SyncError naming the missing env vars (names only, never
    values — spec §0.3 / §10)."""
    sync_cfg = cfg.get("sync") or {}
    user_env = sync_cfg.get("tabroom_username_env") or "TABROOM_USERNAME"
    pass_env = sync_cfg.get("tabroom_password_env") or "TABROOM_PASSWORD"
    username = os.environ.get(user_env)
    password = os.environ.get(pass_env)
    missing = [env for env, val in ((user_env, username), (pass_env, password))
               if not val]
    if missing:
        raise SyncError(
            "Tabroom credentials missing: set the environment variable%s %s "
            "before syncing (your OWN Tabroom login — spec §0.3: never "
            "shared or scraped credentials; config stores env var names, "
            "never values)." % ("s" if len(missing) > 1 else "",
                                " and ".join(missing)))
    return username, password


def _url(api_base: str, endpoints: Dict[str, Any], name: str,
         path_args: Optional[Dict[str, Any]] = None) -> str:
    ep = (endpoints.get("endpoints") or {}).get(name)
    if ep is None:
        raise SyncError("endpoint %r missing from endpoints.toml; "
                        "re-run discover_endpoints()" % name)
    path = ep["path"]
    for k, v in (path_args or {}).items():
        # quote() everything: school/team names can hold spaces & slashes.
        # Values are passed exactly as the parent listing returned them
        # (no case transforms — see docs/api_verify.md §5 on getRounds).
        path = path.replace("{%s}" % k, quote(str(v), safe=""))
    return api_base.rstrip("/") + path


# ---------------------------------------------------------------------------
# Endpoint discovery (spec §2.2 first task; runtime regeneration)
# ---------------------------------------------------------------------------

def discover_endpoints(api_base: str, out_path, *,
                       client: Optional[httpx.Client] = None,
                       timeout: float = 30.0) -> Dict[str, Any]:
    """Fetch the OpenAPI spec at ``<api_base>/docs`` and regenerate
    ``out_path`` (endpoints.toml).

    Merge semantics (the checked-in file is the fallback, never clobbered):
    - Existing [meta]/[auth]/[endpoints.*] entries keep their names, key
      shapes, and ``verified`` flags; an entry whose method+path vanished
      from the live spec is downgraded to ``verified = false``.
    - Paths present in the live spec but absent from the file are appended
      with generated names and ``verified = true`` (their presence in the
      spec is exactly what "verified" means for a transcription).
    - If the spec cannot be fetched, the existing file is returned
      untouched; with no existing file either, SyncError.
    """
    out_path = Path(out_path)
    baseline: Dict[str, Any] = {}
    if out_path.exists():
        with open(out_path, "rb") as f:
            baseline = tomllib.load(f)

    spec_url = api_base.rstrip("/") + "/docs"
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=timeout,
                              headers={"User-Agent": "pf-card-search"})
    try:
        resp = client.get(spec_url)
        resp.raise_for_status()
        spec = resp.json()
    except Exception as e:  # noqa: BLE001 — any fetch/parse failure → fallback
        if baseline:
            log.warning("could not fetch OpenAPI spec at %s (%s); keeping "
                        "the checked-in %s", spec_url, e, out_path)
            return baseline
        raise SyncError(
            "could not fetch the OpenAPI spec at %s and no fallback "
            "endpoints file exists at %s: %r" % (spec_url, out_path, e))
    finally:
        if owns_client:
            client.close()

    merged = _merge_openapi(baseline, spec, api_base=api_base,
                            spec_url=spec_url)
    _write_endpoints_toml(out_path, merged)
    log.info("regenerated %s from %s (%d endpoints)", out_path, spec_url,
             len(merged.get("endpoints") or {}))
    return merged


def _spec_operations(spec: Dict[str, Any]) -> "List[Tuple[str, str, dict]]":
    """[(METHOD, path, operation), ...] in spec order."""
    ops = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                ops.append((method.upper(), path, op if isinstance(op, dict) else {}))
    return ops


def _query_params(op: Dict[str, Any]) -> List[str]:
    return [p.get("name") for p in op.get("parameters") or []
            if isinstance(p, dict) and p.get("in") == "query" and p.get("name")]


def _gen_name(method: str, path: str, existing: Dict[str, Any]) -> str:
    segs = [s for s in path.strip("/").split("/") if s]
    if not segs:
        base = "root"
    elif segs[-1].startswith("{"):
        prev = next((s for s in reversed(segs[:-1]) if not s.startswith("{")),
                    "item")
        base = prev[:-1] if prev.endswith("s") and len(prev) > 1 else prev
    else:
        base = segs[-1]
    base = re.sub(r"[^a-z0-9_]+", "_", base.lower()).strip("_") or "endpoint"
    if method != "GET":
        base = "%s_%s" % (base, method.lower())
    name, n = base, 2
    while name in existing:
        name = "%s_%d" % (base, n)
        n += 1
    return name


def _merge_openapi(baseline: Dict[str, Any], spec: Dict[str, Any], *,
                   api_base: str, spec_url: str) -> Dict[str, Any]:
    ops = _spec_operations(spec)
    op_map = {(m, p): op for m, p, op in ops}
    global_security = bool(spec.get("security"))

    meta = dict(baseline.get("meta") or {})
    meta["api_base"] = api_base.rstrip("/")
    meta["spec_url"] = spec_url
    if spec.get("openapi"):
        meta["openapi_version"] = spec["openapi"]
    title = (spec.get("info") or {}).get("title")
    if title:
        meta["api_title"] = title
    meta["fetched"] = date.today().isoformat()

    auth = dict(baseline.get("auth") or {})
    schemes = ((spec.get("components") or {}).get("securitySchemes") or {})
    for sch in schemes.values():
        if isinstance(sch, dict) and sch.get("type") == "apiKey" \
                and sch.get("in") == "cookie":
            auth["scheme"] = "apiKey"
            auth["location"] = "cookie"
            auth["cookie_name"] = sch.get("name") \
                or auth.get("cookie_name", "caselist_token")
            auth["verified"] = True
            break

    endpoints: Dict[str, Any] = {}
    used: Set[Tuple[str, str]] = set()
    for name, ep in (baseline.get("endpoints") or {}).items():
        new_ep = dict(ep)
        key = (str(ep.get("method", "GET")).upper(), ep.get("path", ""))
        op = op_map.get(key)
        if op is None:
            # vanished from the live spec — keep the entry (a caller may
            # still depend on it) but flag it honestly
            new_ep["verified"] = False
        else:
            used.add(key)
            if op.get("parameters"):
                qp = _query_params(op)
                if qp:
                    new_ep["query_params"] = qp
            # path_params re-derived from the template so the invariant
            # (placeholders == path_params) always holds
            placeholders = PLACEHOLDER_RE.findall(ep.get("path", ""))
            if placeholders:
                new_ep["path_params"] = placeholders
            elif "path_params" in new_ep:
                del new_ep["path_params"]
            # keep baseline's verified flag: presence in the spec never
            # upgrades an entry the research pass deliberately left
            # unverified (e.g. /search's undocumented shard values)
        endpoints[name] = new_ep

    for method, path, op in ops:
        if (method, path) in used:
            continue
        name = _gen_name(method, path, endpoints)
        ep: Dict[str, Any] = {"method": method, "path": path}
        placeholders = PLACEHOLDER_RE.findall(path)
        if placeholders:
            ep["path_params"] = placeholders
        qp = _query_params(op)
        if qp:
            ep["query_params"] = qp
        if "security" in op:
            ep["auth_required"] = bool(op["security"])
        else:
            ep["auth_required"] = global_security
        ep["verified"] = True
        endpoints[name] = ep

    return {"meta": meta, "auth": auth, "endpoints": endpoints}


_EP_KEY_ORDER = ("method", "path", "path_params", "query_params",
                 "body_params", "auth_required", "rate_limit", "verified")


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):          # before int: bool subclasses int
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # json string escaping is valid TOML basic-string escaping for our
        # values (\" \\ \uXXXX)
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError("cannot serialize %r to TOML" % (v,))


def _write_endpoints_toml(out_path: Path, data: Dict[str, Any]) -> None:
    meta = data.get("meta") or {}
    lines = [
        "# openCaselist API endpoints — regenerated by "
        "carddb.api_sync.discover_endpoints",
        "# on %s from %s." % (meta.get("fetched", ""),
                              meta.get("spec_url", "")),
        "# Paths are transcribed from the live OpenAPI spec, never invented "
        "(spec §2.2).",
        "# The previous file's names, shapes, and verified flags were "
        "merged, not clobbered.",
        "",
        "[meta]",
    ]
    for k, v in meta.items():
        lines.append("%s = %s" % (k, _toml_value(v)))
    lines.append("")
    lines.append("[auth]")
    for k, v in (data.get("auth") or {}).items():
        lines.append("%s = %s" % (k, _toml_value(v)))
    for name, ep in (data.get("endpoints") or {}).items():
        lines.append("")
        lines.append("[endpoints.%s]" % name)
        for key in _EP_KEY_ORDER:
            if key in ep and ep[key] is not None:
                lines.append("%s = %s" % (key, _toml_value(ep[key])))
        for k, v in ep.items():
            if k not in _EP_KEY_ORDER and v is not None:
                lines.append("%s = %s" % (k, _toml_value(v)))
    text = "\n".join(lines) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out_path)


# ---------------------------------------------------------------------------
# Sync (spec §2.2, §11 M4)
# ---------------------------------------------------------------------------

def _is_pf(row: Dict[str, Any]) -> bool:
    event = str(row.get("event") or "").strip().lower()
    if event:
        return event in _PF_EVENTS
    # no event field: fall back to slug/name convention. PF slugs embed
    # "pf" at the end of the letter run (hspf25, mspf24, pf24), so accept
    # "pf" followed by a non-letter or end-of-string; ndtceda/nfald/
    # hspolicy/hsld never contain that shape.
    blob = ("%s %s" % (row.get("slug", ""), row.get("name", ""))).lower()
    return bool(re.search(r"pf(?=[^a-z]|$)", blob))


def _since_year(since: Optional[str]) -> Optional[int]:
    if not since:
        return None
    m = re.search(r"\d{4}", str(since))
    return int(m.group(0)) if m else None


def _get_json(client, limiter, max_retries, url, *,
              params: Optional[Dict[str, str]] = None
              ) -> Tuple[Any, bytes, str]:
    """GET a JSON endpoint. Returns (parsed, raw bytes, final url str)."""
    resp = request_with_backoff(client, "GET", url, limiter=limiter,
                                max_retries=max_retries, params=params)
    final_url = str(resp.request.url)
    if resp.status_code == 401:
        raise SyncError("GET %s -> HTTP 401: the caselist session was "
                        "rejected or expired; re-run to log in again."
                        % final_url)
    if resp.status_code != 200:
        raise SyncError("GET %s -> HTTP %d: %s"
                        % (final_url, resp.status_code, resp.text[:200]))
    try:
        return resp.json(), resp.content, final_url
    except ValueError as e:
        raise SyncError("GET %s returned non-JSON (%s)" % (final_url, e))


def _store_blob(conn, raw_root: Path, content: bytes, origin_url: str,
                filename: Optional[str] = None) -> Tuple[str, int]:
    """Raw-store one fetched payload + its documents row (spec §2.3)."""
    sha, path = store_bytes(raw_root, content)
    doc_id = record_document(conn, sha, "api", origin_url, filename,
                             str(path))
    return sha, doc_id


def _login(conn, client, limiter, max_retries, api_base, endpoints, cfg) -> None:
    cookie_name_cfg = (endpoints.get("auth") or {}).get("cookie_name",
                                                        "caselist_token")
    # A saved session (carddb login) beats prompting: openCaselist issues the
    # token for two weeks, so one sign-in covers a fortnight of syncing.
    try:
        from .session import load as _load_session
        saved = _load_session()
    except Exception:
        saved = None
    if saved and saved.get("token"):
        client.cookies.set(saved.get("cookie_name") or cookie_name_cfg,
                           saved["token"])
        return

    username, password = _credentials(cfg)
    url = _url(api_base, endpoints, "login")
    resp = request_with_backoff(
        client, "POST", url, limiter=limiter, max_retries=max_retries,
        json={"username": username, "password": password, "remember": True})
    if resp.status_code not in (200, 201):
        sync_cfg = cfg.get("sync") or {}
        raise SyncError(
            "login failed at %s (HTTP %d). Check the Tabroom credentials in "
            "$%s / $%s." % (url, resp.status_code,
                            sync_cfg.get("tabroom_username_env",
                                         "TABROOM_USERNAME"),
                            sync_cfg.get("tabroom_password_env",
                                         "TABROOM_PASSWORD")))
    # The server sets the caselist_token cookie itself; the 201 body also
    # carries the token — set it manually if the cookie didn't stick.
    cookie_name = (endpoints.get("auth") or {}).get("cookie_name",
                                                    "caselist_token")
    token = None
    try:
        token = resp.json().get("token")
    except ValueError:
        pass
    if token and not client.cookies.get(cookie_name):
        client.cookies.set(cookie_name, token)


# Module-owned bookkeeping (like hf_loader's hf_buckets): one row per sync
# run. Checkpoints only ever skip units inside an *unfinished* run — a
# finished run is history, never a reason to skip (spec §2.2 checkpoints
# exist so a crash resumes instead of re-requesting, not so a completed
# season is frozen).
SYNC_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
)
"""


def _run_scope(caselist: Optional[str], since: Optional[str]) -> str:
    return "%s|%s" % (caselist or "*", since or "*")


def _open_run_id(conn, scope: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM sync_runs WHERE scope = ? AND finished_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (scope,)).fetchone()
    return row["id"] if row else None


def _checkpoint_state(conn, caselist: str, school: str, team: str
                      ) -> Optional[str]:
    row = conn.execute(
        "SELECT state FROM sync_checkpoints "
        "WHERE caselist = ? AND school = ? AND team = ?",
        (caselist, school, team)).fetchone()
    return row["state"] if row else None


def _checkpoint_done(conn, caselist: str, school: str, team: str) -> None:
    conn.execute(
        "INSERT INTO sync_checkpoints (caselist, school, team, state, "
        " updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(caselist, school, team) DO UPDATE SET "
        " state = excluded.state, updated_at = excluded.updated_at",
        (caselist, school, team, "done", now_iso()))


def sync(conn: sqlite3.Connection, cfg: Dict[str, Any],
         caselist: Optional[str] = None, since: Optional[str] = None, *,
         client: Optional[httpx.Client] = None,
         limiter: Optional[RateLimiter] = None) -> IngestStats:
    """Walk PF caselists → schools → teams → rounds and ingest everything.

    ``client``/``limiter`` are injectable for tests (mock transport, fake
    clock); production builds its own from config. Returns IngestStats where
    a "unit" is one (caselist, school, team): ``units_seen`` counts every
    team encountered, ``units_skipped`` the ones resumed past via
    checkpoint (zero HTTP requests issued for those).

    Checkpoints are run-scoped: an unfinished ``sync_runs`` row for the
    same (caselist, since) scope is resumed, so a crash-restart skips its
    completed units; a finished prior run is never resumed — a fresh run
    invalidates the target checkpoints and re-processes every unit (ingest
    is idempotent, so re-processing adds nothing already known, and cached
    blobs mean known files cost zero HTTP).
    """
    sync_cfg = cfg.get("sync") or {}
    endpoints = load_endpoints(cfg)
    api_base = (sync_cfg.get("api_base")
                or (endpoints.get("meta") or {}).get("api_base")
                or DEFAULT_API_BASE)
    max_retries = int(sync_cfg.get("max_retries", 5))
    if limiter is None:
        limiter = RateLimiter(float(sync_cfg.get("rate_limit_rps", 1.0)))
    raw_root = resolve_path(cfg, "raw_store")
    stats = IngestStats()

    conn.execute(SYNC_RUNS_DDL)
    conn.commit()
    scope = _run_scope(caselist, since)
    run_id = _open_run_id(conn, scope)
    resumed = run_id is not None

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=30.0, follow_redirects=True)
    client.headers["User-Agent"] = build_user_agent(cfg)
    try:
        _login(conn, client, limiter, max_retries, api_base, endpoints, cfg)
        rows = _list_caselists(conn, client, limiter, max_retries, api_base,
                               endpoints, raw_root)
        if caselist:
            targets = [r for r in rows
                       if r.get("slug") == caselist or r.get("name") == caselist]
            if not targets:
                raise SyncError(
                    "caselist %r not found in the API listing; available: %s"
                    % (caselist,
                       ", ".join(sorted(str(r.get("slug") or r.get("name"))
                                        for r in rows)) or "(none)"))
        else:
            targets = [r for r in rows if _is_pf(r)]
            skipped_events = sorted({str(r.get("event"))
                                     for r in rows if not _is_pf(r)})
            if skipped_events:
                log.info("non-PF caselists skipped (events: %s)",
                         ", ".join(skipped_events))
        year_floor = _since_year(since)
        if year_floor is not None:
            targets = [r for r in targets
                       if r.get("year") is None or int(r["year"]) >= year_floor]

        if not resumed:
            # New run (no unfinished run for this scope): the previous
            # run's checkpoints are history, not skip permits — invalidate
            # them in the same commit that opens the run, so every unit is
            # re-processed and newly-disclosed rounds are picked up.
            slugs = [str(r.get("slug") or r.get("name")) for r in targets]
            if slugs:
                conn.execute(
                    "DELETE FROM sync_checkpoints WHERE caselist IN (%s)"
                    % ",".join("?" * len(slugs)), slugs)
            cur = conn.execute(
                "INSERT INTO sync_runs (scope, started_at) VALUES (?, ?)",
                (scope, now_iso()))
            run_id = cur.lastrowid
            conn.commit()
        log.info("sync run %s (scope %s) %s", run_id, scope,
                 "resumed" if resumed else "started")

        for row in targets:
            _sync_caselist(conn, client, limiter, max_retries, api_base,
                           endpoints, raw_root, row, stats)
        # clean completion: close this run (and any stray open runs of the
        # same scope) so the next invocation starts fresh
        conn.execute(
            "UPDATE sync_runs SET finished_at = ? "
            "WHERE scope = ? AND finished_at IS NULL", (now_iso(), scope))
        conn.commit()
    finally:
        if owns_client:
            client.close()
    log.info("sync summary: %s", stats.summary())
    return stats


def _list_caselists(conn, client, limiter, max_retries, api_base, endpoints,
                    raw_root) -> List[Dict[str, Any]]:
    """Current + archived caselist listings, merged (backfill seasons are
    archived by now). Two requests total."""
    url = _url(api_base, endpoints, "caselists")
    merged: "Dict[str, Dict[str, Any]]" = {}
    for params in (None, {"archived": "true"}):
        rows, blob, final_url = _get_json(client, limiter, max_retries, url,
                                          params=params)
        _store_blob(conn, raw_root, blob, final_url)
        if not isinstance(rows, list):
            raise SyncError("GET %s did not return a list" % final_url)
        for r in rows:
            key = str(r.get("slug") or r.get("name") or r.get("caselist_id"))
            merged.setdefault(key, r)
    return list(merged.values())


def _sync_caselist(conn, client, limiter, max_retries, api_base, endpoints,
                   raw_root, row, stats: IngestStats) -> None:
    slug = str(row.get("slug") or row.get("name"))
    season = row.get("year")
    caselist_id = get_or_create_caselist(
        conn, slug,
        display_name=row.get("display_name") or row.get("name") or slug,
        season=season, event=row.get("event"), level=row.get("level"))

    url = _url(api_base, endpoints, "schools", {"caselist": slug})
    schools, blob, final_url = _get_json(client, limiter, max_retries, url)
    _store_blob(conn, raw_root, blob, final_url)
    for school_row in schools if isinstance(schools, list) else []:
        sname = str(school_row.get("name") or "").strip()
        if not sname:
            log.warning("caselist %s: school row with no name skipped", slug)
            continue
        school_id = get_or_create_school(
            conn, caselist_id, sname,
            display_name=(school_row.get("displayName")
                          or school_row.get("display_name")),
            state=school_row.get("state"),
            external_id=(str(school_row["school_id"])
                         if school_row.get("school_id") is not None else None))

        turl = _url(api_base, endpoints, "teams",
                    {"caselist": slug, "school": sname})
        teams, blob, final_url = _get_json(client, limiter, max_retries, turl)
        _store_blob(conn, raw_root, blob, final_url)
        for team_row in teams if isinstance(teams, list) else []:
            tname = str(team_row.get("name") or "").strip()
            if not tname:
                log.warning("school %s/%s: team row with no name skipped",
                            slug, sname)
                continue
            stats.units_seen += 1
            if _checkpoint_state(conn, slug, sname, tname) == "done":
                # resume: this unit is complete — zero HTTP requests
                stats.units_skipped += 1
                continue
            try:
                touched = _sync_team(conn, client, limiter, max_retries,
                                     api_base, endpoints, raw_root, slug,
                                     sname, school_id, team_row, stats)
                # unit bookkeeping + checkpoint commit together: the
                # checkpoint only ever exists for a fully-ingested unit
                fts_upsert_cards(conn, touched)
                _checkpoint_done(conn, slug, sname, tname)
                recompute_aggregates(conn, touched)  # commits when non-empty
                conn.commit()
                stats.touched_card_ids |= touched
            except Exception:
                conn.rollback()
                raise


def _sync_team(conn, client, limiter, max_retries, api_base, endpoints,
               raw_root, cl_slug, school_name, school_id, team_row,
               stats: IngestStats) -> Set[int]:
    tname = str(team_row.get("name")).strip()
    team_id = get_or_create_team(
        conn, school_id, tname,
        display_name=(team_row.get("display_name")
                      or team_row.get("displayName")),
        notes=team_row.get("notes"),
        external_id=(str(team_row["team_id"])
                     if team_row.get("team_id") is not None else None))
    # team/school names go into the URL exactly as the listings returned
    # them (the deployed getRounds matches T.name = param verbatim; see
    # docs/api_verify.md §5)
    args = {"caselist": cl_slug, "school": school_name, "team": tname}
    touched: Set[int] = set()

    url = _url(api_base, endpoints, "rounds", args)
    rounds, blob, final_url = _get_json(client, limiter, max_retries, url)
    _store_blob(conn, raw_root, blob, final_url)
    rounds = rounds if isinstance(rounds, list) else []

    fallback: List[Tuple[Any, int, str]] = []  # (api rid, rounds.id, ext id)
    n_docs = 0
    for r in rounds:
        rid = r.get("round_id", r.get("id"))
        if rid is not None:
            ext = "api-%s" % rid   # namespaced: HF round ids live elsewhere
        else:
            ext = "api-%s-%s-%s-%s-%s" % (cl_slug, school_name, tname,
                                          r.get("tournament"), r.get("round"))
        round_db_id = get_or_create_round(
            conn, team_id, external_id=ext, side=r.get("side"),
            tournament=r.get("tournament"),
            round_label=(str(r.get("round"))
                         if r.get("round") is not None else None),
            opponent=r.get("opponent"), judge=r.get("judge"),
            report=r.get("report"))
        opensource = str(r.get("opensource") or "").strip()
        got_doc = False
        if opensource:
            got_doc = _sync_opensource(conn, client, limiter, max_retries,
                                       api_base, endpoints, raw_root,
                                       opensource, round_db_id, ext, stats,
                                       touched)
            n_docs += int(got_doc)
        if not got_doc:
            fallback.append((rid, round_db_id, ext))

    if fallback:
        # pasted cites: fetched lazily, only when some round has no usable
        # open-source doc (spec §2.2 — cites are the lossy fallback record)
        curl = _url(api_base, endpoints, "cites", args)
        cites, blob, final_url = _get_json(client, limiter, max_retries, curl)
        # raw listing blob kept for provenance (spec §2.3) — but variants
        # never attach to it: its sha changes whenever the listing changes,
        # which would mint a fresh documents row and defeat
        # UNIQUE(document_id, ordinal) dedup across runs.
        blob_sha, blob_path = store_bytes(raw_root, blob)
        record_document(conn, blob_sha, "api", final_url, None,
                        str(blob_path))
        by_round: Dict[Any, List[Tuple[int, dict]]] = {}
        for idx, c in enumerate(cites if isinstance(cites, list) else []):
            by_round.setdefault(c.get("round_id"), []).append((idx, c))
        for rid, round_db_id, round_ext in fallback:
            entries = by_round.get(rid, [])
            if not entries:
                continue
            # stable synthetic document identity per round (precedent: the
            # HF loader's synthetic doc shas), so a re-fetched listing
            # attaches to the SAME documents row and old cites dedup
            doc_sha = sha256_bytes(
                ("api:cites:round:%s" % round_ext).encode("utf-8"))
            cites_doc_id = record_document(conn, doc_sha, "api", final_url,
                                           None, str(blob_path))
            for idx, c in entries:
                _ingest_cite(conn, c, idx, cites_doc_id, round_db_id,
                             stats, touched)

    log.info("unit %s/%s/%s: rounds=%d opensource_docs=%d cites_fallback=%d",
             cl_slug, school_name, tname, len(rounds), n_docs, len(fallback))
    return touched


def _path_cached_sha(conn, raw_root: Path, opensource: str
                     ) -> Optional[Tuple[str, Path]]:
    """(sha, local path) of an already-fetched opensource file, else None.

    The ledger maps each fetched file *path* to the sha of its bytes;
    together with the content-addressed raw store this makes a re-linked
    or re-processed file cost zero HTTP (spec §0.2: cache every response
    to disk forever)."""
    row = conn.execute(
        "SELECT sha256 FROM ingest_ledger "
        "WHERE source = 'api' AND external_id = ?",
        ("path:%s" % opensource,)).fetchone()
    if not row or not row["sha256"]:
        return None
    sha = row["sha256"]
    local = raw_root / sha[:2] / sha
    if not local.exists():
        return None  # blob vanished from disk: re-download
    return sha, local


def _convert_doc_bytes(data: bytes, filename: str) -> bytes:
    """Legacy .doc bytes -> .docx bytes via docx_parser.convert_doc_to_docx
    (spec §3.4). Raises ParseFailure when LibreOffice is unavailable or the
    conversion fails."""
    import shutil
    import tempfile

    from . import docx_parser
    tmpdir = Path(tempfile.mkdtemp(prefix="carddb-sync-doc-"))
    try:
        src = tmpdir / (posixpath.basename(filename) or "legacy.doc")
        src.write_bytes(data)
        converted = docx_parser.convert_doc_to_docx(src)
        return Path(converted).read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _sync_opensource(conn, client, limiter, max_retries, api_base, endpoints,
                     raw_root, opensource: str, round_db_id: int,
                     round_ext: str, stats: IngestStats,
                     touched: Set[int]) -> bool:
    """Fetch (or reuse) + parse one round's open-source file.

    Spec §4.1's "parsed once" is about never re-DOWNLOADING: the blob is
    stored once by its real sha, but provenance still attaches per round —
    the documents row is per (round, file sha) via a namespaced synthetic
    sha, so a byte-identical file disclosed by a second round still gets
    that round's variants (parsed from the local cached blob, zero HTTP).

    Returns True when the round has a stored doc (even if parsing failed —
    the cites fallback is only for rounds with *no* open-source doc, spec
    §2.2); False when the file could not be fetched (then the caller falls
    back to cites)."""
    fname = posixpath.basename(opensource)
    url = _url(api_base, endpoints, "download")
    data: Optional[bytes] = None
    cached = _path_cached_sha(conn, raw_root, opensource)
    if cached is not None:
        sha, local = cached
        origin_url = "%s?path=%s" % (url, quote(opensource, safe=""))
    else:
        resp = request_with_backoff(client, "GET", url, limiter=limiter,
                                    max_retries=max_retries,
                                    params={"path": opensource})
        if resp.status_code != 200:
            log.warning("download %s -> HTTP %d; treating round as no-doc",
                        opensource, resp.status_code)
            return False
        data = resp.content
        sha, local = store_bytes(raw_root, data)
        origin_url = str(resp.request.url)
        # provenance row for the fetched bytes themselves (spec §2.3)
        record_document(conn, sha, "api", origin_url, fname, str(local))
        ledger_stamp(conn, "api", "path:%s" % opensource, sha)

    # documents row per (round, file sha): namespaced synthetic identity
    # (precedent: the HF loader's synthetic doc shas), local_path pointing
    # at the shared content-addressed blob
    doc_sha = sha256_bytes(
        ("api:doc:%s:%s" % (round_ext, sha)).encode("utf-8"))
    doc_id = record_document(conn, doc_sha, "api", origin_url, fname,
                             str(local))
    ledger_key = "docx:%s:%s" % (round_ext, sha)
    if ledger_seen(conn, "api", ledger_key, sha):
        return True
    prev = conn.execute("SELECT parse_status FROM documents WHERE id = ?",
                        (doc_id,)).fetchone()
    if prev and prev["parse_status"] == "ok":
        ledger_stamp(conn, "api", ledger_key, sha)
        return True

    if data is None:
        data = local.read_bytes()   # re-parse from the raw store, no HTTP

    # local import so ratelimit/discovery tests never need python-docx
    from .docx_parser import ParseFailure, parse_docx_bytes
    try:
        if fname.lower().endswith(".doc"):
            # legacy Word binary: convert via soffice, then parse the
            # converted bytes (spec §3.4)
            data = _convert_doc_bytes(data, fname)
        if fname.lower().endswith(".pdf"):
            # PDF open-source file: text-only cards, honest fidelity
            # (PdfFailure subclasses ParseFailure, so failures record
            # parse_status='failed' below unchanged)
            from .pdf_parser import parse_pdf_bytes
            parsed = parse_pdf_bytes(data, filename=fname)
        else:
            parsed = parse_docx_bytes(data, filename=fname)
    except ParseFailure as e:
        conn.execute(
            "UPDATE documents SET parse_status='failed', parse_error=?, "
            " parsed_at=? WHERE id=?", (str(e), now_iso(), doc_id))
        stats.failed += 1
        log.warning("parse failed for %s (%s)", opensource, e)
        return True
    for rec in parsed.cards:
        rec.fidelity = "pdf" if fname.lower().endswith(".pdf") else "opensource"
        card_id, created = insert_card(conn, rec)
        _, vcreated = attach_variant(conn, card_id, rec, doc_id, round_db_id)
        stats.new_cards += int(created)
        stats.new_variants += int(vcreated)
        touched.add(card_id)
    conn.execute(
        "UPDATE documents SET parse_status='ok', parse_error=NULL, "
        " parsed_at=? WHERE id=?", (now_iso(), doc_id))
    stats.parsed += 1
    ledger_stamp(conn, "api", ledger_key, sha)
    return True


def _ingest_cite(conn, cite_row: Dict[str, Any], listing_idx: int,
                 cites_doc_id: int, round_db_id: int,
                 stats: IngestStats, touched: Set[int]) -> None:
    """One pasted-cites entry -> fidelity='cites_only' record (spec §2.2).

    The variant's home is the round's *synthetic* cites document and its
    ordinal is the cite entry's stable id, so a re-fetched listing (which
    has a new blob sha) attaches to the same (document_id, ordinal) and
    dedups on the UNIQUE constraint. The ledger sha is the entry's own
    canonical JSON: an unchanged entry skips, a changed one reprocesses."""
    cid = cite_row.get("cite_id")
    ext = "cite-%s" % cid if cid is not None else None
    entry_sha = sha256_bytes(
        json.dumps(cite_row, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8"))
    if ext and ledger_seen(conn, "api", ext, entry_sha):
        return
    title = str(cite_row.get("title") or "").strip() or None
    text = str(cite_row.get("cites") or "").strip() or None
    if not title and not text:
        return
    # stable ordinal: the cite id itself; id-less entries fall back to a
    # negative listing position so they can never collide with real ids
    ordinal = cid if cid is not None else -(listing_idx + 1)
    rec = CardRecord(tag=title, body_text=text,
                     is_analytic=text is None,  # title-only paste: key on tag
                     fidelity="cites_only", ordinal=ordinal, external_id=ext)
    card_id, created = insert_card(conn, rec)
    _, vcreated = attach_variant(conn, card_id, rec, cites_doc_id,
                                 round_db_id)
    stats.new_cards += int(created)
    stats.new_variants += int(vcreated)
    touched.add(card_id)
    if ext:
        ledger_stamp(conn, "api", ext, entry_sha)
