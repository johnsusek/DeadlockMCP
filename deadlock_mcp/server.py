"""Deadlock Panorama live-edit MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from .assets import AssetError, Assets
from .bridge import (
    HOSTS,
    INBOX_LAYOUT,
    INBOX_LAYOUT_XML,
    INBOX_SCRIPT,
    BridgeError,
    Mailbox,
    bridge_script,
    add_stylesheet,
    css_diff_rules,
    css_to_rules,
    ensure_root,
    inject_bridge,
    parse_selector,
    render_bridge_js,
)
from .config import DEADLOCK_APPID, Config
from .consolelog import ConsoleLog
from .vpk import VPK

INSTRUCTIONS = """\
Live-edit Deadlock's Panorama UI (XML / CSS / JS) while the game runs.

Setup once: bridge_install (needs CSDK 13), then launch_game. The in-game bridge picks up requests
the server writes as files and answers through console.log, so no network console is needed.

Typical loop: status → panel_tree / panel_find to locate panels → ui_source to read the game's
original XML/CSS → css_apply / xml_load / js_eval to change things live → screenshot to look →
console_log for Panorama errors.

* js_eval runs real JS in the UI (eval is disabled by the engine, so code is compiled into a script).
* css_apply sets inline styles on matching live panels (type/#id/.class, ' ' and '>' selectors);
  persistent sheets are re-applied every 0.5s.
* xml_load compiles a layout (plus optional real stylesheet) with CSDK (~3s) and loads it live.
* Each top-level UI is a separate panel tree with its own bridge: ui='hud' (HUD, also in the
  hideout), 'dashboard' (menus), 'overlay'. Live tools take ui='auto' (first ready in that order).
* compile_source makes permanent overrides of game files (packed into the addon VPK; restart needed).
"""

cfg = Config()
log = ConsoleLog(cfg.citadel_dir / "console.log" if cfg.citadel_dir else None)
mailbox = Mailbox(cfg.addons_dir, log)
assets = Assets(cfg)

mcp = MCPServer("deadlock-panorama", instructions=INSTRUCTIONS)


def _err(e: Exception | str) -> str:
    return f"ERROR: {e}"


def _json(v) -> str:
    return json.dumps(v, indent=1, ensure_ascii=False)


def _desktop_env(base: dict | None = None) -> dict:
    """MCP clients may start servers with a stripped environment; restore the desktop-session
    variables GUI tools (umu/Proton, spectacle) need, from their standard locations."""
    env = dict(os.environ if base is None else base)
    run = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if run.is_dir():
        env.setdefault("XDG_RUNTIME_DIR", str(run))
        if "WAYLAND_DISPLAY" not in env:
            socks = sorted(p.name for p in run.glob("wayland-[0-9]") if p.is_socket())
            if socks:
                env["WAYLAND_DISPLAY"] = socks[0]
        if (run / "bus").exists():
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run / 'bus'}")
    if "DISPLAY" not in env:
        xs = sorted(Path("/tmp/.X11-unix").glob("X*"))
        if xs:
            env["DISPLAY"] = ":" + xs[0].name[1:]
    env.setdefault("XDG_SESSION_TYPE", "wayland" if env.get("WAYLAND_DISPLAY") else "x11")
    return env


def _game_running() -> bool:
    out = subprocess.run(["pgrep", "-f", r"bin[/\\]win64[/\\]deadlock\.exe"], capture_output=True, text=True)
    return bool(out.stdout.strip())


async def _call(op: str, args: dict | None = None, timeout: float = 5.0, ui: str = "auto", **kw) -> str:
    try:
        return _json(await mailbox.call(op, args, timeout=timeout, ui=ui, **kw))
    except BridgeError as e:
        return _err(e)



# ============================================================================ status / game


@mcp.tool()
async def status() -> str:
    """Report paths, game/bridge state, and ping the in-game bridge."""
    log.start()
    log.poll()
    vpk = cfg.addons_dir / "pak01_dir.vpk" if cfg.addons_dir else None
    info: dict = {
        "deadlock_dir": str(cfg.deadlock_dir),
        "game_running": _game_running(),
        "console_log": str(log.path) if log.exists else "missing (launch with -condebug)",
        "addons_mounted": assets.mount_status(),
        "bridge_installed": bool(vpk and vpk.is_file() and all(h + ".vxml_c" in VPK(vpk) for _, h in HOSTS.values())),
        "bridges_ready": mailbox.ready_uis(),
        "csdk_resourcecompiler": str(cfg.resourcecompiler) if cfg.resourcecompiler and cfg.resourcecompiler.is_file() else "missing",
        "s2v_cli": str(cfg.s2v_cli) if cfg.s2v_cli and cfg.s2v_cli.is_file() else "missing",
    }
    pings = {}
    for ui in mailbox.ready_uis():
        t = time.monotonic()
        try:
            r = await mailbox.call("ping", ui=ui, timeout=3)
            pings[ui] = {"ms": round((time.monotonic() - t) * 1000), "root": r["root"]["id"]}
        except BridgeError as e:
            pings[ui] = _err(e)
    if pings:
        info["ping"] = pings
    return _json(info)


@mcp.tool()
async def console_log(lines: int = 100, pattern: str | None = None) -> str:
    """Recent game console output (from console.log), optionally regex-filtered,
    e.g. pattern='PanoramaScript|JS Exception|panorama'."""
    log.start()
    log.poll()
    if not log.exists:
        return _err("console.log not found (game not running with -condebug?)")
    return "\n".join(log.tail(lines, pattern)) or "(no matching lines)"


DEFAULT_LAUNCH_ARGS = "-console -condebug -dev -novid"


@mcp.tool()
async def launch_game(map: str | None = None, extra_args: str = "", via_steam: bool = False) -> str:
    """Launch Deadlock, optionally straight into a local map with the full match HUD.

    map: 'new_player_basics' (sandbox / practice range — verified), 'dl_hideout', 'dl_midtown'.
    ('hero_testing' crashes the game even unmodded.)

    Default: run deadlock.exe directly through umu-run with the game's own Proton prefix (no Steam
    launch prompt, no shader pre-caching; Steam must be running for login). Always adds
    `-console -condebug -dev -novid` (-condebug writes console.log, which the bridge replies through).
    via_steam=True uses `steam -applaunch` (Steam asks the user to confirm custom arguments).
    """
    if _game_running():
        return "Deadlock is already running."
    msgs = []
    try:
        msgs.append(assets.set_mount(True))
    except AssetError as e:
        msgs.append(_err(e))
    msgs.append(f"cleaned {mailbox.cleanup_all()} stale request files, {assets.cleanup_live()} live-layout files")
    if log.path:  # stale "ready" lines from the last session must not be mistaken for this one
        log.path.unlink(missing_ok=True)
        log.poll()
    if map == "hero_testing":
        return _err("+map hero_testing crashes Deadlock (even unmodded); use new_player_basics")
    args = f"{DEFAULT_LAUNCH_ARGS} {'+map ' + map if map else ''} {extra_args}".split()
    if via_steam:
        steam = shutil.which("steam") or "steam"
        subprocess.Popen([steam, "-applaunch", str(DEADLOCK_APPID), *args], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=_desktop_env())
        msgs.append("Asked Steam to launch Deadlock (Steam will ask to confirm the custom arguments).")
        return "\n".join(msgs)
    if not cfg.deadlock_dir:
        return _err("Deadlock dir not found")
    umu = shutil.which("umu-run")
    if not umu:
        return _err("umu-run not found; install umu-launcher or use via_steam=True")
    steam_root = cfg.deadlock_dir.parent.parent.parent  # <Steam>/steamapps/common/Deadlock
    env = dict(
        _desktop_env(),
        GAMEID=f"umu-{DEADLOCK_APPID}",
        STORE="none",
        SteamAppId=str(DEADLOCK_APPID),
        SteamGameId=str(DEADLOCK_APPID),
        # umu treats WINEPREFIX as the compat-data root and uses <root>/pfx itself
        WINEPREFIX=str(steam_root / "steamapps/compatdata" / str(DEADLOCK_APPID)),
        STEAM_COMPAT_CLIENT_INSTALL_PATH=str(steam_root),
    )
    if cfg.proton_dir:
        env["PROTONPATH"] = str(cfg.proton_dir)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    out = open(cfg.cache_dir / "launch.log", "w")
    subprocess.Popen([umu, "./deadlock.exe", *args], cwd=cfg.deadlock_dir / "game/bin/win64", env=env,
                     stdout=out, stderr=out, start_new_session=True)
    msgs.append(f"Launched deadlock.exe {' '.join(args)} via umu-run "
                f"({cfg.proton_dir.name if cfg.proton_dir else 'default Proton'}). "
                "The bridge answers once the game reaches the start screen (~10-20s); use status.")
    return "\n".join(msgs)


@mcp.tool()
async def quit_game() -> str:
    """Close Deadlock (terminates the process)."""
    if not _game_running():
        return "Deadlock is not running."
    subprocess.run(["pkill", "-f", r"bin[/\\]win64[/\\]deadlock\.exe"], check=False)
    for _ in range(40):
        await asyncio.sleep(0.25)
        if not _game_running():
            break
    mailbox.cleanup_all()
    if not _game_running():
        assets.cleanup_live()
    return "Deadlock closed." if not _game_running() else "Sent terminate; process still exiting."


@mcp.tool()
async def screenshot(max_width: int = 1600, crop: str | None = None, full_screen: bool = False) -> Image | str:
    """Capture the game and return it as an image.

    By default grabs the Deadlock window itself via X11 (works even when other windows cover it,
    no focus change). full_screen=True captures the whole screen instead.
    crop: 'WxH+X+Y' in game-window pixels (e.g. '800x800+0+1100' for the bottom-left HUD), applied
    before resizing — use it to inspect small HUD details.
    """
    spectacle = shutil.which("spectacle")
    grim = shutil.which("grim")
    env = _desktop_env()
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "shot.png"
        win = None
        if not full_screen and shutil.which("xdotool") and shutil.which("import"):
            r = subprocess.run(["xdotool", "search", "--name", "^Deadlock$"], capture_output=True, text=True, env=env)
            win = (r.stdout.split() or [None])[0]
        if win:
            cmd = ["import", "-window", win, str(raw)]
        elif spectacle:
            cmd = [spectacle, "-b", "-n", "-f", "-o", str(raw)]
        elif grim:
            cmd = [grim, str(raw)]
        else:
            return _err("no screenshot tool found (spectacle or grim)")
        errf = Path(td) / "err.txt"
        with open(errf, "wb") as ef:
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=ef, stderr=ef, env=env)
            try:
                await asyncio.wait_for(proc.wait(), 20)
            except asyncio.TimeoutError:
                proc.kill()
                return _err("screenshot timed out")
        if not raw.is_file():
            return _err(f"screenshot failed: {errf.read_text(errors='replace')[-800:]}")
        magick = shutil.which("magick") or shutil.which("convert")
        if magick:
            small = Path(td) / "shot.jpg"
            ops = (["-crop", crop, "+repage"] if crop else []) + ["-resize", f"{max_width}x>", "-quality", "85"]
            subprocess.run([magick, str(raw), *ops, str(small)], check=False)
            if small.is_file():
                return Image(data=small.read_bytes(), format="jpeg")
        return Image(data=raw.read_bytes(), format="png")


# ============================================================================ reading the game's UI


@mcp.tool()
def ui_files(pattern: str | None = None, limit: int = 300) -> str:
    """List Panorama files shipped in the game VPK (regex filter), e.g. pattern='hud'."""
    try:
        names = assets.list_panorama(pattern, limit)
    except AssetError as e:
        return _err(e)
    return "\n".join(names) or "(no matches)"


@mcp.tool()
async def ui_source(path: str, refresh: bool = False) -> str:
    """Decompile a shipped Panorama file to source, e.g. 'layout/base_hud' or 'styles/hud.css'."""
    try:
        return await assets.decompile(path, refresh)
    except AssetError as e:
        return _err(e)


# ============================================================================ live editing


@mcp.tool()
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
    return await _call("eval", {"target": target}, timeout=timeout, ui=ui, code=code, keep=keep_context)


@mcp.tool()
async def panel_tree(target: str = "root", depth: int = 3, max_nodes: int = 400, ui: str = "auto") -> str:
    """Dump the live panel hierarchy (id, type, layout file, size, position, text) under a panel."""
    return await _call("tree", {"target": target, "depth": depth, "max": max_nodes}, timeout=10, ui=ui)


@mcp.tool()
async def panel_find(selector: str, target: str = "root", max_results: int = 100, ui: str = "all") -> str:
    """Find live panels with a CSS-like selector, e.g. 'Label.HeroName', '#HudRoot > Panel'.
    ui='all' (default) searches every loaded UI tree."""
    sel = parse_selector(selector)
    if sel is None:
        return _err("unsupported selector (use Type, #id, .class with ' ' or '>' combinators)")
    args = {"sel": sel, "target": target, "max": max_results}
    if ui != "all":
        return await _call("find", args, timeout=10, ui=ui)
    out = {}
    for name in mailbox.ready_uis() or ["auto"]:
        try:
            out[name] = await mailbox.call("find", args, ui=name, timeout=10)
        except BridgeError as e:
            out[name] = _err(e)
    return _json(out)


_live_counter = int(time.time()) % 100000


@mcp.tool()
async def xml_load(xml: str, target: str = "root", mode: str = "append", id: str | None = None,
                   css: str | None = None, ui: str = "auto") -> str:
    """Compile Panorama XML (and optional CSS) with CSDK and load it into the live UI (~3s).

    mode='append': create a child panel (id replaces any existing child with that id) under target.
    mode='replace': load the layout into target itself, replacing its contents.
    xml: a full <root>...</root> layout or a bare fragment (wrapped automatically). Referenced
    scripts/styles must exist in the game or the addon. css: real Panorama CSS (pseudo-classes,
    animations, @define all work) compiled as this layout's stylesheet.
    """
    global _live_counter
    if mode not in ("append", "replace"):
        return _err("mode must be 'append' or 'replace'")
    _live_counter += 1
    name = f"mcp_live_{_live_counter}"
    files: dict[str, str] = {}
    doc = ensure_root(xml)
    if css:
        files[f"panorama/styles/mcp_live/{name}.css"] = css
        doc = add_stylesheet(doc, f"file://{{resources}}/styles/mcp_live/{name}.css")
    files[f"panorama/layout/mcp_live/{name}.xml"] = doc
    try:
        await assets.compile_many(files, install=False, roots=[f"panorama/layout/mcp_live/{name}.xml"])
        assets.install_loose(list(files))
    except AssetError as e:
        return _err(e)
    out = await _call("load_layout", {"path": f"file://{{resources}}/layout/mcp_live/{name}.xml",
                                      "target": target, "mode": mode, "id": id}, timeout=10, ui=ui)
    return out


def _layout_rel(layout: str) -> str:
    """'layout/foo', 'panorama/layout/foo.xml', 'foo' → 'panorama/layout/foo.xml'."""
    name = Assets.normalize(layout if "/" in layout else f"layout/{layout}")
    return os.path.splitext(name)[0] + ".xml"


@mcp.tool()
async def layout_live_edit(layout: str, css: str | None = None, stylesheet: str | None = None,
                           xml: str | None = None, unsafe_reload: bool = False, ui: str = "auto") -> str:
    """Live-edit the styling of an existing game layout (e.g. layout='citadel_hud_top_bar').

    css: an edited copy of one of the layout's stylesheets — `stylesheet` (default: the same-named
    styles/<layout>.css; get the original with ui_source). Declarations you added or changed are
    applied live as a persistent inline-style sheet named 'live:<stylesheet>' (@define values are
    substituted; pseudo-classes/animations and removed declarations can't be applied live — use
    compile_source for those). Undo with css_clear('live:<stylesheet>').

    unsafe_reload=True instead compiles an edited copy of the layout (xml and/or css) and re-loads
    it into the live panels. This CRASHES the game for layouts whose root is a C++ panel type (most
    HUD/dashboard layouts, e.g. CitadelHudTopBar); only use it for plain Panel-rooted layouts.
    """
    global _live_counter
    rel = _layout_rel(layout)
    stem = os.path.splitext(os.path.basename(rel))[0]
    sheet_stem = os.path.splitext(os.path.basename(stylesheet or f"styles/{stem}"))[0]
    if not unsafe_reload:
        if xml is not None:
            return _err("XML edits of game layouts can't be applied live safely; use compile_source (restart) "
                        "or unsafe_reload=True for Panel-rooted layouts")
        if css is None:
            return _err("pass css= (the edited stylesheet)")
        try:
            original = await assets.decompile(f"styles/{sheet_stem}.css")
        except AssetError as e:
            return _err(e)
        rules, warnings = css_diff_rules(original, css)
        if not rules:
            return _err("no added/changed declarations found\n" + "\n".join(warnings))
        out = await _call("css_apply", {"rules": rules, "sheet": f"live:{sheet_stem}", "persist": True},
                          timeout=10, ui=ui)
        return out + ("\nwarnings:\n" + "\n".join(warnings) if warnings else "")

    try:
        doc = xml or await assets.decompile(rel)
    except AssetError as e:
        return _err(e)
    doc = re.sub(r"^\s*<!--.*?-->\s*", "", doc, count=1, flags=re.S)
    _live_counter += 1
    name = f"mcp_live_{_live_counter}"
    files: dict[str, str] = {}
    if css is not None:
        rx = re.compile(r'src="(?:s2r://panorama/styles/|file://\{resources\}/styles/)' + re.escape(sheet_stem) + r'\.v?css"')
        if not rx.search(doc):
            return _err(f"layout does not include stylesheet {sheet_stem!r}; pass stylesheet= one of its <styles> includes")
        files[f"panorama/styles/mcp_live/{name}_{sheet_stem}.css"] = css
        doc = rx.sub(f'src="file://{{resources}}/styles/mcp_live/{name}_{sheet_stem}.css"', doc)
    live_rel = f"panorama/layout/mcp_live/{name}.xml"
    files[live_rel] = doc
    try:
        await assets.compile_many(files, install=False, roots=[live_rel])
        assets.install_loose(list(files))
    except AssetError as e:
        return _err(e)
    return await _call("reload_layout", {"layout": rel.split("panorama/", 1)[1],
                                         "path": f"file://{{resources}}/layout/mcp_live/{name}.xml"},
                       timeout=10, ui=ui)


@mcp.tool()
async def layout_live_revert(layout: str, ui: str = "auto") -> str:
    """Re-load the game's original layout into panels changed by layout_live_edit(unsafe_reload=True).
    (For CSS edits made the safe way, use css_clear('live:<stylesheet>').)"""
    rel = _layout_rel(layout)
    sub = rel.split("panorama/", 1)[1]
    return await _call("reload_layout", {"layout": sub, "path": f"file://{{resources}}/{sub}", "revert": True},
                       timeout=10, ui=ui)


@mcp.tool()
async def css_apply(css: str, sheet: str = "default", persist: bool = True, ui: str = "auto") -> str:
    """Apply CSS to live panels as inline styles. Replaces earlier CSS with the same sheet name.

    persist=True re-applies every 0.5s so panels the game recreates stay styled. Supports type,
    #id, .class, descendant and '>' selectors; pseudo-classes and at-rules are skipped (use
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



