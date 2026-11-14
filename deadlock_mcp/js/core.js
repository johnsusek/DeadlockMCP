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

  function isInternal(p) {
    var id = p.id || "";
    return id.lastIndexOf("McpReq", 0) === 0;
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
    var c = candidates(sel, root || topRoot()), out = [];
    for (var i = 0; i < c.length; i++) {
      if (!isInternal(c[i]) && matchSelector(c[i], sel, sel.length - 1)) { out.push(c[i]); }
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

  function serialize(v, depth) {
    depth = depth || 0;
    if (v === undefined) { return null; }
    if (v === null || typeof v === "number" || typeof v === "boolean" || typeof v === "string") { return v; }
    if (typeof v === "function") { return "(function " + (v.name || "anonymous") + ")"; }
    if (v && typeof v.paneltype === "string") { return { panel: describe(v) }; }
    if (depth > 4) { return "(…)"; }
    if (Array.isArray(v)) { return v.slice(0, 500).map(function (x) { return serialize(x, depth + 1); }); }
    var o = {}, n = 0;
    try {
      for (var k in v) { if (n++ > 500) { break; } o[k] = serialize(v[k], depth + 1); }
    } catch (e) { return String(v); }
    return o;
  }

  function reply(id, obj) {
    var text;
    try { text = JSON.stringify(obj); }
    catch (e) { text = JSON.stringify({ ok: false, error: "unserializable result: " + e }); }
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

  function applySheets(bySheet, root, cache, maxAge) {
    var hits = 0;
    applyErrors = [];
    root = root || topRoot();
    for (var name in bySheet) {
      var rules = bySheet[name];
      for (var r = 0; r < rules.length; r++) {
        var panels = cachedQuery(rules[r].sel, root, cache, maxAge || 5000);
        for (var j = 0; j < panels.length; j++) {
          var p = panels[j], rec, dirty = false;
          try { rec = JSON.parse(p.GetAttributeString("mcp_css", "{}")); } catch (e) { rec = {}; }
          for (var prop in rules[r].decl) {
            var key = camel(prop), val = rules[r].decl[prop], cur = rec[key];
            if (cur && cur[0] === name && cur[1] === val) { continue; }
            try { p.style[key] = val; rec[key] = [name, val]; dirty = true; } catch (e) {
              var msg = prop + ": " + val + " → " + String(e && e.message || e);
              if (applyErrors.indexOf(msg) < 0 && applyErrors.length < 20) { applyErrors.push(msg); }
            }
          }
          if (dirty) { p.SetAttributeString("mcp_css", JSON.stringify(rec)); }
          hits++;
        }
      }
    }
    return hits;
  }

  function applyRules(sheet, rules, root) {
    var one = {};
    one[sheet] = rules;
    return applySheets(one, root);
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

  function reapplySheets(cache) {
    var sheets = getSheets(), live = {}, any = false;
    for (var name in sheets) { if (sheets[name].persist) { live[name] = sheets[name].rules; any = true; } }
    if (!any) { return 0; }
    return applySheets(live, null, cache);
  }

  return {
    topRoot: topRoot, bridgePanel: bridgePanel, children: children, walk: walk, describe: describe,
    query: query, resolve: resolve, serialize: serialize, reply: reply, applyRules: applyRules,
    clearSheet: clearSheet, resetStyle: resetStyle, getSheets: getSheets, lastApplyErrors: function () { return applyErrors; }, setSheets: setSheets, reapplySheets: reapplySheets,
  };
})();
