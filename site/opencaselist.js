/* PF card search — browser client for the openCaselist API.
 *
 * Dependency-free. Talks straight to https://api.opencaselist.com/v1 from the
 * visitor's own browser with `credentials: 'include'`, which works because
 * that API reflects the request Origin and sets
 * access-control-allow-credentials: true. There is no proxy, no intermediary
 * and no server of ours anywhere in this path — this site is static.
 *
 * POLITENESS (build spec 0.2). openCaselist is a community-run nonprofit with
 * deliberate rate limiters. Every request in this file goes through one
 * queue with a hard concurrency of 1 and at least MIN_SPACING_MS between the
 * end of one request and the start of the next, plus exponential backoff that
 * honours Retry-After on 429 and 5xx. Do not add a code path that bypasses
 * `enqueue`.
 *
 * CREDENTIALS. `login()` reads a username and password, hands them to one
 * fetch, and lets them go. They are never written to localStorage,
 * sessionStorage, IndexedDB, a cookie, a URL, or a variable that outlives the
 * call, and they are never logged. The session token the API returns in the
 * response body is discarded unread; the only thing that persists is the
 * httpOnly `caselist_token` cookie that openCaselist itself sets, which is
 * managed entirely by the browser. There is no telemetry, analytics or error
 * reporting in this file, and nothing here ever calls console.*.
 *
 * Exposes window.OpenCaselist.
 */
