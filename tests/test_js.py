"""Runs the in-game JS (deadlock_mcp/js/*.js) in a real V8 against a fake Panorama (tests/panorama_v8.py)."""

import json
import re

import esprima
import pytest

pytest.importorskip("py_mini_racer")

from panorama_v8 import Panorama, parse_reply  # noqa: E402  (tests/ is on sys.path under pytest)

from deadlock_mcp.bridge import render_bridge_js, render_request_js  # noqa: E402


# ---------------------------------------------------------------- helpers


def sel(text):
    """'.Bar > Label.Text #X' → structured selector as Python sends it."""
    out, comb = [], " "
    for tok in text.split():
        if tok == ">":
            comb = ">"
            continue
        m = re.fullmatch(r"(\*|[A-Za-z_]\w*)?(?:#(\w+))?((?:\.[\w-]+)*)", tok)
        assert m, tok
        c = {"comb": comb}
        if m[1]:
            c["type"] = m[1]
        if m[2]:
            c["id"] = m[2]
        if m[3]:
            c["classes"] = m[3][1:].split(".")
        out.append(c)
        comb = " "
    return out


def P(type_, id_="", classes=(), children=(), **kw):
    d = {"type": type_, "id": id_, "classes": list(classes), "children": list(children)}
    d.update(kw)
    return d


def ok(reply):
    assert reply["ok"], reply
    return reply["result"]


def js(pano, code, **kw):
    """Run user code through the eval op (MCP, $root, $find... and __pano are in scope)."""
    return ok(pano.run_request("eval", code=code, **kw))


def selector_tree():
    return P("Panel", "Root", children=[
        P("Panel", "Hud", ["HudRoot"], [
            P("Panel", "Bar", ["Bar", "Top"], [
                P("Label", "Name", ["Text"], text="first"),
                P("Label", "Name", ["Text", "Big"], text="second"),
                P("Panel", "", ["Row"], [
                    P("Label", "", ["Text"]),
                    P("Image", "Icon", ["Icon"]),
                ]),
            ]),
            P("Panel", "Dup", ["Box"], [
                P("Panel", "Dup", ["Box", "Inner"], [
                    P("Label", "Deep", ["Text"]),
                ]),
            ]),
            P("Panel", "McpReqm00000001", [], [
                P("Label", "Hidden", ["Text", "Big"]),
                P("Panel", "", ["Box", "Bar"], [P("Label", "", ["Text"])]),
            ]),
        ]),
        P("Panel", "Other", [], [
            P("Image", "", ["Icon"]),
            P("Panel", "", ["Bar"], [P("Label", "", ["Text", "Big"])]),
        ]),
    ])


@pytest.fixture
def pano():
    return Panorama(selector_tree())


def hud_tree():
    """Like the real HUD: the top root is the bridge panel."""
    return P("Panel", "CitadelHudRoot", attrs={"mcp_bridge": "1"}, layout="panorama/layout/base_hud.xml", children=[
        P("CitadelHudTopBar", "TopBar", ["TopBar"], layout="panorama/layout/hud/top_bar.xml", children=[
            P("Label", "Clock", ["Clock"], text="12:00"),
        ]),
        P("Panel", "Stats", ["Stats"], layout="panorama/layout/hud/stats.xml", children=[
            P("Panel", "StatsInner", ["Stats"], layout="panorama/layout/hud/stats.xml"),
            P("Label", "Num", ["Num"], text="5"),
        ]),
        P("Panel", "Stats2", ["Stats"], layout="panorama\\layout\\hud\\STATS.xml"),
    ])


# ---------------------------------------------------------------- generated scripts still parse as ES2017


def test_rendered_scripts_parse_es2017():
    esprima.parseScript(render_bridge_js("h"))
    esprima.parseScript(render_request_js(7, "eval", {}, code="1+1", keep=False, domain="h"))


# ---------------------------------------------------------------- selectors


SELECTORS = [
    ".Text", "#Name", "Label", "Image", "*", ".Bar > Label", ".Bar Label", "#Hud .Text", "Panel > Panel > Label",
    ".Box .Box Label", ".Box > .Inner", "#Dup", ".Bar .Row > Image.Icon", "* > .Big", "#Hud > #Bar > Label.Text.Big",
    ".Top Label", "Panel .Text", "#Dup #Dup", ".Box", ".Big", "#Hidden", ".Text.Big", "Label.Icon", "#Nope",
    "#Root > Panel", "#Root .Box > Panel > Label",
]

