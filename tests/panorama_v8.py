"""A fake Panorama in an embedded V8 (mini-racer), for testing deadlock_mcp/js/*.js without the game.

The fake is deliberately small and strict: it implements only the panel / `$` API the bridge uses,
and where the real engine is known to be picky (inline style aliases, BLoadLayout with override on
a C++ panel) it fails loudly instead of being lenient.

Each script (bridge, request, registered layout script) runs wrapped in its own function with its own
`$`, which mimics Panorama giving every layout's scripts a separate global: `var MCP` in one script is
not visible to another. Panels (and their attributes) are shared, as in the game.

Usage::

    pytest.importorskip("py_mini_racer")
    from panorama_v8 import Panorama          # tests/ is on sys.path under pytest
    pano = Panorama()
    pano.build({"type": "Panel", "id": "Root", "children": [
        {"type": "Label", "id": "Title", "classes": ["Big"], "text": "hi"},
    ]})
    reply = pano.run_request("find", {"sel": [{"comb": " ", "classes": ["Big"]}]})
    assert reply["ok"] and reply["result"]["count"] == 1

    # the persistent bridge, driven by the fake clock
    ctx = pano.load_bridge(domain="m")
    pano.register_file("m00000123", "$.Msg('hello');")   # also makes BImageFileExists() see it
    pano.tick(0.2)
    assert "hello" in pano.msgs
"""

from __future__ import annotations

import json
import re

import py_mini_racer

from deadlock_mcp.bridge import render_bridge_js, render_request_js

