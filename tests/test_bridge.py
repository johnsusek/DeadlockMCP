import asyncio
import json
import re

import esprima
import pytest

from deadlock_mcp.bridge import (
    BridgeError,
    Mailbox,
    add_stylesheet,
    css_to_rules,
    ensure_root,
    inject_bridge,
    parse_selector,
    render_bridge_js,
    render_request_js,
    user_function,
)
from deadlock_mcp.consolelog import ConsoleLog
from deadlock_mcp.resource import make_request_layout, make_vjs, request_name


# ---------------------------------------------------------------- generation


def test_bridge_js_parses():
    js = render_bridge_js("d")
    esprima.parseScript(js)
    assert "__MCP_" not in js and 'var DOMAIN = "d"' in js


@pytest.mark.parametrize("code,expr", [
    ("1+1", True),
    ("$root.paneltype", True),
    ("var x = 1; return x", False),
    ("const a = $find('X');\nreturn a.id", False),
    ("[1,2].map(function (x) { return x * 2; })", True),
])
def test_user_function(code, expr):
    fn = user_function(code)
    esprima.parseScript(fn)
    assert ("return (\n" in fn) == expr


def test_request_js_parses():
    js = render_request_js(42, "eval", {"target": None}, code="'héllo ☃'.toUpperCase()", keep=True, domain="h")
    esprima.parseScript(js)
    assert '"id": "h42"' in js and '"keep": true' in js


def test_make_vjs_and_layout():
    data = make_vjs("$.Msg('x');\n")
    assert data.endswith(b"$.Msg('x');\n")
    assert int.from_bytes(data[:4], "little") == len(data)
    lay = make_request_layout(12345, domain="d")
    assert lay.count(b"d00012345") == 2 and b"mcp_inbox" not in lay


# ---------------------------------------------------------------- CSS / XML helpers


def test_parse_selector():
    assert parse_selector("Label.Name.Big") == [{"comb": " ", "type": "Label", "classes": ["Name", "Big"]}]
    assert parse_selector("#Hud > Panel .x") == [
        {"comb": " ", "id": "Hud"},
        {"comb": ">", "type": "Panel"},
        {"comb": " ", "classes": ["x"]},
    ]
    assert parse_selector("Button:hover") is None
    assert parse_selector("") is None


def test_css_to_rules():
    css = """
    /* comment */
    @define red: #f00;
    @keyframes 'pulse' { 0% { opacity: 0; } 100% { opacity: 1; } }
    #HudRoot .Health, Label.Ammo { color: #fff; font-size: 24px; background-color: gradient( linear, 0% 0%, 0% 100%, from( #000 ), to( #111 ) ); }
    Button:hover { wash-color: red; }
    """
    rules, warnings = css_to_rules(css)
    assert len(rules) == 2
    assert rules[0]["decl"]["font-size"] == "24px"
    assert rules[0]["decl"]["background-color"].startswith("gradient(")
    assert any("@keyframes" in w for w in warnings)
    assert any("Button:hover" in w for w in warnings)


def test_xml_helpers():
    assert ensure_root("<root><Panel/></root>") == "<root><Panel/></root>"
    wrapped = ensure_root('<Label text="hi"/>')
    assert wrapped.startswith("<root>")
    styled = add_stylesheet(wrapped, "file://{resources}/styles/x.css")
    assert styled.index("<styles>") < styled.index("x.css") < styled.index("<Panel ")
    styled2 = add_stylesheet('<root>\n<styles>\n<include src="a.css" />\n</styles><Panel/></root>', "b.css")
    assert styled2.count("<styles>") == 1


def test_inject_bridge():
    host = '<!-- banner -->\n<root>\n\t<styles>\n\t\t<include src="s2r://x.vcss" />\n\t</styles>\n\t<Panel class="X" />\n</root>\n'
    out = inject_bridge(host, "h")
    assert out.startswith("<root>")
    assert out.index("</styles>") < out.index("<scripts>") < out.index("mcp_bridge_h.js") < out.index("<Panel")
    with pytest.raises(BridgeError):
        inject_bridge(out, "h")


# ---------------------------------------------------------------- fake game


