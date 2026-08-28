# `site/opencaselist.js` and `site/docx.js` — the interface they expose

Two dependency-free browser modules for the static site. Neither has a build
step, neither imports anything, and both attach a single global.

```html
<script src="opencaselist.js"></script>   <!-- window.OpenCaselist -->
<script src="docx.js"></script>           <!-- window.CardDocx -->
```

Load order does not matter; they do not reference each other. Both are safe to
load on every page — neither does anything at load time except define its
global. Nothing is fetched, stored, or logged until you call a method.

---

## Part 1 — `window.OpenCaselist`

A client for `https://api.opencaselist.com/v1`, called **directly from the
visitor's browser**. This works because that API reflects any `Origin` and
sends `access-control-allow-credentials: true`, so a cross-origin
`fetch(..., {credentials:'include'})` carries the `caselist_token` cookie.
There is no proxy, no intermediary, and no server of ours — this site is
static.

### The rules this module is built to keep

These are load-bearing. If you change the login UI, keep them.

1. **Credentials go from the form straight to openCaselist.** `login()` is the
   only place they appear. They are read from its two arguments, serialised
   into one request body, and dropped. They are never written to
   `localStorage`, `sessionStorage`, IndexedDB, a cookie, a URL, or any
   variable that outlives the call.
2. **The session token is never touched.** openCaselist's login response body
   contains the token as well as setting the httpOnly cookie. This module
   parses the response, ignores `token`, and keeps only `expires`, `trusted`
   and `admin`. `login()`'s return value has no token in it.
3. **No telemetry, no analytics, no logging.** There is not one `console.*`
   call in either file. Progress events (below) carry a method, a path and a
   status — never a body, a credential or a token. A test asserts this.
4. **One queue, one request at a time, ≥ 1 s apart.** Every method goes
   through it. Do not add a code path that calls `fetch` directly.
5. **Login is optional and never auto-prompted.** Nothing in this module runs
   on load, and every other part of the site must work without it. The control
   in the UI is a plain "Connect to openCaselist" the visitor chooses to open.

### The disclosure the login form must carry

Rule 4 of the project brief: put this immediately next to the inputs, not in a
footer, in plain sentences with no marketing tone. It must say, in substance:

- This form sends your Tabroom username and password **directly to
  openCaselist** (`api.opencaselist.com`) and nowhere else.
- This site is static. It has no server. It never receives, sees or stores
  your login.
- openCaselist requires a Tabroom account to read any disclosure at all —
  that is their rule, not ours.
- The source of this page is public and auditable:
  <https://github.com/hossboss33/pf-card-search> (this file and
  `site/opencaselist.js`).
- If you would rather not type it into a web page, run the tool locally
  instead (`carddb sync`, credentials from your own environment variables).

Use `<form>` with real `<label>`s, `type="password"`,
`autocomplete="current-password"` on the password field, and never pre-fill.

### Two honest caveats to surface in the UI

- **Third-party cookie blocking can defeat this entirely.** `api.opencaselist.com`
  is a third party relative to this site's origin. Safari's ITP blocks such
  cookies by default, and Chrome/Firefox do in strict privacy modes. When that
  happens `login()` appears to succeed (the response is 201) but the next
  request comes back 401 — a `kind: "auth"` error. Tell the visitor to allow
  cookies for `api.opencaselist.com`, or to run the tool locally.
- **There is no logout endpoint.** `server/v1/routes/paths.js` has `/login` and
  nothing else, and the cookie is httpOnly on another origin, so no script here
  can delete it. `logout()` clears local state only and says so in its return
  value. Do not label the button in a way that promises more.
- **The browser will not let us send a project User-Agent.** Spec §0.2 asks for
  a UA naming the project and a contact address; `User-Agent` is a forbidden
  header in `fetch`. The Python sync client (`carddb/api_sync.py`) does send
  one. Browser traffic is identified only by the honest request rate.

### Politeness constants

```js
OpenCaselist.MIN_SPACING_MS   // 1000 — minimum gap between requests
OpenCaselist.API_BASE         // "https://api.opencaselist.com/v1"
```

