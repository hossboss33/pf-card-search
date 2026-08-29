/* PF card search, public site.
 *
 * The whole application: it opens a prebuilt SQLite file over HTTP range
 * requests (sql.js-httpvfs, in a Worker) and runs real FTS5 queries against it.
 * No framework, no build step, no server. Spec 7 for the query language and
 * ranking, spec 8 for the look and the interactions.
 */
(function () {
  "use strict";

  var PAGE = 30;
  var DEBOUNCE_MS = 150;
  /* snippet() markers: two control characters, so they survive HTML
     escaping and cannot collide with anything in the card text. */
  var SNIP_OPEN = "\u0001";
  var SNIP_CLOSE = "\u0002";

  var HL_COLORS = {
    green: "#00FF00",
    yellow: "#FFFF00",
    turquoise: "#00FFFF"
  };

  var db = null;          // { query(sql, params) }
  var cfg = { db: "db/cards.sqlite", requestChunkSize: 1024, pageSize: PAGE };
  var meta = {};          // meta table as a plain object
  var topics = [];        // topics table, with a computed .status
  var today = new Date().toISOString().slice(0, 10);

  var state = {
    q: "",
    offset: 0,
    total: 0,
    rows: [],
    sel: -1,
    running: false,
    seq: 0
  };

  var el = {};
  function $(id) { return document.getElementById(id); }

  /* ------------------------------------------------------------------ util */

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function plural(n, one, many) { return n === 1 ? one : many; }

  function num(v) {
    var n = parseInt(v, 10);
    if (isNaN(n)) return null;
    return n;
  }

  /* LIKE pattern for a user string; % _ \ are escaped, used with ESCAPE '\' */
  function likeContains(s) {
    return "%" + String(s).replace(/([\\%_])/g, "\\$1") + "%";
  }

  /* ------------------------------------------------------- query language */

  /* Split on whitespace, keeping "quoted phrases" (and field:"quoted" ) whole. */
  function tokenize(q) {
    var out = [], cur = "", inQuote = false, i;
    for (i = 0; i < q.length; i++) {
      var ch = q.charAt(i);
      if (ch === '"') { inQuote = !inQuote; cur += ch; continue; }
      if (!inQuote && /\s/.test(ch)) {
        if (cur) { out.push(cur); cur = ""; }
        continue;
      }
      cur += ch;
    }
    if (cur) out.push(cur);
    return out;
  }

  function unquote(s) {
    if (s.length >= 2 && s.charAt(0) === '"' && s.charAt(s.length - 1) === '"') {
      return s.slice(1, -1);
    }
    return s.replace(/"/g, "");
  }

  /* Every user term reaches FTS5 as a quoted string literal, with embedded
     quotes doubled. That makes a syntax error or an injected operator
     impossible: FTS5 reads the whole thing as text. */
  function ftsQuote(term) {
    return '"' + String(term).replace(/"/g, '""') + '"';
  }

  var SORTS = { reads: 1, recent: 1, relevance: 1, length: 1 };

  /* Parse a query into { fts, filters, sort, terms }.
     Anything unrecognized degrades to a plain search term. Never throws. */
  function parseQuery(q) {
    var pq = { fts: null, filters: {}, sort: "relevance", pos: [], neg: [] };
    var toks = tokenize(q || "");

    for (var i = 0; i < toks.length; i++) {
      var t = toks[i];
      if (!t) continue;

      var negated = false;
      if (t.charAt(0) === "-" && t.length > 1) { negated = true; t = t.slice(1); }

      var m = /^([a-zA-Z_]+):(.*)$/.exec(t);
      var handled = false;

      if (m && m[2] !== "") {
        var field = m[1].toLowerCase();
        var val = unquote(m[2]);
        handled = true;
        switch (field) {
          case "topic":
            (pq.filters.topic = pq.filters.topic || []).push(val);
            break;
          case "season":
            if (num(val) !== null) pq.filters.season = num(val);
            else handled = false;
            break;
          case "cite":
          case "author":
            (pq.filters.cite = pq.filters.cite || []).push(val);
            break;
          case "year":
            var y = val.replace(/[^0-9]/g, "");
            if (y.length === 4) y = y.slice(2);
            if (y.length === 2) pq.filters.year = y; else handled = false;
            break;
          case "block":
            pq.filters.block = val;
            break;
          case "before":
            if (/^\d{4}-\d{2}-\d{2}$/.test(val)) pq.filters.before = val;
            else handled = false;
            break;
          case "after":
            if (/^\d{4}-\d{2}-\d{2}$/.test(val)) pq.filters.after = val;
            else handled = false;
            break;
          case "is":
            if (val.toLowerCase() === "analytic") pq.filters.is_analytic = true;
            else handled = false;
            break;
          case "min_reads":
            if (num(val) !== null) pq.filters.min_reads = num(val);
            else handled = false;
            break;
          case "sort":
            if (SORTS[val.toLowerCase()]) pq.sort = val.toLowerCase();
            else handled = false;
            break;
          default:
            handled = false;
        }
      }

      if (!handled) {
        var term = unquote(t).trim();
        if (!term) continue;
        (negated ? pq.neg : pq.pos).push(term);
      }
    }

    var pos = pq.pos.map(ftsQuote);
    var neg = pq.neg.map(ftsQuote);
    if (pos.length) {
      pq.fts = pos.join(" AND ");
      /* FTS5's NOT is binary, so exclusions need something on the left. With no
         positive term there is nothing to subtract from and they are dropped
         rather than raising. */
      if (neg.length) pq.fts = "(" + pq.fts + ") NOT (" + neg.join(" OR ") + ")";
    }
    return pq;
  }

  /* -------------------------------------------------------- topic helpers */

  function topicStatus(t) {
    if (t.ends && t.ends < today) return "past";
    if (t.starts && t.starts > today) return "future";
    if (t.starts && t.ends) return "present";
    return "past";
  }

  function resolveTopicToken(tok) {
    var v = String(tok).toLowerCase();
    var i, out = [];
    if (v === "present" || v === "past" || v === "future") {
      for (i = 0; i < topics.length; i++) {
        if (topics[i].status === v) out.push(topics[i].code);
      }
      return out;
    }
    for (i = 0; i < topics.length; i++) {
      if (String(topics[i].code).toLowerCase() === v) return [topics[i].code];
    }
    return [tok]; /* unknown code: let it match nothing rather than everything */
  }

  /* ------------------------------------------------------------ SQL build */

  function buildFilters(pq) {
    var where = [], params = [], i;
    var onlyDefault = true;   // true while the only predicate is the default
                              // analytics exclusion (see the fast path)

    if (pq.filters.is_analytic) { where.push("c.is_analytic = 1"); onlyDefault = false; }
    else where.push("(c.is_analytic = 0 OR c.is_analytic IS NULL)");

    var codes = null;
    if (pq.filters.topic) {
      codes = [];
      for (i = 0; i < pq.filters.topic.length; i++) {
        codes = codes.concat(resolveTopicToken(pq.filters.topic[i]));
      }
    }
    if (pq.filters.season !== undefined) {
      var seasonCodes = [];
      for (i = 0; i < topics.length; i++) {
        if (Number(topics[i].season) === pq.filters.season) seasonCodes.push(topics[i].code);
      }
      codes = codes === null ? seasonCodes
        : codes.filter(function (c) { return seasonCodes.indexOf(c) !== -1; });
    }
    if (codes !== null) {
      if (!codes.length) {
        onlyDefault = false; where.push("0");
      } else {
        var ors = [];
        for (i = 0; i < codes.length; i++) {
          ors.push("c.topic_codes LIKE ? ESCAPE '\\'");
          params.push(likeContains('"' + codes[i] + '"'));
        }
        onlyDefault = false; where.push("(" + ors.join(" OR ") + ")");
      }
    }

    if (pq.filters.cite) {
      for (i = 0; i < pq.filters.cite.length; i++) {
        onlyDefault = false; where.push("(c.cite LIKE ? ESCAPE '\\' OR c.fullcite LIKE ? ESCAPE '\\')");
        params.push(likeContains(pq.filters.cite[i]));
        params.push(likeContains(pq.filters.cite[i]));
      }
    }
    if (pq.filters.year) {
      onlyDefault = false; where.push("c.cite LIKE ? ESCAPE '\\'");
      params.push(likeContains(pq.filters.year));
    }
    if (pq.filters.block) {
      onlyDefault = false; where.push("c.block LIKE ? ESCAPE '\\'");
      params.push(likeContains(pq.filters.block));
    }
    if (pq.filters.before) {
      onlyDefault = false; where.push("c.source_pub_date IS NOT NULL AND c.source_pub_date < ?");
      params.push(pq.filters.before);
    }
    if (pq.filters.after) {
      onlyDefault = false; where.push("c.source_pub_date IS NOT NULL AND c.source_pub_date > ?");
      params.push(pq.filters.after);
    }
    if (pq.filters.min_reads !== undefined) {
      onlyDefault = false; where.push("COALESCE(c.team_count, 0) >= ?");
      params.push(pq.filters.min_reads);
    }
    return { where: where, params: params, onlyDefault: onlyDefault };
  }

  function orderBy(pq, hasFts) {
    switch (pq.sort) {
      case "reads": return "ORDER BY COALESCE(c.team_count,0) DESC, COALESCE(c.school_count,0) DESC, c.id";
      case "recent": return "ORDER BY c.source_pub_date IS NULL, c.source_pub_date DESC, c.id";
      case "length": return "ORDER BY length(COALESCE(c.body_text,'')) DESC, c.id";
      default:
        return hasFts
          ? "ORDER BY rank"
          : "ORDER BY COALESCE(c.team_count,0) DESC, c.id";
    }
  }

  var COLS =
    "c.id, c.tag, c.cite, c.fullcite, c.is_analytic, " +
    "COALESCE(c.team_count,0) AS team_count, COALESCE(c.school_count,0) AS school_count, " +
    "c.topic_codes, c.source_pub_date, length(COALESCE(c.body_text,'')) AS body_len";

  function buildSearchSql(pq, limit, offset) {
    var f = buildFilters(pq);
    var params, sql, countSql, countParams;
    var hasFts = !!pq.fts;

    if (hasFts) {
      var snip = "snippet(card_fts, 3, char(1), char(2), '…', 20) AS snip";
      var rank = "bm25(card_fts, 5.0, 3.0, 2.0, 1.0) AS rank";

      /* The default analytics exclusion alone does not disqualify the fast
         path: the shipped DB normally contains no analytics at all (the
         builder excludes them), and the outer 30-row join re-applies the
         predicate for builds that do ship them. */
      if (f.onlyDefault && pq.sort === "relevance" && !pq.filters.is_analytic) {
        /* Fast path, and it matters a lot over HTTP: rank and page inside the
           FTS table first, then join `cards` for only the rows we display.
           The obvious "FROM card_fts JOIN cards ... LIMIT 30" reads a cards
           row for every match — 1,445 scattered page reads for a common term,
           each one a network round trip. This reads 30. The count likewise
           never touches `cards`. */
        /* The inner query must touch ONLY the FTS index: card_fts is an
           external-content table, so snippet() reads card bodies — inside
           the ranking scan that means every match's body before LIMIT, and
           re-entering the FTS table for the outer 30 makes the planner
           recompute the MATCH for all of them (measured: raw ranking 53 ms,
           the snippet()-bearing shapes 20+ s). So no snippet() here at all:
           rank on index data, join `cards` for the 30 shown rows, and build
           the snippet in JS from body_text, which those rows carry anyway. */
        var inner = "SELECT rowid AS rid, " + rank +
                    " FROM card_fts WHERE card_fts MATCH ?" +
                    " ORDER BY rank LIMIT ? OFFSET ?";
        sql = "SELECT " + COLS + ", c.body_text AS body_text, " +
              "'' AS snip, f.rank AS rank " +
              "FROM (" + inner + ") f JOIN cards c ON c.id = f.rid " +
              "WHERE (c.is_analytic = 0 OR c.is_analytic IS NULL) " +
              "ORDER BY f.rank";
        params = [pq.fts, limit, offset];
        countSql = "SELECT count(*) AS n FROM card_fts WHERE card_fts MATCH ?";
        countParams = [pq.fts];
        return { sql: sql, params: params,
                 countSql: countSql, countParams: countParams };
      }

      /* Filtered or non-relevance sorts need `cards` in scope before LIMIT. */
      var from = "FROM card_fts JOIN cards c ON c.id = card_fts.rowid " +
                 "WHERE card_fts MATCH ?" +
                 (f.where.length ? " AND " + f.where.join(" AND ") : "");
      params = [pq.fts].concat(f.params);
      sql = "SELECT " + COLS + ", " + snip + ", " + rank + " " + from + " " +
            orderBy(pq, true) + " LIMIT ? OFFSET ?";
      countSql = "SELECT count(*) AS n " + from;
      countParams = params.slice();
      params = params.concat([limit, offset]);
    } else {
      var from2 = "FROM cards c" +
                  (f.where.length ? " WHERE " + f.where.join(" AND ") : "");
      params = f.params.slice();
      sql = "SELECT " + COLS + ", '' AS snip, 0 AS rank " + from2 + " " +
            orderBy(pq, false) + " LIMIT ? OFFSET ?";
      countSql = "SELECT count(*) AS n " + from2;
      countParams = params.slice();
      params = params.concat([limit, offset]);
    }
    return { sql: sql, params: params, countSql: countSql, countParams: countParams };
  }

  /* ------------------------------------------------------------ rendering */

  function snippetHtml(snip) {
    if (!snip) return "";
    return esc(snip)
      .split(SNIP_OPEN).join("<b>")
      .split(SNIP_CLOSE).join("</b>");
  }

  /* Client-side snippet for the fast search path (which fetches body_text for
     the shown rows instead of running snippet() — see buildSearchSql). Finds
     the first query-term hit, slices ~20 words either side, and marks every
     term occurrence with the same control characters snippetHtml expects. */
  function makeSnippet(body, terms) {
    if (!body) return "";
    var words = body.split(/\s+/);
    var lows = words.map(function (w) { return w.toLowerCase(); });
    var stems = terms.map(function (t) { return t.toLowerCase().replace(/[^\w]/g, ""); })
                     .filter(function (t) { return t.length > 1; });
    var hit = -1;
    for (var i = 0; i < lows.length && hit < 0; i++) {
      for (var j = 0; j < stems.length; j++) {
        if (lows[i].indexOf(stems[j]) === 0) { hit = i; break; }
      }
    }
    if (hit < 0) hit = 0;
    var a = Math.max(0, hit - 8), b = Math.min(words.length, hit + 32);
    var out = [];
    for (var k = a; k < b; k++) {
      var marked = false;
      for (var m = 0; m < stems.length; m++) {
        if (lows[k].indexOf(stems[m]) === 0) { marked = true; break; }
      }
      out.push(marked ? SNIP_OPEN + words[k] + SNIP_CLOSE : words[k]);
    }
    return (a > 0 ? "…" : "") + out.join(" ") + (b < words.length ? "…" : "");
  }

  function topicList(json) {
    if (!json) return [];
    try {
      var v = JSON.parse(json);
      return Array.isArray(v) ? v : [];
    } catch (e) { return []; }
  }

  function provenanceLine(row) {
    var bits = [];
    bits.push("read by " + row.team_count + " " + plural(row.team_count, "team", "teams"));
    bits.push(row.school_count + " " + plural(row.school_count, "school", "schools"));
    var codes = topicList(row.topic_codes);
    if (codes.length) bits.push(esc(codes.join(", ")));
    if (row.is_analytic) bits.push("analytic");
    return bits.join(" &middot; ");
  }

  function renderRows(rows, append) {
    var html = rows.map(function (r) {
      return '<article class="result" data-id="' + r.id + '">' +
        '<div class="tag"><a href="#/card/' + r.id + '">' +
          esc(r.tag || "(untagged card)") + "</a></div>" +
        '<div class="citeline"><strong class="shortcite">' + esc(r.cite || "") +
          "</strong>" +
          (r.fullcite ? ' <span class="fullcite">' + esc(r.fullcite) + "</span>" : "") +
        "</div>" +
        '<div class="provenance-line">' + provenanceLine(r) + "</div>" +
        (r.snip ? '<div class="snippet">' + snippetHtml(r.snip) + "</div>" : "") +
      "</article>";
    }).join("");
    if (append) el.results.insertAdjacentHTML("beforeend", html);
    else el.results.innerHTML = html;
  }

  function renderPager() {
    var shown = state.rows.length;
    if (shown && shown < state.total) {
      el.pager.innerHTML = '<button type="button" id="more">Show ' +
        Math.min(PAGE, state.total - shown) + " more</button>" +
        '<span class="meta">' + shown + " of " + state.total + " shown</span>";
      $("more").addEventListener("click", function () { loadMore(); });
    } else {
      el.pager.innerHTML = "";
    }
  }

  /* --------------------------------------------------------------- search */

  function runSearch(q, append) {
    if (!db) return;
    var seq = ++state.seq;
    var offset = append ? state.rows.length : 0;
    var pq = parseQuery(q);
    var built = buildSearchSql(pq, PAGE, offset);
    var t0 = (window.performance || Date).now();
    /* The database is read over HTTP a page at a time, so a query has real
       latency. Say so, rather than leaving the line blank and looking dead. */
    if (!append) el.searchmeta.textContent = "Searching\u2026";

    return db.query(built.sql, built.params).then(function (rows) {
      if (seq !== state.seq) return;
      /* Fast-path rows carry body_text instead of a server-built snippet. */
      for (var ri = 0; ri < rows.length; ri++) {
        if (!rows[ri].snip && rows[ri].body_text) {
          rows[ri].snip = makeSnippet(rows[ri].body_text, pq.pos);
        }
      }
      return db.query(built.countSql, built.countParams).then(function (cnt) {
        if (seq !== state.seq) return;
        var ms = Math.round(((window.performance || Date).now() - t0));
        state.total = (cnt && cnt[0] && cnt[0].n) || 0;
        state.rows = append ? state.rows.concat(rows) : rows;
        state.sel = -1;

        renderRows(rows, append);
        renderPager();
        el.searchmeta.textContent =
          state.total + " " + plural(state.total, "result", "results") + " · " + ms + " ms";
        el.empty.hidden = true;
        if (!state.total) {
          el.results.innerHTML = '<p class="meta boot-line">No cards matched. ' +
            "Try fewer words, or drop a filter.</p>";
        }
      });
    }).catch(function (err) {
      if (seq !== state.seq) return;
      /* A query should never fail: every term is quoted before it reaches FTS5.
         If one does, say so plainly instead of leaving a stale result list. */
      el.results.innerHTML = "";
      el.pager.innerHTML = "";
      el.searchmeta.textContent = "That search could not be run: " + (err.message || err);
    });
  }

  function loadMore() { runSearch(state.q, true); }

  function showEmptyState() {
    state.rows = [];
    state.total = 0;
    state.seq++;
    el.results.innerHTML = "";
    el.pager.innerHTML = "";
    el.searchmeta.textContent = "";
    el.empty.hidden = false;
  }

  var debounceTimer = null;
  function onInput() {
    var q = el.q.value;
    state.q = q;
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      var hash = q.trim() ? "#/q/" + encodeURIComponent(q) : "#/";
      if (location.hash !== hash) {
        history.replaceState(null, "", hash);
      }
      if (!q.trim()) showEmptyState();
      else runSearch(q, false);
    }, DEBOUNCE_MS);
  }

  /* ------------------------------------------------------------ sanitizer */

  var ALLOWED = { H1: 1, H2: 1, H3: 1, H4: 1, P: 1, U: 1, STRONG: 1, EM: 1, MARK: 1, SPAN: 1, BR: 1 };
  var DROP_WHOLE = { SCRIPT: 1, STYLE: 1, TEMPLATE: 1, IFRAME: 1, OBJECT: 1, EMBED: 1 };

  function copyChildren(src, dst) {
    for (var n = src.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) {
        dst.appendChild(document.createTextNode(n.nodeValue));
      } else if (n.nodeType === 1) {
        var tag = n.tagName.toUpperCase();
        if (DROP_WHOLE[tag]) continue;
        if (ALLOWED[tag]) {
          var out = document.createElement(tag.toLowerCase());
          var cls = n.getAttribute("class");
          if (cls) {
            cls = cls.replace(/[^A-Za-z0-9 _-]/g, "").slice(0, 64).trim();
            if (cls) out.className = cls;
          }
          dst.appendChild(out);
          copyChildren(n, out);
        } else {
          copyChildren(n, dst); /* unknown tag dropped, its text kept */
        }
      }
    }
  }

  /* The database is treated as untrusted input: markup_html is reparsed and
     rebuilt from an allowlist of tags, with only a scrubbed class surviving. */
  function sanitizeMarkup(html) {
    var host = document.createElement("div");
    var doc = new DOMParser().parseFromString(
      "<body><div id=\"pfroot\"></div></body>", "text/html");
    var root = doc.getElementById("pfroot");
    root.innerHTML = String(html || "");
    copyChildren(root, host);
    return host;
  }

  /* ------------------------------------------------------------ card view */

  var currentCard = null;

  function flat(s) {
    return String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  /* Parsed markup usually opens with the Heading-4 tag and the cite paragraph,
     because that is how the card sat in the speech doc. The card page already
     renders both above the body, so drop the repeat instead of showing the tag
     and cite twice. Only exact repeats go; anything else stays. */
  function dropRepeatedHead(host, card) {
    var tag = flat(card.tag);
    var cite = flat(card.cite);
    var full = flat(card.fullcite);
    var both = (cite + " " + full).trim();
    for (var guard = 0; guard < 3; guard++) {
      var el0 = host.firstElementChild;
      if (!el0) return;
      var txt = flat(el0.textContent);
      if (!txt) { host.removeChild(el0); continue; }
      var isHeading = /^H[1-4]$/.test(el0.tagName);
      var repeat = (isHeading && tag && txt === tag) ||
                   (cite && (txt === cite || txt === full || txt === both));
      if (!repeat) return;
      host.removeChild(el0);
    }
  }

  function renderCard(id) {
    if (!db) return;
    var sql = "SELECT id, tag, cite, fullcite, body_text, markup_html, summary, spoken, " +
      "source_url, source_pub_date, is_analytic, COALESCE(team_count,0) AS team_count, " +
      "COALESCE(school_count,0) AS school_count, topic_codes, pocket, hat, block " +
      "FROM cards WHERE id = ?";
    return db.query(sql, [id]).then(function (rows) {
      var c = rows && rows[0];
      if (!c) {
        el.card.innerHTML = '<p class="meta">No card with id ' + esc(String(id)) + " in this index.</p>";
        el.cardExtras.innerHTML = "";
        currentCard = null;
        return;
      }
      currentCard = c;
      document.title = (c.tag ? c.tag.slice(0, 70) : "Card") + " · PF card search";

      el.card.innerHTML = "";
      var h1 = document.createElement("h1");
      h1.className = "tag";
      h1.textContent = c.tag || "(untagged card)";
      el.card.appendChild(h1);

      var cite = document.createElement("p");
      cite.className = "citeline";
      var short = document.createElement("strong");
      short.className = "shortcite";
      short.textContent = c.cite || "";
      cite.appendChild(short);
      if (c.fullcite) {
        cite.appendChild(document.createTextNode(" "));
        var full = document.createElement("span");
        full.className = "fullcite";
        full.textContent = c.fullcite;
        cite.appendChild(full);
      }
      el.card.appendChild(cite);

      if (c.is_analytic) {
        var an = document.createElement("p");
        an.className = "meta";
        an.textContent = "Analytic. Asserted without evidence; no card body was disclosed.";
        el.card.appendChild(an);
      }

      var body = document.createElement("div");
      body.className = "body";
      body.id = "card-body";
      if (c.markup_html) {
        var clean = sanitizeMarkup(c.markup_html);
        dropRepeatedHead(clean, c);
        while (clean.firstChild) body.appendChild(clean.firstChild);
      } else if (c.body_text) {
        var p = document.createElement("p");
        p.textContent = c.body_text;
        body.appendChild(p);
      }
      if (readingView()) body.classList.add("hide-min");
      el.card.appendChild(body);

      renderExtras(c);
      maybeShowLegend();
    }).catch(function (err) {
      el.card.innerHTML = '<p class="meta">That card could not be read: ' +
        esc(err.message || String(err)) + "</p>";
    });
  }

  function renderExtras(c) {
    var codes = topicList(c.topic_codes);
    var h = '<h2 class="h-quiet">Lineage</h2><p class="meta">read by ' +
      c.team_count + " " + plural(c.team_count, "team", "teams") + " &middot; " +
      c.school_count + " " + plural(c.school_count, "school", "schools");
    for (var i = 0; i < codes.length; i++) {
      h += ' &middot; <a href="#/q/' + encodeURIComponent("topic:" + codes[i]) + '">' +
        esc(codes[i]) + "</a>";
    }
    h += "</p>";

    var path = [c.pocket, c.hat, c.block].filter(function (x) { return !!x; });
    if (path.length) {
      h += '<h2 class="h-quiet">Where it sat in the file</h2><p class="meta">' +
        esc(path.join(" › ")) + "</p>";
    }
    if (c.source_url) {
      h += '<h2 class="h-quiet">Source</h2><p class="meta"><a href="' + esc(c.source_url) +
        '" rel="noopener nofollow">' + esc(c.source_url) + "</a>" +
        (c.source_pub_date ? " &middot; published " + esc(c.source_pub_date) : "") + "</p>";
    }
    if (c.spoken) {
      var words = c.spoken.split(/\s+/).filter(function (w) { return !!w; }).length;
      h += '<h2 class="h-quiet">Spoken text</h2><p class="meta">' + words + " " +
        plural(words, "word", "words") + " highlighted, about " +
        readTime(words) + " to read aloud.</p>";
    }
    el.cardExtras.innerHTML = h;
  }

  function readTime(words) {
    var secs = Math.round((words / 250) * 60);
    var m = Math.floor(secs / 60), s = secs % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  /* ------------------------------------------------------------ settings */

  function lsGet(k, dflt) {
    try { var v = localStorage.getItem(k); return v === null ? dflt : v; }
    catch (e) { return dflt; }
  }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }

  function applyHighlight(name) {
    if (!HL_COLORS[name]) name = "green";
    document.documentElement.setAttribute("data-hl", name);
    document.documentElement.style.setProperty("--hl", HL_COLORS[name]);
    var sw = document.querySelectorAll(".swatch");
    for (var i = 0; i < sw.length; i++) {
      var on = sw[i].getAttribute("data-hl-name") === name;
      sw[i].classList.toggle("active", on);
      sw[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
    lsSet("pf-hl", name);
  }

  function readingView() { return lsGet("pf-reading", "0") === "1"; }

  function applyReadingView() {
    var on = readingView();
    var body = $("card-body");
    if (body) body.classList.toggle("hide-min", on);
    el.toggleMin.textContent = on ? "Show minimized text" : "Reading view";
    el.toggleMin.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function maybeShowLegend() {
    if (lsGet("pf-legend", "0") === "1") { el.legend.hidden = true; return; }
    el.legend.hidden = false;
  }

  /* ------------------------------------------------------------- clipboard */

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      /* The async API is refused without a permission grant in some contexts;
         fall back to the selection trick rather than reporting a failure. */
      return navigator.clipboard.writeText(text).catch(function () {
        return legacyCopy(text);
      });
    }
    return legacyCopy(text);
  }

  function legacyCopy(text) {
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "readonly");
      ta.className = "visually-hidden";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error("copy blocked by the browser"));
    });
  }

  function copySpokenFor(id, statusEl) {
    return db.query("SELECT spoken, body_text FROM cards WHERE id = ?", [id])
      .then(function (rows) {
        var r = rows && rows[0];
        var text = (r && (r.spoken || r.body_text)) || "";
        if (!text) throw new Error("this card has no spoken text");
        return copyText(text).then(function () {
          if (statusEl) {
            statusEl.textContent = "Spoken text copied";
            setTimeout(function () { statusEl.textContent = ""; }, 2500);
          }
        });
      })
      .catch(function (err) {
        if (statusEl) statusEl.textContent = "Not copied: " + (err.message || err);
      });
  }

  /* ---------------------------------------------------------- selection */

  function resultNodes() {
    return el.results.querySelectorAll(".result");
  }

  function moveSel(delta) {
    var nodes = resultNodes();
    if (!nodes.length) return;
    var next = state.sel + delta;
    if (next < 0) next = 0;
    if (next > nodes.length - 1) next = nodes.length - 1;
    for (var i = 0; i < nodes.length; i++) nodes[i].classList.remove("sel");
    state.sel = next;
    nodes[next].classList.add("sel");
    nodes[next].scrollIntoView({ block: "nearest" });
  }

  function selectedId() {
    var nodes = resultNodes();
    if (state.sel < 0 || state.sel >= nodes.length) return null;
    return num(nodes[state.sel].getAttribute("data-id"));
  }

  /* -------------------------------------------------------------- router */

  function show(view) {
    el.viewSearch.hidden = view !== "search";
    el.viewCard.hidden = view !== "card";
    el.viewAbout.hidden = view !== "about";
    var vc = document.getElementById("view-connect");
    if (vc) vc.hidden = view !== "connect";
  }

  function route() {
    var h = location.hash.replace(/^#/, "");
    var m;
    if ((m = /^\/card\/(\d+)$/.exec(h))) {
      show("card");
      el.copyStatus.textContent = "";
      renderCard(num(m[1]));
      applyReadingView();
      window.scrollTo(0, 0);
      return;
    }
    if (h === "/connect") {
      show("connect");
      document.title = "Recent seasons \u00b7 PF card search";
      window.scrollTo(0, 0);
      return;
    }
    if (h === "/about") {
      show("about");
      document.title = "About · PF card search";
      window.scrollTo(0, 0);
      return;
    }
    show("search");
    document.title = "PF card search";
    if ((m = /^\/q\/(.*)$/.exec(h))) {
      var q = "";
      try { q = decodeURIComponent(m[1]); } catch (e) { q = m[1]; }
      if (q !== state.q || !state.rows.length) {
        el.q.value = q;
        state.q = q;
        if (q.trim()) runSearch(q, false);
        else showEmptyState();
      }
      return;
    }
    if (!state.q.trim()) showEmptyState();
  }

  /* ---------------------------------------------------------------- boot */

  function fail(detail) {
    el.fatal.hidden = false;
    el.fatalDetail.textContent = detail;
    el.viewSearch.hidden = true;
  }

  function renderStats(tbody) {
    var rows = [
      ["Canonical cards", meta.card_count],
      ["Analytics", meta.analytic_count],
      ["Teams", meta.team_count],
      ["Schools", meta.school_count],
      ["Seasons covered", meta.seasons_covered]
    ];
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      if (rows[i][1] === undefined || rows[i][1] === null || rows[i][1] === "") continue;
      html += "<tr><th scope=\"row\">" + esc(rows[i][0]) + "</th><td>" +
        esc(rows[i][1]) + "</td></tr>";
    }
    if (!html) html = '<tr><th scope="row">Cards</th><td>count not recorded</td></tr>';
    tbody.innerHTML = html;
  }

  function renderTopicPicker() {
    var groups = [
      ["present", "Present"],
      ["future", "Future"],
      ["past", "Past"]
    ];
    var html = '<option value="">All topics</option>';
    for (var g = 0; g < groups.length; g++) {
      var members = topics.filter(function (t) { return t.status === groups[g][0]; });
      if (!members.length) continue;
      members.sort(function (a, b) { return String(b.starts) < String(a.starts) ? -1 : 1; });
      html += '<optgroup label="' + groups[g][1] + '">';
      for (var i = 0; i < members.length; i++) {
        var t = members[i];
        var n = t.card_count === null || t.card_count === undefined ? 0 : t.card_count;
        html += '<option value="' + esc(t.code) + '">' + esc(t.code) +
          (t.slot ? " · " + esc(t.slot) : "") +
          " · " + esc(n) + " " + plural(Number(n), "card", "cards") + "</option>";
      }
      html += "</optgroup>";
    }
    el.topic.innerHTML = html;
  }

  function renderCurrentTopic() {
    var label = "Current topic";
    var pick = topics.filter(function (t) { return t.status === "present"; })[0];
    if (!pick) {
      /* Between topics, or the build predates the running topic: name the next
         one rather than showing an empty heading. */
      label = "Next topic";
      var future = topics.filter(function (t) { return t.status === "future"; });
      future.sort(function (a, b) { return String(a.starts) < String(b.starts) ? -1 : 1; });
      pick = future[0];
    }
    if (!pick) { el.currentTopic.innerHTML = ""; return; }
    var count = pick.card_count || 0;
    el.currentTopic.innerHTML = '<h2 class="h-quiet">' + label + "</h2><p>" +
      '<a href="#/q/' + encodeURIComponent("topic:" + pick.code) + '">' + esc(pick.code) + "</a>" +
      (pick.resolution ? " &middot; " + esc(pick.resolution) : "") +
      (count ? "" : ' <span class="meta">Announced. No cards disclosed yet.</span>') +
      "</p>";
  }

  function renderAbout() {
    if (meta.coverage_note) el.aboutCoverage.textContent = meta.coverage_note;
    if (meta.source_note) el.aboutSource.textContent = meta.source_note;
    if (meta.subset_note) {
      el.aboutSubset.textContent = meta.subset_note;
      el.aboutSubset.hidden = false;
    }
    if (meta.built_at) {
      el.aboutBuilt.textContent = "Database built " + meta.built_at + ".";
    }
    renderStats(el.aboutStats);
  }

  function merge(j) {
    for (var k in j) if (Object.prototype.hasOwnProperty.call(j, k)) cfg[k] = j[k];
  }

  /* Two configs: the hand-written site one, then the one the builder emits
     next to the database. The builder's wins, because only it knows whether
     the DB had to be split into chunks (a full-corpus DB exceeds GitHub's
     100 MB per-file limit, so it ships as parts). */
  function loadConfig() {
    return fetch("config.json", { cache: "no-cache" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(merge)
      .catch(function () { /* defaults are fine */ })
      .then(function () {
        var dbCfg = (cfg.db || "db/cards.sqlite").replace(/[^/]+$/, "config.json");
        return fetch(dbCfg, { cache: "no-cache" })
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(merge)
          .catch(function () { /* single-file mode */ });
      });
  }

  function openDb() {
    var conf = {
      serverMode: cfg.serverMode === "chunked" ? "chunked" : "full",
      url: cfg.db,
      requestChunkSize: cfg.requestChunkSize || 1024
    };
    if (conf.serverMode === "chunked") {
      /* The worker fetches urlPrefix + a zero-padded part index. */
      conf.urlPrefix = cfg.urlPrefix;
      conf.suffixLength = cfg.suffixLength || 3;
      conf.urlSuffix = cfg.urlSuffix || "";
      conf.serverChunkSize = cfg.serverChunkSize;
      conf.databaseLengthBytes = cfg.databaseLengthBytes;
      delete conf.url;
    }
    return httpvfs.createDbWorker(
      [{ from: "inline", config: conf }],
      "vendor/sqlite.worker.v2.js",
      "vendor/sql-wasm.wasm"
    );
  }

  /* The database is read over HTTP a page at a time. The FIRST full-text
     query has to pull in the FTS index structure, which measured ~13 s on the
     deployed site; every query after it ran in ~1.5 s. So pay that cost here,
     in the background, while the reader is still looking at the empty state —
     by the time they type, the structural pages are cached.

     Deliberately fire-and-forget: it must never block the UI, surface an
     error, or disturb a real search the reader has already started. */
  function warmIndex() {
    if (!db) return;
    setTimeout(function () {
      if (state.q.trim()) return;        // reader beat us to it
      /* A term that matches nothing: the query still walks the FTS segment
         b-tree (the expensive structural pages), but 'the' would have pulled
         a doclist covering nearly every card — megabytes — and every real
         query queues behind this one in the worker. */
      db.query("SELECT rowid FROM card_fts WHERE card_fts MATCH ? LIMIT 1",
               ["zzzqvwxk"])
        .catch(function () { /* warming is best-effort */ });
    }, 300);
  }

  function loadMeta() {
    return db.query("SELECT key, value FROM meta", []).then(function (rows) {
      for (var i = 0; i < rows.length; i++) meta[rows[i].key] = rows[i].value;
    }).catch(function () { /* meta is optional chrome, not the app */ });
  }

  function loadTopics() {
    return db.query(
      "SELECT code, season, slot, resolution, starts, ends, " +
      "COALESCE(card_count,0) AS card_count FROM topics", []
    ).then(function (rows) {
      topics = rows.map(function (t) { t.status = topicStatus(t); return t; });
    }).catch(function () { topics = []; });
  }

  function wire() {
    el.searchform.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (debounceTimer) clearTimeout(debounceTimer);
      state.q = el.q.value;
      var hash = state.q.trim() ? "#/q/" + encodeURIComponent(state.q) : "#/";
      if (location.hash !== hash) history.replaceState(null, "", hash);
      if (state.q.trim()) runSearch(state.q, false); else showEmptyState();
    });
    el.q.addEventListener("input", onInput);

    el.topic.addEventListener("change", function () {
      var code = el.topic.value;
      var q = el.q.value.replace(/(^|\s)-?topic:("[^"]*"|\S*)/g, " ").trim();
      if (code) q = (q + " topic:" + code).trim();
      el.q.value = q;
      state.q = q;
      if (debounceTimer) clearTimeout(debounceTimer);
      var hash = q.trim() ? "#/q/" + encodeURIComponent(q) : "#/";
      if (location.hash !== hash) history.replaceState(null, "", hash);
      if (q.trim()) runSearch(q, false); else showEmptyState();
    });

    el.helpToggle.addEventListener("click", function () {
      var open = el.syntax.hidden;
      el.syntax.hidden = !open;
      el.helpToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });

    var sw = document.querySelectorAll(".swatch");
    for (var i = 0; i < sw.length; i++) {
      sw[i].addEventListener("click", function () {
        applyHighlight(this.getAttribute("data-hl-name"));
      });
    }

    el.legendDismiss.addEventListener("click", function () {
      lsSet("pf-legend", "1");
      el.legend.hidden = true;
      el.toggleMin.focus();
    });

    el.toggleMin.addEventListener("click", function () {
      lsSet("pf-reading", readingView() ? "0" : "1");
      applyReadingView();
    });

    el.copySpoken.addEventListener("click", function () {
      if (currentCard) copySpokenFor(currentCard.id, el.copyStatus);
    });

    el.results.addEventListener("click", function (ev) {
      var node = ev.target.closest ? ev.target.closest(".result") : null;
      if (!node) return;
      var nodes = resultNodes();
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i] === node) { state.sel = i; break; }
      }
    });

    window.addEventListener("hashchange", route);

    document.addEventListener("keydown", function (ev) {
      var t = ev.target;
      var typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
                         t.tagName === "SELECT" || t.isContentEditable);
      if (ev.key === "Escape" && typing) { t.blur(); return; }
      if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;

      if (ev.key === "/") { ev.preventDefault(); el.q.focus(); el.q.select(); return; }
      if (!el.viewCard.hidden) {
        if (ev.key === "y" && currentCard) {
          ev.preventDefault();
          copySpokenFor(currentCard.id, el.copyStatus);
        }
        return;
      }
      if (el.viewSearch.hidden) return;   /* about view: no list keys */
      if (ev.key === "j" || ev.key === "ArrowDown") { ev.preventDefault(); moveSel(state.sel < 0 ? 0 : 1); return; }
      if (ev.key === "k" || ev.key === "ArrowUp") { ev.preventDefault(); moveSel(-1); return; }
      if (ev.key === "Enter") {
        var id = selectedId();
        if (id !== null) { ev.preventDefault(); location.hash = "#/card/" + id; }
        return;
      }
      if (ev.key === "y") {
        var sid = selectedId();
        if (sid !== null) { ev.preventDefault(); copySpokenFor(sid, el.searchmeta); }
      }
    });
  }

  function init() {
    el = {
      q: $("q"), searchform: $("searchform"), searchmeta: $("searchmeta"),
      results: $("results"), pager: $("pager"), empty: $("empty"),
      statsBody: $("stats-body"), currentTopic: $("current-topic"),
      topic: $("topic"), helpToggle: $("help-toggle"), syntax: $("syntax"),
      viewSearch: $("view-search"), viewCard: $("view-card"), viewAbout: $("view-about"),
      card: $("card"), cardExtras: $("card-extras"), legend: $("legend"),
      legendDismiss: $("legend-dismiss"), toggleMin: $("toggle-min"),
      copySpoken: $("copy-spoken"), copyStatus: $("copy-status"),
      fatal: $("fatal"), fatalDetail: $("fatal-detail"),
      aboutCoverage: $("about-coverage"), aboutSource: $("about-source"),
      aboutSubset: $("about-subset"), aboutStats: $("about-stats"),
      aboutBuilt: $("about-built")
    };

    applyHighlight(lsGet("pf-hl", "green"));
    wire();
    el.searchmeta.textContent = "Opening the card index…";

    loadConfig()
      .then(openDb)
      .then(function (worker) {
        db = worker;
        window.__db = worker;   /* console/debug access; harmless */
        window.__build = function (q) { var pq = parseQuery(q); return { pq: pq, built: buildSearchSql(pq, PAGE, 0) }; };
        return Promise.all([loadMeta(), loadTopics()]);
      })
      .then(function () {
        renderStats(el.statsBody);
        renderTopicPicker();
        renderCurrentTopic();
        renderAbout();
        el.searchmeta.textContent = "";
        route();
        warmIndex();
      })
      .catch(function (err) {
        fail("The database at " + cfg.db + " could not be opened: " +
             (err && err.message ? err.message : String(err)));
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
