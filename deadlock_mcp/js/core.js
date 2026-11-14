// Deadlock MCP — shared Panorama JS core.
// Prepended to the persistent bridge script and to every request script. Each layout file runs
// in its own JS context in Deadlock, so nothing here is shared at runtime except via panels
// (attributes, Data()) and the console. `eval`/`new Function` are disabled by the engine.
"use strict";

// Deadlock's V8 has no ICU data: Intl / toLocale* / localeCompare / normalize hit a V8 *fatal*
// error that kills the game (verified). Replace them in this context with a catchable error.
(function () {
  function crashGuard(name) {
    return function () {
      throw new Error(name + " is disabled: locale APIs crash Deadlock (V8 has no ICU data). " +
                      "Use toISOString()/toFixed()/manual formatting instead.");
    };
  }
  var targets = [
    [Date.prototype, ["toLocaleString", "toLocaleDateString", "toLocaleTimeString"]],
    [Number.prototype, ["toLocaleString"]],
    [Array.prototype, ["toLocaleString"]],
    [String.prototype, ["localeCompare", "normalize", "toLocaleUpperCase", "toLocaleLowerCase"]],
  ];
  for (var i = 0; i < targets.length; i++) {
    for (var j = 0; j < targets[i][1].length; j++) {
      var n = targets[i][1][j];
      try { Object.defineProperty(targets[i][0], n, { value: crashGuard(n), configurable: true, writable: true }); } catch (e) {}
    }
  }
  try {
    var intlGuard = new Proxy({}, { get: function (_, k) { return crashGuard("Intl." + String(k)); } });
    Object.defineProperty(globalThis, "Intl", { value: intlGuard, configurable: true, writable: true });
  } catch (e) {}
})();

