# openCaselist access requirements — auth per route, and PF caselist slugs

Follow-on to `docs/api_verify.md`. That pass transcribed the endpoints and the
cookie scheme from the live OpenAPI document. **This pass answers the
enforcement question from the server's source code instead**, so the claim does
not rest on a spec file that could be aspirational.

Research pass run 2026-08-28.
**Live requests to api.opencaselist.com / opencaselist.com: ZERO.** The source
was unambiguous, so the politeness budget (3 requests) went unspent. All
evidence below comes from `raw.githubusercontent.com` and the GitHub API
against `ashtarcommunications/caselist` @ `master`, plus
`speechanddebate/nsda-js-utils` for one date helper.

---

## 1. The headline answer

**Yes. Reading PF rounds, cites, schools, teams, caselists, recent changes,
bulk-download listings, search, and files all require a valid
`caselist_token` cookie, which can only be obtained by POSTing a real
Tabroom username and password to `/v1/login`.** There is no public read path,
no anonymous mode, and no API-key alternative.

The only two unauthenticated endpoints in the entire API are `/status` and
`/login` itself.

This is not inferred from documentation. It is enforced by middleware, declared
per-operation in every controller, and asserted by the project's own test suite.

### The three-link proof chain

**Link 1 — the security handler is wired into the express app.**
`server/index.js` lines 232-234:

```js
	securityHandlers: {
		cookie: auth,
	},
```

That is passed to `initialize()` from `express-openapi` (line 217), together
with `apiDoc` and `paths`. `express-openapi` invokes the named handler for any
operation whose `security` list names that scheme, and returns the thrown
status if it rejects.

**Link 2 — the handler is a hard cookie check against the sessions table.**
`server/v1/helpers/auth.js` lines 7-21 and 55-59:

```js
const auth = async (req) => {
	if (!req.cookies.caselist_token) {
		const err = new Error('Not Authorized');
		err.status = 401;
		throw err;
	}

	const hash = crypto
		.createHash('sha256')
		.update(req.cookies.caselist_token)
		.digest('hex');
	let sql = SQL`
        SELECT * FROM sessions WHERE token = ${hash} AND expires_at > NOW()
    `;
	const session = await query(sql);
	...
	// Default to unauthorized
	const err = new Error('Not Authorized');
	err.status = 401;
```

No cookie → 401. Cookie present but no live `sessions` row → 401. There is no
branch that lets a request through unauthenticated.