@mcp.tool()
async def css_clear(sheet: str | None = None, ui: str = "auto") -> str:
    """Remove live CSS: one sheet by name, or all sheets if omitted.

    Caveat: once an inline `visibility: collapse` has been applied, clearing it does NOT make the
    panel visible again (Panorama keeps the panel's visible flag false). To hide things you may
    want back, prefer toggling `visibility` explicitly (set 'visible' again) via js_eval.
    """
    return await _call("css_clear", {"sheet": sheet}, timeout=10, ui=ui)


@mcp.tool()
async def panel_delete(target: str, ui: str = "auto") -> str:
    """Delete a live panel by id (e.g. one created with xml_load)."""
    return await _call("delete", {"target": target}, ui=ui)


# ============================================================================ building / installing


@mcp.tool()
async def bridge_install(mount: bool = True) -> str:
    """Build and install the in-game bridges (needs CSDK 13 + Source2Viewer-CLI): one per UI root
    (HUD, dashboard, overlay). Compiles each host layout override + bridge script into the addon
    VPK and mounts the addon in gameinfo.gi. Restart the game afterwards."""
    steps = []
    try:
        files = {INBOX_SCRIPT: "// template for request scripts\n", INBOX_LAYOUT: INBOX_LAYOUT_XML}
        for dom, (name, host) in HOSTS.items():
            src = await assets.decompile(host)
            files[bridge_script(dom)] = render_bridge_js(dom)
            files[host + ".xml"] = inject_bridge(src, dom)
            steps.append(f"prepared {name} bridge in {host}")
        res = await assets.compile_many(files)
        steps.append(f"compiled {len(res['files'])} files")
        steps.append(f"packed {len(res['vpk']['files'])} files → {res['vpk']['vpk']}")
        if mount:
            steps.append(assets.set_mount(True))
        steps.append("Restart Deadlock for the bridges to load." if _game_running() else "Now run launch_game.")
    except (AssetError, BridgeError) as e:
        steps.append(_err(e))
    return "\n".join(steps)