var MCP = (function () {
  var OUT_CHUNK = 400;

  function topRoot(from) {
    var p = from || $.GetContextPanel();
    while (p.GetParent && p.GetParent()) { p = p.GetParent(); }
    return p;
  }

  // The persistent bridge panel (CitadelHudDBOverlay root of the host layout).
  function bridgePanel() {
    var p = $.GetContextPanel();
    while (p) {
      if (p.GetAttributeString && p.GetAttributeString("mcp_bridge", "") === "1") { return p; }
      p = p.GetParent();
    }
    return $.GetContextPanel();
  }

  function children(p) {
    var n = p.GetChildCount ? p.GetChildCount() : 0, out = [];
    for (var i = 0; i < n; i++) { out.push(p.GetChild(i)); }
    return out;
  }

  function walk(p, fn) {
    if (fn(p) === false) { return; }
    var c = children(p);
    for (var i = 0; i < c.length; i++) { walk(c[i], fn); }
  }

  // Hidden request hosts the bridge creates (and everything in them) are not part of the UI.
  function isInternal(p) {
    var id = p.id || "";
    return id.lastIndexOf("McpReq", 0) === 0;
  }

  // True if p sits inside an internal panel below `root` (p itself not counted).
  function underInternal(p, root) {
    var up = p === root ? null : p.GetParent();
    while (up && up !== root) {
      if (isInternal(up)) { return true; }
      up = up.GetParent();
    }
    return false;
  }

  // Panels whose deletion or layout replacement would take the bridge (and usually the whole UI)
  // down: the top root, the bridge panel, and every ancestor of the bridge. In the HUD the top root
  // and the bridge are the same plain Panel (#CitadelHudRoot).
  function isProtected(p) {
    if (!p) { return false; }
    if (!p.GetParent()) { return true; }
    if (p.GetAttributeString && p.GetAttributeString("mcp_bridge", "") === "1") { return true; }
    var b = bridgePanel();
    while (b) {
      if (b === p) { return true; }
      b = b.GetParent();
    }
    return false;
  }

  function describe(p) {
    if (!p || !p.IsValid || !p.IsValid()) { return null; }
    var d = { id: p.id || "", type: p.paneltype };
    try { if (p.layoutfile) { d.layout = p.layoutfile; } } catch (e) {}
    try { if (!p.visible) { d.visible = false; } } catch (e) {}
    try {
      if (typeof p.text === "string" && p.text.length) {
        d.text = p.text.length > 80 ? p.text.substr(0, 80) + "…" : p.text;
      }
    } catch (e) {}
    try { d.size = [Math.round(p.actuallayoutwidth), Math.round(p.actuallayoutheight)]; } catch (e) {}
    try { d.pos = [Math.round(p.actualxoffset), Math.round(p.actualyoffset)]; } catch (e) {}
    d.children = p.GetChildCount ? p.GetChildCount() : 0;
    return d;
  }

  // Structured selector from Python: [{type, id, classes[], comb}], comb = combinator before it.
  function matchCompound(p, c) {
    if (c.type && c.type !== "*" && p.paneltype !== c.type) { return false; }
    if (c.id && p.id !== c.id) { return false; }
    if (c.classes) {
      for (var i = 0; i < c.classes.length; i++) { if (!p.BHasClass(c.classes[i])) { return false; } }
    }
    return true;
  }

  function matchSelector(p, sel, idx) {
    if (!matchCompound(p, sel[idx])) { return false; }
    if (idx === 0) { return true; }
    var up = p.GetParent();
    if (sel[idx].comb === ">") { return !!up && matchSelector(up, sel, idx - 1); }
    while (up) {
      if (matchSelector(up, sel, idx - 1)) { return true; }
      up = up.GetParent();
    }
    return false;
  }

  function hasClass(c) { return !!(c.classes && c.classes.length); }

  // Candidate panels for a selector. Panorama ids are NOT unique (e.g. several #BuffModifiers) and
  // FindChildTraverse only returns the first, so only classes are used as native anchors
  // (FindChildrenWithClassTraverse returns all). Without a class we walk the tree (~20ms in the
  // HUD); the bridge's persistent re-apply loop caches matches so it rarely pays that.
  // From the anchor we expand step by step: '>' looks at children, ' ' walks the subtrees.
  function candidates(sel, root) {
    var k = sel.length - 1;
    while (k >= 0 && !hasClass(sel[k])) { k--; }
    var set = [];
    if (k < 0) {
      walk(root, function (q) { if (isInternal(q)) { return false; } set.push(q); });
      return set;
    }
    if (root.BHasClass(sel[k].classes[0])) { set.push(root); }
    var found = root.FindChildrenWithClassTraverse(sel[k].classes[0]);
    for (var f = 0; f < found.length; f++) { set.push(found[f]); }
    for (var i = k + 1; i < sel.length; i++) {
      var next = [], seen = new Set(), c = sel[i];
      for (var j = 0; j < set.length; j++) {
        if (c.comb === ">") {
          var kids = children(set[j]);
          for (var m = 0; m < kids.length; m++) {
            if (!seen.has(kids[m]) && !isInternal(kids[m]) && matchCompound(kids[m], c)) { seen.add(kids[m]); next.push(kids[m]); }
          }
        } else {
          walk(set[j], function (q) {
            if (q === set[j]) { return; }
            if (isInternal(q) || seen.has(q)) { return false; }
            seen.add(q);
            if (matchCompound(q, c)) { next.push(q); }
          });
        }
      }
      set = next;
    }
    return set;
  }

  function query(sel, root) {
    root = root || topRoot();
    var c = candidates(sel, root), out = [];
    for (var i = 0; i < c.length; i++) {
      if (!isInternal(c[i]) && matchSelector(c[i], sel, sel.length - 1) && !underInternal(c[i], root)) { out.push(c[i]); }
    }
    return out;
  }

  // "root" / "" → top root, "bridge" → bridge panel, "#id" or "id" → FindChildTraverse from root.
  function resolve(t) {
    if (!t || t === "root") { return topRoot(); }
    if (t === "bridge" || t === "ctx") { return bridgePanel(); }
    var id = t.charAt(0) === "#" ? t.substr(1) : t;
    var r = topRoot();
    if (r.id === id) { return r; }
    var p = r.FindChildTraverse(id);
    if (!p) { throw new Error("panel not found: " + t); }
    return p;
  }

  var MAX_ITEMS = 500;

  // Iterables (Map entries / Set values) → bounded array.
  function serializeIter(it, depth) {
    var out = [], step;
    while (out.length < MAX_ITEMS && !(step = it.next()).done) { out.push(serialize(step.value, depth + 1)); }
    return out;
  }

  function serialize(v, depth) {
    depth = depth || 0;
    if (v === undefined) { return null; }
    if (v === null || typeof v === "number" || typeof v === "boolean" || typeof v === "string") { return v; }
    if (typeof v === "function") { return "(function " + (v.name || "anonymous") + ")"; }
    if (v && typeof v.paneltype === "string") { return { panel: describe(v) }; }
    // Tag checks instead of instanceof: values may come from another layout's JS context.
    // Never use toLocale*() here: locale APIs crash the game.
    var tag = Object.prototype.toString.call(v);
    if (tag === "[object Date]") { return isNaN(v.getTime()) ? "Invalid Date" : v.toISOString(); }
    if (tag === "[object Error]") {
      try { return String(v.name || "Error") + ": " + String(v.message); } catch (e) { return "(error)"; }
    }
    if (depth > 4) { return "(…)"; }
    if (Array.isArray(v)) { return v.slice(0, MAX_ITEMS).map(function (x) { return serialize(x, depth + 1); }); }
    if (tag === "[object Map]") {
      var pairs = [], step, it = v.entries();
      while (pairs.length < MAX_ITEMS && !(step = it.next()).done) {
        pairs.push([serialize(step.value[0], depth + 2), serialize(step.value[1], depth + 2)]);
      }
      return pairs;
    }
    if (tag === "[object Set]") { return serializeIter(v.values(), depth); }
    var o = {}, n = 0;
    try {
      for (var k in v) { if (n++ > 500) { break; } o[k] = serialize(v[k], depth + 1); }
    } catch (e) { return String(v); }
    return o;
  }

  // Non-ASCII → \uXXXX (per UTF-16 unit; JSON parsers re-pair surrogates), so console lines are
  // pure ASCII and a chunk boundary can never split a character.
  function asciiJson(text) {
    return text.replace(/[\u0080-\uffff]/g, function (c) {
      var h = c.charCodeAt(0).toString(16);
      return "\\u" + "0000".substr(h.length) + h;
    });
  }

  function reply(id, obj) {
    var text;
    try { text = JSON.stringify(obj); }
    catch (e) { text = JSON.stringify({ ok: false, error: "unserializable result: " + e }); }
    if (text === undefined) { text = "null"; }
    text = asciiJson(text);
    var total = Math.max(1, Math.ceil(text.length / OUT_CHUNK));
    for (var k = 0; k < total; k++) {
      $.Msg("@@MCP " + id + " " + k + "/" + total + " " + text.substr(k * OUT_CHUNK, OUT_CHUNK));
    }
  }

  // ---------- live CSS (inline styles) ----------
  function camel(prop) { return prop.replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); }); }

  // Apply {sheetName: rules[]}. Each panel records what we set in its "mcp_css" attribute
  // ({prop: [sheet, value]}) so unchanged props are skipped and sheets can be undone.
  var applyErrors = [];

  // cache (optional, used by the persistent bridge loop): {key: {panels, t}}; matches are re-queried
  // when a cached panel has been destroyed or after maxAge ms.
  function cachedQuery(sel, root, cache, maxAge) {
    if (!cache) { return query(sel, root); }
    var key = JSON.stringify(sel), e = cache[key], now = Date.now();
    var stale = !e || now - e.t > maxAge;
    if (!stale) {
      for (var i = 0; i < e.panels.length; i++) { if (!e.panels[i].IsValid()) { stale = true; break; } }
    }
    if (stale) { e = cache[key] = { panels: query(sel, root), t: now }; }
    e.used = now;
    return e.panels;
  }

  // Drop cache entries not used for maxIdle ms (e.g. selectors of sheets that were cleared), so
  // they don't keep arrays of panels alive. Returns the number dropped.
  function pruneCache(cache, maxIdle, now) {
    now = now === undefined ? Date.now() : now;
    var n = 0;
    for (var key in cache) {
      if (now - (cache[key].used || cache[key].t) > maxIdle) { delete cache[key]; n++; }
    }
    return n;
  }

  // sheets: [{name, rules}] in priority order (later sheet, and later rule within a sheet, wins).
  // Resolve first, write second: collect the winning [sheet, value] per panel per prop, then write
  // only props whose recorded "mcp_css" entry differs. Returns rule × panel matches.
  function applySheets(sheets, root, cache, maxAge) {
    var hits = 0, targets = [], wants = new Map();
    applyErrors = [];
    root = root || topRoot();
    for (var s = 0; s < sheets.length; s++) {
      var name = sheets[s].name, rules = sheets[s].rules || [];
      for (var r = 0; r < rules.length; r++) {
        var panels = cachedQuery(rules[r].sel, root, cache, maxAge || 5000);
        for (var j = 0; j < panels.length; j++) {
          var w = wants.get(panels[j]);
          if (!w) { w = {}; wants.set(panels[j], w); targets.push(panels[j]); }
          for (var prop in rules[r].decl) {
            var key = camel(prop);
            delete w[key];  // re-insert so the write order follows the winning declaration order
            w[key] = [name, rules[r].decl[prop], prop];
          }
          hits++;
        }
      }
    }
    for (var t = 0; t < targets.length; t++) {
      var p = targets[t], want = wants.get(p), rec, dirty = false;
      if (!p.IsValid()) { continue; }
      try { rec = JSON.parse(p.GetAttributeString("mcp_css", "{}")); } catch (e) { rec = {}; }
      for (var k in want) {
        var win = want[k], cur = rec[k];
        if (cur && cur[0] === win[0] && cur[1] === win[1]) { continue; }
        try { p.style[k] = win[1]; rec[k] = [win[0], win[1]]; dirty = true; } catch (e) {
          var msg = win[2] + ": " + win[1] + " → " + String(e && e.message || e);
          if (applyErrors.indexOf(msg) < 0 && applyErrors.length < 20) { applyErrors.push(msg); }
        }
      }
      if (dirty) { p.SetAttributeString("mcp_css", JSON.stringify(rec)); }
    }
    return hits;
  }

  function applyRules(sheet, rules, root) {
    return applySheets([{ name: sheet, rules: rules }], root);
  }

  // Reset an inline style. Many style props are aliases of a shorthand (fontSize → font,
  // marginLeft → margin, borderTopLeftRadius → border…): only the base accepts null.
  // Aliases whose base shorthand isn't a camel-case prefix of the alias.
  var STYLE_BASE = { horizontalAlign: "align", verticalAlign: "align" };

  function resetStyle(p, key) {
    if (STYLE_BASE[key]) {
      try { p.style[STYLE_BASE[key]] = null; return true; } catch (e) {}
    }
    var k = key;
    while (k) {
      try { p.style[k] = null; return true; } catch (e) {
        if (String(e).indexOf("alias") < 0) { return false; }
      }
      var m = k.match(/^(.*[a-z])[A-Z][a-z0-9]*$/);
      k = m ? m[1] : null;
    }
    return false;
  }

  function clearSheet(sheet) {
    var cleared = 0;
    walk(topRoot(), function (p) {
      var raw = p.GetAttributeString ? p.GetAttributeString("mcp_css", "") : "";
      if (!raw) { return; }
      var rec = {};
      try { rec = JSON.parse(raw); } catch (e) {}
      for (var key in rec) {
        if (sheet === null || rec[key][0] === sheet) {
          resetStyle(p, key);
          delete rec[key];
          cleared++;
        }
      }
      p.SetAttributeString("mcp_css", JSON.stringify(rec));
    });
    return cleared;
  }

  function getSheets() {
    try { return JSON.parse(bridgePanel().GetAttributeString("mcp_sheets", "{}")); } catch (e) { return {}; }
  }

  function setSheets(sheets) { bridgePanel().SetAttributeString("mcp_sheets", JSON.stringify(sheets)); }

  // Store a sheet as the most recently applied one (it wins over every other sheet). Each sheet
  // carries a sequence number: object key order isn't reliable (integer-like names sort first).
  function putSheet(sheets, name, sheet) {
    var top = 0;
    for (var n in sheets) { if (n !== name && (sheets[n].seq || 0) > top) { top = sheets[n].seq || 0; } }
    delete sheets[name];
    sheet.seq = top + 1;
    sheets[name] = sheet;
    return sheets;
  }

  function orderedSheets(sheets) {
    var list = [];
    for (var name in sheets) { list.push({ name: name, rules: sheets[name].rules, persist: sheets[name].persist, seq: sheets[name].seq || 0 }); }
    list.sort(function (a, b) { return a.seq - b.seq; });  // stable: legacy entries keep key order
    return list;
  }

  function reapplySheets(cache) {
    var all = orderedSheets(getSheets()), live = [];
    for (var i = 0; i < all.length; i++) { if (all[i].persist) { live.push(all[i]); } }
    if (!live.length) { return 0; }
    return applySheets(live, null, cache);
  }

  return {
    topRoot: topRoot, bridgePanel: bridgePanel, children: children, walk: walk, describe: describe,
    isInternal: isInternal, isProtected: isProtected, matchSelector: matchSelector,
    query: query, resolve: resolve, serialize: serialize, reply: reply, applyRules: applyRules,
    clearSheet: clearSheet, resetStyle: resetStyle, getSheets: getSheets, lastApplyErrors: function () { return applyErrors; },
    setSheets: setSheets, putSheet: putSheet, reapplySheets: reapplySheets, pruneCache: pruneCache,
  };
})();