Note also lines 32-50: an *additional* `trusted` check gates non-GET requests
("You must be a real student, judge, or coach on Tabroom to make
modifications"). That extra gate does not apply to us — we only read — but it
confirms the account model is Tabroom-backed identity, not a service key.

**Link 3 — the scheme is applied globally and re-declared per operation.**
`server/v1/routes/api-doc.js` lines 14-19:

```js
		securitySchemes: {
			cookie: { type: 'apiKey', in: 'cookie', name: 'caselist_token' },
		},
	},
	paths: {},
	security: [{ cookie: [] }],
```

A global `security` in OpenAPI can be overridden per-operation by an empty
array. So the global default is not sufficient proof on its own — I checked
every controller for an override. Results in §2.

---

## 2. Per-route auth table

Each row cites the controller file's own `apiDoc.security` declaration and,
where one exists, the co-located test that asserts the 401. Paths are relative
to `https://api.opencaselist.com/v1`.

| Route | Controller | `security` declaration | 401 test | Auth? |
|---|---|---|---|---|
| `GET /status` | `status/status.js:29` | `security: []` | none (public) | **PUBLIC** |
| `POST /login` | `login/postLogin.js:152` | `security: []` | none (public) | **PUBLIC** |
| `GET /caselists` | `caselists/getCaselists.js:48` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /caselists/{caselist}` | `caselists/getCaselist.js:38` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /caselists/{caselist}/recent` | `caselists/getRecent.js:54` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /caselists/{caselist}/downloads` | `caselists/getBulkDownloads.js:67` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /caselists/{caselist}/schools` | `schools/getSchools.js:43` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /.../schools/{school}` | `schools/getSchool.js:56` | `security: [{ cookie: [] }]` | — | **REQUIRED** |
| `GET /.../schools/{school}/teams` | `teams/getTeams.js:89` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /.../teams/{team}` | `teams/getTeam.js:91` | `security: [{ cookie: [] }]` | — | **REQUIRED** |
| `GET /.../teams/{team}/rounds` | `rounds/getRounds.js:74` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /.../rounds/{round}` | `rounds/getRound.js:65` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /.../teams/{team}/cites` | `cites/getCites.js:83` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /download?path=` | `download/getDownload.js:76` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /search` | `search/getSearch.js:147` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |
| `GET /openev` | `openev/getFiles.js:42` | `security: [{ cookie: [] }]` | yes | **REQUIRED** |

Source root for all of the above:
`https://raw.githubusercontent.com/ashtarcommunications/caselist/master/server/v1/controllers/<path>`

**Exhaustiveness check.** A grep for `security` across every GET controller
fetched returns exactly the 20 lines above — 15 occurrences of
`security: [{ cookie: [] }]`, 5 of `security: []` (the five HTTP verbs on
`/status`), plus `postLogin.js`'s `security: []`. No controller omits the key,
and no data-bearing controller declares an empty list.

### The strongest single piece of evidence: the project's own tests

Every data-bearing GET controller ships a co-located `*.test.js` containing an
identically-named case. From
`server/v1/controllers/rounds/getRounds.test.js` lines 53-61:

```js
	it('should return a 401 with no authorization cookie', async () => {
		await request(server)
			.get(
				`/v1/caselists/testcaselist/schools/testschool/teams/testteam/rounds`,
			)
			.set('Accept', 'application/json')
			.expect('Content-Type', /json/)
			.expect(401);
	});
```

The same `should return a 401 with no authorization cookie` test was confirmed
present in: `getCaselists.test.js`, `getCaselist.test.js`, `getRecent.test.js`,
`getBulkDownloads.test.js`, `getSchools.test.js`, `getTeams.test.js`,
`getRound.test.js`, `getCites.test.js`, `getDownload.test.js`,
`getSearch.test.js`, `getFiles.test.js`. `status.test.js` contains no reference
to 401 at all.

The happy-path half of the same test sets the cookie explicitly
(`.set('Cookie', ['caselist_token=user'])`, line 12), which is exactly the
header our sync client must send.

### Front-end corroboration

`client/src/home/Home.jsx` line 21 renders the login form and nothing else
until a session exists:

```jsx
			{!auth.user?.loggedIn ? (
				<Login />
			) : (
```

The caselist links are inside the `else` branch. The site itself has no
logged-out browsing mode.

---

## 3. The bulk weekly S3 zip path — and whether *it* needs auth

This is the politer backfill route and it deserves a precise answer, because
the answer is split.

**The listing endpoint requires auth. The zip files themselves do not.**

`GET /caselists/{caselist}/downloads` is authenticated (table above, and it has
the 401 test). But look at what it returns —
`caselists/getBulkDownloads.js` lines 27-32:

```js
			filelist.forEach((f) => {
				files.push({
					name: f,
					url: `https://${config.S3_BUCKET}.${config.S3_ENDPOINT}/weekly/${req.params.caselist}/${f}`,
				});
			});
```

That URL is built by plain string interpolation. **It is not a presigned S3
URL** — no signature, no expiry, no credential query parameters. `server/config.js`
lines 33-35 give the defaults:

```js
	S3_BUCKET: 'caselist-files',
	S3_ENDPOINT: 's3.us-east-005.backblazeb2.com',
	S3_REGION: 'us-east-005',
```

So the archives live on Backblaze B2 at
`https://caselist-files.s3.us-east-005.backblazeb2.com/weekly/<caselist>/<caselist>-all-<date>.zip`
(and `-weekly-<date>.zip`), which is a **different host from
api.opencaselist.com**.

The client confirms these are fetched with no credentials at all —
`client/src/caselist/Downloads.jsx` lines 74 and 79:

```jsx
					{fulldownload.map((d) => (
						<p key={d.url}>{d.url && <a href={d.url}>{d.name}</a>}</p>
					))}
```

A bare `<a href>`. The browser follows it cross-origin carrying no
`caselist_token` (wrong domain) and no S3 credentials. For openCaselist's own
bulk-download page to function, the B2 bucket objects must be publicly
readable. The bucket policy is not in the repo, so *strictly* this last step is
an inference — but it is forced by the client's own code.

Three consequences for us, all favourable:

1. **You still need to log in once** to call `/caselists/{caselist}/downloads`
   and learn the current archive filenames. That single authenticated request
   is the only API call a bulk backfill needs.
2. **Fetching the zip does not touch the openCaselist API.** It goes to
   Backblaze. It therefore consumes **none** of the owner's `/download` quota —
   neither the 10/minute limiter nor the 5/day `weekly` limiter in
   `download/getDownload.js` lines 6-34. Those limiters key on
   `req.query.path.includes('weekly')` for requests to the *API's* `/download`
   route serving files off the server's own `UPLOAD_DIR`. A direct B2 URL never
   reaches that code.
3. This makes the zip the unambiguously politest backfill path: **one
   authenticated API call plus one large B2 transfer per season**, versus tens
   of thousands of per-round `/download` hits that would blow through 10/min
   for weeks.

Archives are regenerated by cron — `server/index.js` lines 66-73,
`cron.schedule('0 0 * * 2', ...)`, i.e. midnight Tuesday.

---

## 4. PF caselist slugs by season

### The convention, confirmed from source

The slug is the `caselists.name` column. `caselists/getCaselist.js` lines 6-9
resolves the `{caselist}` path parameter against it:

```js
		const [caselist] = await query(SQL`
            SELECT * FROM caselists C
            wHERE C.name = ${req.params.caselist}
        `);
```

`server/v1/migration/oldCaselists.sql` lines 34-38 gives the historical PF rows
verbatim:

```sql
    (1030, 'hspf17', 'HS PF 2017-18', 2017, 'pf', 'hs', 2, 1, NULL),
    (1031, 'hspf18', 'HS PF 2018-19', 2018, 'pf', 'hs', 2, 1, NULL),
    (1032, 'hspf19', 'HS PF 2019-20', 2019, 'pf', 'hs', 2, 1, NULL),
    (1033, 'hspf20', 'HS PF 2020-21', 2020, 'pf', 'hs', 2, 1, NULL),
    (1034, 'hspf21', 'HS PF 2021-22', 2021, 'pf', 'hs', 2, 1, NULL),
```

and `server/v1/db/testData.sql` line 5 continues it:

```sql
    (1004, 'hspf22', 'HS PF 2022-23', 2022, 'pf', 'hs', 2, 0),
```

Convention: **`hspf` + the two-digit *start* year of the academic season.**
`display_name` is `HS PF <start>-<end2>`, `event` is `'pf'`, `level` is `'hs'`,
`team_size` is `2`.

### Why 2023-24 onward is near-certain, not a guess

The front end does not hardcode slugs — it *generates* them.
`client/src/home/Home.jsx` line 16 and line 41:

```jsx
	const shortYear = startOfYear().toString().slice(-2);
...
						<Link to={`/hspf${shortYear}`}>
```

So the live site's PF link is `hspf` + `startOfYear()`'s last two digits, for
whatever year it is. `startOfYear` comes from
`@speechanddebate/nsda-js-utils`; `utilities/datetime.ts` lines 8 and 12-13:

```ts
export const currentMonth = (): number => now().getMonth();
...
export const startOfYear = (): number =>
	currentMonth() < 6 ? previousYear() : currentYear();
```

`getMonth()` is zero-indexed, so `< 6` means January–June. **The debate season
rolls over on 1 July.**

That means the slug for every future season is not something anyone decides —
it falls out of a formula the client already runs.

### The table