BRUTE = """
var out = [];
(function w(p) {
  if (MCP.isInternal(p)) { return; }
  if (MCP.matchSelector(p, S, S.length - 1)) { out.push(p.__h); }
  for (var i = 0; i < p.GetChildCount(); i++) { w(p.GetChild(i)); }
})(START);
return out;
"""


@pytest.mark.parametrize("target", [None, "Other", "Hud"])
def test_query_matches_brute_force(pano, target):
    for s in SELECTORS:
        structured = json.dumps(sel(s))
        start = "$root" if target is None else f"$find({json.dumps(target)})"
        got = js(pano, f"var r = $query({structured}, {start}); return r.map(function (p) {{ return p.__h; }});")
        want = js(pano, f"var S = {structured}, START = {start};" + BRUTE)
        assert len(got) == len(set(got)), (s, "duplicates")
        assert sorted(got) == sorted(want), s


def test_query_specific_cases(pano):
    def ids(s):
        return [p["id"] for p in ok(pano.run_request("find", {"sel": sel(s)}))["panels"]]

    assert ids("#Name") == ["Name", "Name"]  # duplicate ids are all found
    assert sorted(ids("#Dup")) == ["Dup", "Dup"]
    assert ids("#Hidden") == []  # inside an McpReq host
    assert len(ids(".Big")) == 2  # not the one under McpReq
    assert ids(".Box > .Inner") == ["Dup"]
    assert ids("#Hud > #Bar > Label.Text.Big") == ["Name"]


def test_find_max_and_count(pano):
    r = ok(pano.run_request("find", {"sel": sel("Label"), "max": 2}))
    assert r["count"] == 5 and len(r["panels"]) == 2


# ---------------------------------------------------------------- resolve / describe


def test_resolve(pano):
    assert js(pano, "return MCP.resolve('root').id") == "Root"
    assert js(pano, "return MCP.resolve('').id") == "Root"
    assert js(pano, "return MCP.resolve('#Root').id") == "Root"
    assert js(pano, "return MCP.resolve('Deep').id") == "Deep"
    assert js(pano, "return MCP.resolve('#Name').text") == "first"  # first match
    assert js(pano, "return MCP.resolve('bridge').id") == "Root"  # harness flags the root
    r = pano.run_request("eval", code="return MCP.resolve('#Nope')")
    assert not r["ok"] and "panel not found: #Nope" in r["error"]


def test_bridge_panel_is_flagged_ancestor():
    pano = Panorama(P("Panel", "Root", children=[P("Panel", "B", attrs={"mcp_bridge": "1"})]))
    assert js(pano, "return [MCP.bridgePanel().id, MCP.topRoot().id]") == ["B", "Root"]


def test_describe():
    pano = Panorama(P("Panel", "Root", children=[
        P("Label", "L", text="x" * 100, size=[10.4, 20.6], pos=[1.2, 3.7], visible=False, layout="panorama/layout/a.xml"),
    ]))
    d = js(pano, "return MCP.describe($find('L'))")
    assert d == {"id": "L", "type": "Label", "layout": "panorama/layout/a.xml", "visible": False,
                 "text": "x" * 80 + "…", "size": [10, 21], "pos": [1, 4], "children": 0}
    assert js(pano, "var p = $find('L'); p.DeleteAsync(0); return MCP.describe(p)") is None


# ---------------------------------------------------------------- serialize / reply


def test_serialize_basics(pano):
    r = js(pano, "return {n: 1, s: 'a', b: true, u: undefined, z: null, f: function foo() {}, g: function () {},"
                 " arr: [1, [2, [3]]], p: $find('Deep')}")
    assert r["n"] == 1 and r["s"] == "a" and r["b"] is True and r["u"] is None and r["z"] is None
    assert r["f"] == "(function foo)" and r["g"] == "(function g)"
    assert r["arr"] == [1, [2, [3]]]
    assert r["p"]["panel"]["id"] == "Deep"