Internally: concurrency 1, up to 4 retries, only on `429` and `5xx` and network
failures; backoff is `max(Retry-After, 2000 · 2^attempt)` capped at 60 s plus
jitter. `4xx` other than 429 is never retried. `login()` is never retried at
all (a retry would mean holding the credentials longer, and `/login` is limited
to 20/minute).

### Methods

Everything is `async` and returns a promise. Everything can reject with an
error described under **Errors**.

| Call | Route | Returns |
|---|---|---|
| `login(username, password)` | `POST /login` | `{ok:true, expires, trusted, admin}` |
| `logout()` | *(none exists)* | `{ok:true, serverLogout:false, note}` — synchronous |
| `isConnected()` | `GET /caselists` | `true` / `false`, never throws on 401 |
| `sessionInfo()` | — | `{connected, checkedAt, expires, trusted, admin}` — synchronous |
| `listPFCaselists()` | `GET /caselists` + `?archived=true` | `Caselist[]`, PF only, newest first (2 requests, ~2 s) |
| `caselists(archived)` | `GET /caselists[?archived=true]` | `Caselist[]`, unfiltered |
| `caselist(slug)` | `GET /caselists/{caselist}` | one `Caselist` |
| `schools(caselist)` | `GET /caselists/{caselist}/schools` | raw rows |
| `teams(caselist, school)` | `.../schools/{school}/teams` | raw rows |
| `rounds(caselist, school, team, side?)` | `.../teams/{team}/rounds` | raw rows — `opensource` is the download path |
| `cites(caselist, school, team, side?)` | `.../teams/{team}/cites` | raw rows |
| `recent(caselist)` | `GET /caselists/{caselist}/recent` | raw rows |
| `bulkDownloads(caselist)` | `GET /caselists/{caselist}/downloads` | `[{name, url}]` — direct Backblaze links |
| `downloadOpenSource(path)` | `GET /download?path=` | `ArrayBuffer` |
| `status()` | `GET /status` | `{ok, status}` — unauthenticated |
| `onProgress(fn)` | — | unsubscribe function |
| `queueLength()` | — | number of requests queued or in flight |

`login` posts `{username, password, remember: false}` as JSON with
`credentials: 'include'`.

`downloadOpenSource` refuses locally, without a request, any path containing
`..` or starting with `/` — openCaselist answers those with a 400 and there is
no reason to send one. Server-side limits on that route are 10/minute, and
5/day for any path containing `weekly`.

For anything season-sized, use `bulkDownloads()` and fetch the zip from the
Backblaze URL it returns. That host is not openCaselist and consumes none of
their `/download` quota (`docs/api_access.md` §3).

### The `Caselist` object

The live `/caselists` response is the raw DB row, so **there is no `slug`
key** — the slug is in `name` and the label in `display_name`
(`docs/api_access.md` §5a). This module normalises both shapes, so callers
never have to care:

```js
{ slug: "hspf25",              // row.slug ?? row.name
  display_name: "HS PF 2025-26",
  event: "pf", level: "hs", year: 2025, team_size: 2,
  archived: true,              // row.archived === true || === 1
  caselist_id: 1034,
  raw: { /* the untouched row */ } }
```

PF selection mirrors `carddb/api_sync.py::_is_pf`: `event` in
`{pf, pfd, hspf, publicforum, public forum, public-forum}`, falling back to a
`pf(?=[^a-z]|$)` match on slug + label when the row has no `event`.

### Progress events

```js
const stop = OpenCaselist.onProgress(e => { /* render e.message */ });
```

`e` is `{phase, method, path, attempt, queueLength, waitMs, status, message}`.
`phase` is one of `"queued"`, `"waiting"` (pacing), `"request"`, `"retry"`,
`"done"`, `"error"`. `message` is a short human string like
`"rate-limited by openCaselist; waiting"` or `"downloading Neg-Round1.docx"`.
No event ever contains a body, a credential or a token. A listener that throws
is ignored rather than allowed to break a sync.

### Errors

Every rejection is an `Error` with `name === "OpenCaselistError"` and:

```js
{ kind, status, path, retryAfterMs, needsLogin }
```

