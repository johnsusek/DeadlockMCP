// Deadlock MCP — request runner (appended to core.js + the generated __mcpUser function).
// Runs once, in the context of a hidden panel created by the bridge.
(function () {
  var REQ = __MCP_REQUEST__;
  var host = $.GetContextPanel();
  if (REQ.keep) { host.SetAttributeString("mcp_keep", "1"); }

  function label(p) { return (p.paneltype || "panel") + (p.id ? " #" + p.id : ""); }

  // Why BLoadLayout(..., override=true) must not touch this panel, or "" if it may.
  function replaceBlocker(p) {
    if (MCP.isProtected(p)) { return "it is the UI root or contains the MCP bridge (replacing it would wipe the UI)"; }
    if (p.paneltype !== "Panel") {
      return "it is a C++ panel type (" + p.paneltype + "); re-loading a layout into it crashes the game";
    }
    return "";
  }

  var ops = {
    ping: function () {
      return { root: MCP.describe(MCP.topRoot()), bridge: MCP.describe(MCP.bridgePanel()) };
    },

    eval: function (a) {
      var target = a.target ? MCP.resolve(a.target) : MCP.bridgePanel();
      var v = __mcpUser(target, MCP.topRoot(), MCP.resolve, MCP.query, MCP.children, MCP.describe);
      return MCP.serialize(v);
    },

    tree: function (a) {
      var start = MCP.resolve(a.target), max = a.max || 400, depth = a.depth == null ? 3 : a.depth, n = 0;
      function rec(p, d) {
        var node = MCP.describe(p);
        n++;
        if (d < depth && n < max) {
          var c = MCP.children(p), kids = [];
          for (var i = 0; i < c.length && n < max; i++) {
            if (MCP.isInternal(c[i])) { continue; }
            kids.push(rec(c[i], d + 1));
          }
          if (kids.length) { node.kids = kids; }
        }
        return node;
      }
      return { tree: rec(start, 0), truncated: n >= max };
    },

    find: function (a) {
      var res = MCP.query(a.sel, a.target ? MCP.resolve(a.target) : MCP.topRoot());
      return { count: res.length, panels: res.slice(0, a.max || 100).map(MCP.describe) };
    },

    load_layout: function (a) {
      var target = MCP.resolve(a.target), p = target, holder = null;
      if (a.mode !== "append") {
        var why = replaceBlocker(target);
        if (why) {
          throw new Error("refusing to replace the layout of " + label(target) + ": " + why +
                          ". Use mode 'append' to add the layout inside it, or compile_source to override the game's layout file.");
        }
      }
      if (a.mode === "append") {
        if (a.id) {
          var existing = target.FindChild(a.id);
          if (existing && MCP.isProtected(existing)) {
            throw new Error("refusing to replace " + label(existing) + " (it contains the MCP bridge); pick another id");
          }
          if (existing) { existing.DeleteAsync(0); }
        }
        // A full-size, click-through holder carries the id; the layout loads into a child so the
        // layout's root element keeps its own CSS (Panorama maps a layout's root onto the panel
        // BLoadLayout is called on).
        holder = $.CreatePanel("Panel", target, a.id || ("McpPanel" + Date.now()));
        holder.style.width = "100%";
        holder.style.height = "100%";
        holder.hittest = false;
        p = $.CreatePanel("Panel", holder, (a.id || "McpPanel") + "_layout");
      }
      var ok = p.BLoadLayout(a.path, a.mode !== "append", false);
      if (!ok) {
        if (holder) { holder.DeleteAsync(0); }
        throw new Error("BLoadLayout failed for " + a.path + " (see console_log for Panorama errors)");
      }
      MCP.reapplySheets();
      return { panel: MCP.describe(a.mode === "append" ? p.GetParent() : p), layout_root: MCP.describe(p) };
    },

    // Re-load a game layout file into every live panel that is the root of it.
    reload_layout: function (a) {
      var want = a.layout.toLowerCase(), roots = [];
      function norm(f) { return (f || "").replace(/\\/g, "/").toLowerCase(); }
      MCP.walk(MCP.topRoot(), function (p) {
        if (MCP.isInternal(p)) { return false; }
        var orig = p.GetAttributeString("mcp_orig_layout", "");
        var lf = orig || norm(p.layoutfile);
        if (lf.slice(-want.length) !== want) { return; }
        var par = p.GetParent();
        var plf = par ? (par.GetAttributeString("mcp_orig_layout", "") || norm(par.layoutfile)) : "";
        if (plf === lf) { return; }
        roots.push(p);
        return false;
      });
      var done = [], skipped = [];
      for (var i = 0; i < roots.length; i++) {
        var p = roots[i], why = replaceBlocker(p);
        if (why) { skipped.push({ panel: MCP.describe(p), reason: why }); continue; }
        if (!p.GetAttributeString("mcp_orig_layout", "")) { p.SetAttributeString("mcp_orig_layout", norm(p.layoutfile)); }
        var ok = p.BLoadLayout(a.path, true, false);
        if (a.revert) { p.SetAttributeString("mcp_orig_layout", ""); }
        done.push({ ok: ok, panel: MCP.describe(p) });
      }
      MCP.reapplySheets();
      return { reloaded: done.length, roots: done, skipped: skipped };
    },

    css_apply: function (a) {
      var name = a.sheet || "default";
      MCP.clearSheet(name);
      // Re-inserting moves the sheet to the end: the most recently applied sheet wins, both here
      // and in the bridge's periodic re-apply.
      var sheets = MCP.putSheet(MCP.getSheets(), name, { rules: a.rules, persist: a.persist !== false });
      MCP.setSheets(sheets);
      var matched = MCP.applyRules(name, a.rules), errs = MCP.lastApplyErrors();
      var out = { sheet: name, rules: a.rules.length, matched: matched };
      if (errs.length) { out.invalid = errs; }
      return out;
    },

    css_clear: function (a) {
      var name = a.sheet == null ? null : a.sheet, sheets = MCP.getSheets();
      if (name === null) { sheets = {}; } else { delete sheets[name]; }
      MCP.setSheets(sheets);
      var cleared = MCP.clearSheet(name);
      MCP.reapplySheets();  // values from the remaining persistent sheets come back now, not a second later
      return { cleared: cleared, remaining: Object.keys(sheets) };
    },

    delete: function (a) {
      var p = MCP.resolve(a.target);
      if (MCP.isProtected(p)) {
        throw new Error("refusing to delete " + label(p) + ": it is the UI root or contains the MCP bridge, so the " +
                        "whole UI would go with it. Delete a specific child panel, or hide it with css_apply " +
                        "(visibility: collapse) instead.");
      }
      p.DeleteAsync(0);
      return { deleted: a.target };
    },
  };

  var op = ops[REQ.op];
  if (!op) { MCP.reply(REQ.id, { ok: false, error: "unknown op: " + REQ.op }); return; }
  try {
    MCP.reply(REQ.id, { ok: true, result: op(REQ.args || {}) });
  } catch (e) {
    var msg = String(e), stack = e && e.stack ? String(e.stack) : "";
    if (stack.indexOf(msg) === 0) { stack = stack.substr(msg.length).replace(/^\s+/, ""); }
    MCP.reply(REQ.id, { ok: false, error: msg, stack: stack || undefined });
  }
})();