def test_serialize_depth_and_bounds(pano):
    r = js(pano, "return {a: {b: {c: {d: {e: {f: {g: 1}}}}}}}")
    assert r["a"]["b"]["c"]["d"]["e"] == "(…)"
    assert len(js(pano, "var a = []; for (var i = 0; i < 1000; i++) a.push(i); return a")) == 500
    assert js(pano, "return undefined") is None


def test_serialize_date_error_map_set(pano):
    r = js(pano, "return {d: new Date(Date.UTC(2026, 10, 14, 1, 2, 3)), bad: new Date(NaN),"
                 " e: new TypeError('boom'), m: new Map([['a', 1], ['b', [2]]]), s: new Set([1, 'x']),"
                 " big: new Set(Array.from({length: 900}, function (_, i) { return i; }))}")
    assert r["d"] == "2026-11-14T01:02:03.000Z"
    assert r["bad"] == "Invalid Date"
    assert r["e"] == "TypeError: boom"
    assert r["m"] == [["a", 1], ["b", [2]]]
    assert r["s"] == [1, "x"]
    assert len(r["big"]) == 500


def test_locale_apis_are_guarded(pano):
    r = pano.run_request("eval", code="(1234).toLocaleString()")
    assert not r["ok"] and "locale APIs crash Deadlock" in r["error"]
    r = pano.run_request("eval", code="Intl.NumberFormat")
    assert r["ok"]  # the stub itself is a function; calling it throws
    r = pano.run_request("eval", code="new Date().toLocaleDateString()")
    assert not r["ok"]


def test_reply_chunks_reassemble(pano):
    r = pano.run_request("eval", code="var s = ''; for (var i = 0; i < 2000; i++) s += String.fromCharCode(97 + i % 26); return s")
    assert ok(r) == "".join(chr(97 + i % 26) for i in range(2000))
    lines = pano.last_reply_lines
    assert len(lines) == 6
    for k, ln in enumerate(lines):
        m = re.match(r"@@MCP (\S+) (\d+)/(\d+) (.*)$", ln)
        assert m[1] == "m" + str(pano._serial) and int(m[2]) == k and int(m[3]) == 6
        assert len(m[4]) <= 400


def test_reply_echoes_opaque_id(pano):
    pano.run_request("ping", serial=42, domain="h", nonce="ab12")
    assert pano.last_reply_lines[0].startswith("@@MCP h42-ab12 0/1 {")


@pytest.mark.parametrize("pad", range(370, 402))
def test_reply_is_ascii_and_survives_astral_split(pano, pad):
    s = "a" * pad + "😀é☃" + "b" * 10
    r = pano.run_request("eval", code=f"return {json.dumps(s)}")
    for ln in pano.last_reply_lines:
        assert ln.isascii(), ln
    assert ok(r) == s
    # the chunk text itself (UTF-16 slices of the JSON text) must never hold lone surrogates
    for ln in pano.last_reply_lines:
        ln.encode("utf-8")


# ---------------------------------------------------------------- inline CSS


def css_tree():
    return P("Panel", "Root", children=[
        P("Panel", "A", ["X"], [P("Label", "A1", ["X", "Y"])]),
        P("Panel", "B", ["Y"]),
    ])


def test_css_apply_and_clear():
    pano = Panorama(css_tree())
    a, a1, b = pano.handle("A"), pano.handle("A1"), pano.handle("B")
    r = ok(pano.run_request("css_apply", {"sheet": "s1", "rules": [
        {"sel": sel(".X"), "decl": {"background-color": "#ff0000", "font-size": "20px"}},
        {"sel": sel(".Y"), "decl": {"horizontal-align": "center", "color": "nope"}},
    ]}))
    assert r["sheet"] == "s1" and r["rules"] == 2 and r["matched"] == 4
    assert r["invalid"] and "color: nope" in r["invalid"][0] and "Invalid value" in r["invalid"][0]
    assert pano.style(a) == {"backgroundColor": "#ff0000", "fontSize": "20px"}
    assert pano.style(a1) == {"backgroundColor": "#ff0000", "fontSize": "20px", "horizontalAlign": "center"}
    assert pano.style(b) == {"horizontalAlign": "center"}
    assert json.loads(pano.attr(a1, "mcp_css")) == {
        "backgroundColor": ["s1", "#ff0000"], "fontSize": ["s1", "20px"], "horizontalAlign": ["s1", "center"]}
    sheets = json.loads(pano.attr(pano.root, "mcp_sheets"))
    assert list(sheets) == ["s1"] and sheets["s1"]["persist"] is True

    # unchanged re-apply writes nothing
    pano.clear_style_logs()
    ok(pano.run_request("eval", code="return MCP.reapplySheets()"))
    assert pano.style_log(a1) == []

    r = ok(pano.run_request("css_clear", {"sheet": "s1"}))
    assert r == {"cleared": 6, "remaining": []}
    assert pano.style(a) == {} and pano.style(a1) == {} and pano.style(b) == {}
    # aliases are reset through their base shorthand
    assert ["font", None] in pano.style_log(a1) and ["align", None] in pano.style_log(a1)
    assert json.loads(pano.attr(a1, "mcp_css")) == {}


