/* PF card search — in-browser .docx card parser.
 *
 * Parses a .docx ArrayBuffer into card objects with no external library and
 * no network access: the ZIP container is read by hand and inflated with the
 * platform's own DecompressionStream('deflate-raw'), then word/document.xml
 * and word/styles.xml are walked with DOMParser.
 *
 * The segmentation and markup rules mirror carddb/docx_parser.py (spec 1.3,
 * 3.4) closely enough that both parsers produce the same body_text, summary,
 * spoken, markup_html, cite/fullcite split and analytic verdict for the same
 * file. Deliberate divergences are listed in site/LIVE_API.md; each one is
 * marked "DIVERGENCE" in a comment here too.
 *
 * The low-level OOXML plumbing (namespaced-DOM helpers, run-property
 * extraction/merge, style-chain resolution, run collection, run text) is
 * adapted from the owner's own battle-tested reader, window.CardReaderParser
 * in https://github.com/hossboss33/cardviewer (public repo, same author).
 * The card model above it is written fresh, because CardReader emits a
 * three-tier viewer model (read/emph/bulk) rather than the four-layer
 * CardRecord this project indexes.
 *
 * Exposes window.CardDocx.
 */
(function () {
  "use strict";

  var W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

  /* ===================================================================
   * 1. ZIP container
   * =================================================================== */

  var SIG_EOCD = 0x06054b50;
  var SIG_EOCD64 = 0x06064b50;
  var SIG_LOC64 = 0x07064b50;
  var SIG_CDIR = 0x02014b50;
  var SIG_LOCAL = 0x04034b50;

  function DocxError(message, code) {
    var e = new Error(message);
    e.name = "DocxError";
    e.code = code || "docx_error";
    return e;
  }

  /* Read the ZIP central directory and return a map of name -> descriptor.
     Only the entries named in `wanted` are inflated. */
  function readCentralDirectory(buf) {
    var u8 = new Uint8Array(buf);
    var dv = new DataView(buf);
    if (u8.length < 22) throw DocxError("not a .docx: file is too small", "not_zip");

    var back = Math.min(u8.length, 0xffff + 22);
    var eocd = -1;
    for (var i = u8.length - 22; i >= u8.length - back && i >= 0; i--) {
      if (dv.getUint32(i, true) === SIG_EOCD) { eocd = i; break; }
    }
    if (eocd < 0) throw DocxError("not a .docx: no ZIP end-of-central-directory record", "not_zip");

    var count = dv.getUint16(eocd + 10, true);
    var cdOff = dv.getUint32(eocd + 16, true);

    /* ZIP64: a .docx this large is unlikely but cheap to support. */
    if (cdOff === 0xffffffff || count === 0xffff) {
      var loc = eocd - 20;
      if (loc >= 0 && dv.getUint32(loc, true) === SIG_LOC64) {
        var z64 = Number(dv.getBigUint64(loc + 8, true));
        if (dv.getUint32(z64, true) === SIG_EOCD64) {
          count = Number(dv.getBigUint64(z64 + 32, true));
          cdOff = Number(dv.getBigUint64(z64 + 48, true));
        }
      }
    }

    var dec = new TextDecoder("utf-8");
    var entries = {};
    var p = cdOff;
    for (var n = 0; n < count; n++) {
      if (p + 46 > u8.length || dv.getUint32(p, true) !== SIG_CDIR) break;
      var method = dv.getUint16(p + 10, true);
      var compSize = dv.getUint32(p + 20, true);
      var uncompSize = dv.getUint32(p + 24, true);
      var nameLen = dv.getUint16(p + 28, true);
      var extraLen = dv.getUint16(p + 30, true);
      var cmtLen = dv.getUint16(p + 32, true);
      var localOff = dv.getUint32(p + 42, true);
      var name = dec.decode(u8.subarray(p + 46, p + 46 + nameLen));

      /* ZIP64 extended information extra field */
      if (compSize === 0xffffffff || uncompSize === 0xffffffff || localOff === 0xffffffff) {
        var ex = p + 46 + nameLen;
        var exEnd = ex + extraLen;
        while (ex + 4 <= exEnd) {
          var hid = dv.getUint16(ex, true);
          var hlen = dv.getUint16(ex + 2, true);
          var q = ex + 4;
          if (hid === 0x0001) {
            if (uncompSize === 0xffffffff) { uncompSize = Number(dv.getBigUint64(q, true)); q += 8; }
            if (compSize === 0xffffffff) { compSize = Number(dv.getBigUint64(q, true)); q += 8; }
            if (localOff === 0xffffffff) { localOff = Number(dv.getBigUint64(q, true)); q += 8; }
            break;
          }
          ex += 4 + hlen;
        }
      }

      entries[name] = {
        name: name, method: method, compSize: compSize,
        uncompSize: uncompSize, localOff: localOff
      };
      p += 46 + nameLen + extraLen + cmtLen;
    }
    return { entries: entries, u8: u8, dv: dv };
  }

  function entryBytes(zip, entry) {
    var dv = zip.dv, u8 = zip.u8;
    var off = entry.localOff;
    if (off + 30 > u8.length || dv.getUint32(off, true) !== SIG_LOCAL) {
      throw DocxError("corrupt .docx: bad local header for " + entry.name, "corrupt_zip");
    }
    /* The local header's own sizes are unreliable when a data descriptor is
       used (bit 3 of the general-purpose flags), so the central directory's
       sizes win. Its name/extra lengths, however, are authoritative here. */
    var nameLen = dv.getUint16(off + 26, true);
    var extraLen = dv.getUint16(off + 28, true);
    var start = off + 30 + nameLen + extraLen;
    return u8.subarray(start, start + entry.compSize);
  }

  function inflateRaw(bytes) {
    if (typeof DecompressionStream !== "function") {
      throw DocxError(
        "this browser has no DecompressionStream('deflate-raw'); .docx parsing needs " +
        "Chrome 103+, Edge 103+, Safari 16.4+ or Firefox 113+",
        "no_decompression_stream"
      );
    }
    var ds = new DecompressionStream("deflate-raw");
    var writer = ds.writable.getWriter();
    writer.write(bytes);
    writer.close();
    return new Response(ds.readable).arrayBuffer();
  }

  async function readEntryText(zip, name) {
    var entry = zip.entries[name];
    if (!entry) return null;
    var raw = entryBytes(zip, entry);
    var out;
    if (entry.method === 0) {           /* STORED */
      out = raw;
    } else if (entry.method === 8) {    /* DEFLATE */
      out = new Uint8Array(await inflateRaw(raw));
    } else {
      throw DocxError(
        "unsupported ZIP compression method " + entry.method + " for " + name,
        "unsupported_compression"
      );
    }
    return new TextDecoder("utf-8").decode(out);
  }

  /* Public: pull the XML parts a card parse needs out of a .docx buffer. */
  async function readDocxParts(arrayBuffer) {
    if (!arrayBuffer || !arrayBuffer.byteLength) {
      throw DocxError("empty file: no bytes", "empty_file");
    }
    var zip = readCentralDirectory(arrayBuffer);
    if (!zip.entries["word/document.xml"]) {
      throw DocxError("not a .docx: word/document.xml is missing", "not_docx");
    }
    return {
      documentXml: await readEntryText(zip, "word/document.xml"),
      stylesXml: await readEntryText(zip, "word/styles.xml"),
      names: Object.keys(zip.entries)
    };
  }

  /* ===================================================================
   * 2. Namespaced DOM helpers   (adapted from window.CardReaderParser)
   * =================================================================== */

  function kids(node, local) {
    var o = [];
    if (!node) return o;
    for (var c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 1 && c.localName === local && c.namespaceURI === W) o.push(c);
    }
    return o;
  }
  function kid(node, local) {
    if (!node) return null;
    for (var c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 1 && c.localName === local && c.namespaceURI === W) return c;
    }
    return null;
  }
  function wval(node, name) {
    if (!node || !node.getAttributeNS) return null;
    var v = node.getAttributeNS(W, name);
    return (v === "" || v === null) ? null : v;
  }
  /* An OOXML toggle: present with no val, or val in {1,true,on} => on. */
  function toggleOn(node) {
    var v = wval(node, "val");
    if (v == null) return true;
    var s = ("" + v).toLowerCase();
    return !(s === "0" || s === "false" || s === "off");
  }

  /* ===================================================================
   * 3. Run properties and style resolution
   *
   * Mirrors carddb/docx_parser.py `_resolve`: for each attribute take the
   * run's direct value, else the run's character-style chain, else the
   * paragraph-style chain. python-docx does NOT consult w:docDefaults for
   * these attributes, so neither does this (it matters: the default template
   * sets sz=22 in docDefaults, which would otherwise change nothing but is
   * exactly the kind of drift that breaks agreement).
   * =================================================================== */

  function extractRpr(rPr) {
    var o = {};
    if (!rPr) return o;
    var b = kid(rPr, "b"); if (b) o.bold = toggleOn(b);
    var u = kid(rPr, "u");
    if (u) {
      var uv = wval(u, "val");
      /* python-docx maps w:u val="none" -> False and any other value -> truthy. */
      o.underline = (uv == null) ? true : (("" + uv).toLowerCase() !== "none");
    }
    var sz = kid(rPr, "sz");
    if (sz) { var v = parseInt(wval(sz, "val"), 10); if (!isNaN(v)) o.szHP = v; }
    var hl = kid(rPr, "highlight");
    if (hl) {
      var hv = wval(hl, "val");
      /* DIVERGENCE: python-docx raises ValueError on w:highlight val="none"
         (WD_COLOR_INDEX has no such member), which makes carddb reject the
         whole document. Here it simply means "not highlighted", which is what
         Word means by it. Every other value, white and black included, counts
         as a highlight — matching python-docx's truthiness test. */
      if (hv != null) o.hl = (("" + hv).toLowerCase() !== "none");
    }
    var rStyle = kid(rPr, "rStyle"); if (rStyle) o.rStyle = wval(rStyle, "val");
    return o;
  }

  function mergeRpr(base, over) {
    var o = {};
    for (var a in base) if (base[a] !== undefined) o[a] = base[a];
    for (var k in over) if (over[k] !== undefined) o[k] = over[k];
    return o;
  }

  function buildStyles(stylesDoc) {
    var map = {}, defaultParaId = null, empty = true;
    if (!stylesDoc || !stylesDoc.documentElement) {
      return { map: map, defaultParaId: null, empty: true };
    }
    var root = stylesDoc.documentElement;
    var list = kids(root, "style");
    for (var i = 0; i < list.length; i++) {
      var st = list[i];
      var id = wval(st, "styleId");
      if (!id) continue;
      empty = false;
      var nameEl = kid(st, "name");
      var basedOn = kid(st, "basedOn");
      map[id] = {
        id: id,
        name: nameEl ? (wval(nameEl, "val") || "") : "",
        basedOn: basedOn ? wval(basedOn, "val") : null,
        rPr: extractRpr(kid(st, "rPr"))
      };
      if (defaultParaId === null && wval(st, "type") === "paragraph" &&
          toggleOn2(st, "default")) {
        defaultParaId = id;
      }
    }
    return { map: map, defaultParaId: defaultParaId, empty: empty };
  }

  function toggleOn2(node, attr) {
    var v = node.getAttributeNS(W, attr);
    if (v === null || v === "") return false;
    var s = ("" + v).toLowerCase();
    return !(s === "0" || s === "false" || s === "off");
  }

  /* python-docx resolves an unknown or absent pStyle to the document's
     default paragraph style (normally "Normal"), which is what decides both
     the inherited run properties and the heading level. */
  function resolveStyleId(styleTable, styleId) {
    if (styleId && styleTable.map[styleId]) return styleId;
    if (styleTable.empty) return styleId || null;   /* no styles.xml at all */
    return styleTable.defaultParaId;
  }

  /* Flatten style -> basedOn -> ... with the most-derived style winning,
     which is the same "first non-None along the chain" rule python-docx's
     `_style_chain` walk produces. */
  function styleChainRpr(styleTable, styleId) {
    if (!styleId || !styleTable.map[styleId]) return {};
    var chain = [], guard = {}, id = styleId;
    while (id && styleTable.map[id] && !guard[id]) {
      guard[id] = 1;
      chain.push(styleTable.map[id]);
      id = styleTable.map[id].basedOn;
    }
    var acc = {};
    for (var i = chain.length - 1; i >= 0; i--) acc = mergeRpr(acc, chain[i].rPr || {});
    return acc;
  }

  function styleNameOf(styleTable, styleId) {
    if (!styleId) return null;
    var s = styleTable.map[styleId];
    if (s) return s.name || null;
    return styleTable.empty ? styleId : null;
  }

  /* ===================================================================
   * 4. Run collection and run text
   * =================================================================== */

  /* python-docx's Paragraph.iter_inner_content() is `./w:r | ./w:hyperlink`,
     i.e. DIRECT children only. Runs nested in w:ins (tracked insertions),
     w:smartTag or w:sdt are therefore invisible to carddb. Default behaviour
     matches that exactly; opts.acceptTrackedInsertions widens it. Runs inside
     w:del are skipped either way (they are deleted text). */
  function collectRuns(p, accept, stats) {
    var runs = [];
    (function rec(node, depth) {
      for (var c = node.firstChild; c; c = c.nextSibling) {
        if (c.nodeType !== 1 || c.namespaceURI !== W) continue;
        var ln = c.localName;
        if (ln === "del") continue;
        if (ln === "r") { runs.push(c); continue; }
        if (ln === "hyperlink") { rec(c, depth + 1); continue; }
        if (ln === "ins" || ln === "smartTag" || ln === "sdt" || ln === "sdtContent") {
          if (accept) { rec(c, depth + 1); }
          else if (stats && ln === "ins" && c.getElementsByTagNameNS(W, "t").length) {
            stats.skippedInsertions++;
          }
          continue;
        }
      }
    })(p, 0);
    return runs;
  }

  /* python-docx CT_R.text: `w:br | w:cr | w:noBreakHyphen | w:ptab | w:t | w:tab`
     where w:br contributes "\n" only for a textWrapping (default) break. */
  function runText(r) {
    var s = "";
    for (var c = r.firstChild; c; c = c.nextSibling) {
      if (c.nodeType !== 1 || c.namespaceURI !== W) continue;
      var ln = c.localName;
      if (ln === "t") s += c.textContent;
      else if (ln === "tab" || ln === "ptab") s += "\t";
      else if (ln === "cr") s += "\n";
      else if (ln === "br") { var ty = wval(c, "type"); if (ty == null || ty === "textWrapping") s += "\n"; }
      else if (ln === "noBreakHyphen") s += "-";
    }
    return s;
  }

  /* ===================================================================
   * 5. Text helpers — direct ports of the Python ones
   * =================================================================== */

  /* Python's str.split() splits on str.isspace(), a slightly wider set than
     JS \s (it adds the C1/file separators and U+0085). */
  var PY_WS = /[\s\u001c-\u001f\u0085]+/;
  var PY_WS_G = /[\s\u001c-\u001f\u0085]+/g;

  function pyWords(s) {
    if (!s) return [];
    var t = String(s).replace(/^[\s\u001c-\u001f\u0085]+|[\s\u001c-\u001f\u0085]+$/g, "");
    return t === "" ? [] : t.split(PY_WS_G);
  }
  function clean(s) { return pyWords(s).join(" "); }
  function wc(s) { return pyWords(s).length; }
  function pyStrip(s) {
    return String(s == null ? "" : s)
      .replace(/^[\s\u001c-\u001f\u0085]+|[\s\u001c-\u001f\u0085]+$/g, "");
  }
  function cpLen(s) { return Array.from(s || "").length; }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#x27;");
  }

  /* Python's [\w'’.-] is Unicode-aware; JS \w is not, so spell it out. */
  var WCH = "[\\p{L}\\p{N}_'’.\\-]";

  var SHORT_CITE_RE = new RegExp(
    "^\\s*(?:[A-Z]" + WCH + "+(?:,? (?:and|&) [A-Z]" + WCH + "+)?" +
    "|[A-Z]" + WCH + "+ et al\\.?),? ['’]?\\d{2}(?:\\d{2})?\\b", "u");

  var MULTI_AUTHOR_CITE_RE = new RegExp(
    "^\\s*[A-Z]" + WCH + "+(?:,\\s*[A-Z]" + WCH + "+)+" +
    "(?:,?\\s*(?:and|&)\\s*[A-Z]" + WCH + "+)?,?\\s+['’]?\\d{2}(?:\\d{2})?\\b", "u");

  var CITE_RES = [SHORT_CITE_RE, MULTI_AUTHOR_CITE_RE];

  var URL_RE = /https?:\/\/[^\s<>"'\)\]]+/i;
  var HEADING_STYLE_RE = /^heading\s*(\d+)/i;

  var RE_MDY = /\b(\d{1,2})[-\/](\d{1,2})[-\/](\d{4})\b/g;
  var RE_MONTH_D_Y = new RegExp(
    "\\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|" +
    "jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|" +
    "dec(?:ember)?)\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?\\s*,?\\s+(\\d{4})\\b", "gi");
  var RE_YEAR_G = /\b(?:19|20)\d{2}\b/g;
  var RE_YEAR = /\b(?:19|20)\d{2}\b/;
  var MONTH_NUM = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
                    jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };

  var ACCESS_RE = /\b(?:accessed|access date|date of access|retrieved)\b/i;
  var DOA_RE = /\bDOA\b/;

  var DATE_ZONE = 200;
  var FULLCITE_MAX_WORDS = 80;
  var ANALYTIC_MAX_BODY_WORDS = 40;
  var FALLBACK_TAG_MAX_WORDS = 40;
  var FALLBACK_BOLD_FRACTION = 0.8;
  var FALLBACK_MIN_PT = 12.5;
  var MIN_RUN_PT = 9.0;

  var TAG_WRAP = {
    mark: ["<mark>", "</mark>"],
    strong_u: ["<strong><u>", "</u></strong>"],
    u: ["<u>", "</u>"],
    strong: ["<strong>", "</strong>"],
    min: ['<span class="min">', "</span>"],
    plain: ["", ""]
  };
  var SUMMARY_CLASSES = { mark: 1, strong_u: 1, u: 1, strong: 1 };

  function citeMatch(text) {
    for (var i = 0; i < CITE_RES.length; i++) {
      var m = CITE_RES[i].exec(text || "");
      if (m) return m;
    }
    return null;
  }

  function extractSourceUrl(fullcite) {
    if (!fullcite) return null;
    var m = URL_RE.exec(fullcite);
    if (!m) return null;
    return m[0].replace(/[.,;:!?]+$/, "");
  }

  function pad(n, w) { var s = String(n); while (s.length < w) s = "0" + s; return s; }

  function extractPubDate(fullcite) {
    if (!fullcite) return null;
    var zone = fullcite.slice(0, DATE_ZONE);
    var cands = [], m;
    RE_MDY.lastIndex = 0;
    while ((m = RE_MDY.exec(zone)) !== null) {
      var mo = +m[1], d = +m[2], y = +m[3];
      if (mo >= 1 && mo <= 12 && d >= 1 && d <= 31 && y >= 1900 && y <= 2099) {
        cands.push([m.index, 0, pad(y, 4) + "-" + pad(mo, 2) + "-" + pad(d, 2)]);
      }
    }
    RE_MONTH_D_Y.lastIndex = 0;
    while ((m = RE_MONTH_D_Y.exec(zone)) !== null) {
      var mo2 = MONTH_NUM[m[1].slice(0, 3).toLowerCase()];
      var d2 = +m[2], y2 = +m[3];
      if (d2 >= 1 && d2 <= 31 && y2 >= 1900 && y2 <= 2099) {
        cands.push([m.index, 0, pad(y2, 4) + "-" + pad(mo2, 2) + "-" + pad(d2, 2)]);
      }
    }
    RE_YEAR_G.lastIndex = 0;
    while ((m = RE_YEAR_G.exec(zone)) !== null) cands.push([m.index, 1, m[0]]);
    if (!cands.length) return null;
    /* stable sort on (position, specificity), like Python's list.sort */
    cands = cands.map(function (c, i) { return [c[0], c[1], c[2], i]; });
    cands.sort(function (a, b) { return (a[0] - b[0]) || (a[1] - b[1]) || (a[3] - b[3]); });
    return cands[0][2];
  }

  function looksLikeFullcite(text) {
    if (!text) return false;
    if (URL_RE.test(text)) return true;
    if (extractPubDate(text) !== null) return true;
    return (text.split(",").length - 1) >= 2 && wc(text) <= 120;
  }

  function isStrongFullcite(text) {
    if (!text || wc(text) > FULLCITE_MAX_WORDS) return false;
    return !!(URL_RE.test(text) || RE_YEAR.test(text) ||
              ACCESS_RE.test(text) || DOA_RE.test(text));
  }

  /* ===================================================================
   * 6. Paragraph rendering
   * =================================================================== */

  function makeCtx(styleTable, opts, stats) {
    return { styles: styleTable, opts: opts, stats: stats, cache: new Map() };
  }

  /* -> { el, styleId, styleName, segs:[[cls,text]], plain, runsRaw } */
  function paraInfo(ctx, pEl) {
    var hit = ctx.cache.get(pEl);
    if (hit) return hit;

    var pPr = kid(pEl, "pPr");
    var pStyleEl = pPr ? kid(pPr, "pStyle") : null;
    var pStyleId = resolveStyleId(ctx.styles, pStyleEl ? wval(pStyleEl, "val") : null);
    var parChain = styleChainRpr(ctx.styles, pStyleId);

    var runEls = collectRuns(pEl, ctx.opts.acceptTrackedInsertions, ctx.stats);
    var segs = [];
    var runsRaw = [];
    for (var i = 0; i < runEls.length; i++) {
      var r = runEls[i];
      var direct = extractRpr(kid(r, "rPr"));
      var eff = parChain;
      if (direct.rStyle) eff = mergeRpr(eff, styleChainRpr(ctx.styles, direct.rStyle));
      eff = mergeRpr(eff, direct);
      var text = runText(r);
      runsRaw.push({ text: text, eff: eff });
      if (!text) continue;
      var cls = classifyRun(eff);
      if (segs.length && segs[segs.length - 1][0] === cls) segs[segs.length - 1][1] += text;
      else segs.push([cls, text]);
    }

    var out = {
      el: pEl,
      styleId: pStyleId,
      styleName: styleNameOf(ctx.styles, pStyleId),
      segs: segs,
      runsRaw: runsRaw,
      plain: segs.map(function (s) { return s[1]; }).join("")
    };
    ctx.cache.set(pEl, out);
    return out;
  }

  /* Spec 3.4 precedence, exact. */
  function classifyRun(eff) {
    if (eff.hl) return "mark";
    var bold = !!eff.bold, under = !!eff.underline;
    if (bold && under) return "strong_u";
    if (under) return "u";
    if (bold) return "strong";
    var pt = (eff.szHP === undefined || eff.szHP === null) ? null : eff.szHP / 2;
    if (pt !== null && pt <= MIN_RUN_PT) return "min";
    return "plain";
  }

  function renderParagraph(info) {
    var htmlParts = [], summary = [], spoken = [];
    for (var i = 0; i < info.segs.length; i++) {
      var cls = info.segs[i][0], text = info.segs[i][1];
      var wrap = TAG_WRAP[cls];
      htmlParts.push(wrap[0] + escapeHtml(text).replace(/\n/g, "<br>") + wrap[1]);
      var frag = clean(text);
      if (frag) {
        if (SUMMARY_CLASSES[cls]) summary.push(frag);
        if (cls === "mark") spoken.push(frag);
      }
    }
    return { plain: info.plain, html: htmlParts.join(""), summary: summary, spoken: spoken };
  }

  /* ===================================================================
   * 7. Document stream
   * =================================================================== */

  function headingLevel(info) {
    var cands = [info.styleName, info.styleId];
    for (var i = 0; i < cands.length; i++) {
      var c = cands[i];
      if (!c) continue;
      var m = HEADING_STYLE_RE.exec(String(c).trim());
      if (m) {
        var n = parseInt(m[1], 10);
        return isNaN(n) ? null : n;
      }
    }
    return null;
  }

  /* python-docx `_Cell.paragraphs` / `_Cell.tables` are direct children;
     `_TableRow.cells` resolves gridSpan (same cell repeated) and vMerge
     (continuation rows map back to the origin cell). Deduping by element and
     skipping vMerge-continue cells reproduces both. */
  function tableCells(tblEl) {
    var out = [];
    var rows = kids(tblEl, "tr");
    for (var i = 0; i < rows.length; i++) {
      var cells = kids(rows[i], "tc");
      for (var j = 0; j < cells.length; j++) {
        var tc = cells[j];
        var tcPr = kid(tc, "tcPr");
        var vm = tcPr ? kid(tcPr, "vMerge") : null;
        if (vm && (wval(vm, "val") || "continue").toLowerCase() !== "restart") continue;
        out.push(tc);
      }
    }
    return out;
  }

  function iterTableParagraphs(tblEl) {
    var seen = new Set();
    var out = [];
    var cells = tableCells(tblEl);
    for (var i = 0; i < cells.length; i++) {
      var tc = cells[i];
      if (seen.has(tc)) continue;
      seen.add(tc);
      var ps = kids(tc, "p");
      for (var k = 0; k < ps.length; k++) out.push(ps[k]);
      /* one level of nested tables, text only */
      var nested = kids(tc, "tbl");
      for (var n = 0; n < nested.length; n++) {
        var ncells = tableCells(nested[n]);
        for (var m = 0; m < ncells.length; m++) {
          var ntc = ncells[m];
          if (seen.has(ntc)) continue;
          seen.add(ntc);
          var nps = kids(ntc, "p");
          for (var q = 0; q < nps.length; q++) out.push(nps[q]);
        }
      }
    }
    return out;
  }

  /* Entries: {k:"h", level, text} | {k:"p", el, text, wc} | {k:"t", el, text, wc} */
  function buildStream(ctx, bodyEl) {
    var stream = [];
    for (var c = bodyEl.firstChild; c; c = c.nextSibling) {
      if (c.nodeType !== 1 || c.namespaceURI !== W) continue;
      if (c.localName === "p") {
        var info = paraInfo(ctx, c);
        var lvl = headingLevel(info);
        if (lvl !== null) stream.push({ k: "h", level: lvl, text: clean(info.plain) });
        else stream.push({ k: "p", el: c, text: info.plain, wc: wc(info.plain) });
      } else if (c.localName === "tbl") {
        var ps = iterTableParagraphs(c);
        var lines = [];
        for (var i = 0; i < ps.length; i++) {
          var t = paraInfo(ctx, ps[i]).plain.trim();
          if (t) lines.push(t);
        }
        var text = lines.join("\n");
        stream.push({ k: "t", el: c, text: text, wc: wc(text) });
      }
    }
    return stream;
  }

  /* ===================================================================
   * 8. Card assembly
   * =================================================================== */

  function splitCite(entries) {
    var n = entries.length;
    if (n === 0) return { cite: null, fullcite: null, bodyStart: 0, analytic: true };
    function txt(i) { return entries[i].text; }

    if (!entries[0].from_table) {
      var m = citeMatch(txt(0));
      if (m) {
        var cite = clean(m[0]).replace(/,+$/, "");
        var rem = clean(txt(0).slice(m[0].length).replace(/^[ \t\n,;:\-–—]+/, ""));
        var bodyStart = 1;
        if (rem.length < 15 && n > 1 && !entries[1].from_table &&
            !citeMatch(txt(1)) && looksLikeFullcite(txt(1))) {
          rem = clean(rem + " " + txt(1));
          bodyStart = 2;
        }
        return { cite: cite, fullcite: rem || null, bodyStart: bodyStart, analytic: false };
      }
    }
    if (n > 1 && !entries[1].from_table && wc(txt(0)) < 60) {
      var m2 = citeMatch(txt(1));
      if (m2) {
        var cite2 = clean(m2[0]).replace(/,+$/, "");
        var rem2 = clean(txt(1).slice(m2[0].length).replace(/^[ \t\n,;:\-–—]+/, ""));
        var lead = clean(txt(0));
        var full = [lead, rem2].filter(Boolean).join(" ");
        full = clean(full);
        return { cite: cite2, fullcite: full || null, bodyStart: 2, analytic: false };
      }
    }
    if (!entries[0].from_table && isStrongFullcite(txt(0))) {
      return { cite: null, fullcite: clean(txt(0)) || null, bodyStart: 1, analytic: false };
    }
    if (n > 1 && !entries[0].from_table && !entries[1].from_table &&
        wc(txt(0)) < 60 && isStrongFullcite(txt(1))) {
      var full2 = clean([clean(txt(0)), clean(txt(1))].filter(Boolean).join(" "));
      return { cite: null, fullcite: full2 || null, bodyStart: 2, analytic: false };
    }
    return { cite: null, fullcite: null, bodyStart: 0, analytic: true };
  }

  function buildCard(ctx, raw, warnings) {
    var entries = [];
    var hasTable = false;
    for (var i = 0; i < raw.members.length; i++) {
      var e = raw.members[i];
      if (e.k === "p") {
        var r = renderParagraph(paraInfo(ctx, e.el));
        if (pyStrip(r.plain)) {
          entries.push({ text: pyStrip(r.plain), html: r.html, summary: r.summary,
                         spoken: r.spoken, from_table: false });
        }
      } else if (e.k === "t") {
        hasTable = true;
        var ps = iterTableParagraphs(e.el);
        for (var j = 0; j < ps.length; j++) {
          var r2 = renderParagraph(paraInfo(ctx, ps[j]));
          if (pyStrip(r2.plain)) {
            entries.push({ text: pyStrip(r2.plain), html: r2.html, summary: r2.summary,
                           spoken: r2.spoken, from_table: true });
          }
        }
      }
    }

    var sc = splitCite(entries);
    var analytic = sc.analytic;
    var tag = clean(raw.tag || "");
    if (analytic && !tag) {
      warnings.push("dropped a card with neither tag nor cite");
      return null;
    }

    var bodyEntries = entries.slice(sc.bodyStart);
    var bodyText = bodyEntries.map(function (e) { return e.text; }).join("\n");
    if (analytic && wc(bodyText) >= ANALYTIC_MAX_BODY_WORDS) analytic = false;
    if (!analytic && !bodyText) {
      warnings.push("card " + pyRepr(tag.slice(0, 40)) + " has a cite but no body; treated as analytic");
      analytic = true;
    }
    var summary = [].concat.apply([], bodyEntries.map(function (e) { return e.summary; })).join(" ");
    var spoken = [].concat.apply([], bodyEntries.map(function (e) { return e.spoken; })).join(" ");
    var ratio = bodyText ? Math.min(1.0, cpLen(spoken) / cpLen(bodyText)) : 0.0;

    var htmlParts = [];
    if (tag) htmlParts.push("<h4>" + escapeHtml(tag) + "</h4>");
    for (var k = 0; k < entries.length; k++) htmlParts.push("<p>" + entries[k].html + "</p>");

    var rec = {
      tag: tag || null,
      cite: sc.cite,
      fullcite: sc.fullcite,
      body_text: bodyText,
      is_analytic: analytic,
      source_url: extractSourceUrl(sc.fullcite),
      source_pub_date: extractPubDate(sc.fullcite),
      pocket: raw.pocket, hat: raw.hat, block: raw.block,
      markup_html: htmlParts.join(""),
      summary: summary,
      spoken: spoken,
      highlight_ratio: ratio,
      fidelity: "opensource",
      ordinal: null,
      extras: {}
    };
    if (hasTable) rec.extras.has_table = true;
    return rec;
  }

  /* Python warnings embed a repr() of the tag; reproduce it so warning text
     compares equal across the two parsers. */
  function pyRepr(s) {
    var body = String(s).replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/\t/g, "\\t").replace(/\r/g, "\\r");
    if (body.indexOf("'") !== -1 && body.indexOf('"') === -1) return '"' + body + '"';
    return "'" + body.replace(/'/g, "\\'") + "'";
  }

  /* ===================================================================
   * 9. Pass 1 — heading styles
   * =================================================================== */

  function stylePass(stream, warnings) {
    var pocket = null, hat = null, block = null;
    var rawCards = [], current = null, unassigned = 0;
    for (var i = 0; i < stream.length; i++) {
      var e = stream[i];
      if (e.k === "h") {
        if (current) { rawCards.push(current); current = null; }
        var lvl = e.level, text = e.text;
        if (lvl === 1) { pocket = text || null; hat = null; block = null; }
        else if (lvl === 2) { hat = text || null; block = null; }
        else if (lvl === 3) { block = text || null; }
        else if (lvl === 4) {
          current = { tag: text, pocket: pocket, hat: hat, block: block, members: [] };
        } else {
          warnings.push("ignored heading level " + lvl + ": " + pyRepr(text.slice(0, 40)));
        }
      } else if (current) {
        current.members.push(e);
      } else {
        unassigned += e.wc;
      }
    }
    if (current) rawCards.push(current);
    return { rawCards: rawCards, unassigned: unassigned };
  }

  /* ===================================================================
   * 10. Pass 2 — direct-formatting fallback
   * =================================================================== */

  function isTagShaped(ctx, el, text) {
    var words = wc(text);
    if (words === 0 || words >= FALLBACK_TAG_MAX_WORDS) return false;
    if (citeMatch(text)) return false;
    var info = paraInfo(ctx, el);
    var total = 0, boldLarge = 0;
    for (var i = 0; i < info.runsRaw.length; i++) {
      var t = info.runsRaw[i].text;
      if (!t) continue;
      total += t.length;
      var eff = info.runsRaw[i].eff;
      if (eff.bold) {
        var pt = (eff.szHP === undefined || eff.szHP === null) ? null : eff.szHP / 2;
        if (pt !== null && pt >= FALLBACK_MIN_PT) boldLarge += t.length;
      }
    }
    return total > 0 && (boldLarge / total) >= FALLBACK_BOLD_FRACTION;
  }

  function fallbackPass(ctx, stream) {
    var seq = stream.filter(function (e) {
      return e.k === "h" || ((e.k === "p" || e.k === "t") && pyStrip(e.text));
    });
    var tagIdx = new Set();
    for (var i = 0; i < seq.length; i++) {
      var e = seq[i];
      if (e.k !== "p" || !isTagShaped(ctx, e.el, e.text)) continue;
      var seenParas = 0;
      for (var j = i + 1; j < seq.length; j++) {
        var nxt = seq[j];
        if (nxt.k === "h") break;
        if (nxt.k !== "p") continue;
        seenParas++;
        if (citeMatch(nxt.text)) { tagIdx.add(i); break; }
        if (seenParas >= 2) break;
      }
    }

    var pocket = null, hat = null, block = null;
    var rawCards = [], current = null;
    for (var x = 0; x < seq.length; x++) {
      var s = seq[x];
      if (s.k === "h") {
        if (current) { rawCards.push(current); current = null; }
        if (s.level === 1) { pocket = s.text || null; hat = null; block = null; }
        else if (s.level === 2) { hat = s.text || null; block = null; }
        else if (s.level === 3) { block = s.text || null; }
        else if (s.level === 4) {
          current = { tag: s.text, pocket: pocket, hat: hat, block: block, members: [] };
        }
      } else if (tagIdx.has(x)) {
        if (current) rawCards.push(current);
        current = { tag: clean(s.text), pocket: pocket, hat: hat, block: block, members: [] };
      } else if (current) {
        current.members.push(s);
      }
    }
    if (current) rawCards.push(current);
    return rawCards;
  }

  /* ===================================================================
   * 11. Top level
   * =================================================================== */

  function finalize(ctx, rawCards, warnings) {
    var out = [];
    for (var i = 0; i < rawCards.length; i++) {
      var rec = buildCard(ctx, rawCards[i], warnings);
      if (rec) out.push(rec);
    }
    return out;
  }

  function parseXml(text, label) {
    var doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.getElementsByTagName("parsererror").length) {
      throw DocxError("could not parse " + label, "bad_xml");
    }
    return doc;
  }

  /* Parse already-extracted OOXML parts. Useful for tests and for callers
     that unzipped elsewhere. */
  function parseDocumentXml(documentXml, stylesXml, options) {
    var opts = options || {};
    var warnings = [];
    var stats = { skippedInsertions: 0 };

    var dpx = parseXml(documentXml, "word/document.xml");
    var stylesDoc = null;
    if (stylesXml) {
      try { stylesDoc = parseXml(stylesXml, "word/styles.xml"); }
      catch (e) { warnings.push("word/styles.xml is unreadable; using direct formatting only"); }
    }
    var styleTable = buildStyles(stylesDoc);
    var ctx = makeCtx(styleTable, {
      acceptTrackedInsertions: !!opts.acceptTrackedInsertions
    }, stats);

    var bodyEl = kid(dpx.documentElement, "body");
    if (!bodyEl) throw DocxError("the document has no <w:body>", "no_body");

    var stream = buildStream(ctx, bodyEl);
    var totalWords = stream.reduce(function (a, e) {
      return a + ((e.k === "p" || e.k === "t") ? e.wc : 0);
    }, 0);

    var sp = stylePass(stream, warnings);
    var cards = finalize(ctx, sp.rawCards, warnings);

    var usedFallback = false;
    var runFallback =
      (!cards.length && totalWords > 0) ||
      (cards.length > 0 && totalWords >= 100 &&
       (sp.unassigned / Math.max(totalWords, 1)) > 0.6);

    if (runFallback) {
      var fb = finalize(ctx, fallbackPass(ctx, stream), warnings);
      if (fb.length > cards.length) {
        warnings.push("direct-formatting fallback used: " + cards.length +
                      " card(s) from styles, " + fb.length + " from formatting");
        cards = fb;
        usedFallback = true;
      } else if (!cards.length) {
        usedFallback = true;
        warnings.push("no cards found (style and fallback passes)");
      } else {
        warnings.push("fallback pass triggered but found no more cards; kept style-pass results");
      }
    }

    if (!cards.length && totalWords === 0) warnings.push("document contains no text");

    for (var i = 0; i < cards.length; i++) cards[i].ordinal = i;

    if (stats.skippedInsertions) {
      warnings.push("skipped " + stats.skippedInsertions +
                    " tracked-insertion run group(s), matching carddb; pass " +
                    "{acceptTrackedInsertions:true} to include them");
    }
    return { cards: cards, warnings: warnings, usedFallback: usedFallback };
  }

  /* The main entry point. */
  async function parseDocx(arrayBuffer, options) {
    var parts = await readDocxParts(arrayBuffer);
    return parseDocumentXml(parts.documentXml, parts.stylesXml, options);
  }

  window.CardDocx = {
    VERSION: "1.0.0",
    parseDocx: parseDocx,
    parseDocumentXml: parseDocumentXml,
    readDocxParts: readDocxParts,
    /* exported for tests / reuse */
    extractSourceUrl: extractSourceUrl,
    extractPubDate: extractPubDate,
    SHORT_CITE_RE: SHORT_CITE_RE
  };
})();
