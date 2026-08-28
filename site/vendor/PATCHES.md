# Local patch to `sqlite.worker.v2.js`

(The file is named `.v2` because a browser holding the UNPATCHED worker from
an earlier deploy under the old name kept requesting chunk URLs without the
suffix and 404ing. A new filename cannot collide with any cached copy.)

Vendored from `sql.js-httpvfs@0.8.12`, with one deliberate change.

## What changed

Chunk URLs are built as `urlPrefix + paddedIndex + cacheBust`. The patch adds
an optional `urlSuffix` between the index and the cache-bust query:

```js
// before
url: e.urlPrefix + String(n).padStart(e.suffixLength, "0") + l
// after
url: e.urlPrefix + String(n).padStart(e.suffixLength, "0") + (e.urlSuffix || "") + l
```

`urlSuffix` defaults to empty, so unpatched behaviour is unchanged.

## Why

GitHub Pages gzips `application/octet-stream`, and **range requests against a
gzip-encoded response return bytes from the compressed stream**, not the file.
sql.js-httpvfs asks for raw file offsets, so every read lands on the wrong
bytes and SQLite reports `database disk image is malformed`.

Measured on this repo's own deploy: a 45,000,000-byte chunk was served with
`content-encoding: gzip` and `content-length: 8513117` whenever the client
sent `Accept-Encoding: gzip` — which every browser does. `curl` without that
header got the correct 45,000,000 bytes, which is why the files looked fine
when checked from the shell.

`Accept-Encoding` is a forbidden header name, so a page cannot opt out from
`fetch`. The only lever is the file extension. Probing a deploy with identical
200,000-byte files:

| extension | content-encoding | length |
|---|---|---|
| `.png` `.jpg` `.gz` `.zip` `.woff2` `.mp4` | none | 200000 |
| `.bin` `.br` | gzip | 200083 |

So the database chunks ship as `....000.png`. They are not images; the
extension exists purely to stop the CDN compressing them. `.gz` would have
worked too, but browsers may try to decode it.

## Re-applying

If sql.js-httpvfs is ever re-vendored, re-apply this one-line change, or the
site will break with `database disk image is malformed` the moment the chunks
are large enough for the CDN to compress.