def test_reset_style_alias_walk(pano):
    r = js(pano, "var p = $find('Deep'); p.style.marginLeft = '3px'; p.style.backgroundColor = 'red';"
                 "return [MCP.resetStyle(p, 'marginLeft'), MCP.resetStyle(p, 'backgroundColor'),"
                 " MCP.resetStyle(p, 'bogusProp'), p.style.marginLeft, p.style.backgroundColor]")
    assert r == [True, True, False, None, None]


def test_css_clear_all():
    pano = Panorama(css_tree())
    ok(pano.run_request("css_apply", {"sheet": "s1", "rules": [{"sel": sel(".X"), "decl": {"opacity": "0.5"}}]}))
    ok(pano.run_request("css_apply", {"sheet": "s2", "rules": [{"sel": sel(".Y"), "decl": {"color": "red"}}]}))
    r = ok(pano.run_request("css_clear", {}))
    assert r == {"cleared": 4, "remaining": []}
    assert pano.style(pano.handle("A1")) == {}


def test_css_apply_replaces_previous_version_of_sheet():
    pano = Panorama(css_tree())
    a = pano.handle("A")
    ok(pano.run_request("css_apply", {"sheet": "s", "rules": [{"sel": sel(".X"), "decl": {"opacity": "0.5", "color": "red"}}]}))
    ok(pano.run_request("css_apply", {"sheet": "s", "rules": [{"sel": sel(".X"), "decl": {"color": "blue"}}]}))
    assert pano.style(a) == {"color": "blue"}


def test_overlapping_sheets_settle_and_latest_wins():
    pano = Panorama(css_tree())
    pano.load_bridge()
    a, b = pano.handle("A"), pano.handle("B")
    ok(pano.run_request("css_apply", {"sheet": "A", "rules": [
        {"sel": sel(".X"), "decl": {"color": "red", "opacity": "0.5"}}]}))
    ok(pano.run_request("css_apply", {"sheet": "B", "rules": [
        {"sel": sel("#A"), "decl": {"color": "blue"}}, {"sel": sel(".Y"), "decl": {"color": "green"}}]}))
    assert pano.style(a) == {"color": "blue", "opacity": "0.5"}
    pano.tick(1.0)
    pano.clear_style_logs()
    attr_before = pano.attr(a, "mcp_css")
    pano.tick(5.0)  # five reapply passes
    assert pano.style_log(a) == [] and pano.style_log(b) == []
    assert pano.attr(a, "mcp_css") == attr_before
    assert pano.style(a) == {"color": "blue", "opacity": "0.5"}

    # re-applying A moves it to the end: A now wins, immediately and in the loop
    ok(pano.run_request("css_apply", {"sheet": "A", "rules": [
        {"sel": sel(".X"), "decl": {"color": "red", "opacity": "0.5"}}]}))
    assert list(json.loads(pano.attr(pano.root, "mcp_sheets"))) == ["B", "A"]
    assert pano.style(a)["color"] == "red"
    pano.tick(3.0)
    assert pano.style(a)["color"] == "red"
    assert pano.style(b)["color"] == "green"

    # clearing A brings B's value back without waiting for the loop
    ok(pano.run_request("css_clear", {"sheet": "A"}))
    assert pano.style(a) == {"color": "blue"}