class FakeGame:
    """Imitates the in-game bridge: watches the inbox, 'runs' requests, appends to console.log."""

    def __init__(self, root, logfile, base=5000, crash_op=None, domain="h"):
        self.root, self.logfile, self.next, self.crash_op, self.d = root, logfile, base, crash_op, domain
        self.write(f"[PanoramaScript] @@MCPLOG ready domain={domain} version=test base={base}")

    def write(self, line):
        with open(self.logfile, "a") as f:
            f.write(f"11/13 10:00:00 {line}\n")

    async def run(self):
        while True:
            name = request_name(self.next, self.d)
            lay = self.root / f"panorama/layout/{name}.vxml_c"
            js = self.root / f"panorama/scripts/{name}.vjs_c"
            if lay.exists():
                self.write(f"[PanoramaScript] @@MCPLOG took {self.d}{self.next}")
                src = js.read_bytes().decode("utf-8", "replace")
                req = json.loads(re.search(r"var REQ = (\{.*?\});", src).group(1))
                if req["op"] == self.crash_op:
                    self.write("[PanoramaScript] JS Exception! *** Skipping rest of script ***")
                    self.write(f"file:///panorama/scripts/{name}.vjs")
                    self.write("Uncaught SyntaxError: Unexpected token")
                else:
                    res = {"ok": True, "result": "x" * 1000} if req["op"] == "eval" else \
                        {"ok": False, "error": "boom"} if req["op"] == "bad" else {"ok": True, "result": req["op"]}
                    text = json.dumps(res)
                    parts = [text[i:i + 400] for i in range(0, len(text), 400)]
                    for i, p in enumerate(parts):
                        self.write(f"[PanoramaScript] @@MCP {req['id']} {i}/{len(parts)} {p}")
                self.next += 1
            await asyncio.sleep(0.01)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "mcp_addons"
    (root / "panorama").mkdir(parents=True)
    logfile = tmp_path / "console.log"
    logfile.write_text("11/13 10:00:00 boot\n")
    return root, logfile


async def test_mailbox_end_to_end(env):
    root, logfile = env
    game = FakeGame(root, logfile)
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        assert await mb.call("ping", timeout=2) == "ping"
        assert await mb.call("eval", {}, code="1", timeout=2) == "x" * 1000
        with pytest.raises(BridgeError, match="boom"):
            await mb.call("bad", timeout=2)
        results = await asyncio.gather(*(mb.call("tree", timeout=3) for _ in range(5)))
        assert results == ["tree"] * 5
        assert not list(root.rglob("m0*"))  # request files cleaned up
    finally:
        task.cancel()


async def test_mailbox_script_exception(env):
    root, logfile = env
    game = FakeGame(root, logfile, crash_op="eval")
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        with pytest.raises(BridgeError, match="JS exception"):
            await mb.call("eval", {}, code="(", timeout=2)
        assert await mb.call("ping", timeout=2) == "ping"
    finally:
        task.cancel()


async def test_mailbox_not_ready(env):
    root, logfile = env
    mb = Mailbox(root, ConsoleLog(logfile))
    with pytest.raises(BridgeError, match="no bridge has reported ready"):
        await mb.call("ping", timeout=0.5)


async def test_mailbox_resets_on_new_log(env):
    root, logfile = env
    game = FakeGame(root, logfile, base=100)
    log = ConsoleLog(logfile)
    mb = Mailbox(root, log)
    log.poll()
    assert mb.sessions["h"]["next"] == 100
    logfile.unlink()
    logfile.write_text("11/13 11:00:00 new session\n")
    log.poll()
    assert not mb.ready
    del game


async def test_mailbox_multi_domain(env):
    root, logfile = env
    hud = FakeGame(root, logfile, base=700, domain="h")
    dash = FakeGame(root, logfile, base=700, domain="d")  # same base: ids must not collide
    tasks = [asyncio.create_task(hud.run()), asyncio.create_task(dash.run())]
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        r = await asyncio.gather(mb.call("a", ui="hud", timeout=2), mb.call("b", ui="dashboard", timeout=2),
                                 mb.call("c", timeout=2))
        assert r == ["a", "b", "c"]
        assert mb.ready_uis() == ["hud", "dashboard"]
        with pytest.raises(BridgeError, match="overlay bridge is not loaded"):
            await mb.call("x", ui="overlay", timeout=1)
        with pytest.raises(BridgeError, match="unknown ui"):
            await mb.call("x", ui="nope", timeout=1)
    finally:
        for t in tasks:
            t.cancel()


def test_css_defines_and_diff():
    from deadlock_mcp.bridge import css_diff_rules

    orig = """@define accent: #ff0000;
@define accent2: accent;
.GameTime { color: white; font-size: 20px; }
.A, .B { opacity: 1; }
Button:hover { color: red; }
"""
    edited = orig.replace("font-size: 20px", "font-size: 40px") + "\n.New { wash-color: accent2; }\n"
    rules, warnings = css_diff_rules(orig, edited)
    by = {r["sel"][0].get("classes", [""])[0]: r["decl"] for r in rules}
    assert by == {"GameTime": {"font-size": "40px"}, "New": {"wash-color": "#ff0000"}}
    rules2, w2 = css_to_rules("@define c: #123456;\n#X { color: c; border: 1px solid c; }")
    assert rules2[0]["decl"] == {"color": "#123456", "border": "1px solid #123456"}
    assert not any("@define" in w for w in w2)


def test_repl_style_last_expression():
    fn = user_function("$.Schedule(1, function(){});\n'scheduled'")
    assert "return 'scheduled'" in fn
    fn2 = user_function("var x = 2;\nif (x) { x++; }")
    assert "return" not in fn2.split("{", 1)[1].split("if")[0]
    esprima.parseScript(fn)
    esprima.parseScript(fn2)
