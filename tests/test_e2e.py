"""End to end: the real Mailbox against the real bridge and request scripts, running in embedded V8.

The "game" is tests/panorama_v8.py. It runs bridge_main.js's poll loop on a fake clock, sees the request
files the Mailbox writes into the addon dir, executes them, and its $.Msg lines are appended to the
console.log the Mailbox tails. Nothing here is mocked on the Python side."""

import asyncio
import struct

import pytest

pytest.importorskip("py_mini_racer")

from panorama_v8 import Panorama  # noqa: E402

from deadlock_mcp.bridge import BridgeError, Mailbox  # noqa: E402
from deadlock_mcp.consolelog import ConsoleLog  # noqa: E402
from deadlock_mcp.css import css_to_rules, parse_selector  # noqa: E402

TREE = {"type": "Panel", "id": "CitadelHudRoot", "layout": "panorama\\layout\\base_hud.xml", "children": [
    {"type": "Panel", "id": "Hud", "children": [
        {"type": "Label", "id": "Clock", "classes": ["GameTime"], "text": "12:34"},
        {"type": "CitadelHudTopBar", "id": "TopBar", "classes": ["TopBar"], "children": [
            {"type": "Label", "id": "Score", "classes": ["Score", "Big"], "text": "héllo ☃"},
        ]},
    ]},
]}


def read_vjs(data: bytes) -> str:
    """The JS source inside a compiled .vjs_c (inverse of resource.make_vjs)."""
    boff, count = struct.unpack_from("<II", data, 8)
    for i in range(count):
        at = 8 + boff + i * 12
        typ, off, size = struct.unpack_from("<4sII", data, at)
        if typ == b"DATA":
            return data[at + 4 + off: at + 4 + off + size].decode("utf-8")
    raise ValueError("no DATA block")


class V8Game:
    def __init__(self, root, logfile, domain="h"):
        self.root, self.logfile, self.domain = root, logfile, domain
        self.pano = Panorama(TREE)
        self.pano.load_bridge(domain=domain)
        self.paused = False  # a paused game runs no script: the bridge does not poll
        self.files: set[str] = set()
        self.flush()

    def flush(self):
        lines = self.pano.take_msgs()
        if lines:
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.writelines(f"11/14 10:00:00 [PanoramaScript] {ln}\n" for ln in lines)

    def sync_files(self):
        """Mirror the request files on disk into the fake engine's file system."""
        layouts = self.root / "panorama/layout"
        present = {p.name.removesuffix(".vxml_c") for p in layouts.glob(f"{self.domain}[0-9]*.vxml_c")}
        for name in present:  # re-read every time: a withdrawn request's serial is reused with new content
            try:
                self.pano.register_file(name, read_vjs((self.root / f"panorama/scripts/{name}.vjs_c").read_bytes()))
            except FileNotFoundError:
                present = present - {name}
        for name in self.files - present:
            self.pano.unregister_file(name)
        self.files = present

    async def run(self):
        while True:
            if not self.paused:
                self.sync_files()
                self.pano.tick(0.1)
                self.flush()
            await asyncio.sleep(0.005)


@pytest.fixture
async def game(tmp_path):
    root = tmp_path / "mcp_addons"
    (root / "panorama/layout").mkdir(parents=True)
    logfile = tmp_path / "console.log"
    logfile.write_text("11/14 10:00:00 boot\n")
    g = V8Game(root, logfile)
    task = asyncio.create_task(g.run())
    mb = Mailbox(root, ConsoleLog(logfile))
    yield g, mb
    task.cancel()
    if mb.log._task:
        mb.log._task.cancel()