def test_later_rule_in_same_sheet_wins_with_hits():
    pano = Panorama(css_tree())
    a1 = pano.handle("A1")
    r = ok(pano.run_request("css_apply", {"sheet": "s", "rules": [
        {"sel": sel(".X"), "decl": {"color": "red"}}, {"sel": sel(".Y"), "decl": {"color": "blue"}}]}))
    assert r["matched"] == 4  # rule x panel matches: .X→A,A1 ; .Y→A1,B
    assert pano.style(a1) == {"color": "blue"}
    assert pano.style_log(a1) == [["color", "blue"]]  # no intermediate red write


def test_numeric_sheet_names_keep_application_order():
    pano = Panorama(css_tree())
    a = pano.handle("A")
    ok(pano.run_request("css_apply", {"sheet": "zz", "rules": [{"sel": sel("#A"), "decl": {"color": "red"}}]}))
    ok(pano.run_request("css_apply", {"sheet": "2", "rules": [{"sel": sel("#A"), "decl": {"color": "blue"}}]}))
    ok(pano.run_request("eval", code="return MCP.reapplySheets()"))
    assert pano.style(a)["color"] == "blue"


def test_non_persistent_sheet_is_not_reapplied():
    pano = Panorama(css_tree())
    pano.load_bridge()
    ok(pano.run_request("css_apply", {"sheet": "tmp", "persist": False,
                                      "rules": [{"sel": sel(".X"), "decl": {"color": "red"}}]}))
    new = pano.build(P("Panel", "Late", ["X"]), parent=pano.root)
    pano.tick(7.0)
    assert pano.style(new) == {}


def test_persistent_sheet_reaches_new_panels():
    pano = Panorama(css_tree())
    pano.load_bridge()
    ok(pano.run_request("css_apply", {"sheet": "p", "rules": [{"sel": sel(".X"), "decl": {"color": "red"}}]}))
    new = pano.build(P("Panel", "Late", ["X"]), parent=pano.root)
    pano.tick(7.0)  # past the 5s match-cache age
    assert pano.style(new) == {"color": "red"}
    assert pano.errors == []


def test_prune_cache():
    pano = Panorama(css_tree())
    r = js(pano, "var c = {}; MCP.reapplySheets(c); MCP.setSheets({s: {persist: true, rules: [{sel: "
                 + json.dumps(sel(".X")) + ", decl: {color: 'red'}}]}});"
                 "MCP.reapplySheets(c); var n1 = Object.keys(c).length;"
                 "MCP.setSheets({});"  # the sheet is gone; its cache entry is no longer used
                 "var t0 = Date.now(); var p0 = MCP.pruneCache(c, 30000, t0 + 10000);"
                 "var p1 = MCP.pruneCache(c, 30000, t0 + 31000);"
                 "return [n1, p0, p1, Object.keys(c).length]")
    assert r == [1, 0, 1, 0]


def test_bridge_prunes_unused_match_cache():
    # Indirect: after the sheet is cleared, the loop keeps running cleanly for a long time.
    pano = Panorama(css_tree())
    pano.load_bridge()
    ok(pano.run_request("css_apply", {"sheet": "p", "rules": [{"sel": sel(".X"), "decl": {"color": "red"}}]}))
    pano.tick(2)
    ok(pano.run_request("css_clear", {"sheet": "p"}))
    pano.tick(120)
    assert pano.errors == []
    assert "MCP.pruneCache(matchCache" in render_bridge_js("m")


# ---------------------------------------------------------------- ops


def test_ping_and_unknown_op(pano):
    r = ok(pano.run_request("ping"))
    assert r["root"]["id"] == "Root" and r["bridge"]["id"] == "Root"
    r = pano.run_request("nope")
    assert r == {"ok": False, "error": "unknown op: nope"}


def test_eval_target_and_errors(pano):
    assert js(pano, "$panel.id", args={"target": "Deep"}) == "Deep"
    assert ok(pano.run_request("eval", {"target": "Deep"}, code="$panel.id")) == "Deep"
    assert ok(pano.run_request("eval", {}, code="$panel.id")) == "Root"  # default: bridge
    r = pano.run_request("eval", code="throw new Error('kaput')")
    assert not r["ok"] and r["error"] == "Error: kaput" and "stack" in r
    assert not r["stack"].startswith("Error: kaput")


