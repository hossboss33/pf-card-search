/* PF card search - vanilla JS. Spec 8.6: keyboard-first, instant search,
   highlight setting persisted locally. No libraries, no build step. */
(function () {
  "use strict";
  var root = document.documentElement;
  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var $$ = function (sel, el) {
    return Array.prototype.slice.call((el || document).querySelectorAll(sel));
  };

  /* --- highlight color setting (spec 8.3) ------------------------------ */
  var HL = { green: "#00FF00", yellow: "#FFFF00", blue: "#0000FF", turquoise: "#00FFFF" };
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  function applyHl(name) {
    if (!HL[name]) name = "green";
    root.style.setProperty("--hl", HL[name]);
    root.setAttribute("data-hl", name);
    store("hl", name);
    $$(".swatch").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-hl-name") === name);
    });
    /* export forms carry the current color so the .docx matches the screen */
    $$("input.hl-input").forEach(function (i) { i.value = name; });
    $$("form.export-form").forEach(function (f) {
      f.action = f.action.split("?")[0] + "?hl=" + name;
    });
  }
  applyHl(load("hl") || "green");
  $$(".swatch").forEach(function (b) {
    b.addEventListener("click", function () {
      applyHl(b.getAttribute("data-hl-name"));
    });
  });

  /* --- clipboard helper ------------------------------------------------- */
  function copyText(text, btn) {
    function done() {
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = old; }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  /* --- instant search (150 ms debounce, plain result line) -------------- */
  var q = $("#q"), topicSel = $("#topic"), resultsEl = $("#results"),
      metaEl = $("#searchmeta"), timer = null, seq = 0;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function renderHits(data) {
    var html = data.hits.map(function (h) {
      var meta = ["&middot; read by " + h.team_count + " team" + (h.team_count === 1 ? "" : "s"),
                  "&middot; " + h.school_count + " school" + (h.school_count === 1 ? "" : "s")];
      (h.topic_codes || []).forEach(function (c) {
        meta.push('&middot; <a href="/topic/' + esc(c) + '">' + esc(c) + "</a>");
      });
      if (h.source_pub_date) meta.push("&middot; " + esc(h.source_pub_date));
      if (h.is_analytic) meta.push("&middot; analytic");
      (h.flags || []).forEach(function (f) {
        meta.push('<span class="glyph" title="' + esc(f.label) + ": " + esc(f.detail) + '">' + esc(f.code) + "</span>");
      });
      return '<article class="result" data-id="' + h.card_id + '" tabindex="-1">' +
        '<h3 class="tag"><a href="/card/' + h.card_id + '">' + esc(h.tag || "(untagged card)") + "</a></h3>" +
        '<p class="citeline">' + (h.cite ? '<span class="shortcite">' + esc(h.cite) + "</span> " : "") +
        '<span class="meta">' + meta.join(" ") + "</span></p>" +
        (h.snippet_html ? '<p class="snippet">' + h.snippet_html + "</p>" : "") +
        "</article>";
    }).join("");
    resultsEl.innerHTML = html;
    metaEl.textContent = data.total + " results · " + Math.round(data.elapsed_ms) + " ms";
    var empty = $(".empty");
    if (empty) empty.hidden = true;
  }

  function runSearch() {
    var query = q.value.trim(), topic = topicSel ? topicSel.value : "";
    if (!query && !topic) {
      resultsEl.innerHTML = ""; metaEl.textContent = "";
      var empty = $(".empty");
      if (empty) empty.hidden = false;
      return;
    }
    var my = ++seq;
    var url = "/search?format=json&q=" + encodeURIComponent(query) +
              "&topic=" + encodeURIComponent(topic);
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      if (my === seq) renderHits(data);
    }).catch(function () {});
  }

  if (q && resultsEl) {
    q.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(runSearch, 150);
    });
    if (topicSel) topicSel.addEventListener("change", runSearch);
  }

  /* --- keyboard map (spec 8.6, documented in the footer) ----------------- */
  var selIdx = -1;
  function results() { return $$(".result"); }
  function select(i) {
    var rows = results();
    if (!rows.length) return;
    if (selIdx >= 0 && rows[selIdx]) rows[selIdx].classList.remove("sel");
    selIdx = Math.max(0, Math.min(i, rows.length - 1));
    rows[selIdx].classList.add("sel");
    rows[selIdx].scrollIntoView({ block: "nearest" });
  }
  function selectedId() {
    var rows = results();
    return selIdx >= 0 && rows[selIdx] ? rows[selIdx].getAttribute("data-id") : null;
  }
  function cardIdHere() {
    var el = $("#card");
    return selectedId() || (el ? el.getAttribute("data-card-id") : null);
  }
  function downloadDocx(id) {
    var f = document.createElement("form");
    f.method = "post";
    f.action = "/export/docx?hl=" + (load("hl") || "green");
    [["ids", id], ["preset", "house"], ["hl", load("hl") || "green"]].forEach(function (p) {
      var i = document.createElement("input");
      i.type = "hidden"; i.name = p[0]; i.value = p[1];
      f.appendChild(i);
    });
    document.body.appendChild(f);
    f.submit();
  }

  document.addEventListener("keydown", function (e) {
    var t = e.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
                                     t.tagName === "SELECT" || t.isContentEditable);
    if (e.key === "Escape" && typing) { t.blur(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") {
      if (q) { e.preventDefault(); q.focus(); q.select(); }
    } else if (e.key === "j" || e.key === "ArrowDown") {
      if (results().length) { e.preventDefault(); select(selIdx + 1); }
    } else if (e.key === "k" || e.key === "ArrowUp") {
      if (results().length) { e.preventDefault(); select(selIdx - 1); }
    } else if (e.key === "Enter") {
      var id = selectedId();
      if (id) { e.preventDefault(); window.location.href = "/card/" + id; }
    } else if (e.key === "y") {
      var cid = cardIdHere();
      if (cid) {
        var local = $("#spoken-text");
        if (local && !selectedId()) copyText(local.textContent.trim());
        else fetch("/card/" + cid + "/spoken.txt").then(function (r) {
          return r.text();
        }).then(function (txt) { copyText(txt); }).catch(function () {});
      }
    } else if (e.key === "d") {
      var did = cardIdHere();
      if (did) { e.preventDefault(); downloadDocx(did); }
    } else if (e.key === "f") {
      if (topicSel) { e.preventDefault(); topicSel.focus(); }
    }
  });

  /* --- card page: parts legend, first visit only ------------------------- */
  var legend = $("#legend");
  if (legend && !load("legendDismissed")) {
    legend.hidden = false;
    var dbtn = $("#legend-dismiss");
    if (dbtn) dbtn.addEventListener("click", function () {
      legend.hidden = true;
      store("legendDismissed", "1");
    });
  }

  /* --- card page: per-team highlighting toggle + consensus mode ---------- */
  $$(".variant-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var which = btn.getAttribute("data-variant");
      $$(".variant-btn").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      $$(".variant-body, .consensus-body").forEach(function (div) {
        div.hidden = div.getAttribute("data-variant") !== which;
      });
    });
  });

  /* --- card page: reading view (hide minimized text) --------------------- */
  var minBtn = $("#toggle-min");
  if (minBtn) minBtn.addEventListener("click", function () {
    var bodies = $(".variant-bodies");
    var on = bodies.classList.toggle("hide-min");
    minBtn.textContent = on ? "Show minimized text" : "Hide minimized text";
  });

  /* --- card page: copy spoken text --------------------------------------- */
  var copyBtn = $("#copy-spoken");
  if (copyBtn) copyBtn.addEventListener("click", function () {
    var el = $("#spoken-text");
    copyText(el ? el.textContent.trim() : "", copyBtn);
  });

  /* --- card page: route the add-to-box form at the chosen box ------------ */
  var boxForm = $("#box-add-form"), boxSel = $("#box-select");
  if (boxForm && boxSel) {
    boxForm.addEventListener("submit", function () {
      boxForm.action = "/boxes/" + boxSel.value + "/add";
    });
  }
})();