| `kind` | when | what the UI should say |
|---|---|---|
| `auth` | 401, or a rejected login | "Sign in again." `needsLogin === true` |
| `forbidden` | 403 | the request was refused |
| `not_found` | 404 | nothing at that path |
| `rate_limited` | 429 after retries are exhausted | back off, try later |
| `server` | 5xx after retries | openCaselist is having trouble |
| `network` | fetch threw / timed out | offline, or third-party cookies blocked |
| `bad_response` | 200 with unparseable JSON | |
| `client` | other 4xx | |
| `usage` | our own argument check | a bug in the caller |

`kind === "auth"` is the one to branch on: show "connect again", not a generic
failure.

---

## Part 2 — `window.CardDocx`

Parses a `.docx` into card objects entirely in the browser. No library. The ZIP
end-of-central-directory and central directory are read by hand (STORED
method 0 and DEFLATE method 8, plus ZIP64), inflated with the platform's own
`DecompressionStream('deflate-raw')`, and `word/document.xml` +
`word/styles.xml` are walked with `DOMParser`.

```js
const { cards, warnings, usedFallback } = await CardDocx.parseDocx(arrayBuffer);
```

| Call | Returns |
|---|---|
| `parseDocx(arrayBuffer, opts?)` | `Promise<{cards, warnings, usedFallback}>` |
| `parseDocumentXml(documentXml, stylesXml, opts?)` | same, synchronously, from already-extracted XML |
| `readDocxParts(arrayBuffer)` | `Promise<{documentXml, stylesXml, names}>` |
| `extractSourceUrl(fullcite)` / `extractPubDate(fullcite)` | the two cite helpers, exported for reuse |

`opts.acceptTrackedInsertions` (default `false`) — see divergences.

Errors are `Error` with `name === "DocxError"` and a `code`:
`empty_file`, `not_zip`, `not_docx`, `corrupt_zip`, `unsupported_compression`,
`no_decompression_stream`, `bad_xml`, `no_body`. A bad file must never abort a
batch — catch, record, move on, exactly as `carddb` does with `ParseFailure`.

`no_decompression_stream` means the browser is too old (needs Chrome/Edge 103+,
Safari 16.4+, Firefox 113+). Say that rather than "parse failed".

### The card object

Field-for-field the same as `carddb.ingest.CardRecord`, so a browser-parsed
card and a server-indexed card are directly comparable:

```js
{ tag, cite, fullcite, body_text, is_analytic,
  source_url, source_pub_date,
  pocket, hat, block,
  markup_html, summary, spoken, highlight_ratio,
  fidelity: "opensource", ordinal, extras: { has_table? } }
```

`markup_html` uses only `<h4> <p> <u> <strong> <mark> <span class="min"> <br>`,
with `&`, `<`, `>`, `"` and `'` escaped — the exact output of the Python
parser after `carddb/sanitize.py`, so it is already safe to assign to
`innerHTML`.

Segmentation and markup follow spec §1.3 / §3.4 as implemented in
`carddb/docx_parser.py`: Heading 1/2/3 set pocket/hat/block, each Heading 4
opens a card that closes at the next heading of any level; run precedence is
highlight → `<mark>`, bold+underline → `<strong><u>`, underline → `<u>`,
bold → `<strong>`, size ≤ 9pt → `<span class="min">`, else plain, resolved
against inherited character- and paragraph-style formatting from
`word/styles.xml`, not just direct run properties. The widened fullcite-shaped
cite detection and the "≥ ~40 words of body with no cite is evidence, not an
analytic" rule are both carried over, as is the direct-formatting fallback
pass for non-Verbatim files.

### Where this came from

The low-level OOXML plumbing — namespaced-DOM helpers, `extractRpr` /
`mergeRpr`, style-chain resolution, run collection, run text — is **adapted
from the owner's own `window.CardReaderParser`** in
<https://github.com/hossboss33/cardviewer>, which is already battle-tested on
real PF packets. That code was evaluated for wholesale reuse and rejected for
the layer above: CardReader produces a three-tier *viewer* model
(`read`/`emph`/`bulk`) with its own much wider `isCiteish` heuristic and no
notion of pocket/hat/block or minimized runs, so adopting it would have meant
a browser index that disagrees with the server index on nearly every card. The
plumbing is the part worth reusing, and it is.

### Divergences from `carddb/docx_parser.py`

Everything below is a deliberate, tested difference. Nothing else differs — see
the verification section.