def test_eval_keep_flag(pano):
    pano.run_request("eval", code="1", keep=True, serial=900)
    host = pano.handle("McpReqm00000900")
    assert pano.attr(host, "mcp_keep") == "1"


def test_tree(pano):
    r = ok(pano.run_request("tree", {"target": "Hud", "depth": 1}))
    t = r["tree"]
    assert t["id"] == "Hud" and [k["id"] for k in t["kids"]] == ["Bar", "Dup"]  # McpReq host excluded
    assert "kids" not in t["kids"][0] and r["truncated"] is False
    r = ok(pano.run_request("tree", {"target": "root", "max": 3, "depth": 10}))
    assert r["truncated"] is True
    r = ok(pano.run_request("tree", {"target": "root"}))  # default depth 3
    assert r["tree"]["kids"][0]["kids"][0]["kids"][0]["id"] == "Name"
    assert "kids" not in r["tree"]["kids"][0]["kids"][0]["kids"][0]


def test_load_layout_append():
    pano = Panorama(hud_tree())
    r = ok(pano.run_request("load_layout", {"target": "Stats", "mode": "append", "id": "MyPanel",
                                            "path": "file://{resources}/layout/mcp_live/x.xml"}))
    assert r["panel"]["id"] == "MyPanel" and r["layout_root"]["id"] == "MyPanel_layout"
    holder = pano.handle("MyPanel")
    info = pano.info(holder)
    assert info["parent"] == pano.handle("Stats") and info["hittest"] is False
    assert info["style"] == {"width": "100%", "height": "100%"}
    load = pano.loads[-1]
    assert load["id"] == "MyPanel_layout" and load["override"] is False
    # appending again with the same id replaces the old holder
    ok(pano.run_request("load_layout", {"target": "Stats", "mode": "append", "id": "MyPanel",
                                        "path": "file://{resources}/layout/mcp_live/y.xml"}))
    assert pano.info(holder)["valid"] is False
    assert sum(1 for h in pano.info(pano.handle("Stats"))["children"] if pano.info(h)["id"] == "MyPanel") == 1


def test_load_layout_append_works_on_root_and_cpp_panels():
    pano = Panorama(hud_tree())
    ok(pano.run_request("load_layout", {"target": "root", "mode": "append", "id": "M1", "path": "file://a.xml"}))
    ok(pano.run_request("load_layout", {"target": "TopBar", "mode": "append", "id": "M2", "path": "file://a.xml"}))
    assert pano.crashed is None


def test_load_layout_append_failure_removes_holder():
    pano = Panorama(hud_tree())
    pano.set_load_result(False)
    stats = pano.handle("Stats")
    before = pano.info(stats)["children"]
    r = pano.run_request("load_layout", {"target": "Stats", "mode": "append", "id": "MyPanel", "path": "file://bad.xml"})
    assert not r["ok"] and "BLoadLayout failed for file://bad.xml" in r["error"]
    assert pano.info(stats)["children"] == before


def test_load_layout_replace_plain_panel():
    pano = Panorama(hud_tree())
    r = ok(pano.run_request("load_layout", {"target": "Stats", "mode": "replace", "path": "file://a.xml"}))
    assert r["panel"]["id"] == "Stats" and r["layout_root"]["id"] == "Stats"
    assert pano.loads[-1]["override"] is True and pano.loads[-1]["id"] == "Stats"


@pytest.mark.parametrize("target", ["root", "", "bridge", "CitadelHudRoot", "#CitadelHudRoot"])
def test_load_layout_replace_refuses_protected(target):
    pano = Panorama(hud_tree())
    r = pano.run_request("load_layout", {"target": target, "mode": "replace", "path": "file://a.xml"})
    assert not r["ok"] and "append" in r["error"]
    assert pano.loads == []


def test_load_layout_replace_refuses_cpp_panel():
    pano = Panorama(hud_tree())
    r = pano.run_request("load_layout", {"target": "TopBar", "mode": "replace", "path": "file://a.xml"})
    assert not r["ok"] and "CitadelHudTopBar" in r["error"] and "compile_source" in r["error"]
    assert pano.loads == [] and pano.crashed is None