FAKE_PANORAMA_JS = r"""
var __pano = (function () {
  var G = globalThis;
  var registry = {}, nextHandle = 1;
  var msgs = [], errors = [], loads = [], files = {}, images = {}, queue = [];
  var clock = 0, seq = 0;
  var cfg = { loadResult: true, loadResults: {}, crashed: null, probeError: null };
  var EPOCH = 1763000000000;
  var realNow = Date.now;
  var fakeNow = function () { return EPOCH + Math.round(clock * 1000); };
  Date.now = fakeNow;

  // ---------------------------------------------------------------- inline style
  // base props accept null (reset); aliases of a shorthand throw an "alias" error on null.
  var ALIAS = {
    fontSize: "font", fontWeight: "font", fontFamily: "font", fontStyle: "font",
    marginLeft: "margin", marginRight: "margin", marginTop: "margin", marginBottom: "margin",
    paddingLeft: "padding", paddingRight: "padding", paddingTop: "padding", paddingBottom: "padding",
    horizontalAlign: "align", verticalAlign: "align",
    borderColor: "border", borderWidth: "border", borderStyle: "border"
  };
  var BASE = ["font", "margin", "padding", "align", "border", "borderRadius", "width", "height", "x", "y",
              "color", "backgroundColor", "washColor", "opacity", "visibility", "transform",
              "flowChildren", "zIndex", "textAlign", "preTransformScale2d", "boxShadow"];
  var LEN = /^(-?\d*\.?\d+(px|%)?|fit-children|auto|fill-parent-flow\([^)]*\)|(width|height)-percentage\([^)]*\))$/;
  var COLOR = /^(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|gradient\(.*\)|transparent|none|white|black|red|green|blue|yellow)$/;
  function lengths(v) { var parts = v.split(/\s+/); for (var i = 0; i < parts.length; i++) { if (!LEN.test(parts[i])) { return false; } } return parts.length <= 4; }
  var VALID = {
    width: lengths, height: lengths, x: lengths, y: lengths, margin: lengths, padding: lengths,
    marginLeft: lengths, marginRight: lengths, marginTop: lengths, marginBottom: lengths,
    paddingLeft: lengths, paddingRight: lengths, paddingTop: lengths, paddingBottom: lengths,
    fontSize: lengths, borderWidth: lengths, borderRadius: lengths,
    color: function (v) { return COLOR.test(v); }, backgroundColor: function (v) { return COLOR.test(v); },
    washColor: function (v) { return COLOR.test(v); }, borderColor: function (v) { return COLOR.test(v); },
    opacity: function (v) { return /^\d*\.?\d+$/.test(v); },
    visibility: function (v) { return v === "visible" || v === "collapse"; },
    flowChildren: function (v) { return /^(none|right|down|left|up)(-wrap)?$/.test(v); },
    horizontalAlign: function (v) { return /^(left|center|right)$/.test(v); },
    verticalAlign: function (v) { return /^(top|center|bottom)$/.test(v); }
  };
  function known(k) { return BASE.indexOf(k) >= 0 || ALIAS.hasOwnProperty(k); }

  function makeStyle(panel) {
    var values = {}, log = [];
    return new Proxy({}, {
      get: function (_, k) {
        if (k === "__log") { return log; }
        if (k === "__values") { return values; }
        if (typeof k !== "string") { return undefined; }
        return values.hasOwnProperty(k) ? values[k] : null;
      },
      set: function (_, k, v) {
        if (typeof k !== "string" || !known(k)) { throw new Error("Unknown style property '" + String(k) + "'"); }
        if (v === null || v === undefined) {
          if (ALIAS.hasOwnProperty(k)) {
            throw new Error("Cannot clear style alias property '" + k + "'; clear its base property '" + ALIAS[k] + "'");
          }
          delete values[k];
          for (var a in ALIAS) { if (ALIAS[a] === k) { delete values[a]; } }
          log.push([k, null]);
          return true;
        }
        v = String(v);
        if (!v.length || (VALID[k] && !VALID[k](v))) {
          throw new Error("Invalid value '" + v + "' for style property '" + k + "'");
        }
        values[k] = v;
        log.push([k, v]);
        if (k === "visibility") { panel.visible = v !== "collapse"; }
        return true;
      }
    });
  }

  // ---------------------------------------------------------------- panels
  function Panel(type, id) {
    this.__h = nextHandle++;
    registry[this.__h] = this;
    this.paneltype = type || "Panel";
    this.id = id || "";
    this.layoutfile = "";
    this.visible = true;
    this.hittest = true;
    this.actuallayoutwidth = 0;
    this.actuallayoutheight = 0;
    this.actualxoffset = 0;
    this.actualyoffset = 0;
    this.__kids = [];
    this.__parent = null;
    this.__classes = [];
    this.__attrs = {};
    this.__valid = true;
    this.style = makeStyle(this);
  }
  Panel.prototype.IsValid = function () { return this.__valid; };
  Panel.prototype.GetParent = function () { return this.__parent; };
  Panel.prototype.GetChildCount = function () { return this.__kids.length; };
  Panel.prototype.GetChild = function (i) { return this.__kids[i] || null; };
  Panel.prototype.Children = function () { return this.__kids.slice(); };
  Panel.prototype.BHasClass = function (c) { return this.__classes.indexOf(c) >= 0; };
  Panel.prototype.AddClass = function (c) { if (!this.BHasClass(c)) { this.__classes.push(c); } };
  Panel.prototype.RemoveClass = function (c) { var i = this.__classes.indexOf(c); if (i >= 0) { this.__classes.splice(i, 1); } };
  Panel.prototype.GetAttributeString = function (k, d) { return this.__attrs.hasOwnProperty(k) ? this.__attrs[k] : d; };
  Panel.prototype.SetAttributeString = function (k, v) { this.__attrs[k] = String(v); };
  Panel.prototype.FindChild = function (id) {
    for (var i = 0; i < this.__kids.length; i++) { if (this.__kids[i].id === id) { return this.__kids[i]; } }
    return null;
  };
  function dfs(p, fn) {
    for (var i = 0; i < p.__kids.length; i++) {
      if (fn(p.__kids[i]) === true) { return p.__kids[i]; }
      var r = dfs(p.__kids[i], fn);
      if (r) { return r; }
    }
    return null;
  }
  Panel.prototype.FindChildTraverse = function (id) { return dfs(this, function (q) { return q.id === id; }); };
  Panel.prototype.FindChildrenWithClassTraverse = function (c) {
    var out = [];
    dfs(this, function (q) { if (q.BHasClass(c)) { out.push(q); } });
    return out;
  };
  function invalidate(p) {
    p.__valid = false;
    for (var i = 0; i < p.__kids.length; i++) { invalidate(p.__kids[i]); }
  }
  Panel.prototype.DeleteAsync = function (delay) {
    if (this.__parent) {
      var sib = this.__parent.__kids, i = sib.indexOf(this);
      if (i >= 0) { sib.splice(i, 1); }
    }
    this.__parent = null;
    invalidate(this);
  };
  Panel.prototype.__append = function (c) {
    if (c.__parent) { var sib = c.__parent.__kids; sib.splice(sib.indexOf(c), 1); }
    c.__parent = this;
    this.__kids.push(c);
  };
  Panel.prototype.BLoadLayout = function (path, override, partial) {
    loads.push({ panel: this.__h, id: this.id, path: path, override: !!override, partial: !!partial });
    if (override && this.paneltype !== "Panel") {
      cfg.crashed = "BLoadLayout(override) on C++ panel " + this.paneltype + " #" + this.id;
      throw new Error("FAKE GAME CRASH: " + cfg.crashed);
    }
    var m = /layout\/(.+)\.xml$/.exec(path), name = m ? m[1] : null;
    if (name !== null && files.hasOwnProperty(name)) {
      this.layoutfile = path;
      runScript(this, files[name], name);
      return true;
    }
    var ok = cfg.loadResults.hasOwnProperty(path) ? cfg.loadResults[path] : cfg.loadResult;
    if (ok) { this.layoutfile = path; }
    return !!ok;
  };

  // ---------------------------------------------------------------- $ and scripts
  function makeDollar(ctx) {
    return {
      GetContextPanel: function () { return ctx; },
      CreatePanel: function (type, parent, id) {
        var p = new Panel(type, id);
        if (parent) { parent.__append(p); }
        return p;
      },
      Msg: function () { msgs.push(Array.prototype.join.call(arguments, "")); },
      Schedule: function (delay, fn) {
        var id = ++seq;
        queue.push({ t: clock + Math.max(0, Number(delay) || 0), seq: id, fn: fn, ctx: ctx });
        return id;
      },
      CancelScheduled: function (id) { queue = queue.filter(function (e) { return e.seq !== id; }); },
      BImageFileExists: function (path) {
        if (cfg.probeError) { throw new Error(cfg.probeError); }
        return images.hasOwnProperty(path);
      }
    };
  }

  function recordError(label, e, file) {
    var s = String(e);
    errors.push({ label: label, error: s, stack: e && e.stack ? String(e.stack) : "" });
    msgs.push("JS Exception! *** " + s + " ***");
    if (file) { msgs.push("file:///panorama/scripts/" + file + ".vjs"); }
  }

  function runScript(ctx, src, file) {
    var fn;
    try { fn = (0, eval)("(function ($) {\n" + src + "\n})"); }
    catch (e) { recordError(file || "script", e, file); return false; }
    try { fn(makeDollar(ctx)); return true; }
    catch (e) { recordError(file || "script", e, file); return false; }
  }

  function tick(seconds) {
    var end = clock + seconds, guard = 0;
    for (;;) {
      var best = -1;
      for (var i = 0; i < queue.length; i++) {
        if (queue[i].t <= end && (best < 0 || queue[i].t < queue[best].t ||
            (queue[i].t === queue[best].t && queue[i].seq < queue[best].seq))) { best = i; }
      }
      if (best < 0) { break; }
      if (++guard > 200000) { throw new Error("fake clock: runaway Schedule loop"); }
      var item = queue.splice(best, 1)[0];
      clock = Math.max(clock, item.t);
      try { item.fn(); } catch (e) { recordError("scheduled", e, null); }
    }
    clock = end;
  }

  // ---------------------------------------------------------------- building / inspection
  function build(spec, parent) {
    var p = new Panel(spec.type, spec.id);
    var c = spec.classes || [];
    for (var i = 0; i < c.length; i++) { p.AddClass(c[i]); }
    var at = spec.attrs || {};
    for (var k in at) { p.SetAttributeString(k, at[k]); }
    if (spec.layout) { p.layoutfile = spec.layout; }
    if (spec.text !== undefined) { p.text = spec.text; }
    if (spec.visible === false) { p.visible = false; }
    if (spec.size) { p.actuallayoutwidth = spec.size[0]; p.actuallayoutheight = spec.size[1]; }
    if (spec.pos) { p.actualxoffset = spec.pos[0]; p.actualyoffset = spec.pos[1]; }
    if (parent) { parent.__append(p); }
    var kids = spec.children || [];
    for (var j = 0; j < kids.length; j++) { build(kids[j], p); }
    return p.__h;
  }

  function info(p) {
    return {
      handle: p.__h, id: p.id, type: p.paneltype, classes: p.__classes.slice(), attrs: p.__attrs,
      valid: p.__valid, visible: p.visible, hittest: p.hittest, layoutfile: p.layoutfile,
      parent: p.__parent ? p.__parent.__h : null,
      children: p.__kids.map(function (q) { return q.__h; }),
      style: p.style.__values, style_log: p.style.__log
    };
  }

  return {
    Panel: Panel, P: function (h) { return registry[h] || null; }, build: build, info: info,
    runScript: runScript, tick: tick, makeDollar: makeDollar,
    msgs: msgs, errors: errors, loads: loads, files: files, images: images, cfg: cfg,
    queueSize: function () { return queue.length; },
    now: function () { return clock; },
    useRealClock: function () { Date.now = realNow; },
    handleOf: function (p) { return p ? p.__h : null; },
    clearStyleLogs: function () { for (var h in registry) { registry[h].style.__log.length = 0; } }
  };
})();
"""


