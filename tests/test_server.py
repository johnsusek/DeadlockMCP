"""Tool layer: error boundary, launch/mount ordering, status, stylesheet resolution. No game, no toolchain."""

import asyncio
import inspect
import json
import os
import time

import pytest

from deadlock_mcp import game, server, watchdog
from deadlock_mcp.assets import Assets
from deadlock_mcp.bridge import Mailbox, analyze_user_code
from deadlock_mcp.config import Config
from deadlock_mcp.consolelog import ConsoleLog
from deadlock_mcp.env import desktop_env
from deadlock_mcp.errors import BridgeError, DeadlockError

GAMEINFO = "\t\tSearchPaths\n\t\t{\n\t\t\tGame\t\t\t\tcitadel\n\t\t}\n"


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A fake game install wired into the server's singletons; the game is not running."""
    citadel = tmp_path / "Deadlock/game/citadel"
    citadel.mkdir(parents=True)
    (citadel / "gameinfo.gi").write_text(GAMEINFO)
    cfg = Config(deadlock_dir=tmp_path / "Deadlock", cache_dir=tmp_path / "cache", csdk_dir=tmp_path / "csdk",
                 data_dir=tmp_path / "data", extra_mounts=["citadel/addons"])
    log = ConsoleLog(citadel / "console.log")
    monkeypatch.setattr(log, "start", lambda *a, **k: None)  # no background poller outliving the test loop
    mb = Mailbox(cfg.addons_dir, log, game_running=lambda: game.game_running())
    monkeypatch.setattr(server, "cfg", cfg)
    monkeypatch.setattr(server, "log", log)
    monkeypatch.setattr(server, "mailbox", mb)
    monkeypatch.setattr(server, "assets", Assets(cfg))
    monkeypatch.setattr(game, "game_running", lambda: False)
    return cfg


# ---------------------------------------------------------------- error boundary


async def test_bad_regex_is_an_error_reply(install):
    assert (await server.export_mod("x", include="(")).startswith("ERROR: invalid regular expression")
    (install.citadel_dir / "console.log").write_text("hello\n")
    assert (await server.console_log(pattern="[")).startswith("ERROR: invalid regular expression")