(function () {
  "use strict";

  var API_BASE = "https://api.opencaselist.com/v1";

  /* Politeness knobs. Raising the rate is a decision about someone else's
     server; leave these alone. */
  var MIN_SPACING_MS = 1000;   /* >= 1s between requests, always */
  var MAX_RETRIES = 4;         /* per request, on 429 / 5xx / network only */
  var BASE_BACKOFF_MS = 2000;
  var MAX_BACKOFF_MS = 60000;
  var DOWNLOAD_TIMEOUT_MS = 60000;
  var JSON_TIMEOUT_MS = 30000;

  /* PF event values, mirroring carddb/api_sync.py _PF_EVENTS. */
  var PF_EVENTS = { "pf": 1, "pfd": 1, "hspf": 1, "publicforum": 1,
                    "public forum": 1, "public-forum": 1 };
  var PF_SLUG_RE = /pf(?=[^a-z]|$)/;

  /* ------------------------------------------------------------------
   * Typed errors
   * ------------------------------------------------------------------ */

  /* kind: "auth" | "forbidden" | "not_found" | "rate_limited" | "server"
   *     | "network" | "bad_response" | "client" | "usage" */
  function ApiError(kind, message, extra) {
    var e = new Error(message);
    e.name = "OpenCaselistError";
    e.kind = kind;
    e.status = (extra && extra.status) || null;
    e.path = (extra && extra.path) || null;
    e.retryAfterMs = (extra && extra.retryAfterMs) || null;
    /* Convenience flag the UI can branch on to say "sign in again". */
    e.needsLogin = (kind === "auth");
    return e;
  }

  function isRetryableStatus(s) { return s === 429 || (s >= 500 && s <= 599); }

  function errorForStatus(status, path, retryAfterMs, detail) {
    var kind =
      status === 401 ? "auth" :
      status === 403 ? "forbidden" :
      status === 404 ? "not_found" :
      status === 429 ? "rate_limited" :
      status >= 500 ? "server" : "client";
    var msg =
      status === 401 ? "openCaselist says you are not signed in (401). Connect again." :
      status === 403 ? "openCaselist refused this request (403)." :
      status === 404 ? "openCaselist has nothing at " + path + " (404)." :
      status === 429 ? "openCaselist is rate-limiting this client (429). Slow down." :
      status >= 500 ? "openCaselist returned a server error (" + status + ")." :
      "openCaselist rejected the request (" + status + ").";
    if (detail) msg += " " + detail;
    return ApiError(kind, msg, { status: status, path: path, retryAfterMs: retryAfterMs });
  }

  /* ------------------------------------------------------------------
   * Progress reporting
   * ------------------------------------------------------------------ */

  var progressHandlers = [];

  /* Register a callback; returns an unsubscribe function.
     Events: { phase, method, path, attempt, queueLength, waitMs, status,
               message }
     phase is one of "queued" | "waiting" | "request" | "retry" | "done"
                   | "error". No event ever carries a credential or a token. */
  function onProgress(fn) {
    if (typeof fn !== "function") throw ApiError("usage", "onProgress needs a function");
    progressHandlers.push(fn);
    return function () {
      var i = progressHandlers.indexOf(fn);
      if (i >= 0) progressHandlers.splice(i, 1);
    };
  }

  function emit(evt) {
    for (var i = 0; i < progressHandlers.length; i++) {
      try { progressHandlers[i](evt); } catch (e) { /* a bad listener must not break a sync */ }
    }
  }

  /* ------------------------------------------------------------------
   * The queue: concurrency 1, >= MIN_SPACING_MS apart, backoff on 429/5xx
   * ------------------------------------------------------------------ */

  var queue = [];
  var running = false;
  var lastFinishedAt = 0;

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  function enqueue(job) {
    return new Promise(function (resolve, reject) {
      queue.push({ job: job, resolve: resolve, reject: reject });
      emit({ phase: "queued", path: job.path, method: job.method,
             queueLength: queue.length });
      pump();
    });
  }

  async function pump() {
    if (running) return;
    running = true;
    try {
      while (queue.length) {
        var item = queue.shift();
        try {
          var value = await runJob(item.job);
          item.resolve(value);
        } catch (err) {
          item.reject(err);
        }
      }
    } finally {
      running = false;
    }
  }

  async function waitForSlot(job) {
    var wait = (lastFinishedAt + MIN_SPACING_MS) - Date.now();
    if (wait > 0) {
      emit({ phase: "waiting", path: job.path, method: job.method, waitMs: wait,
             message: "pacing to 1 request/second" });
      await sleep(wait);
    }
  }

  /* Retry-After is either delta-seconds or an HTTP date. */
  function retryAfterMs(res) {
    var h = res && res.headers ? res.headers.get("Retry-After") : null;
    if (!h) return null;
    var secs = Number(h);
    if (!isNaN(secs) && secs >= 0) return Math.min(secs * 1000, MAX_BACKOFF_MS);
    var when = Date.parse(h);
    if (!isNaN(when)) return Math.max(0, Math.min(when - Date.now(), MAX_BACKOFF_MS));
    return null;
  }

  function backoffMs(attempt, fromHeader) {
    var expo = Math.min(BASE_BACKOFF_MS * Math.pow(2, attempt), MAX_BACKOFF_MS);
    var jitter = Math.floor(Math.random() * 400);
    return Math.max(fromHeader || 0, expo) + jitter;
  }

  async function runJob(job) {
    var attempt = 0;
    var maxRetries = (job.retries === undefined) ? MAX_RETRIES : job.retries;
    for (;;) {
      await waitForSlot(job);
      emit({ phase: "request", method: job.method, path: job.path,
             attempt: attempt, queueLength: queue.length,
             message: job.label || null });

      var res = null, netErr = null;
      var controller = ("AbortController" in window) ? new AbortController() : null;
      var timer = controller
        ? setTimeout(function () { controller.abort(); }, job.timeoutMs || JSON_TIMEOUT_MS)
        : null;
      try {
        res = await fetch(job.url, {
          method: job.method,
          credentials: "include",
          mode: "cors",
          cache: "no-store",
          redirect: "follow",
          headers: job.headers || undefined,
          body: job.body === undefined ? undefined : job.body,
          signal: controller ? controller.signal : undefined
        });
      } catch (e) {
        netErr = e;
      } finally {
        if (timer) clearTimeout(timer);
        lastFinishedAt = Date.now();
      }

      if (netErr) {
        if (attempt < maxRetries) {
          var nwait = backoffMs(attempt, null);
          emit({ phase: "retry", method: job.method, path: job.path,
                 attempt: attempt, waitMs: nwait,
                 message: "network error; backing off" });
          attempt++;
          await sleep(nwait);
          continue;
        }
        throw ApiError("network",
          "could not reach openCaselist (" + job.path + "). Check the connection, " +
          "or the browser may be blocking third-party cookies for api.opencaselist.com.",
          { path: job.path });
      }

      if (res.ok) {
        emit({ phase: "done", method: job.method, path: job.path,
               status: res.status, queueLength: queue.length });
        return res;
      }

      if (isRetryableStatus(res.status) && attempt < maxRetries) {
        var hdr = retryAfterMs(res);
        var wait = backoffMs(attempt, hdr);
        emit({ phase: "retry", method: job.method, path: job.path,
               status: res.status, attempt: attempt, waitMs: wait,
               message: res.status === 429
                 ? "rate-limited by openCaselist; waiting"
                 : "server error; waiting" });
        attempt++;
        await sleep(wait);
        continue;
      }

      var detail = await readErrorDetail(res);
      throw errorForStatus(res.status, job.path, retryAfterMs(res), detail);
    }
  }

  async function readErrorDetail(res) {
    try {
      var txt = await res.text();
      if (!txt) return "";
      try {
        var j = JSON.parse(txt);
        var m = j && (j.message || j.error);
        return m ? String(m).slice(0, 200) : "";
      } catch (e) {
        return txt.slice(0, 200);
      }
    } catch (e) {
      return "";
    }
  }

  /* ------------------------------------------------------------------
   * Request helpers — every public method goes through these
   * ------------------------------------------------------------------ */

  function seg(s) { return encodeURIComponent(String(s)); }

  function buildUrl(path, query) {
    var url = API_BASE + path;
    if (query) {
      var parts = [];
      for (var k in query) {
        if (query[k] === undefined || query[k] === null || query[k] === "") continue;
        parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(query[k])));
      }
      if (parts.length) url += "?" + parts.join("&");
    }
    return url;
  }

  async function getJson(path, query, label) {
    var res = await enqueue({
      method: "GET", path: path, url: buildUrl(path, query),
      headers: { "Accept": "application/json" },
      label: label, timeoutMs: JSON_TIMEOUT_MS
    });
    var text = await res.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (e) {
      throw ApiError("bad_response",
        "openCaselist returned something that is not JSON for " + path, { path: path });
    }
  }

  /* ------------------------------------------------------------------
   * Session
   * ------------------------------------------------------------------ */

  /* In-memory only, and none of it is secret. Cleared by logout(). */
  var session = { connected: false, checkedAt: null, expires: null,
                  trusted: null, admin: null };

  function sessionInfo() {
    return { connected: session.connected, checkedAt: session.checkedAt,
             expires: session.expires, trusted: session.trusted,
             admin: session.admin };
  }

  /* POST the visitor's Tabroom credentials directly to openCaselist.
   *
   * The two arguments are used once, inside this function, and are not
   * retained anywhere afterwards. Never retried: a retry would mean holding
   * the credentials longer, and /login is rate-limited to 20/minute.
   */
  async function login(username, password) {
    if (!username || !password) {
      throw ApiError("usage", "a Tabroom username and password are both required");
    }
    /* The only copy that leaves this scope is the request body string, which
       is dropped as soon as fetch has consumed it. */
    var body = JSON.stringify({
      username: String(username),
      password: String(password),
      remember: false
    });
    username = null;
    password = null;

    var res;
    try {
      res = await enqueue({
        method: "POST", path: "/login", url: buildUrl("/login", null),
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: body,
        retries: 0,
        label: "signing in to openCaselist",
        timeoutMs: JSON_TIMEOUT_MS
      });
    } catch (err) {
      body = null;
      session.connected = false;
      if (err.status === 401 || err.status === 400) {
        throw ApiError("auth",
          "openCaselist did not accept that Tabroom username and password.",
          { status: err.status, path: "/login" });
      }
      throw err;
    } finally {
      body = null;
    }

    /* The response body also carries the session token. We deliberately do
       not read, return, store or log it — the httpOnly cookie the server set
       alongside it is the only thing this client uses. */
    var info = null;
    try {
      var parsed = JSON.parse(await res.text());
      if (parsed && typeof parsed === "object") {
        info = { expires: parsed.expires || null,
                 trusted: !!parsed.trusted,
                 admin: !!parsed.admin };
      }
      parsed = null;
    } catch (e) { /* a 2xx with an unparseable body still means the cookie is set */ }

    session.connected = true;
    session.checkedAt = Date.now();
    session.expires = info ? info.expires : null;
    session.trusted = info ? info.trusted : null;
    session.admin = info ? info.admin : null;

    return { ok: true, expires: session.expires, trusted: session.trusted,
             admin: session.admin };
  }

  /* Probe a cheap authenticated endpoint to see whether the cookie is live. */
  async function isConnected() {
    try {
      await getJson("/caselists", null, "checking the openCaselist session");
      session.connected = true;
      session.checkedAt = Date.now();
      return true;
    } catch (err) {
      if (err.kind === "auth") {
        session.connected = false;
        session.checkedAt = Date.now();
        return false;
      }
      throw err;
    }
  }

  /* The API has no logout route (server/v1/routes/paths.js has /login only),
     and the caselist_token cookie is httpOnly on api.opencaselist.com, so no
     script on this origin can delete it. All we can honestly do is drop our
     own in-memory state; the cookie itself expires with the browser session
     unless the visitor clears site data for api.opencaselist.com. Say so in
     the UI rather than implying a real sign-out happened. */
  function logout() {
    session = { connected: false, checkedAt: null, expires: null,
                trusted: null, admin: null };
    return {
      ok: true,
      serverLogout: false,
      note: "openCaselist has no logout endpoint and its session cookie is " +
            "httpOnly, so this only clears local state. To end the session " +
            "everywhere, clear cookies for api.opencaselist.com or close the " +
            "browser."
    };
  }

  /* ------------------------------------------------------------------
   * Data routes (config/endpoints.toml)
   * ------------------------------------------------------------------ */

  function asArray(v) {
    if (Array.isArray(v)) return v;
    if (v && Array.isArray(v.rows)) return v.rows;
    if (v && Array.isArray(v.data)) return v.data;
    return v == null ? [] : [v];
  }

  /* The live /caselists response is the raw DB row: the slug lives in `name`
     and the label in `display_name`. There is NO `slug` key (docs/api_access.md
     section 5a). Handle both shapes so a future server fix does not break us. */
  function normalizeCaselist(row) {
    if (!row || typeof row !== "object") return null;
    var slug = row.slug || row.name || null;
    if (!slug) return null;
    var label = row.display_name || row.displayName || null;
    /* When `slug` was absent, `name` was the slug, so it is not a label. */
    if (!label) label = row.slug ? (row.name || slug) : slug;
    return {
      slug: String(slug),
      display_name: String(label),
      event: row.event == null ? null : String(row.event),
      level: row.level == null ? null : String(row.level),
      year: row.year == null ? null : Number(row.year),
      team_size: row.team_size == null ? null : Number(row.team_size),
      archived: row.archived === true || row.archived === 1,
      caselist_id: row.caselist_id == null ? null : row.caselist_id,
      raw: row
    };
  }

  /* Mirrors carddb/api_sync.py _is_pf. */
  function isPF(c) {
    var event = (c.event || "").trim().toLowerCase();
    if (event) return PF_EVENTS[event] === 1;
    return PF_SLUG_RE.test(((c.slug || "") + " " + (c.display_name || "")).toLowerCase());
  }

  async function caselists(archived) {
    var raw = await getJson("/caselists", archived ? { archived: "true" } : null,
                            archived ? "listing archived caselists" : "listing caselists");
    return asArray(raw).map(normalizeCaselist).filter(Boolean);
  }

  /* Both the live and the archived listings, merged and filtered to PF.
     Two requests, so ~2 seconds. */
  async function listPFCaselists() {
    var live = await caselists(false);
    var old = await caselists(true);
    var bySlug = {};
    var order = [];
    function take(list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (!isPF(c)) continue;
        if (!(c.slug in bySlug)) { bySlug[c.slug] = c; order.push(c.slug); }
        else if (c.archived) { bySlug[c.slug] = c; }
      }
    }
    take(live);
    take(old);
    var out = order.map(function (s) { return bySlug[s]; });
    /* Newest season first; slugs like hspf25 sort correctly by year. */
    out.sort(function (a, b) {
      var ay = a.year || 0, by = b.year || 0;
      if (ay !== by) return by - ay;
      return a.slug < b.slug ? -1 : (a.slug > b.slug ? 1 : 0);
    });
    return out;
  }

  async function caselist(slug) {
    var raw = await getJson("/caselists/" + seg(slug), null, "reading caselist " + slug);
    return normalizeCaselist(raw);
  }

  async function schools(caselistSlug) {
    var raw = await getJson("/caselists/" + seg(caselistSlug) + "/schools", null,
                            "listing schools in " + caselistSlug);
    return asArray(raw);
  }

  async function teams(caselistSlug, school) {
    var raw = await getJson(
      "/caselists/" + seg(caselistSlug) + "/schools/" + seg(school) + "/teams",
      null, "listing teams for " + school);
    return asArray(raw);
  }

  async function rounds(caselistSlug, school, team, side) {
    var raw = await getJson(
      "/caselists/" + seg(caselistSlug) + "/schools/" + seg(school) +
      "/teams/" + seg(team) + "/rounds",
      side ? { side: side } : null, "listing rounds for " + team);
    return asArray(raw);
  }

  async function cites(caselistSlug, school, team, side) {
    var raw = await getJson(
      "/caselists/" + seg(caselistSlug) + "/schools/" + seg(school) +
      "/teams/" + seg(team) + "/cites",
      side ? { side: side } : null, "listing cites for " + team);
    return asArray(raw);
  }

  async function recent(caselistSlug) {
    var raw = await getJson("/caselists/" + seg(caselistSlug) + "/recent", null,
                            "listing recent changes in " + caselistSlug);
    return asArray(raw);
  }

  /* The bulk weekly archives. The `url` values are direct Backblaze links on
     a different host, fetched with no credentials, and they do NOT consume
     openCaselist's /download quota (docs/api_access.md section 3). Prefer
     them over per-round downloads for anything season-sized. */
  async function bulkDownloads(caselistSlug) {
    var raw = await getJson("/caselists/" + seg(caselistSlug) + "/downloads", null,
                            "listing bulk archives for " + caselistSlug);
    return asArray(raw);
  }

  /* Fetch one open-source file. `path` is a round's `opensource` value, passed
     verbatim. Rate limits on the server: 10/minute, and 5/day for any path
     containing "weekly". Returns an ArrayBuffer. */
  async function downloadOpenSource(path) {
    if (!path || typeof path !== "string") {
      throw ApiError("usage", "downloadOpenSource needs a round's opensource path");
    }
    if (path.indexOf("..") !== -1 || path.charAt(0) === "/") {
      throw ApiError("usage",
        "refusing to request a path with '..' or a leading '/' — openCaselist " +
        "rejects those with a 400 and there is no reason to send one");
    }
    var res = await enqueue({
      method: "GET", path: "/download",
      url: buildUrl("/download", { path: path }),
      headers: { "Accept": "*/*" },
      label: "downloading " + path.split("/").pop(),
      timeoutMs: DOWNLOAD_TIMEOUT_MS
    });
    return res.arrayBuffer();
  }

  /* The only other unauthenticated route. Useful as a preflight. */
  async function status() {
    var res = await enqueue({
      method: "GET", path: "/status", url: buildUrl("/status", null),
      headers: { "Accept": "application/json" }, label: "checking openCaselist status",
      timeoutMs: JSON_TIMEOUT_MS
    });
    return { ok: res.ok, status: res.status };
  }

  function queueLength() { return queue.length + (running ? 1 : 0); }

  window.OpenCaselist = {
    VERSION: "1.0.0",
    API_BASE: API_BASE,
    MIN_SPACING_MS: MIN_SPACING_MS,

    onProgress: onProgress,

    login: login,
    logout: logout,
    isConnected: isConnected,
    sessionInfo: sessionInfo,

    listPFCaselists: listPFCaselists,
    caselists: caselists,
    caselist: caselist,
    schools: schools,
    teams: teams,
    rounds: rounds,
    cites: cites,
    recent: recent,
    bulkDownloads: bulkDownloads,
    downloadOpenSource: downloadOpenSource,
    status: status,

    queueLength: queueLength
  };
})();