class Panorama:
    """Python handle on a fake Panorama V8 context. Panels are referred to by integer handles."""

    def __init__(self, tree: dict | None = None, *, fake_now: bool = True):
        self.ctx = py_mini_racer.MiniRacer()
        self.ctx.eval(FAKE_PANORAMA_JS)
        if not fake_now:
            self.ctx.eval("__pano.useRealClock()")
        self.root: int | None = None
        self._serial = 0
        self._msg_seen = 0
        self.last_reply_lines: list[str] = []
        if tree is not None:
            self.build(tree)

    # ---- raw access
    def eval(self, js: str):
        """Evaluate JS in the harness global (no `$`, no MCP) and return mini-racer's value."""
        return self.ctx.eval(js)

    def json(self, expr: str):
        """Evaluate a JS expression and return it via JSON (handles objects/arrays)."""
        raw = self.ctx.eval(f"JSON.stringify({expr})")
        return None if raw is None else json.loads(raw)

    @staticmethod
    def ref(handle: int) -> str:
        """JS expression for the panel with this handle, e.g. f"{pano.ref(h)}.id"."""
        return f"__pano.P({int(handle)})"

    # ---- tree
    def build(self, spec: dict, parent: int | None = None) -> int:
        """Create panels from {type, id, classes[], attrs{}, layout, text, visible, size, pos, children[]}.
        The first parentless tree becomes `root`. Returns the new panel's handle."""
        par = self.ref(parent) if parent is not None else "null"
        h = self.ctx.eval(f"__pano.build({json.dumps(spec)}, {par})")
        if parent is None and self.root is None:
            self.root = h
        return h

    def handle(self, panel_id: str, start: int | None = None) -> int:
        """Handle of the first panel with this id (depth-first, `start` itself included)."""
        s = self.ref(start if start is not None else self.root)
        h = self.ctx.eval(f"(function (s) {{ if (s.id === {json.dumps(panel_id)}) return s.__h; "
                          f"var p = s.FindChildTraverse({json.dumps(panel_id)}); return p ? p.__h : -1; }})({s})")
        if h < 0:
            raise KeyError(panel_id)
        return h

    def info(self, handle: int) -> dict:
        """State of a panel: id, type, classes, attrs, valid, visible, parent, children, style, style_log."""
        return self.json(f"__pano.info({self.ref(handle)})")

    def attr(self, handle: int, name: str, default: str = "") -> str:
        return self.ctx.eval(f"{self.ref(handle)}.GetAttributeString({json.dumps(name)}, {json.dumps(default)})")

    def style(self, handle: int) -> dict:
        return self.info(handle)["style"]

    def style_log(self, handle: int) -> list:
        return self.info(handle)["style_log"]

    def clear_style_logs(self) -> None:
        self.ctx.eval("__pano.clearStyleLogs()")

    # ---- console / engine state
    @property
    def msgs(self) -> list[str]:
        return self.json("__pano.msgs")

    def take_msgs(self) -> list[str]:
        """Messages logged since the previous take_msgs() call."""
        all_ = self.msgs
        new, self._msg_seen = all_[self._msg_seen:], len(all_)
        return new

    @property
    def errors(self) -> list[dict]:
        return self.json("__pano.errors")

    @property
    def loads(self) -> list[dict]:
        """Every BLoadLayout call: {panel, id, path, override, partial}."""
        return self.json("__pano.loads")

    @property
    def crashed(self) -> str | None:
        """Set when a script did something that crashes the real game (BLoadLayout override on C++ panel)."""
        return self.ctx.eval("__pano.cfg.crashed")

    def set_load_result(self, ok: bool, path: str | None = None) -> None:
        """Result of BLoadLayout for unregistered layouts (all paths, or one path)."""
        if path is None:
            self.ctx.eval(f"__pano.cfg.loadResult = {json.dumps(ok)}")
        else:
            self.ctx.eval(f"__pano.cfg.loadResults[{json.dumps(path)}] = {json.dumps(ok)}")

    def add_image(self, path: str) -> None:
        """Make $.BImageFileExists(path) true."""
        self.ctx.eval(f"__pano.images[{json.dumps(path)}] = true")

    def remove_image(self, path: str) -> None:
        self.ctx.eval(f"delete __pano.images[{json.dumps(path)}]")

    def register_file(self, name: str, src: str, probe: bool = True) -> None:
        """Register a request-style layout `name` (e.g. "m00000042") whose script is `src`.
        BLoadLayout("file://{resources}/layout/<name>.xml") runs it in the target panel's context.
        With probe=True also makes $.BImageFileExists("s2r://panorama/layout/<name>.vxml") true."""
        self.ctx.eval(f"__pano.files[{json.dumps(name)}] = {json.dumps(src)}")
        if probe:
            self.add_image(f"s2r://panorama/layout/{name}.vxml")

    def unregister_file(self, name: str) -> None:
        self.ctx.eval(f"delete __pano.files[{json.dumps(name)}]")
        self.remove_image(f"s2r://panorama/layout/{name}.vxml")

    def tick(self, seconds: float) -> None:
        """Advance the fake clock, running $.Schedule callbacks that fall due (Date.now follows it)."""
        self.ctx.eval(f"__pano.tick({float(seconds)!r})")

    @property
    def now(self) -> float:
        return self.ctx.eval("__pano.now()")

    # ---- scripts
    def run(self, src: str, context: int | None = None) -> bool:
        """Run a script in the context of a panel (default: root). False if it threw (see `errors`)."""
        ctx = context if context is not None else self.root
        return self.ctx.eval(f"__pano.runScript({self.ref(ctx)}, {json.dumps(src)}, null)")

    def bridge_handle(self) -> int:
        """The panel flagged mcp_bridge="1"; if none, flag the root (as in the HUD, root == bridge)."""
        h = self.ctx.eval("(function (r) { if (r.GetAttributeString('mcp_bridge', '') === '1') return r.__h; "
                          "var out = -1; (function w(p) { for (var i = 0; i < p.GetChildCount() && out < 0; i++) {"
                          " var c = p.GetChild(i); if (c.GetAttributeString('mcp_bridge', '') === '1') out = c.__h;"
                          " else w(c); } })(r); return out; })" f"({self.ref(self.root)})")
        if h < 0:
            self.ctx.eval(f"{self.ref(self.root)}.SetAttributeString('mcp_bridge', '1')")
            h = self.root
        return h

    def load_bridge(self, domain: str = "m", poll: float = 0.1, context: int | None = None) -> int:
        """Run the real bridge script (render_bridge_js) in `context` (default: root). Returns its handle."""
        ctx = context if context is not None else self.root
        if not self.run(render_bridge_js(domain, poll), ctx):
            raise RuntimeError(f"bridge script failed: {self.errors}")
        return ctx

    def bridge_base(self, domain: str = "m") -> int:
        """`base` serial from the bridge's ready line."""
        for line in self.msgs:
            m = re.match(rf"@@MCPLOG ready domain={domain} version=\S+ base=(\d+)", line)
            if m:
                return int(m[1])
        raise LookupError("no ready line")

    def request_js(self, op: str, args: dict | None = None, code: str | None = None, keep: bool = False,
                   *, serial: int | None = None, domain: str = "m", **kw) -> str:
        if serial is None:
            self._serial += 1
            serial = self._serial
        return render_request_js(serial, op, args or {}, code, keep, domain, **kw)

    def run_request(self, op: str, args: dict | None = None, code: str | None = None, keep: bool = False,
                    *, serial: int | None = None, domain: str = "m", bridge: int | None = None, **kw) -> dict:
        """Run one request the way the bridge would (in a hidden McpReq host under the bridge panel)
        and return the parsed reply ({"ok": ..., "result"/"error": ...})."""
        if serial is None:
            self._serial += 1
            serial = self._serial
        src = render_request_js(serial, op, args or {}, code, keep, domain, **kw)
        b = bridge if bridge is not None else self.bridge_handle()
        host = self.ctx.eval(
            f"(function () {{ var h = __pano.makeDollar(null).CreatePanel('Panel', {self.ref(b)}, "
            f"'McpReq{domain}{serial:08d}'); h.visible = false; h.hittest = false; return h.__h; }})()")
        start = len(self.msgs)
        self.run(src, host)
        lines = self.msgs[start:]
        self.last_reply_lines = [ln for ln in lines if ln.startswith("@@MCP ")]
        if not self.last_reply_lines:
            raise AssertionError(f"no reply; console: {lines}; errors: {self.errors}")
        return parse_reply(self.last_reply_lines)


def parse_reply(lines: list[str], req_id: str | None = None) -> dict:
    """Reassemble `@@MCP <id> <k>/<n> <chunk>` lines (of one id) and json.loads the result."""
    parts: dict[int, str] = {}
    total = None
    for ln in lines:
        m = re.match(r"@@MCP (\S+) (\d+)/(\d+) (.*)$", ln, re.S)
        if not m:
            continue
        if req_id is None:
            req_id = m[1]
        if m[1] != req_id:
            continue
        k, n = int(m[2]), int(m[3])
        assert total in (None, n), "inconsistent chunk count"
        total = n
        assert k not in parts, f"duplicate chunk {k}"
        parts[k] = m[4]
    assert total is not None and sorted(parts) == list(range(total)), f"missing chunks: {sorted(parts)} of {total}"
    return json.loads("".join(parts[i] for i in range(total)))