1. **`w:highlight w:val="none"`.** python-docx raises `ValueError`
   (`WD_COLOR_INDEX` has no `none` member), which makes `carddb` reject the
   *whole document* with a `ParseFailure`. `docx.js` reads it as "not
   highlighted", which is what Word means by it. Real files written by Word do
   emit this, so the browser parser will succeed on files the Python parser
   currently fails. *This is a bug in `carddb`, not in `docx.js`* — worth
   fixing there (wrap the `highlight_color` read in a `try`).
2. **Tracked insertions (`w:ins`).** python-docx's `Paragraph.iter_inner_content()`
   is `./w:r | ./w:hyperlink` — direct children only — so runs inside `w:ins`,
   `w:smartTag` and `w:sdt` are invisible to `carddb`. `docx.js` matches that
   by default and adds a warning saying how many run groups it skipped. Pass
   `{acceptTrackedInsertions: true}` to include them; the output will then be
   a superset of the Python parse. Runs inside `w:del` are skipped either way.
3. **A missing or unreadable `word/styles.xml`** produces a warning and a
   direct-formatting-only parse rather than an exception.
4. **Unicode whitespace and word classes.** Python's `str.split()` and `\w`
   are Unicode-aware; JS's `\s` and `\w` are not. `docx.js` spells both out
   (`[\s\u001c-\u001f\u0085]` for whitespace, `[\p{L}\p{N}_'’.-]` for the cite
   regexes) so the two agree on ordinary text. Exotic separators
   (U+FEFF, some C1 controls) may still differ by a character.
5. **Astral characters.** `highlight_ratio` uses code-point lengths
   (`Array.from(s).length`) so it matches Python's `len()` rather than
   UTF-16 units.

Merged-cell handling, nested tables (one level), manual line breaks, `w:tab`,
`w:cr`, `w:ptab` and `w:noBreakHyphen` all match python-docx's behaviour
exactly.

---

## Verification

Run 2026-08-28. **Zero authenticated requests to openCaselist; zero requests
to `api.opencaselist.com` at all.** Both suites ran against a local
`python3 -m http.server` on `127.0.0.1`, with `fetch` stubbed for the API tests.

**Parser.** Twelve `.docx` fixtures were built with the repo's own generator
(`tests/fixtures/docx_builders.py`) — the eight existing shapes, an empty
file, an "everything" document covering every markup layer plus a table, a
manual line break, HTML-escaping edge characters, a fullcite-only card, a
no-cite ≥40-word evidence card and an analytic, and a repack of that document
with every ZIP entry STORED (method 0). Each was parsed by
`carddb/docx_parser.py` and by `docx.js` in the browser and compared field by
field on `tag, cite, fullcite, body_text, is_analytic, source_url,
source_pub_date, pocket, hat, block, markup_html, summary, spoken,
highlight_ratio, ordinal, has_table`, plus the warning list and the fallback
flag.

```
ok   all_highlighted      py=1 js=1      ok   non_verbatim         py=2 js=2
ok   analytic_with_text   py=1 js=1      ok   table_doc            py=1 js=1
ok   empty                py=0 js=0      ok   two_para_cite        py=1 js=1
ok   everything           py=5 js=5      ok   verbatim             py=3 js=3
ok   everything_stored    py=5 js=5      ok   year_only_cite       py=1 js=1
ok   loose_pf             py=2 js=2      ok   manual_breaks        py=1 js=1

0 of 12 documents differ
```

`non_verbatim` exercises the direct-formatting fallback and inherited
paragraph-style formatting; both parsers report `usedFallback = true` and the
identical warning string.

**API client.** 38 contract checks, all passing, with `window.fetch` replaced
by a stub so nothing left the machine: request spacing (measured gaps 1242 ms
and 2001 ms for three concurrent calls), concurrency 1, `credentials:'include'`
on every request, 401 → `kind:"auth"` with `needsLogin`, `isConnected()`
returning `false` rather than throwing on 401, a 429 with `Retry-After: 1`
retried once after 2999 ms, 404 not retried, the no-`slug` caselist shape,
`login()` sending exactly `{username, password, remember:false}` and returning
no token, `localStorage`/`sessionStorage` still empty afterwards, and no
credential or token string anywhere in `sessionInfo()` or the progress event
log.
