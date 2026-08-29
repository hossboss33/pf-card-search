# Putting the sign-in on a website

The published GitHub Pages site (https://hossboss33.github.io/pf-card-search/)
cannot sign in to openCaselist, and no amount of front-end work will change
that. Pages serves **static files — there is no server**, and openCaselist's
session cookie is set `SameSite=Lax` and scoped to `opencaselist.com`:

```js
// caselist/server/v1/controllers/login/postLogin.js
res.cookie('caselist_token', nonce, {
    maxAge: remember && user.trusted ? 1000*60*60*24*14 : undefined,
    httpOnly: false, path: '/', sameSite: 'Lax', domain: config.COOKIE_DOMAIN });
```

A browser will not attach a `SameSite=Lax` cookie to a request coming from
another site, `Cookie` is a forbidden header so a page cannot attach it
itself, and `server/v1/helpers/auth.js` reads the token **only** from
`req.cookies`. A static page therefore has nothing that can hold the session:
the login POST succeeds and every request after it is unauthorised, which is
exactly the "it signs in then instantly expires" symptom.

**Running the app on a real host fixes it**, because then the login happens in
Python, server-side, over an ordinary HTTPS request — the same way it already
works on your laptop. Same code, same `/connect` page, just not static.

## Option 1 — your laptop (works right now, nothing to sign up for)

```bash
.venv/bin/python -m carddb signin
```

Starts the app and opens the sign-in page. One command.

## Option 2 — Hugging Face Spaces (free, public URL)

1. Create a Space at https://huggingface.co/new-space, type **Docker**.
2. Push this repository to it.
3. In the Space's **Settings → Variables and secrets**, set
   `CARDDB_ALLOW_REMOTE_LOGIN=1`.

That variable is the deliberate opt-in that lets `/connect` accept a password
from outside loopback. It is off by default, and even when on the page still
refuses a request that did not arrive over HTTPS — a password must never
cross the network unencrypted. Spaces terminate TLS for you.

## Option 3 — any container host

`Dockerfile` at the repo root runs the whole app:

```bash
docker build -t pf-card-search .
docker run -p 7860:7860 -e CARDDB_ALLOW_REMOTE_LOGIN=1 pf-card-search
```

Render, Fly.io, Railway and Cloud Run all take this Dockerfile unchanged. Set
`CARDDB_ALLOW_REMOTE_LOGIN=1` and serve over HTTPS.

## What to keep in mind

Signing in on a hosted instance means **your** Tabroom session lives in that
server's memory. Deploy it for yourself or your team, not as a public login
page for strangers: openCaselist's terms are between each person and them, and
their API is rate-limited per account. The card index itself needs no sign-in
at all — that is what the static site is for.