@mcp.tool()
def bridge_uninstall() -> str:
    """Remove the bridge files from the addon VPK (other compiled overrides stay)."""
    targets = []
    for dom, (_, host) in HOSTS.items():
        targets += [host + ".vxml_c", f"panorama/scripts/mcp_bridge_{dom}.vjs_c"]
    removed = [p for p in targets if assets.uninstall(p)]
    return ("removed: " + ", ".join(removed)) if removed else "bridge files not present"


@mcp.tool()
async def compile_source(path: str, source: str, install: bool = True) -> str:
    """Compile a Panorama source file with CSDK 13 and pack it into the addon VPK as a permanent
    override (takes effect after a game restart).

    path: e.g. 'panorama/layout/citadel_hud_top_bar.xml' or 'panorama/styles/hud.css'. To override
    a game file, start from ui_source(...) and keep the same path.
    """
    try:
        return _json(await assets.compile_many({path: source}, install))
    except AssetError as e:
        return _err(e)


@mcp.tool()
def list_overrides() -> str:
    """List the compiled files currently packed into the addon VPK (bridge files + compile_source overrides)."""
    return "\n".join(sorted(assets.addon_files())) or "(none)"


@mcp.tool()
def remove_override(path: str) -> str:
    """Remove a compile_source override from the addon VPK (e.g. 'panorama/styles/hud.css').
    Takes effect after a game restart."""
    rel = Assets.normalize(path)
    if any(rel == h + ".vxml_c" for _, h in HOSTS.values()) or "mcp_bridge" in rel:
        return _err("that file belongs to the bridge; use bridge_uninstall")
    return f"removed {rel}; restart the game to apply" if assets.uninstall(rel) else _err(f"{rel} is not in the addon")


