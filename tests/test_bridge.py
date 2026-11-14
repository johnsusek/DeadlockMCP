import asyncio
import json
import re

import esprima
import pytest

from deadlock_mcp.bridge import (
    BridgeError,
    Mailbox,
    analyze_user_code,
    bridge_version,
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
    assert re.fullmatch(r"\S+\+[0-9a-f]{8}", bridge_version()) and f'var VERSION = "{bridge_version()}"' in js


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
    assert '"id": "h42-ab12"' in render_request_js(42, "ping", {}, None, False, "h", nonce="ab12")


def test_analyze_user_code_reports_unparseable():
    src, note = analyze_user_code("var a = $find('X');\na?.text")  # optional chaining is newer than esprima
    assert note and "return" in note and src == user_function("var a = $find('X');\na?.text")
    assert analyze_user_code("var a = 1;\na + 1") == (user_function("var a = 1;\na + 1"), None)
    assert analyze_user_code("1+1")[1] is None


def test_make_vjs_and_layout():
    data = make_vjs("$.Msg('x');\n")
    assert data.endswith(b"$.Msg('x');\n")
    assert int.from_bytes(data[:4], "little") == len(data)
    lay = make_request_layout(12345, domain="d")
    assert lay.count(b"d00012345") == 2 and b"mcp_inbox" not in lay


# ---------------------------------------------------------------- fake game


class FakeGame:
    """Imitates the in-game bridge: watches the inbox, 'runs' requests, appends to console.log."""

    def __init__(self, root, logfile, base=5000, crash_op=None, domain="h"):
        self.root, self.logfile, self.crash_op, self.d = root, logfile, crash_op, domain
        self.paused = False  # map loading / game paused: the bridge stops probing
        self.took_only = False  # takes requests but never replies (request file vanished under it, script hung)
        self.announce(base)

    def announce(self, base):
        """(Re)load the bridge, as when its UI is reloaded."""
        self.next = base
        self.write(f"[PanoramaScript] @@MCPLOG ready domain={self.d} version=test base={base}")

    def write(self, line):
        with open(self.logfile, "a") as f:
            f.write(f"11/13 10:00:00 {line}\n")

    async def run(self):
        while True:
            name = request_name(self.next, self.d)
            lay = self.root / f"panorama/layout/{name}.vxml_c"
            js = self.root / f"panorama/scripts/{name}.vjs_c"
            if not self.paused and lay.exists():
                self.write(f"[PanoramaScript] @@MCPLOG took {self.d}{self.next}")
                src = js.read_bytes().decode("utf-8", "replace")
                req = json.loads(re.search(r"var REQ = (\{.*?\});", src).group(1))
                if self.took_only:
                    pass
                elif req["op"] == self.crash_op:
                    self.write("[PanoramaScript] JS Exception! *** Skipping rest of script ***")
                    self.write(f"file:///panorama/scripts/{name}.vjs")
                    self.write("Uncaught SyntaxError: Unexpected token")
                else:
                    res = {"ok": True, "result": "x" * 1000} if req["op"] == "eval" else \
                        {"ok": False, "error": "boom"} if req["op"] == "bad" else \
                        {"ok": True, "result": req["args"].get("text", self.d)} if req["op"] == "echo" else \
                        {"ok": True, "result": req["op"]}
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
        assert not [p for p in root.rglob("*") if re.fullmatch(r"[hdm]\d{8}\.v(js|xml)_c", p.name)]  # cleaned up
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


def test_repl_style_last_expression():
    fn = user_function("$.Schedule(1, function(){});\n'scheduled'")
    assert "return 'scheduled'" in fn
    fn2 = user_function("var x = 2;\nif (x) { x++; }")
    assert "return" not in fn2.split("{", 1)[1].split("if")[0]
    esprima.parseScript(fn)
    esprima.parseScript(fn2)


def _request_files(root):
    return [p for p in root.rglob("*") if re.fullmatch(r"[hdm]\d{8}\.v(js|xml)_c(\.tmp)?", p.name)]


async def test_paused_game_then_resume(env):
    """A request the bridge never took must not move the serial (it would desync the bridge forever)."""
    root, logfile = env
    game = FakeGame(root, logfile, base=5000)
    game.paused = True
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        for _ in range(2):
            with pytest.raises(BridgeError, match="never picked up"):
                await mb.call("ping", timeout=0.1)
        assert mb.sessions["h"]["next"] == 5000 and mb.ui_states()["hud"]["stalled"]
        assert not _request_files(root)
        game.paused = False
        assert await mb.call("ping", timeout=1) == "ping"
        assert await mb.call("ping", timeout=1) == "ping"
        assert mb.sessions["h"]["next"] == 5002 == game.next and not mb.ui_states()["hud"]["stalled"]
    finally:
        task.cancel()


async def test_reply_at_deadline_is_read(env):
    """The final poll after a timeout picks up a reply that landed just before the deadline."""
    root, logfile = env
    game = FakeGame(root, logfile)
    task = asyncio.create_task(game.run())
    log = ConsoleLog(logfile)
    log._task = asyncio.get_running_loop().create_future()  # no background polling: only call()'s own polls
    mb = Mailbox(root, log)
    try:
        assert await mb.call("ping", timeout=0.1) == "ping"
    finally:
        task.cancel()


async def test_taken_without_reply_advances_serial(env):
    root, logfile = env
    game = FakeGame(root, logfile, base=5000)
    game.took_only = True
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        with pytest.raises(BridgeError, match="loaded the request but sent no reply"):
            await mb.call("ping", timeout=0.1)
        assert mb.sessions["h"]["next"] == 5001
        game.took_only = False
        assert await mb.call("ping", timeout=1) == "ping"
    finally:
        task.cancel()


async def test_late_took_after_timeout_advances_serial(env):
    """The bridge takes the request just as we give up: its late "took" line must still move the serial."""
    root, logfile = env
    game = FakeGame(root, logfile, base=5000)
    game.paused = True
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        with pytest.raises(BridgeError, match="never picked up"):
            await mb.call("ping", timeout=0.1)
        game.write("[PanoramaScript] @@MCPLOG took h5000")  # it probed before the files were withdrawn
        game.next = 5001
        game.paused = False
        assert await mb.call("ping", timeout=1) == "ping"
        assert mb.sessions["h"]["next"] == 5002
    finally:
        task.cancel()


async def test_reply_text_is_not_mistaken_for_an_exception(env):
    root, logfile = env
    game = FakeGame(root, logfile, base=5000)
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        text = "label text: JS Exception seen"
        assert await mb.call("echo", {"text": text}, timeout=1) == text
        game.write("[PanoramaScript] JS Exception! unrelated game script")
        text = "see file:///panorama/scripts/h00005001.vjs"  # names this very request's script
        assert await mb.call("echo", {"text": text}, timeout=1) == text
    finally:
        task.cancel()


async def test_log_vanishing_clears_sessions(env):
    root, logfile = env
    FakeGame(root, logfile)
    log = ConsoleLog(logfile)
    mb = Mailbox(root, log)
    assert mb.ready_uis() == ["hud"]  # polls the log itself
    logfile.unlink()
    assert mb.ready_uis() == [] and not mb.ready


async def test_reset_and_game_not_running(env):
    root, logfile = env
    game = FakeGame(root, logfile)
    game.paused = True
    task = asyncio.create_task(game.run())
    running = [True]
    mb = Mailbox(root, ConsoleLog(logfile), game_running=lambda: running[0])
    try:
        mb.ready_uis()
        mb.reset()
        assert not mb.ready
        game.announce(6000)
        running[0] = False
        with pytest.raises(BridgeError, match="game is not running"):
            await mb.call("ping", timeout=0.1)
        assert mb.sessions == {}
    finally:
        task.cancel()


async def test_auto_skips_stalled_ui(env):
    root, logfile = env
    hud, dash = FakeGame(root, logfile, base=100, domain="h"), FakeGame(root, logfile, base=200, domain="d")
    hud.paused = True
    tasks = [asyncio.create_task(hud.run()), asyncio.create_task(dash.run())]
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        with pytest.raises(BridgeError, match="hud bridge never picked up"):
            await mb.call("echo", timeout=0.1)
        assert mb.ui_states() == {"hud": {"version": "test", "current": False, "stalled": True},
                                  "dashboard": {"version": "test", "current": False, "stalled": False}}
        assert await mb.call("echo", timeout=1) == "d"  # auto now avoids the stalled hud
        hud.paused = False
        assert await mb.call("echo", ui="hud", timeout=1) == "h"  # explicit ui still tries it; success clears it
        assert await mb.call("echo", timeout=1) == "h"
    finally:
        for t in tasks:
            t.cancel()


async def test_ui_reload_fails_pending_request(env):
    root, logfile = env
    game = FakeGame(root, logfile, base=5000)
    game.paused = True
    task = asyncio.create_task(game.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, game.announce, 9000)
        t0 = loop.time()
        with pytest.raises(BridgeError, match="hud UI was reloaded"):
            await mb.call("ping", timeout=5)
        assert loop.time() - t0 < 1
        game.paused = False
        assert await mb.call("ping", timeout=1) == "ping" and game.next == 9001
    finally:
        task.cancel()


async def test_foreign_replies_are_ignored(env):
    """Two servers sharing a bridge write the same serial; each accepts only its own nonce."""
    root, logfile = env
    game = FakeGame(root, logfile)
    a, b = Mailbox(root, ConsoleLog(logfile)), Mailbox(root, ConsoleLog(logfile))
    a.ready_uis(), b.ready_uis()
    ra = asyncio.create_task(a.call("a", timeout=0.2))
    rb = asyncio.create_task(b.call("b", timeout=0.2))
    await asyncio.sleep(0)  # both have written h00005000 now; b's request overwrote a's
    task = asyncio.create_task(game.run())
    try:
        assert await rb == "b"
        with pytest.raises(BridgeError, match="no reply"):
            await ra
        game.paused = True
        forged = '[PanoramaScript] @@MCP h5001 0/1 {"ok": true, "result": "forged"}'  # no nonce: not ours
        asyncio.get_running_loop().call_later(0.03, game.write, forged)
        with pytest.raises(BridgeError, match="never picked up"):
            await a.call("ping", timeout=0.1)
    finally:
        task.cancel()
