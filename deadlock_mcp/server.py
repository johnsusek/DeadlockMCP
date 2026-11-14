"""Deadlock Panorama live-edit MCP server: the tool definitions and their wiring."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import secrets
import time

from mcp.server.mcpserver import Image, MCPServer

from . import game, watchdog
from .assets import Assets
from .bridge import HOSTS, Mailbox, analyze_user_code, bridge_script, bridge_version, render_bridge_js
from .config import PROJECT_ROOT, Config
from .consolelog import ConsoleLog
from .css import css_diff_rules, css_to_rules, parse_selector
from .errors import AssetError, DeadlockError
from .layout import (add_stylesheet, ensure_root, inject_bridge, normalize_stylesheet, replace_stylesheet,
                     stylesheet_includes)
from .vpk import write_vpk

INSTRUCTIONS = """\
Live-edit Deadlock's Panorama UI (XML / CSS / JS) while the game runs.

Setup once: bridge_install (needs CSDK 13), then launch_game. The in-game bridge picks up requests
the server writes as files and answers through console.log, so no network console is needed.

Typical loop: status → panel_tree / panel_find to locate panels → ui_source to read the game's
original XML/CSS → css_apply / xml_load / js_eval to change things live → screenshot to look →
console_log for Panorama errors.

* js_eval runs real JS in the UI (eval is disabled by the engine, so code is compiled into a script).
* css_apply sets inline styles on matching live panels (type/#id/.class, ' ' and '>' selectors);
  persistent sheets are re-applied about once a second.
* xml_load compiles a layout (plus optional real stylesheet) with CSDK (~2s) and loads it live.
* Each top-level UI is a separate panel tree with its own bridge: ui='hud' (HUD, also in the
  hideout), 'dashboard' (menus), 'overlay'. Live tools take ui='auto' (first ready in that order).
* compile_source makes permanent overrides of game files (packed into the addon VPK; restart needed).
"""

cfg = Config()
log = ConsoleLog(cfg.citadel_dir / "console.log" if cfg.citadel_dir else None)
mailbox = Mailbox(cfg.addons_dir, log, game_running=lambda: game.game_running())
assets = Assets(cfg)

mcp = MCPServer("deadlock-panorama", instructions=INSTRUCTIONS)

# The three bridge host layouts (compiled names). Overriding one with compile_source would drop that UI's bridge.
HOST_FILES = {host + ".vxml_c" for _, host in HOSTS.values()}
MANIFEST = "mcp_bridge.json"  # next to the addon VPK: what bridge_install built, against which game build
STARTUP_GRACE = 180.0  # a launch younger than this may not show up in pgrep yet (matches the watchdog's wait)


def _err(e: Exception | str) -> str:
    return f"ERROR: {e}"


def _json(v) -> str:
    return json.dumps(v, indent=1, ensure_ascii=False)


def tool(fn):
    """Register an async tool behind one error boundary: expected failures become an "ERROR: ..." reply instead
    of an exception. functools.wraps keeps the signature and docstring the SDK builds the tool schema from."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except re.error as e:
            return _err(f"invalid regular expression: {e}")
        except (DeadlockError, OSError) as e:
            return _err(e)

    return mcp.tool()(wrapper)


async def _call(op: str, args: dict | None = None, timeout: float = 5.0, ui: str = "auto", **kw) -> str:
    return _json(await mailbox.call(op, args, timeout=timeout, ui=ui, **kw))


def _bridge_file(rel: str) -> bool:
    return rel in HOST_FILES or "mcp_bridge" in rel


# ============================================================================ status / game


def _manifest_path():
    return cfg.addons_dir / MANIFEST if cfg.addons_dir else None


def _manifest() -> dict:
    path = _manifest_path()
    try:
        return json.loads(path.read_text()) if path and path.is_file() else {}
    except (OSError, ValueError):
        return {}


def _game_build_warnings(manifest: dict) -> list[str]:
    """Was the installed bridge built against the host layouts of the current game build?"""
    try:
        changed = [h for h, crc in manifest.get("hosts", {}).items() if assets.shipped_crc(h) != crc]
    except AssetError as e:
        return [f"cannot check the game build: {e}"]
    if not changed:
        return []
    return [f"game updated since bridge_install ({', '.join(os.path.basename(h) for h in changed)} changed); "
            "run bridge_install"]


def _launch_age() -> float | None:
    """Seconds since the last launch_game whose watchdog has not finished (None if there is none)."""
    try:
        return time.time() - watchdog.token_file(cfg).stat().st_mtime
    except OSError:
        return None


PING_TIMEOUT = 3.0


async def _ping(ui: str) -> dict | str:
    t = time.monotonic()
    try:
        r = await mailbox.call("ping", ui=ui, timeout=PING_TIMEOUT)
    except DeadlockError as e:
        return _err(e)
    return {"ms": round((time.monotonic() - t) * 1000), "root": r["root"]["id"]}


@tool
async def status() -> str:
    """Report paths, game/bridge state and anything that needs fixing ("warnings"), and ping each in-game bridge."""
    log.start()
    log.poll()
    running = game.game_running()
    mounted = assets.mount_status()
    names = assets.installed_names()
    installed = all(h in names for h in HOST_FILES) and all(
        os.path.splitext(bridge_script(d))[0] + ".vjs_c" in names for d in HOSTS)
    warnings = []
    manifest = _manifest() if installed else {}
    stale_install = installed and manifest.get("bridge_version") != bridge_version()  # no manifest: built by old code
    if stale_install:
        warnings.append("installed bridge is older than this code; run bridge_install and restart the game")
    warnings += _game_build_warnings(manifest)
    info: dict = {
        "deadlock_dir": str(cfg.deadlock_dir),
        "game_running": running,
        "console_log": str(log.path) if log.exists else "missing (launch with -condebug)",
        "addons_mounted": mounted,
        "bridge_installed": installed,
    }
    # Ping every bridge console.log lists (independent bridges: all at once), even when pgrep sees no game. A
    # bridge that answers is better evidence than the process list (which a sandboxed server may not see), and
    # for a game that is gone the first failed ping makes the mailbox drop all its sessions.
    states = mailbox.ui_states()
    pings = dict(zip(states, await asyncio.gather(*(_ping(ui) for ui in states)))) if states else {}
    if not running and any(isinstance(p, dict) for p in pings.values()):
        running = info["game_running"] = True
        info["game_process"] = "not visible from this process, but a bridge answers"
    states = mailbox.ui_states()
    info["bridges_ready"] = list(states)
    if running:
        for ui, st in states.items():
            if not st["current"] and not stale_install:
                warnings.append(f"the {ui} bridge in the running game ({st['version']}) is older than the installed one "
                                f"({bridge_version()}); restart the game")
        if pings:
            info["ping"] = pings
    else:
        age = _launch_age()
        if age is not None and age < STARTUP_GRACE:
            info["game_starting"] = f"launched {age:.0f}s ago; not running yet"
        elif mounted:
            warnings.append("the dev addon is still mounted but the game is not running (crash or reboot?): a normal "
                            "Steam launch would load the bridge into regular play; run addons_mount(False)")
    info["csdk_resourcecompiler"] = (str(cfg.resourcecompiler) if cfg.resourcecompiler and cfg.resourcecompiler.is_file()
                                     else "missing")
    info["s2v_cli"] = str(cfg.s2v_cli) if cfg.s2v_cli and cfg.s2v_cli.is_file() else "missing"
    if warnings:
        info["warnings"] = warnings
    return _json(info)


@tool
async def console_log(lines: int = 100, pattern: str | None = None) -> str:
    """Recent game console output (from console.log), optionally regex-filtered,
    e.g. pattern='PanoramaScript|JS Exception|panorama'."""
    log.start()
    log.poll()
    if not log.exists:
        return _err("console.log not found (game not running with -condebug?)")
    return "\n".join(log.tail(lines, pattern)) or "(no matching lines)"


@tool
async def launch_game(map: str | None = None, extra_args: str = "", via_steam: bool = False) -> str:
    """Launch Deadlock, optionally straight into a local map with the full match HUD.

    map: 'new_player_basics' (sandbox / practice range — verified), 'dl_hideout', 'dl_midtown'.
    ('hero_testing' crashes the game even unmodded.)

    Default: run deadlock.exe directly through umu-run with the game's own Proton prefix (no Steam
    launch prompt, no shader pre-caching; Steam must be running for login). Always adds
    `-console -condebug -dev -novid` (-condebug writes console.log, which the bridge replies through).
    extra_args is split like a shell command line.
    Mounts the dev addon for this session (if that fails, the game is not launched); it is unmounted
    automatically when the game exits. via_steam=True uses `steam -applaunch` (Steam asks the user to
    confirm custom arguments).
    """
    if game.game_running():
        return "Deadlock is already running."
    if map == "hero_testing":
        return _err("+map hero_testing crashes Deadlock (even unmodded); use new_player_basics")
    args = game.launch_args(map, extra_args)
    if via_steam:
        cmd, env = game.steam_command(args)
        cwd = None
    else:
        cmd, cwd, env = game.umu_command(cfg, args)  # validate before touching gameinfo.gi
    token = secrets.token_hex(8)
    tf = watchdog.token_file(cfg)

    def claim() -> bool:
        # Runs under the gameinfo lock, before mounting: the previous launch's watchdog checks the token under
        # the same lock, so it either unmounts before we mount or sees it was superseded.
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(token)
        return True

    try:
        msgs = [assets.set_mount(True, only_if=claim)]
    except AssetError:
        watchdog.release_token(cfg, token)
        raise  # no bridge without the mount: don't launch
    mailbox.reset("a new game was launched")
    msgs.append(f"cleaned {mailbox.cleanup_all()} stale request files, {assets.cleanup_live()} live-layout files")
    if log.path:  # stale "ready" lines from the last session must not be mistaken for this one
        log.path.unlink(missing_ok=True)
        log.poll()
    try:
        game.start_watchdog(cfg, token)  # waits for the game to appear, then for it to exit
        game.spawn(cmd, None if via_steam else cfg.cache_dir / "launch.log", cwd=cwd, env=env)
    except OSError:
        assets.set_mount(False, only_if=lambda: watchdog.release_token(cfg, token))
        raise
    if via_steam:
        msgs.append("Asked Steam to launch Deadlock (Steam will ask to confirm the custom arguments).")
    else:
        msgs.append(f"Launched deadlock.exe {' '.join(args)} via umu-run "
                    f"({cfg.proton_dir.name if cfg.proton_dir else 'default Proton'}). "
                    "The bridge answers once the game reaches the start screen (~10-20s); use status.")
    return "\n".join(msgs)


@tool
async def quit_game() -> str:
    """Close Deadlock (terminates the process)."""
    if not game.game_running():
        return "Deadlock is not running."
    gone = await game.quit_game()
    mailbox.reset("quit_game closed the game")
    mailbox.cleanup_all()
    if gone:
        assets.cleanup_live()
    return "Deadlock closed." if gone else "Sent terminate; process still exiting."


@tool
async def screenshot(max_width: int = 1600, crop: str | None = None, full_screen: bool = False) -> Image | str:
    """Capture the game and return it as an image.

    By default grabs the Deadlock window itself via X11 (works even when other windows cover it,
    no focus change). If the window can't be captured (minimised, not found) this is an error, not
    a picture of the desktop. full_screen=True captures the whole screen instead, other windows
    included.
    crop: 'WxH+X+Y' in captured-image pixels (the game window's, e.g. '800x800+0+1100' for the
    bottom-left HUD), applied before resizing — use it to inspect small HUD details. A crop that
    fails is an error, not an uncropped image.
    """
    data, fmt = await game.screenshot(max_width, crop, full_screen)
    return Image(data=data, format=fmt)


# ============================================================================ reading the game's UI


@tool
async def ui_files(pattern: str | None = None, limit: int = 300) -> str:
    """List Panorama files shipped in the game VPK (regex filter), e.g. pattern='hud'."""
    return "\n".join(assets.list_panorama(pattern, limit)) or "(no matches)"


@tool
async def ui_source(path: str, refresh: bool = False) -> str:
    """Decompile a shipped Panorama file to source, e.g. 'layout/base_hud' or 'styles/hud.css'."""
    return await assets.decompile(path, refresh)


# ============================================================================ live editing


@tool
async def js_eval(code: str, target: str | None = None, ui: str = "auto", keep_context: bool = False,
                  timeout: float = 5.0) -> str:
    """Run JavaScript inside Deadlock's Panorama UI and return the result (JSON-serialised; panels
    are described as {panel: {...}}).

    A single expression is returned automatically; multi-statement code must `return` a value.
    In scope: $ (Panorama API), $panel (target panel; default the bridge panel), $root (top-level
    panel), $find(id), $query(structuredSelector), $children(p), $describe(p).
    target: panel id ('#Foo' or 'Foo'), 'root', or 'bridge'.
    keep_context=True keeps the script's context alive so event handlers / $.Schedule callbacks it
    registers keep working (otherwise contexts are recycled after ~64 requests).
    """
    _, note = analyze_user_code(code)
    result = await mailbox.call("eval", {"target": target}, timeout=timeout, ui=ui, code=code, keep=keep_context)
    return _json(result) + (f"\nnote: {note}" if note and result is None else "")


@tool
async def panel_tree(target: str = "root", depth: int = 3, max_nodes: int = 400, ui: str = "auto") -> str:
    """Dump the live panel hierarchy (id, type, layout file, size, position, text) under a panel."""
    return await _call("tree", {"target": target, "depth": depth, "max": max_nodes}, timeout=10, ui=ui)


@tool
async def panel_find(selector: str, target: str = "root", max_results: int = 100, ui: str = "all") -> str:
    """Find live panels with a CSS-like selector, e.g. 'Label.HeroName', '#HudRoot > Panel'.
    ui='all' (default) searches every loaded UI tree."""
    sel = parse_selector(selector)
    if sel is None:
        return _err("unsupported selector (use Type, #id, .class with ' ' or '>' combinators)")
    args = {"sel": sel, "target": target, "max": max_results}
    if ui != "all":
        return await _call("find", args, timeout=10, ui=ui)

    async def find(name: str):
        try:
            return await mailbox.call("find", args, ui=name, timeout=10)
        except DeadlockError as e:
            return _err(e)

    names = mailbox.ready_uis() or ["auto"]
    return _json(dict(zip(names, await asyncio.gather(*(find(n) for n in names)))))


_live_counter = int(time.time()) % 100000


def _live_name() -> str:
    global _live_counter
    _live_counter += 1
    return f"mcp_live_{_live_counter}"


@tool
async def xml_load(xml: str, target: str = "root", mode: str = "append", id: str | None = None,
                   css: str | None = None, ui: str = "auto") -> str:
    """Compile Panorama XML (and optional CSS) with CSDK and load it into the live UI (~2s).

    mode='append': create a child panel (id replaces any existing child with that id) under target.
    mode='replace': load the layout into target itself, replacing its contents. Refused for the UI
    root, the bridge panel (and its ancestors), and any panel whose type isn't plain `Panel`
    (loading into a C++ panel crashes the game).
    xml: a full <root>...</root> layout or a bare fragment (wrapped automatically). Referenced
    scripts/styles must exist in the game or the addon. css: real Panorama CSS (pseudo-classes,
    animations, @define all work) compiled as this layout's stylesheet, included after the
    layout's own <styles> so it wins ties.
    """
    if mode not in ("append", "replace"):
        return _err("mode must be 'append' or 'replace'")
    name = _live_name()
    files: dict[str, str] = {}
    doc = ensure_root(xml)
    if css:
        files[f"panorama/styles/mcp_live/{name}.css"] = css
        doc = add_stylesheet(doc, f"file://{{resources}}/styles/mcp_live/{name}.css")
    files[f"panorama/layout/mcp_live/{name}.xml"] = doc
    await assets.compile_many(files, install=False, roots=[f"panorama/layout/mcp_live/{name}.xml"])
    assets.install_loose(list(files))
    return await _call("load_layout", {"path": f"file://{{resources}}/layout/mcp_live/{name}.xml",
                                       "target": target, "mode": mode, "id": id}, timeout=10, ui=ui)


def _layout_rel(layout: str) -> str:
    """'layout/foo', 'panorama/layout/foo.xml', 'foo' → 'panorama/layout/foo.xml'."""
    name = Assets.normalize(layout if "/" in layout else f"layout/{layout}")
    return os.path.splitext(name)[0] + ".xml"


def _sheet_rel(stylesheet: str) -> str:
    """Any stylesheet spelling ('hud/x', 'styles/hud/x.css', 's2r://panorama/styles/x.vcss', ...) → 'styles/....css'."""
    s = normalize_stylesheet(stylesheet)
    if not s.startswith("styles/"):
        s = "styles/" + s
    return s if s.endswith(".css") else s + ".css"


def resolve_stylesheet(layout_rel: str, doc: str, stylesheet: str | None) -> str:
    """The stylesheet a layout_live_edit refers to, as 'styles/....css'. Default: the layout's include with its
    own stem, else its only include."""
    if stylesheet:
        return _sheet_rel(stylesheet)
    includes = list(dict.fromkeys(stylesheet_includes(doc)))
    stem = os.path.splitext(os.path.basename(layout_rel))[0]
    same = [s for s in includes if os.path.splitext(os.path.basename(s))[0] == stem]
    if len(same) == 1:
        return same[0]
    if len(includes) == 1:
        return includes[0]
    if not includes:
        raise DeadlockError(f"{os.path.basename(layout_rel)} includes no stylesheets")
    raise DeadlockError(f"{os.path.basename(layout_rel)} includes several stylesheets; pass stylesheet= one of: "
                        + ", ".join(includes))


@tool
async def layout_live_edit(layout: str, css: str | None = None, stylesheet: str | None = None,
                           xml: str | None = None, unsafe_reload: bool = False, ui: str = "auto") -> str:
    """Live-edit the styling of an existing game layout (e.g. layout='citadel_hud_top_bar').

    css: an edited copy of one of the layout's stylesheets. stylesheet: which one, in any form
    ('hud/foo', 'styles/hud/foo.css', 's2r://panorama/styles/hud/foo.vcss'); default: the layout's
    include with the layout's own name, else its only include (an error lists the choices). Get
    the original with ui_source. Declarations you added or changed are applied live as a persistent
    inline-style sheet named 'live:<path under styles/ without extension>', e.g. 'live:hud/foo'
    (@define values are substituted; pseudo-classes/animations and removed declarations can't be
    applied live — use compile_source for those). Undo with css_clear('live:hud/foo').

    unsafe_reload=True instead compiles an edited copy of the layout (xml and/or css) and re-loads
    it into the live panels. This CRASHES the game for layouts whose root is a C++ panel type (most
    HUD/dashboard layouts, e.g. CitadelHudTopBar); the bridge skips panels it knows are unsafe and
    lists them under 'skipped'. Only use it for plain Panel-rooted layouts.
    """
    rel = _layout_rel(layout)
    if not unsafe_reload:
        if xml is not None:
            return _err("XML edits of game layouts can't be applied live safely; use compile_source (restart) "
                        "or unsafe_reload=True for Panel-rooted layouts")
        if css is None:
            return _err("pass css= (the edited stylesheet)")
        sheet = _sheet_rel(stylesheet) if stylesheet else resolve_stylesheet(rel, await assets.decompile(rel), None)
        original = await assets.decompile(sheet)
        rules, warnings = css_diff_rules(original, css)
        if not rules:
            return _err("no added/changed declarations found\n" + "\n".join(warnings))
        live = "live:" + os.path.splitext(sheet.split("/", 1)[1])[0]
        out = await _call("css_apply", {"rules": rules, "sheet": live, "persist": True}, timeout=10, ui=ui)
        return out + ("\nwarnings:\n" + "\n".join(warnings) if warnings else "")

    doc = xml or await assets.decompile(rel)
    doc = re.sub(r"^\s*<!--.*?-->\s*", "", doc, count=1, flags=re.S)
    name = _live_name()
    files: dict[str, str] = {}
    if css is not None:
        sheet = resolve_stylesheet(rel, doc, stylesheet)
        flat = re.sub(r"[^\w]", "_", os.path.splitext(sheet.split("/", 1)[1])[0])
        live_css = f"panorama/styles/mcp_live/{name}_{flat}.css"
        files[live_css] = css
        doc = replace_stylesheet(doc, sheet, "file://{resources}/" + live_css.split("panorama/", 1)[1])
    live_rel = f"panorama/layout/mcp_live/{name}.xml"
    files[live_rel] = doc
    await assets.compile_many(files, install=False, roots=[live_rel])
    assets.install_loose(list(files))
    return await _call("reload_layout", {"layout": rel.split("panorama/", 1)[1],
                                         "path": f"file://{{resources}}/layout/mcp_live/{name}.xml"},
                       timeout=10, ui=ui)


@tool
async def layout_live_revert(layout: str, ui: str = "auto") -> str:
    """Re-load the game's original layout into panels changed by layout_live_edit(unsafe_reload=True).
    (For CSS edits made the safe way, use css_clear('live:<stylesheet>').)"""
    sub = _layout_rel(layout).split("panorama/", 1)[1]
    return await _call("reload_layout", {"layout": sub, "path": f"file://{{resources}}/{sub}", "revert": True},
                       timeout=10, ui=ui)


@tool
async def css_apply(css: str, sheet: str = "default", persist: bool = True, ui: str = "auto") -> str:
    """Apply CSS to live panels as inline styles. Replaces earlier CSS with the same sheet name.

    persist=True re-applies about once a second so panels the game recreates stay styled. Supports
    type, #id, .class, descendant and '>' selectors; pseudo-classes and at-rules are skipped (use
    xml_load's css for those on new panels, or compile_source for permanent game-wide styles).
    """
    rules, warnings = css_to_rules(css)
    if not rules:
        return _err("no applicable rules\n" + "\n".join(warnings))
    if persist:
        slow = [r for r in rules if not any(c.get("classes") for c in r["sel"])]
        if slow:
            warnings.append(f"note: {len(slow)} rule(s) have no .class in the selector, so matching walks the whole UI "
                            "tree (~20-60ms in the HUD); the persistent re-apply caches matches and re-walks every 5s")
    out = await _call("css_apply", {"rules": rules, "sheet": sheet, "persist": persist}, timeout=10, ui=ui)
    return out + ("\nwarnings:\n" + "\n".join(warnings) if warnings else "")


@tool
async def css_clear(sheet: str | None = None, ui: str = "auto") -> str:
    """Remove live CSS: one sheet by name, or all sheets if omitted. Panels styled by several sheets
    get the remaining sheets' values re-applied right away.

    Caveat: once an inline `visibility: collapse` has been applied, clearing it does NOT make the
    panel visible again (Panorama keeps the panel's visible flag false). To hide things you may
    want back, prefer toggling `visibility` explicitly (set 'visible' again) via js_eval.
    """
    return await _call("css_clear", {"sheet": sheet}, timeout=10, ui=ui)


@tool
async def panel_delete(target: str, ui: str = "auto") -> str:
    """Delete a live panel by id (e.g. one created with xml_load). The UI root, the bridge panel and
    its ancestors are refused."""
    return await _call("delete", {"target": target}, ui=ui)


# ============================================================================ building / installing


@tool
async def bridge_install(mount: bool = False) -> str:
    """Build and install the in-game bridges (needs CSDK 13 + Source2Viewer-CLI): one per UI root
    (HUD, dashboard, overlay). Compiles each host layout override + bridge script into the addon
    VPK. launch_game mounts the addon for its session (mount=True mounts it now). Restart the game
    afterwards. Re-run it after a game update or an update of this tool (status says when)."""
    steps, files, crcs = [], {}, {}
    for dom, (name, host) in HOSTS.items():
        src = await assets.decompile(host)
        crcs[host] = assets.shipped_crc(host)
        files[bridge_script(dom)] = render_bridge_js(dom)
        files[host + ".xml"] = inject_bridge(src, dom)
        steps.append(f"prepared {name} bridge in {host}")
    # compiling a host layout also rebuilds the bridge script it includes, so 3 compiler runs cover all 6 files
    res = await assets.compile_many(files, install=False, roots=[host + ".xml" for _, host in HOSTS.values()])
    steps.append(f"compiled {len(res['files'])} files")
    packed = await asyncio.to_thread(assets.pack_addon)
    steps.append(f"packed {len(packed['files'])} files → {packed['vpk']}")
    _manifest_path().write_text(json.dumps({"bridge_version": bridge_version(), "hosts": crcs}, indent=1))
    if mount:
        steps.append(assets.set_mount(True))
    steps.append("Restart Deadlock with launch_game for the bridges to load." if game.game_running()
                 else "Now run launch_game.")
    return "\n".join(steps)


@tool
async def bridge_uninstall() -> str:
    """Remove the bridge files from the addon VPK (other compiled overrides stay)."""
    targets = [*HOST_FILES, *(os.path.splitext(bridge_script(d))[0] + ".vjs_c" for d in HOSTS)]
    removed = await asyncio.to_thread(assets.uninstall_many, sorted(targets))
    if path := _manifest_path():
        path.unlink(missing_ok=True)
    return ("removed: " + ", ".join(removed)) if removed else "bridge files not present"


@tool
async def compile_source(path: str, source: str, install: bool = True) -> str:
    """Compile a Panorama source file with CSDK 13 and pack it into the addon VPK as a permanent
    override (takes effect after a game restart).

    path: e.g. 'panorama/layout/citadel_hud_top_bar.xml' or 'panorama/styles/hud.css'. To override
    a game file, start from ui_source(...) and keep the same path. The bridge host layouts
    (base_hud, base_dashboard, base_hud_and_db_overlay) can't be overridden: that would remove the
    bridge from that UI.
    """
    if _bridge_file(Assets.normalize(path)):
        return _err(f"{path} hosts or is part of the in-game bridge; overriding it would remove that UI's bridge "
                    "(and export_mod would leave it out). Change a layout it includes instead.")
    res = await assets.compile_many({path: source}, install=False)
    if install:
        res["vpk"] = await asyncio.to_thread(assets.pack_addon)
    return _json(res)


@tool
async def list_overrides() -> str:
    """List the compiled files currently packed into the addon VPK (bridge files + compile_source overrides)."""
    return "\n".join(sorted(assets.addon_files())) or "(none)"


@tool
async def remove_override(path: str) -> str:
    """Remove a compile_source override from the addon VPK (e.g. 'panorama/styles/hud.css').
    Takes effect after a game restart."""
    rel = Assets.normalize(path)
    if _bridge_file(rel):
        return _err("that file belongs to the bridge; use bridge_uninstall")
    removed = await asyncio.to_thread(assets.uninstall_many, [rel])
    return f"removed {rel}; restart the game to apply" if removed else _err(f"{rel} is not in the addon")


@tool
async def export_mod(name: str, include: str = "") -> str:
    """Export compiled overrides as a standalone mod VPK (no bridge files) at mods/<name>/pak01_dir.vpk.

    include: regex selecting which addon files to export (default: every non-bridge override; see
    list_overrides). Install by copying the VPK into game/citadel/addons/ as pakNN_dir.vpk (with
    `Game citadel/addons` in gameinfo.gi, as Deadlock mod managers set up).
    """
    rx = re.compile(include) if include else None
    files = {rel: p for rel, p in assets.addon_files().items()
             if not _bridge_file(rel) and (not rx or rx.search(rel))}
    if not files:
        return _err("nothing to export (see list_overrides)")
    out = PROJECT_ROOT / "mods" / re.sub(r"[^\w.-]", "_", name) / "pak01_dir.vpk"

    def pack() -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        write_vpk(out, {rel: p.read_bytes() for rel, p in files.items()})

    await asyncio.to_thread(pack)
    return _json({"vpk": str(out), "files": sorted(files)})


@tool
async def addons_mount(enable: bool = True) -> str:
    """Add (or remove) the dev addon search path in gameinfo.gi. launch_game mounts it and unmounts it
    when the game exits; removing it keeps the standard mod folder mounted."""
    return assets.set_mount(enable)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
