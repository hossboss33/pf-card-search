/* connect.js — the optional "Recent seasons" flow.
 *
 * The search index on this site stops at 2022-23. Seasons after that exist
 * only on openCaselist, which requires a Tabroom account to read anything.
 * This file lets a visitor authenticate their OWN browser against
 * openCaselist and read those seasons live.
 *
 * Credential rules (see site/LIVE_API.md; do not weaken these):
 *   - The username and password go straight to api.opencaselist.com via
 *     window.OpenCaselist.login(). There is no server here to send them to.
 *   - They are never written to localStorage, sessionStorage, IndexedDB, a
 *     cookie, or a URL. They are read from the form, passed to login(), and
 *     the field is cleared immediately after.
 *   - Nothing is logged. There is no console call in this file.
 */
(function () {
  "use strict";

  var OC = window.OpenCaselist;
  var Docx = window.CardDocx;
  if (!OC) return;

  function $(id) { return document.getElementById(id); }
  function txt(id, s) { var e = $(id); if (e) e.textContent = s; }

  function note(msg, kind) {
    var box = $("connect-status");
    if (!box) return;
    box.hidden = !msg;
    box.textContent = msg || "";
    box.className = "notice" + (kind ? " " + kind : "");
  }

  function setConnected(on) {
    var b = $("connect-browse"), out = $("connect-signout");
    var sub = $("connect-submit"), u = $("tabroom-user"), p = $("tabroom-pass");
    if (b) b.hidden = !on;
    if (out) out.hidden = !on;
    if (sub) sub.disabled = on;
    if (u) u.disabled = on;
    if (p) p.disabled = on;
  }

  OC.onProgress(function (ev) {
    if (!ev) return;
    if (ev.phase === "retry") {
      txt("live-progress", "openCaselist asked us to slow down; retrying in "
          + Math.round((ev.waitMs || 0) / 1000) + "s.");
    } else if (ev.phase === "request") {
      txt("live-progress", "Reading " + (ev.path || "openCaselist") + "…");
    }
  });

  /* ------------------------------------------------------------- sign in */

  var form = $("connect-form");
  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var uEl = $("tabroom-user"), pEl = $("tabroom-pass");
      var user = uEl ? uEl.value : "";
      var pass = pEl ? pEl.value : "";
      if (!user || !pass) {
        note("Enter your Tabroom email and password.", "warn");
        return;
      }
      note("Signing in to openCaselist…");
      var sub = $("connect-submit");
      if (sub) sub.disabled = true;

      OC.login(user, pass).then(function () {
        /* Drop the password as soon as the request has been issued. */
        if (pEl) pEl.value = "";
        user = null; pass = null;
        note("Signed in. Loading the seasons your account can see…", "ok");
        setConnected(true);
        return loadCaselists();
      })["catch"](function (err) {
        if (pEl) pEl.value = "";
        user = null; pass = null;
        if (sub) sub.disabled = false;
        note(err && err.kind === "auth"
          ? "openCaselist rejected that login. Use the same email and password you use on Tabroom."
          : "Could not reach openCaselist: " + ((err && err.message) || "unknown error"),
          "warn");
      });
    });
  }

  var signout = $("connect-signout");
  if (signout) {
    signout.addEventListener("click", function () {
      OC.logout();
      setConnected(false);
      ["live-caselist", "live-school", "live-team"].forEach(function (id) {
        var s = $(id);
        if (s) { s.innerHTML = ""; if (id !== "live-caselist") s.disabled = true; }
      });
      txt("live-progress", "");
      var r = $("live-results");
      if (r) r.innerHTML = "";
      var sub = $("connect-submit");
      if (sub) sub.disabled = false;
      note("Signed out in this browser. The session cookie belongs to "
           + "openCaselist and is cleared by your browser's site settings.", "ok");
    });
  }

  /* ------------------------------------------------------------- browsing */

  function fill(sel, items, label, value) {
    if (!sel) return;
    sel.innerHTML = "";
    var blank = document.createElement("option");
    blank.value = "";
    blank.textContent = items.length ? "— choose —" : "— none —";
    sel.appendChild(blank);
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = value(it);
      o.textContent = label(it);
      sel.appendChild(o);
    });
    sel.disabled = items.length === 0;
  }

  function slugOf(c) { return c.slug || c.name || ""; }
  function labelOf(c) { return c.display_name || c.displayName || slugOf(c); }

  function loadCaselists() {
    return OC.listPFCaselists().then(function (list) {
      fill($("live-caselist"), list || [], labelOf, slugOf);
      txt("live-progress", (list && list.length)
        ? "Pick a season."
        : "Your account cannot see any PF caselists.");
    })["catch"](function (err) { note(describe(err), "warn"); });
  }

  function describe(err) {
    if (err && err.kind === "auth") return "Session expired. Sign in again.";
    return "openCaselist error: " + ((err && err.message) || "unknown");
  }

  var caselistSel = $("live-caselist");
  if (caselistSel) {
    caselistSel.addEventListener("change", function () {
      var cl = caselistSel.value;
      var r = $("live-results"); if (r) r.innerHTML = "";
      fill($("live-team"), [], String, String);
      if (!cl) { fill($("live-school"), [], String, String); return; }
      txt("live-progress", "Loading schools…");
      OC.schools(cl).then(function (rows) {
        fill($("live-school"), rows || [],
          function (s) { return s.display_name || s.displayName || s.name; },
          function (s) { return s.name; });
        txt("live-progress", "Pick a school.");
      })["catch"](function (err) { note(describe(err), "warn"); });
    });
  }

  var schoolSel = $("live-school");
  if (schoolSel) {
    schoolSel.addEventListener("change", function () {
      var cl = caselistSel.value, sc = schoolSel.value;
      var r = $("live-results"); if (r) r.innerHTML = "";
      if (!cl || !sc) { fill($("live-team"), [], String, String); return; }
      txt("live-progress", "Loading teams…");
      OC.teams(cl, sc).then(function (rows) {
        fill($("live-team"), rows || [],
          function (t) { return t.display_name || t.displayName || t.name; },
          function (t) { return t.name; });
        txt("live-progress", "Pick a team.");
      })["catch"](function (err) { note(describe(err), "warn"); });
    });
  }

  var teamSel = $("live-team");
  if (teamSel) {
    teamSel.addEventListener("change", function () {
      var cl = caselistSel.value, sc = schoolSel.value, tm = teamSel.value;
      if (!cl || !sc || !tm) return;
      loadTeam(cl, sc, tm);
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function sideName(s) {
    var v = String(s || "").toUpperCase();
    if (v === "A" || v === "P" || v === "PRO") return "Pro";
    if (v === "N" || v === "C" || v === "CON") return "Con";
    return "";
  }

  /* Read a team's rounds, then parse each open-source .docx in the browser so
     the visitor sees real cards, not just the lossy pasted cites. */
  function loadTeam(cl, sc, tm) {
    var out = $("live-results");
    if (out) out.innerHTML = "";
    txt("live-progress", "Loading rounds…");
    OC.rounds(cl, sc, tm).then(function (rounds) {
      rounds = rounds || [];
      if (!rounds.length) {
        txt("live-progress", "This team has no disclosed rounds.");
        return;
      }
      var withDocs = rounds.filter(function (r) { return r.opensource; });
      txt("live-progress", "Found " + rounds.length + " rounds, "
          + withDocs.length + " with open-source files. Reading them one at a "
          + "time (openCaselist limits downloads).");
      renderRounds(rounds);
      return withDocs.reduce(function (chain, r, i) {
        return chain.then(function () {
          txt("live-progress", "Parsing file " + (i + 1) + " of " + withDocs.length + "…");
          return OC.downloadOpenSource(r.opensource).then(function (buf) {
            if (!Docx) return;
            var res = Docx.parseDocx(buf);
            renderCards(r, (res && res.cards) || []);
          })["catch"](function () {
            renderCards(r, null);   /* one bad file must not stop the rest */
          });
        });
      }, Promise.resolve()).then(function () {
        txt("live-progress", "Done. " + withDocs.length + " files read live from openCaselist.");
      });
    })["catch"](function (err) { note(describe(err), "warn"); });
  }

  function renderRounds(rounds) {
    var out = $("live-results");
    if (!out) return;
    var html = rounds.map(function (r) {
      var bits = [esc(r.tournament || "Tournament unknown")];
      if (r.round) bits.push("Round " + esc(r.round));
      var sd = sideName(r.side);
      if (sd) bits.push(sd);
      if (r.opponent) bits.push("vs " + esc(r.opponent));
      return '<section class="live-round" id="live-round-' + esc(r.round_id || r.id) + '">'
        + '<h3>' + bits.join(" · ") + "</h3>"
        + (r.report ? '<p class="meta">' + esc(r.report) + "</p>" : "")
        + '<div class="live-cards"><p class="meta">'
        + (r.opensource ? "Reading file…" : "No open-source file for this round.")
        + "</p></div></section>";
    }).join("");
    out.innerHTML = html;
  }

  function renderCards(round, cards) {
    var sec = $("live-round-" + (round.round_id || round.id));
    if (!sec) return;
    var box = sec.querySelector(".live-cards");
    if (!box) return;
    if (cards === null) {
      box.innerHTML = '<p class="meta">That file could not be read in the browser.</p>';
      return;
    }
    var real = cards.filter(function (c) { return !c.is_analytic; });
    if (!real.length) {
      box.innerHTML = '<p class="meta">No cards found in that file.</p>';
      return;
    }
    box.innerHTML = '<p class="meta">' + real.length + " cards</p>" + real.map(function (c) {
      return '<article class="card-doc live-card">'
        + '<h4 class="card-tag">' + esc(c.tag || "(untagged card)") + "</h4>"
        + (c.cite ? '<p class="card-cite"><strong>' + esc(c.cite) + "</strong></p>" : "")
        + (c.fullcite ? '<p class="card-fullcite">' + esc(c.fullcite) + "</p>" : "")
        + '<div class="card-body">' + sanitize(c.markup_html || esc(c.body_text || "")) + "</div>"
        + "</article>";
    }).join("");
  }

  /* The markup comes from a file on someone else's server: treat it as
     untrusted and allow only the card-rendering tags. */
  var ALLOWED = { H1: 1, H2: 1, H3: 1, H4: 1, P: 1, U: 1, STRONG: 1, EM: 1, MARK: 1, SPAN: 1, BR: 1 };
  function sanitize(html) {
    var doc = new DOMParser().parseFromString("<div>" + html + "</div>", "text/html");
    var root = doc.body.firstChild;
    (function walk(node) {
      var kids = Array.prototype.slice.call(node.childNodes);
      kids.forEach(function (k) {
        if (k.nodeType === 3) return;
        if (k.nodeType !== 1 || !ALLOWED[k.tagName]) {
          var text = doc.createTextNode(k.textContent || "");
          node.replaceChild(text, k);
          return;
        }
        Array.prototype.slice.call(k.attributes).forEach(function (a) {
          if (a.name !== "class") k.removeAttribute(a.name);
        });
        walk(k);
      });
    })(root);
    return root.innerHTML;
  }
})();
