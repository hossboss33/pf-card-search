# openCaselist API verification report

Task: spec §2.2 and §12 items 1–3. Research pass run 2026-08-27.
Method: 11 total HTTP requests (2 to opencaselist.com/api.opencaselist.com,
9 to GitHub raw/API), User-Agent `pf-card-search-build`, no authentication,
no data endpoints touched — docs, robots.txt, and repo source only.

## 1. The OpenAPI spec is live and `/v1/docs` IS the raw JSON

`GET https://api.opencaselist.com/v1/docs` (HTTP 200) returns the raw OpenAPI
3.0.2 document itself (title "Caselist API v1", `servers: [{"url": "/v1"}]`),
not a Swagger HTML page. The repo README says exactly this:

> More extensive developer documentation is auto-generated according to the
> OpenAPI spec at: https://api.opencaselist.com
>
> That URL is running Swagger UI, pointing to
> https://api.opencaselist.com/v1/docs, which is the raw version of the spec.

(Source: https://raw.githubusercontent.com/ashtarcommunications/caselist/master/README.md)

The server's route registry at
`server/v1/routes/paths.js` (fetched from repo master) matches the live spec
path-for-path. Every endpoint in `config/endpoints.toml` appears verbatim in
both sources; nothing was invented.

## 2. Confirmed endpoints (transcribed into config/endpoints.toml)

| Purpose | Method + path | Notes |
|---|---|---|
| Login | `POST /login` | body `{username, password, remember}`; no auth; 20/min limit |
| List caselists | `GET /caselists` | optional `?archived=bool`; returns `{caselist_id, slug, name, event, year, archived}` |
| One caselist | `GET /caselists/{caselist}` | |
| Schools in caselist | `GET /caselists/{caselist}/schools` | `{name, displayName, state}` |
| Teams in school | `GET /caselists/{caselist}/schools/{school}/teams` | |
| Rounds for team | `GET /caselists/{caselist}/schools/{school}/teams/{team}/rounds` | optional `?side=`; rows include `opensource` file path (see below) |
| Single round | `GET .../rounds/{round}` | |
| Cites for team | `GET /caselists/{caselist}/schools/{school}/teams/{team}/cites` | optional `?side=`; `{cite_id, round_id, title, cites}` — the lossy pasted-cites fallback (spec §2.2) |
| Download a file | `GET /download?path=<opensource>` | cookie auth; 10/min; `weekly` paths 5/day |
| Bulk archives | `GET /caselists/{caselist}/downloads` | `[{name, url}]` — direct S3 links to weekly `.zip` archives |
| Recent changes | `GET /caselists/{caselist}/recent` | includes `opensource`; good for weekly sync deltas |
| Health check | `GET /status` | no auth |

All of the above: `verified = true` in endpoints.toml, each with the exact
source URL in a comment.

### The `opensource` file-path chain (spec §12.3: yes, the API serves files directly)

- The OpenAPI `Round` schema omits it, but `getRounds.js` does
  `SELECT R.* ... FROM rounds R`, and `postRound.js` does
  `INSERT INTO rounds (team_id, side, tournament, round, opponent, judge,
  report, opensource, video, tourn_id, external_id, ...)` — so round rows
  returned by the API carry an `opensource` column holding the uploaded
  file's relative path. The `Recent` schema confirms the field publicly:
  `opensource: string`.
- `getDownload.js` serves `${config.UPLOAD_DIR}/${req.query.path}` via
  `res.download(...)`, rejecting paths containing `..` or starting with `/`.
  So: **round.opensource → `GET /download?path=<that value>` → .docx bytes.**
- Bulk/archive downloads also exist (spec §12.3 second half):
  `getBulkDownloads.js` lists an S3 bucket under `weekly/<caselist>/` and
  returns direct S3 URLs to `.zip` archives. The README: weekly archive
  uploads are "kicked off by node-cron at midnight on Tuesdays." For season
  backfill, one weekly zip per caselist is far politer than crawling every
  round — use it.

## 3. Auth flow (spec §12.2), as documented in the repo

Cookie-based. OpenAPI security scheme (global, applied to every endpoint
except `/login` and `/status`):

```json
"cookie": { "type": "apiKey", "in": "cookie", "name": "caselist_token" }
```

Flow, from `server/v1/controllers/login/postLogin.js` (repo master,
fetched 2026-08-27):

1. Client POSTs the user's **Tabroom** credentials to `/v1/login`
   (`Login` schema: `username`, `password`, `remember`).
2. The server forwards them to Tabroom's login endpoint using its own
   server-to-server API key:

   ```js
   const url = `${config.TABROOM_API_URL}/login`;
   const base64 = Buffer.from(
       `${config.TABROOM_API_USER_ID}:${config.TABROOM_API_KEY}`,
   ).toString('base64');

   const response = await fetch(url, {
       method: 'POST',
       headers: {
           'Content-Type': 'application/json',
           Authorization: `Basic ${base64}`,
       },
       body: JSON.stringify({ username, password }),
   });
   user = await response.json();
   ```

   The README corroborates: "It ties in to the Tabroom auth endpoint for
   authentication" and "The `TABROOM_API_USER_ID` and `TABROOM_API_KEY`
   environment variables need valid credentials in the Tabroom DB".
3. On success the server mints a random nonce, stores its sha256 in a
   `sessions` row expiring in 2 weeks
   (`INSERT INTO sessions (token, user_id, ip, expires_at) VALUES (${hash},
   ${user.person_id}, ${req.ip}, DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 2 WEEK))`),
   and sets cookies:

   ```js
   res.cookie('caselist_token', nonce, {
       maxAge: remember && user.trusted ? 1000 * 60 * 60 * 24 * 14 : undefined,
       httpOnly: false, path: '/', sameSite: 'Lax', domain: config.COOKIE_DOMAIN,
   });
   ```

   plus `caselist_user_id` (and `caselist_trusted` / `caselist_admin` when
   applicable).
4. The 201 JSON body also returns the token directly:
   `{ message: 'Successfully logged in', token: nonce, expires, trusted,
   userId, admin }` — so a sync client can either replay the Set-Cookie or
   set `Cookie: caselist_token=<token>` manually. Sessions last 2 weeks;
   re-login when a 401 comes back rather than proactively.

This matches spec §0.3 exactly: use the owner's own Tabroom credentials from
an env var, once per sync run, never scraped/shared credentials.

## 4. Automated-access posture (spec §12.1), honestly

- **robots.txt** (`https://opencaselist.com/robots.txt`, HTTP 200): fully
  permissive — `User-agent: *` with an empty `Disallow:`, i.e. crawling is
  not forbidden for any path. It contains **no** terms-of-use reference,
  no crawl-delay, no sitemap.
- **No formal terms-of-use document was found** in robots.txt or the repo
  README. I did not locate a ToS page in this pass; do not read that as
  "no terms exist" — the client app may render one behind login.
- The site's real posture is expressed in code, deliberately. README:
  "It implements a number of rate limiters to avoid abuse." Verified
  concrete limits in the controllers fetched:
  - login: 20 attempts/minute per user/IP (429 with a plain-English message);
  - file downloads: 10/minute per user/IP;
  - weekly bulk archives: 5/day per user/IP.
- Honest summary: automated access is neither invited nor forbidden. The
  API is public, documented, and open source; everything data-bearing sits
  behind Tabroom-authenticated cookies and per-user rate limits, which means
  automation is expected to be *logged-in, attributable, and slow*. The
  spec's own rules (§0.2: ≤1 rps, backoff, contact email in the UA,
  overnight bulk jobs, ask the maintainer before any full-season pull) are
  stricter than anything the site publishes, and the weekly S3 zip archives
  are the sanctioned-looking bulk path. Per spec §12.1, if any doubt remains
  before M4, email the maintainer — the README author (ashtarcommunications /
  Aaron Hardy) is reachable via the repo.

