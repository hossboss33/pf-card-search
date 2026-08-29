/* filters.js — the Filters panel.
 *
 * Deliberately decoupled from app.js: the controls do not call the search
 * directly. They rewrite the operator tokens inside the search box and fire an
 * "input" event, which is exactly what typing those operators by hand would
 * do. One code path, one grammar, and every filtered search stays shareable as
 * a URL — the filter state lives in the query string, not in hidden UI state.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var q = $("q");
  var panel = $("filters");
  var toggle = $("filters-toggle");
  if (!q || !panel || !toggle) return;

  /* Operators this panel owns. Anything else the user typed is left alone. */
  var OWNED = ["after", "before", "year", "cite", "min_reads", "sort", "is", "event"];

  var els = {
    after: $("f-after"), before: $("f-before"), year: $("f-year"),
    cite: $("f-cite"), reads: $("f-reads"), sort: $("f-sort"),
    event: $("f-event"), analytic: $("f-analytic")
  };

  /* Split a query into bare terms and the operators we manage. Quoted phrases
     must survive intact, so scan rather than split on spaces. */
  function tokenize(s) {
    var out = [], cur = "", inQ = false, i, c;
    for (i = 0; i < s.length; i++) {
      c = s.charAt(i);
      if (c === '"') { inQ = !inQ; cur += c; continue; }
      if (c === " " && !inQ) { if (cur) out.push(cur); cur = ""; continue; }
      cur += c;
    }
    if (cur) out.push(cur);
    return out;
  }

  function isOwned(tok) {
    var m = /^-?([a-zA-Z_]+):/.exec(tok);
    if (!m) return false;
    var f = m[1].toLowerCase();
    if (f === "is") return /^-?is:analytic$/i.test(tok);
    return OWNED.indexOf(f) !== -1 && f !== "is";
  }

  function readBox() {
    var rest = [], owned = {};
    tokenize(q.value).forEach(function (t) {
      if (!isOwned(t)) { rest.push(t); return; }
      var m = /^-?([a-zA-Z_]+):(.*)$/.exec(t);
      owned[m[1].toLowerCase()] = m[2].replace(/^"|"$/g, "");
    });
    return { rest: rest, owned: owned };
  }

  /* Panel -> query box */
  function apply() {
    var parts = readBox().rest;
    function add(name, val) {
      if (val === "" || val == null) return;
      parts.push(name + ":" + (/\s/.test(val) ? '"' + val + '"' : val));
    }
    add("after", els.after.value);
    add("before", els.before.value);
    var y = (els.year.value || "").replace(/[^0-9]/g, "");
    if (y.length === 4) y = y.slice(2);
    if (y.length === 2) add("year", y);
    add("cite", (els.cite.value || "").trim());
    var reads = (els.reads.value || "").trim();
    if (reads !== "" && Number(reads) > 0) add("min_reads", String(Number(reads)));
    if (els.event.value) add("event", els.event.value);
    if (els.sort.value) add("sort", els.sort.value);
    if (els.analytic.checked) parts.push("is:analytic");

    q.value = parts.join(" ").replace(/\s+/g, " ").trim();
    /* Let app.js's own debounced handler run the search. */
    q.dispatchEvent(new Event("input", { bubbles: true }));
  }

  /* Query box -> panel, so a shared URL populates the controls */
  function sync() {
    var owned = readBox().owned;
    els.after.value = owned.after || "";
    els.before.value = owned.before || "";
    els.year.value = owned.year || "";
    els.cite.value = owned.cite || "";
    els.reads.value = owned.min_reads || "";
    els.sort.value = owned.sort || "";
    els.event.value = owned.event || "";
    els.analytic.checked = /(^|\s)is:analytic(\s|$)/i.test(q.value);
    markActive();
  }

  function markActive() {
    var n = 0, k;
    for (k in els) {
      if (!els.hasOwnProperty(k)) continue;
      if (k === "analytic") { if (els[k].checked) n++; }
      else if (els[k].value) n++;
    }
    toggle.textContent = n ? "Filters (" + n + ")" : "Filters";
  }

  Object.keys(els).forEach(function (k) {
    if (!els[k]) return;
    els[k].addEventListener("change", function () { apply(); markActive(); });
  });

  var clear = $("f-clear");
  if (clear) {
    clear.addEventListener("click", function () {
      els.after.value = ""; els.before.value = ""; els.year.value = "";
      els.cite.value = ""; els.reads.value = ""; els.sort.value = "";
      els.event.value = ""; els.analytic.checked = false;
      apply(); markActive();
    });
  }

  function open(on) {
    panel.hidden = !on;
    toggle.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) { sync(); els.after.focus(); }
  }

  toggle.addEventListener("click", function () { open(panel.hidden); });

  /* The spec's key map: f opens filters. Do not steal it while typing. */
  document.addEventListener("keydown", function (ev) {
    var t = ev.target || {};
    var tag = (t.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (ev.key === "f" && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      open(panel.hidden);
    }
  });

  q.addEventListener("change", sync);
  window.addEventListener("hashchange", sync);
  sync();
})();