def test_load_layout_replace_refuses_bridge_ancestor():
    pano = Panorama(P("Panel", "Root", children=[
        P("Panel", "Outer", children=[P("Panel", "Bridge", attrs={"mcp_bridge": "1"})]),
        P("Panel", "Free"),
    ]))
    for t in ("Outer", "Bridge", "root"):
        r = pano.run_request("load_layout", {"target": t, "mode": "replace", "path": "file://a.xml"})
        assert not r["ok"], t
        r = pano.run_request("delete", {"target": t})
        assert not r["ok"], t
    ok(pano.run_request("load_layout", {"target": "Free", "mode": "replace", "path": "file://a.xml"}))


def test_load_layout_append_id_cannot_replace_bridge_ancestor():
    pano = Panorama(P("Panel", "Root", children=[
        P("Panel", "Outer", children=[P("Panel", "Bridge", attrs={"mcp_bridge": "1"})]),
    ]))
    r = pano.run_request("load_layout", {"target": "root", "mode": "append", "id": "Outer", "path": "file://a.xml"})
    assert not r["ok"] and "refusing" in r["error"]
    assert pano.info(pano.handle("Outer"))["valid"] is True


def test_reload_layout():
    pano = Panorama(hud_tree())
    r = ok(pano.run_request("reload_layout", {"layout": "layout/hud/stats.xml", "path": "file://new_stats.xml"}))
    assert r["reloaded"] == 2  # Stats and Stats2 (backslashes, case); StatsInner is nested in Stats
    assert [x["panel"]["id"] for x in r["roots"]] == ["Stats", "Stats2"]
    assert all(x["ok"] for x in r["roots"])
    assert r["skipped"] == []
    stats = pano.handle("Stats")
    assert pano.attr(stats, "mcp_orig_layout") == "panorama/layout/hud/stats.xml"
    assert [ld["id"] for ld in pano.loads] == ["Stats", "Stats2"]
    # after the reload the layoutfile changed, but the original is remembered → found again
    r = ok(pano.run_request("reload_layout", {"layout": "layout/hud/stats.xml", "path": "file://orig.xml", "revert": True}))
    assert r["reloaded"] == 2
    assert pano.attr(stats, "mcp_orig_layout") == ""


def test_reload_layout_skips_protected_and_cpp():
    pano = Panorama(hud_tree())
    r = ok(pano.run_request("reload_layout", {"layout": "layout/hud/top_bar.xml", "path": "file://x.xml"}))
    assert r["reloaded"] == 0 and r["roots"] == []
    assert len(r["skipped"]) == 1 and r["skipped"][0]["panel"]["id"] == "TopBar"
    assert "CitadelHudTopBar" in r["skipped"][0]["reason"]
    r = ok(pano.run_request("reload_layout", {"layout": "layout/base_hud.xml", "path": "file://x.xml"}))
    assert r["reloaded"] == 0 and r["skipped"][0]["panel"]["id"] == "CitadelHudRoot"
    assert pano.loads == [] and pano.crashed is None


def test_delete():
    pano = Panorama(hud_tree())
    stats = pano.handle("Stats")
    r = ok(pano.run_request("delete", {"target": "Stats"}))
    assert r == {"deleted": "Stats"}
    assert pano.info(stats)["valid"] is False


@pytest.mark.parametrize("target", ["root", "", "bridge", "ctx", "CitadelHudRoot"])
def test_delete_refuses_protected(target):
    pano = Panorama(hud_tree())
    r = pano.run_request("delete", {"target": target})
    assert not r["ok"] and "refusing" in r["error"]
    assert pano.info(pano.root)["valid"] is True and len(pano.info(pano.root)["children"]) >= 3


def test_is_internal_used_by_request_main():
    from deadlock_mcp.bridge import _js

    src = _js("request_main.js")
    assert "McpReq" not in src and "MCP.isInternal" in src


# ---------------------------------------------------------------- bridge poll loop


def bridge_pano():
    pano = Panorama(P("Panel", "CitadelHudRoot", children=[P("Panel", "Stuff")]))
    pano.load_bridge(domain="h", poll=0.1)
    return pano


def name(base, i, domain="h"):
    return f"{domain}{base + i:08d}"