## 5. What was NOT verified (marked `verified = false` in endpoints.toml)

- `GET /search` — required `shard` query param's valid values are
  undocumented; confirming them needs an authenticated request (out of scope
  for this pass).
- `GET /openev` — endpoint shape confirmed from spec + `paths.js`, but the
  File.path → `/download` hand-off was not exercised.
- The exact `event` values in `/caselists` rows (PF filtering is
  client-side; the exact string for PF — `"pf"`? — must be read from the
  live response during M4, per spec "Enumerate PF caselists from the API's
  own caselist listing").
- Response `Content-Type`s are declared `*/*` throughout the spec; assume
  JSON for API endpoints and binary for `/download`, but confirm at M4.
- Note for the sync implementer: deployed `getRounds.js` contains
  `AND LOWER(T.name = ${req.params.team})` — an operator-precedence slip
  that effectively makes the team match `T.name = <param>` (MySQL default
  collation, case-insensitive). Practical consequence: pass the team `name`
  exactly as returned by the teams listing; do not lowercase or otherwise
  transform it client-side.

## 6. Fetch log (all 11 requests)

| # | URL | Result |
|---|---|---|
| 1 | https://api.opencaselist.com/v1/docs | 200, OpenAPI 3.0.2 JSON (24,262 B) |
| 2 | https://opencaselist.com/robots.txt | 200, permissive (67 B) |
| 3 | https://raw.githubusercontent.com/ashtarcommunications/caselist/master/README.md | 200 |
| 4 | https://api.github.com/repos/ashtarcommunications/caselist/git/trees/master?recursive=1 | 404 (used contents API instead) |
| 5 | https://api.github.com/repos/ashtarcommunications/caselist/contents/server/v1/routes | 200 |
| 6 | .../server/v1/routes/paths.js (raw) | 200 |
| 7 | .../server/v1/controllers/login/postLogin.js (raw) | 200 |
| 8 | .../server/v1/controllers/rounds/getRounds.js (raw) | 200 |
| 9 | .../server/v1/controllers/download/getDownload.js (raw) | 200 |
| 10 | .../server/v1/controllers/caselists/getBulkDownloads.js (raw) | 200 |
| 11 | .../server/v1/controllers/rounds/postRound.js (raw) | 200 |