async def test_round_trips(game):
    g, mb = game
    ping = await mb.call("ping", ui="hud", timeout=3)
    assert ping["root"]["id"] == "CitadelHudRoot" and ping["bridge"]["id"] == "CitadelHudRoot"
    assert await mb.call("eval", {"target": None}, code="$find('Clock').text", timeout=3) == "12:34"
    assert await mb.call("eval", {"target": "Clock"}, code="var t = $panel.text;\nt.length", timeout=3) == 5
    found = await mb.call("find", {"sel": parse_selector("#Hud > Label.GameTime"), "target": "root", "max": 10}, timeout=3)
    assert found["count"] == 1 and found["panels"][0]["id"] == "Clock"
    tree = await mb.call("tree", {"target": "root", "depth": 5, "max": 100}, timeout=3)
    ids = []

    def walk(n):
        ids.append(n["id"])
        for k in n.get("kids", []):
            walk(k)
    walk(tree["tree"])
    assert ids == ["CitadelHudRoot", "Hud", "Clock", "TopBar", "Score"]  # the hidden request hosts are not listed
    assert not list(g.root.rglob("h[0-9]*"))  # request files are removed after each call


async def test_large_non_ascii_reply_survives_chunking(game):
    _, mb = game
    for pad in (369, 370, 371):  # puts the emoji's surrogate pair on, before and after a 400-char chunk boundary
        code = f"'x'.repeat({pad}) + '\\ud83d\\ude00' + $find('Score').text + 'y'.repeat(900)"
        assert await mb.call("eval", {}, code=code, timeout=3) == "x" * pad + "😀héllo ☃" + "y" * 900


async def test_live_css(game):
    g, mb = game
    clock = g.pano.handle("Clock")
    rules, warnings = css_to_rules("@define hot: #ff0000;\n.GameTime { color: hot; font-size: 40px; }\n.GameTime:hover { color: blue; }")
    assert len(warnings) == 1
    out = await mb.call("css_apply", {"rules": rules, "sheet": "t", "persist": True}, timeout=3)
    assert out["matched"] == 1 and "invalid" not in out
    assert g.pano.style(clock)["color"] == "#ff0000"
    cleared = await mb.call("css_clear", {"sheet": "t"}, timeout=3)
    assert cleared["cleared"] == 2 and cleared["remaining"] == []
    assert "color" not in g.pano.style(clock)


async def test_request_the_bridge_never_took_does_not_desync(game):
    g, mb = game
    assert (await mb.call("ping", timeout=3))["root"]["id"] == "CitadelHudRoot"
    g.paused = True  # e.g. the game is loading a map
    for _ in range(3):
        with pytest.raises(BridgeError, match="never picked up"):
            await mb.call("ping", timeout=0.15)
    assert mb.sessions["h"]["stalled"]
    g.paused = False
    assert (await mb.call("ping", timeout=3))["root"]["id"] == "CitadelHudRoot"  # the real poll loop still finds it
    assert not mb.sessions["h"]["stalled"]
    results = await asyncio.gather(*(mb.call("eval", {}, code=str(i), timeout=5) for i in range(6)))
    assert results == list(range(6))


async def test_script_errors_are_reported_and_the_bridge_survives(game):
    _, mb = game
    with pytest.raises(BridgeError, match="JS exception in request script"):
        await mb.call("eval", {}, code="(", timeout=3)  # syntax error: the script never runs, only the log tells
    with pytest.raises(BridgeError, match="nope is not defined"):
        await mb.call("eval", {}, code="nope.x", timeout=3)
    with pytest.raises(BridgeError, match="locale APIs crash Deadlock"):
        await mb.call("eval", {}, code="new Date().toLocaleString()", timeout=3)
    assert await mb.call("eval", {}, code="1 + 1", timeout=3) == 2


async def test_destructive_requests_are_refused(game):
    g, mb = game
    with pytest.raises(BridgeError, match="refusing to delete"):
        await mb.call("delete", {"target": "root"}, timeout=3)
    with pytest.raises(BridgeError, match="refusing to replace"):  # a C++ panel type: this crashes the real game
        await mb.call("load_layout", {"path": "file://{resources}/layout/x.xml", "target": "TopBar",
                                      "mode": "replace", "id": None}, timeout=3)
    assert g.pano.crashed is None
    assert (await mb.call("delete", {"target": "Score"}, timeout=3)) == {"deleted": "Score"}
    assert (await mb.call("find", {"sel": parse_selector(".Score"), "target": "root", "max": 5}, timeout=3))["count"] == 0