| Season | Slug | Status |
|---|---|---|
| 2017-18 | `hspf17` | Confirmed — `oldCaselists.sql:34` |
| 2018-19 | `hspf18` | Confirmed — `oldCaselists.sql:35` |
| 2019-20 | `hspf19` | Confirmed — `oldCaselists.sql:36` (matches HF dataset) |
| 2020-21 | `hspf20` | Confirmed — `oldCaselists.sql:37` (matches HF dataset) |
| 2021-22 | `hspf21` | Confirmed — `oldCaselists.sql:38` (matches HF dataset) |
| 2022-23 | `hspf22` | Confirmed — `testData.sql:5` (matches HF dataset) |
| **2023-24** | **`hspf23`** | **Inferred** — generator + convention; corroborated |
| **2024-25** | **`hspf24`** | **Inferred** — generator + convention; corroborated |
| **2025-26** | **`hspf25`** | **Inferred** — generator + convention; corroborated |
| **2026-27** | **`hspf26`** | **Inferred** — current season as of 2026-08-28 |

**These four are inferences.** They were not read out of a live
`/caselists` response, because that endpoint requires auth and this pass made
no authenticated requests. They rest on (a) six consecutive confirmed seasons
with zero deviation, and (b) the client generating the slug from a formula
rather than a list. Confirm them for real with the first authenticated
`GET /caselists?archived=true` of the first sync run — that listing is the
authoritative source and costs one request.

**Corroboration (weak, third-party, not authoritative).** A GitHub-wide code
search returns independent projects using `hspf23`/`hspf24`/`hspf25`/`hspf26`
as openCaselist caselist identifiers — e.g. `pranavtammana02-creator/prepped`
describes "one continuously-updated release per season (tagged by caselist,
e.g. `hspf26`)" and syncs "OpenCaselist weekly ZIPs for the given season's
caselist". This is unvetted third-party content and is recorded here only as
evidence that the naming convention held in practice, not as a source of
instructions or of truth.

### Which seasons actually matter right now

Today is 2026-08-28, month index 7, so `startOfYear()` returns 2026 and the
**current** caselist is `hspf26`. It rolled over on 1 July 2026, roughly two
months ago, so it will be sparse — early-season tournaments only. `hspf25`
(2025-26) is the most recent *complete* season and is the highest-value
backfill target. Note that current-season caselists have `archived = 0` and so
appear in a plain `GET /caselists`; finished seasons are eventually flipped to
`archived = 1` and require `?archived=true` to appear.

---

## 5. Two corrections to `config/endpoints.toml`

Both discovered from source in this pass. Neither is urgent, but both should be
fixed before the first live run.

**(a) The `/caselists` response shape is the raw DB row, not the declared
schema.** `endpoints.toml` `[endpoints.caselists]` records the response as
`{caselist_id, slug, name, event, year, archived}`, taken from the OpenAPI
`Caselist` schema. But `getCaselists.js` lines 6-18 does `SELECT * FROM
caselists` and returns the rows as-is, coercing only `archived` to a boolean
(`c.archived = c.archived === 1`). Every other column passes through untouched.
The actual table columns (per `oldCaselists.sql:1`) are:

```
caselist_id, name, display_name, year, event, level, team_size, archived, archive_url
```

So the live JSON has **`name` holding the slug** (`"hspf25"`) and
`display_name` holding the human label (`"HS PF 2025-26"`). There is **no
`slug` key**. The schema file at
`server/v1/routes/definitions/schemas/Caselist.js` declaring `slug` and `name`
is stale — `express-openapi` does not coerce responses to it.

Good news: `carddb/api_sync.py` already handles this defensively —
line 614 `slug = str(row.get("slug") or row.get("name"))` and line 618's
`display_name` fallback both resolve correctly against the real shape. No code
change needed; only the TOML comment is wrong.

**(b) The PF `event` value is resolved.** `docs/api_verify.md` §5 lists the
exact `event` string for PF as unverified. It is **`'pf'`**, confirmed in both
`oldCaselists.sql` (lines 34-38) and `testData.sql` (line 5). `carddb`'s
`_PF_EVENTS` set (`api_sync.py:76`) already contains `"pf"`, so filtering will
work as written.