async def test_boundary_catches_deadlock_and_os_errors(install, monkeypatch):
    async def boom(*a, **k):
        raise BridgeError("bridge gone")

    monkeypatch.setattr(server.mailbox, "call", boom)
    assert await server.panel_tree() == "ERROR: bridge gone"
    monkeypatch.setattr(server.assets, "list_panorama", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    assert await server.ui_files() == "ERROR: denied"


async def test_tool_signatures_and_schema_survive_the_decorator():
    assert list(inspect.signature(server.launch_game).parameters) == ["map", "extra_args", "via_steam"]
    assert "Launch Deadlock" in server.launch_game.__doc__
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert len(tools) == 24
    props = tools["launch_game"].input_schema["properties"]
    assert props["extra_args"]["default"] == "" and props["via_steam"]["default"] is False and "map" in props
    assert tools["screenshot"].input_schema["properties"]["max_width"]["default"] == 1600
    assert tools["status"].input_schema["properties"] == {}
    assert tools["launch_game"].description.startswith("Launch Deadlock")


# ---------------------------------------------------------------- launch / mount ordering


async def test_launch_claims_token_before_mounting(install, monkeypatch):
    cfg, tf = install, watchdog.token_file(install)
    cfg.cache_dir.mkdir(exist_ok=True)
    tf.write_text("old")  # the previous launch's watchdog is still waiting
    monkeypatch.setattr(game, "umu_command", lambda cfg, args: (["true"], cfg.deadlock_dir, {}))
    seen = {}
    real_set_mount = server.assets.set_mount

    def set_mount(enable, only_if=None):
        out = real_set_mount(enable, only_if=only_if)
        seen.setdefault("token_at_mount", tf.read_text())
        return out

    monkeypatch.setattr(server.assets, "set_mount", set_mount)
    monkeypatch.setattr(game, "start_watchdog", lambda cfg, token: seen.setdefault("watchdog", token))
    monkeypatch.setattr(game, "spawn", lambda cmd, log, **k: seen.setdefault("spawn", (cmd, server.assets.mount_status())))
    out = await server.launch_game(map="new_player_basics")
    assert "ERROR" not in out
    assert seen["token_at_mount"] == seen["watchdog"] == tf.read_text() != "old"
    assert seen["spawn"] == (["true"], True)  # mounted before the game starts
    # the old watchdog now sees the game it waited for exit: it must leave the new launch's mount alone
    assert Assets(cfg).set_mount(False, only_if=lambda: watchdog.release_token(cfg, "old")).startswith("skipped")
    assert server.assets.mount_status()


async def test_launch_does_not_start_game_when_mount_fails(install, monkeypatch):
    install.gameinfo.write_text("no search paths here\n")
    monkeypatch.setattr(game, "umu_command", lambda cfg, args: (["true"], cfg.deadlock_dir, {}))
    spawned = []
    monkeypatch.setattr(game, "spawn", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(game, "start_watchdog", lambda *a: spawned.append(a))
    out = await server.launch_game()
    assert out.startswith("ERROR") and "Game citadel" in out
    assert not spawned and not watchdog.token_file(install).exists()


def test_umu_command_uses_compat_data_and_steam_root(tmp_path, monkeypatch):
    lib, steam = tmp_path / "lib2", tmp_path / "Steam"
    (lib / "steamapps/common/Deadlock").mkdir(parents=True)
    (steam / "steamapps").mkdir(parents=True)
    monkeypatch.setenv("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(steam))
    monkeypatch.setattr(game.shutil, "which", lambda name: f"/usr/bin/{name}")
    cfg = Config(deadlock_dir=lib / "steamapps/common/Deadlock", cache_dir=tmp_path / "cache")
    cmd, cwd, env = game.umu_command(cfg, game.launch_args("dl_hideout", "-foo 'a b'"))
    assert env["WINEPREFIX"] == str(lib / "steamapps/compatdata/1422450")  # the game's library, not Steam's
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(steam)
    assert cmd[-4:] == ["+map", "dl_hideout", "-foo", "a b"] and cwd == cfg.deadlock_dir / "game/bin/win64"
    with pytest.raises(DeadlockError, match="extra_args"):
        game.launch_args(None, "'unbalanced")


def test_steam_command_native_snap_flatpak(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    found = {}
    monkeypatch.setattr(game.shutil, "which", lambda name, path=None: found.get(name))
    with pytest.raises(DeadlockError, match="Steam not found"):
        game.steam_command([])
    found["flatpak"] = "/usr/bin/flatpak"
    (tmp_path / ".var/app/com.valvesoftware.Steam").mkdir(parents=True)
    assert game.steam_command(["-x"])[0] == ["flatpak", "run", "com.valvesoftware.Steam", "-applaunch", "1422450", "-x"]
    found["steam"] = "/snap/bin/steam"
    assert game.steam_command([])[0][0] == "/snap/bin/steam"


def test_spawn_closes_log_in_parent(tmp_path):
    before = len(os.listdir("/proc/self/fd"))
    p = game.spawn(["true"], tmp_path / "x.log")
    p.wait()
    assert len(os.listdir("/proc/self/fd")) == before


# ---------------------------------------------------------------- status


def _ready_log(cfg, *domains, version="x"):
    (cfg.citadel_dir / "console.log").write_text(
        "".join(f"@@MCPLOG ready domain={d} version={version} base=5\n" for d in domains))


async def test_status_game_not_running(install, monkeypatch):
    _ready_log(install, "h", "d")  # an old console.log from the last session
    server.assets.set_mount(True)
    monkeypatch.setattr(server, "PING_TIMEOUT", 0.1)
    t = time.monotonic()
    info = json.loads(await server.status())
    # the phantom bridges are pinged once, together; nothing answers and no game process exists, so they are dropped
    assert time.monotonic() - t < 1.0
    assert info["game_running"] is False and info["bridges_ready"] == [] and "ping" not in info
    assert server.mailbox.sessions == {}
    assert any("addons_mount(False)" in w for w in info["warnings"])

    async def no_call(*a, **k):
        raise AssertionError("nothing left to ping")

    monkeypatch.setattr(server.mailbox, "call", no_call)
    assert json.loads(await server.status())["bridges_ready"] == []


async def test_status_trusts_a_bridge_that_answers_over_the_process_list(install, monkeypatch):
    _ready_log(install, "h", version=server.bridge_version())
    server.assets.set_mount(True)

    async def ping(op, args=None, *, ui=None, timeout=5.0, **k):  # e.g. a sandboxed server: pgrep sees no game
        return {"root": {"id": "CitadelHudRoot"}}

    monkeypatch.setattr(server.mailbox, "call", ping)
    info = json.loads(await server.status())
    assert info["game_running"] is True and info["bridges_ready"] == ["hud"] and "game_process" in info
    assert info["ping"]["hud"]["root"] == "CitadelHudRoot" and "warnings" not in info
    assert server.mailbox.sessions  # not thrown away


async def test_status_right_after_launch_is_not_a_stale_mount(install):
    server.assets.set_mount(True)  # creates the cache dir (gameinfo lock)
    watchdog.token_file(install).write_text("tok")
    info = json.loads(await server.status())
    assert "game_starting" in info and "warnings" not in info


async def test_status_pings_bridges_concurrently(install, monkeypatch):
    _ready_log(install, "h", "d", "m")
    monkeypatch.setattr(game, "game_running", lambda: True)

    async def ping(op, args=None, *, ui=None, timeout=5.0, **k):
        await asyncio.sleep(0.3)
        return {"root": {"id": ui}}

    monkeypatch.setattr(server.mailbox, "call", ping)
    t = time.monotonic()
    info = json.loads(await server.status())
    assert time.monotonic() - t < 0.8
    assert info["bridges_ready"] == ["hud", "dashboard", "overlay"]
    assert info["ping"]["overlay"]["root"] == "overlay"
    assert any("older than the installed one" in w for w in info["warnings"])  # version "x" isn't this code's


async def test_status_flags_game_update(install, monkeypatch):
    names = {h for h in server.HOST_FILES} | {f"panorama/scripts/mcp_bridge_{d}.vjs_c" for d in "hdm"}
    monkeypatch.setattr(server.assets, "installed_names", lambda: names)
    monkeypatch.setattr(server.assets, "shipped_crc", lambda path: 2)
    install.addons_dir.mkdir(parents=True)
    hosts = {"panorama/layout/base_hud": 1, "panorama/layout/base_dashboard": 2}
    (install.addons_dir / server.MANIFEST).write_text(json.dumps({"bridge_version": "old", "hosts": hosts}))
    info = json.loads(await server.status())
    assert info["bridge_installed"] is True
    w = "\n".join(info["warnings"])
    assert "older than this code" in w and "game updated since bridge_install (base_hud changed)" in w


# ---------------------------------------------------------------- layout_live_edit stylesheet resolution

LAYOUT = """<root>
\t<styles>
\t\t<include src="s2r://panorama/styles/citadel_base_styles.vcss" />
\t\t<include src="s2r://panorama/styles/hud/foo_widget.vcss" />
\t</styles>
\t<Panel class="FooWidget" />
</root>
"""


def test_resolve_stylesheet():
    rel = "panorama/layout/foo_widget.xml"
    assert server.resolve_stylesheet(rel, LAYOUT, None) == "styles/hud/foo_widget.css"  # same stem, in a subdir
    single = LAYOUT.replace('\t\t<include src="s2r://panorama/styles/hud/foo_widget.vcss" />\n', "")
    assert server.resolve_stylesheet("panorama/layout/other.xml", single, None) == "styles/citadel_base_styles.css"
    with pytest.raises(DeadlockError, match="citadel_base_styles.css, styles/hud/foo_widget.css"):
        server.resolve_stylesheet("panorama/layout/other.xml", LAYOUT, None)
    with pytest.raises(DeadlockError, match="no stylesheets"):
        server.resolve_stylesheet(rel, "<root><Panel /></root>", None)
    for spelling in ("hud/foo_widget", "styles/hud/foo_widget.css", "s2r://panorama/styles/hud/foo_widget.vcss",
                     "panorama/styles/hud/foo_widget.vcss_c"):
        assert server.resolve_stylesheet(rel, LAYOUT, spelling) == "styles/hud/foo_widget.css"


class FakeAssets:
    def __init__(self, sources):
        self.sources, self.decompiled, self.compiled, self.loose = sources, [], None, None

    async def decompile(self, path, refresh=False):
        self.decompiled.append(path)
        return self.sources[path]

    async def compile_many(self, files, install=True, roots=None):
        self.compiled = (files, roots)
        return {"files": []}

    def install_loose(self, rels):
        self.loose = rels


async def test_layout_live_edit_subdirectory_sheet(install, monkeypatch):
    fake = FakeAssets({"panorama/layout/foo_widget.xml": LAYOUT, "styles/hud/foo_widget.css": ".A { color: red; }"})
    monkeypatch.setattr(server, "assets", fake)
    calls = []

    async def call(op, args=None, **k):
        calls.append((op, args))
        return {"ok": 1}

    monkeypatch.setattr(server.mailbox, "call", call)
    out = await server.layout_live_edit("foo_widget", css=".A { color: blue; }")
    assert "ERROR" not in out and fake.decompiled[-1] == "styles/hud/foo_widget.css"
    assert calls[0][0] == "css_apply" and calls[0][1]["sheet"] == "live:hud/foo_widget"

    out = await server.layout_live_edit("foo_widget", css=".A { color: blue; }", unsafe_reload=True)
    assert "ERROR" not in out
    files, roots = fake.compiled
    doc = files[roots[0]]
    live_css = next(f for f in files if f.endswith(".css"))
    assert live_css.endswith("_hud_foo_widget.css") and live_css.split("panorama/", 1)[1] in doc
    assert "hud/foo_widget.vcss" not in doc and "citadel_base_styles.vcss" in doc
    assert calls[-1][0] == "reload_layout"


async def test_layout_live_edit_ambiguous_sheet_is_an_error(install, monkeypatch):
    monkeypatch.setattr(server, "assets", FakeAssets({"panorama/layout/other.xml": LAYOUT}))
    out = await server.layout_live_edit("other", css=".A { color: blue; }")
    assert out.startswith("ERROR") and "pass stylesheet=" in out


# ---------------------------------------------------------------- bridge files


@pytest.mark.parametrize("path", ["panorama/layout/base_hud.xml", "layout/base_dashboard",
                                  "panorama/layout/base_hud_and_db_overlay.vxml_c", "scripts/mcp_bridge_h.js"])
async def test_compile_source_refuses_bridge_files(install, monkeypatch, path):
    monkeypatch.setattr(server, "assets", FakeAssets({}))
    out = await server.compile_source(path, "<root />")
    assert out.startswith("ERROR") and "bridge" in out and server.assets.compiled is None
    assert (await server.remove_override(path)).startswith("ERROR")


async def test_bridge_install_compiles_three_roots_and_writes_manifest(install, monkeypatch):
    fake = FakeAssets({f"panorama/layout/{n}": "<root>\n\t<Panel />\n</root>\n"
                       for n in ("base_hud", "base_dashboard", "base_hud_and_db_overlay")})
    fake.shipped_crc = lambda path: 42
    fake.pack_addon = lambda: (install.addons_dir.mkdir(parents=True), {"vpk": "v", "files": ["a"]})[1]
    monkeypatch.setattr(server, "assets", fake)
    out = await server.bridge_install()
    assert "ERROR" not in out
    files, roots = fake.compiled
    assert len(files) == 6 and sorted(roots) == sorted(h + ".xml" for h in (
        "panorama/layout/base_hud", "panorama/layout/base_dashboard", "panorama/layout/base_hud_and_db_overlay"))
    assert not any("mcp_inbox" in f for f in files)
    manifest = json.loads((install.addons_dir / server.MANIFEST).read_text())
    assert manifest["hosts"]["panorama/layout/base_hud"] == 42 and manifest["bridge_version"]


# ---------------------------------------------------------------- js_eval note


async def test_js_eval_appends_note_for_null_result(install, monkeypatch):
    code = "class A { #p = 1; }\nnew A()"  # private fields: newer than esprima's ES2017
    assert analyze_user_code(code)[1]

    async def call(*a, **k):
        return None

    monkeypatch.setattr(server.mailbox, "call", call)
    out = await server.js_eval(code)
    assert out.startswith("null\nnote:") and "return" in out
    assert await server.js_eval("1 + 1") == "null"  # analysable code: no note


# ---------------------------------------------------------------- environment / screenshot


def test_desktop_env_finds_xauthority(tmp_path):
    run, home = tmp_path / "run", tmp_path / "home"
    run.mkdir()
    home.mkdir()
    base = {"XDG_RUNTIME_DIR": str(run), "HOME": str(home), "DISPLAY": ":0"}
    assert "XAUTHORITY" not in desktop_env(base)
    (home / ".Xauthority").write_text("x")
    assert desktop_env(base)["XAUTHORITY"] == str(home / ".Xauthority")
    (run / ".mutter-Xwaylandauth.ABC").write_text("x")
    assert desktop_env(base)["XAUTHORITY"] == str(run / ".mutter-Xwaylandauth.ABC")
    old, new = run / "xauth_old", run / "xauth_new"
    old.write_text("x")
    new.write_text("x")
    os.utime(old, (1, 1))
    assert desktop_env(base)["XAUTHORITY"] == str(new)  # KDE's XWayland cookie, newest first
    assert desktop_env(dict(base, XAUTHORITY="/mine"))["XAUTHORITY"] == "/mine"  # never overridden


@pytest.mark.parametrize("crop,ok", [("800x800+0+1100", True), ("10x10+5+5", True), ("800x800", False),
                                     ("800x800+0+1100; rm -rf /", False), ("-crop", False), ("1x1-5+5", False)])
def test_crop_validation(crop, ok):
    if ok:
        game.check_crop(crop)
    else:
        with pytest.raises(DeadlockError, match="WxH"):
            game.check_crop(crop)


async def test_screenshot_failed_crop_is_an_error(monkeypatch):
    monkeypatch.setattr(game.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("grim", "magick") else None)

    async def run(cmd, env, timeout=20):
        if cmd[0].endswith("grim"):
            open(cmd[-1], "wb").write(b"PNG")
            return 0, ""
        return 1, "magick: geometry does not contain image"

    monkeypatch.setattr(game, "_run", run)
    assert (await server.screenshot(crop="10x10+5000+5000", full_screen=True)).startswith("ERROR: cropping")
    img = await server.screenshot(full_screen=True)  # no crop asked: the raw capture is fine
    assert img.data == b"PNG"
    assert (await server.screenshot(crop="nope")).startswith("ERROR: crop must")


async def test_screenshot_never_swaps_the_game_window_for_the_desktop(monkeypatch):
    monkeypatch.setattr(game.shutil, "which", lambda n: f"/usr/bin/{n}")
    calls = []

    def fake(visible_ids, all_ids, import_ok=True):
        async def run(cmd, env, timeout=20):
            calls.append(cmd[0])
            if cmd[0] == "xdotool":
                ids = visible_ids if "--onlyvisible" in cmd else all_ids
                return (0, ids) if ids else (1, "")
            if cmd[0] == "import" and import_ok:
                open(cmd[-1], "wb").write(b"WINDOW")
            if cmd[0].endswith("magick"):
                return 1, ""  # no resize: the raw capture comes back
            return 0, ""
        monkeypatch.setattr(game, "_run", run)

    fake("", "94371843\n", import_ok=False)  # the game minimised itself: import can't read an unmapped window
    out = await server.screenshot()
    assert out.startswith("ERROR: the Deadlock window is minimised") and "full_screen=True" in out
    fake("", "")
    assert "no Deadlock window found" in await server.screenshot()
    fake("94371843\n", "94371843\n", import_ok=False)
    assert "capturing the Deadlock window failed" in await server.screenshot()
    assert not any("spectacle" in c or "grim" in c for c in calls)  # the desktop was never captured
    fake("94371843\n", "94371843\n")
    assert (await server.screenshot()).data == b"WINDOW"


# ---------------------------------------------------------------- export_mod


async def test_export_mod_installs_into_next_free_slot_and_replaces_its_own(install, monkeypatch):
    src = install.cache_dir / "hud.vcss_c"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"v1")
    monkeypatch.setattr(server.assets, "addon_files", lambda: {"panorama/styles/hud.vcss_c": src})
    addons = install.citadel_dir / "addons"
    addons.mkdir()
    (addons / "pak01_dir.vpk").write_bytes(b"someone else's mod")
    info = json.loads(await server.export_mod("my hud", install=True))
    assert info["vpk"] == str(install.data_dir / "mods/my_hud/pak01_dir.vpk")
    assert info["installed"] == str(addons / "pak02_dir.vpk")
    src.write_bytes(b"v2")
    info = json.loads(await server.export_mod("my hud", install=True))
    assert info["installed"] == str(addons / "pak02_dir.vpk")  # same slot, updated
    assert (addons / "pak02_dir.vpk").read_bytes() == (install.data_dir / "mods/my_hud/pak01_dir.vpk").read_bytes()
    (addons / "pak02_dir.vpk").write_bytes(b"a mod manager reused the slot")
    info = json.loads(await server.export_mod("my hud", install=True))
    assert info["installed"] == str(addons / "pak03_dir.vpk")  # never overwrites a file we didn't write
    assert "installed" not in json.loads(await server.export_mod("other"))