@mcp.tool()
def export_mod(name: str, include: str = "") -> str:
    """Export compiled overrides as a standalone mod VPK (no bridge files) at mods/<name>/pak01_dir.vpk.

    include: regex selecting which addon files to export (default: every non-bridge override; see
    list_overrides). Install by copying the VPK into game/citadel/addons/ as pakNN_dir.vpk (with
    `Game citadel/addons` in gameinfo.gi, as Deadlock mod managers set up).
    """
    from .config import PROJECT_ROOT
    from .vpk import write_vpk

    bridge = {h + ".vxml_c" for _, h in HOSTS.values()}
    rx = re.compile(include) if include else None
    files = {rel: p for rel, p in assets.addon_files().items()
             if rel not in bridge and "mcp_bridge" not in rel and (not rx or rx.search(rel))}
    if not files:
        return _err("nothing to export (see list_overrides)")
    out = PROJECT_ROOT / "mods" / re.sub(r"[^\w.-]", "_", name) / "pak01_dir.vpk"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_vpk(out, {rel: p.read_bytes() for rel, p in files.items()})
    return _json({"vpk": str(out), "files": sorted(files)})


@mcp.tool()
def addons_mount(enable: bool = True) -> str:
    """Add (or remove) the addon search path in gameinfo.gi. Game updates rewrite gameinfo.gi;
    launch_game re-mounts automatically."""
    try:
        return assets.set_mount(enable)
    except AssetError as e:
        return _err(e)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