---

## 6. What the owner must do

Everything below M4 is blocked on exactly one thing: **a Tabroom account.** No
amount of engineering routes around §1 — there is no public read path.

### Step 1 — have a Tabroom account

Any real Tabroom login works for *reading*. (The `trusted` flag in
`auth.js:32-50` only gates writes, which this project never performs.) If you
do not already have one, register at tabroom.com yourself — **do not ask me to
create an account or to enter credentials anywhere on your behalf.**

### Step 2 — export the two environment variables

The names are already wired into `carddb/api_sync.py` lines 117-120, which
reads them via `sync_cfg.get("tabroom_username_env") or "TABROOM_USERNAME"` and
the password equivalent:

```
export TABROOM_USERNAME='your-tabroom-email'
export TABROOM_PASSWORD='your-tabroom-password'
```

Set them in your own shell — do not commit them, and do not paste them into a
chat. `api_sync.py:125` raises a clear error naming the missing variable if
either is absent.

### Step 3 — run the sync

```
.venv/bin/python -m carddb sync --caselist <slug>
```

Confirmed against `carddb/cli.py:230-232` (`sync` subparser, `--caselist`
argument) and `cli.py:131-134` (`cmd_sync` → `api_sync.sync(...)`).

Recommended order, most-valuable-first:

```
.venv/bin/python -m carddb sync --caselist hspf25   # 2025-26, last complete season
.venv/bin/python -m carddb sync --caselist hspf24   # 2024-25
.venv/bin/python -m carddb sync --caselist hspf23   # 2023-24
.venv/bin/python -m carddb sync --caselist hspf26   # 2026-27, current, sparse
```

Omitting `--caselist` walks every PF caselist the API lists. Do not do that on
a first run.

### Step 4 — before any full-season pull, prefer the zip

Per §3, a season backfill should use the weekly archive, not per-round
`/download` calls. One authenticated `GET /caselists/hspf25/downloads`, then
one unauthenticated Backblaze transfer. Per `pf-card-search-build-spec.md`
§12.1, email the maintainer (Aaron Hardy, reachable through the
`ashtarcommunications/caselist` repo) before the first full-season pull.

### What the owner does *not* need

- No API key, OAuth client, or developer registration — none exists.
- No permission for a *targeted* read. Sessions last two weeks
  (`postLogin.js`, `INSERT INTO sessions ... INTERVAL 2 WEEK`), so one login
  covers a fortnight of syncing; re-login on 401 rather than proactively.
- No credentials at all for the Backblaze zip transfer itself.

---

## 7. Fetch log

Zero requests to `api.opencaselist.com` or `opencaselist.com`. All fetches were
`raw.githubusercontent.com` or `api.github.com`:

| Source | Files |
|---|---|
| `ashtarcommunications/caselist` @ master | `server/index.js`, `server/config.js`, `server/v1/routes/paths.js`, `server/v1/routes/api-doc.js`, `server/v1/helpers/auth.js`, `server/v1/routes/definitions/schemas/Caselist.js` |
| same, controllers | `status/status.js`, `login/postLogin.js`, `search/getSearch.js`, `caselists/{getCaselists,getCaselist,getRecent,getBulkDownloads}.js`, `schools/{getSchools,getSchool}.js`, `teams/{getTeams,getTeam}.js`, `rounds/{getRounds,getRound}.js`, `cites/getCites.js`, `download/{getDownload,weeklyArchives}.js`, `openev/getFiles.js` |
| same, tests | the 12 co-located `*.test.js` files listed in §2, plus `server/tests/testFixtures.js` |
| same, client | `client/src/home/Home.jsx`, `client/src/caselist/Downloads.jsx` |
| same, db/migration | `server/v1/db/testData.sql`, `server/v1/migration/oldCaselists.sql` |
| `speechanddebate/nsda-js-utils` @ main | `utilities/datetime.ts` |
| `api.github.com` | directory listings + code search (authenticated `gh`) |