def test_bridge_ready_line():
    pano = bridge_pano()
    msgs = pano.take_msgs()
    assert len(msgs) == 1
    m = re.fullmatch(r"@@MCPLOG ready domain=h version=(\S+) base=(\d+)", msgs[0])
    assert m and f'var VERSION = "{m[1]}"' in render_bridge_js("h") and int(m[2]) % 100 == 1
    assert pano.attr(pano.root, "mcp_bridge") == "1"


def test_bridge_takes_requests_in_order():
    pano = bridge_pano()
    base = pano.bridge_base("h")
    pano.take_msgs()
    # base+1 exists but base does not: nothing is taken (strictly sequential)
    pano.register_file(name(base, 1), "$.Msg('ran 1 ' + $.GetContextPanel().id);")
    pano.tick(0.5)
    assert pano.take_msgs() == []
    pano.register_file(name(base, 0), "$.Msg('ran 0 ' + $.GetContextPanel().id);")
    pano.register_file(name(base, 2), "$.Msg('ran 2');")
    pano.tick(0.1)
    assert pano.take_msgs() == [
        f"@@MCPLOG took h{base}", f"ran 0 McpReq{name(base, 0)}",
        f"@@MCPLOG took h{base + 1}", f"ran 1 McpReq{name(base, 1)}",
        f"@@MCPLOG took h{base + 2}", "ran 2",
    ]
    host = pano.handle(f"McpReq{name(base, 0)}")
    info = pano.info(host)
    assert info["visible"] is False and info["hittest"] is False and info["parent"] == pano.root
    assert pano.loads[0]["path"] == f"file://{{resources}}/layout/{name(base, 0)}.xml"
    assert pano.loads[0]["override"] is False
    pano.tick(1.0)
    assert pano.take_msgs() == []  # each serial is taken once


def test_bridge_runs_real_request_and_replies():
    pano = bridge_pano()
    base = pano.bridge_base("h")
    pano.register_file(name(base, 0), render_request_js(base, "ping", {}, None, False, "h"))
    pano.take_msgs()
    pano.tick(0.1)
    msgs = pano.take_msgs()
    assert msgs[0] == f"@@MCPLOG took h{base}"
    assert parse_reply(msgs[1:])["result"]["bridge"]["id"] == "CitadelHudRoot"


def test_bridge_recycles_hosts_but_keeps_kept():
    pano = bridge_pano()
    base = pano.bridge_base("h")
    pano.register_file(name(base, 0), "$.GetContextPanel().SetAttributeString('mcp_keep', '1');")
    for i in range(1, 80):
        pano.register_file(name(base, i), "")
    pano.tick(1.0)
    kids = [pano.info(h) for h in pano.info(pano.root)["children"]]
    hosts = [k["id"] for k in kids if k["id"].startswith("McpReq")]
    assert len(hosts) == 65  # 64 recycled slots + the kept one
    assert f"McpReq{name(base, 0)}" in hosts
    assert f"McpReq{name(base, 1)}" not in hosts and f"McpReq{name(base, 79)}" in hosts


def test_bridge_script_exception_is_reported_and_loop_continues():
    pano = bridge_pano()
    base = pano.bridge_base("h")
    pano.register_file(name(base, 0), "throw new Error('bad');")
    pano.register_file(name(base, 1), "$.Msg('after');")
    pano.take_msgs()
    pano.tick(0.3)
    msgs = pano.take_msgs()
    assert any("JS Exception" in m for m in msgs) and f"file:///panorama/scripts/{name(base, 0)}.vjs" in msgs
    assert "after" in msgs


def test_bridge_poll_error_logged_once():
    pano = bridge_pano()
    pano.take_msgs()
    pano.eval("__pano.cfg.probeError = 'disk on fire'")
    pano.tick(2.0)
    errs = [m for m in pano.take_msgs() if "poll error" in m]
    assert errs == ["@@MCPLOG poll error: Error: disk on fire"]
    # it recovers, and a recurrence after recovery is logged again
    pano.eval("__pano.cfg.probeError = null")
    pano.tick(0.5)
    pano.eval("__pano.cfg.probeError = 'disk on fire'")
    pano.tick(0.5)
    assert [m for m in pano.take_msgs() if "poll error" in m] == ["@@MCPLOG poll error: Error: disk on fire"]
    assert pano.eval("__pano.queueSize()") == 2  # poll + reapply loops are still scheduled
