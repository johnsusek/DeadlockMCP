# deadlock-mcp: technical notes

## Setup

Client config: [README.md](README.md#getting-started). For development, `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`; the included `.mcp.json` runs that checkout for clients that read project-level MCP config.

`setup` installs the toolchain into the data dir, running the long steps (Source 2 Viewer download, CSDK download with `gdown` and extraction with `7z`, `proton run` from the Proton build the compiler's wine belongs to, which sets up the DXVK the compiler needs) as background tasks, so each call returns at once with each step's state. If the Google Drive download fails it looks for a `*CSDK*13*.7z` in `~/Downloads` instead.

On first use, `bridge_install` decompiles the three UI host layouts, injects the bridge, and compiles them into the dev addon. `launch_game` mounts the addon for each session.

## Tools

| tool | what it does |
|---|---|
| `status` | game, addon and bridge state; pings each loaded UI at once; `warnings` says what to fix (stale bridge after a game or tool update, addon left mounted after a crash) |
| `setup` | installs the toolchain (see above); lists missing system tools with the install command for the distro (apt, dnf, pacman, zypper, rpm-ostree) |
| `launch_game` / `quit_game` | runs `deadlock.exe` directly via `umu-run` in Steam's prefix (no launch-options prompt, no shader pre-cache), adding `-console -condebug -dev -novid`; `via_steam=True` uses `steam -applaunch` (native, Snap or Flatpak Steam) instead; `map="new_player_basics"` boots into the sandbox with the full match HUD; refuses to launch if the dev addon can't be mounted |
| `console_log` | tail of `console.log` (Panorama errors, `$.Msg` output) |
| `screenshot` | captures the game window as an image, with optional `WxH+X+Y` crop; `full_screen=True` captures the whole screen. A minimised game window is an error, never a picture of the desktop |
| `ui_files` / `ui_source` | list the game's Panorama files / decompile any of them |
| `panel_tree` / `panel_find` | inspect the live panel tree with CSS-like selectors |
| `js_eval` | run JS in a UI context and get the value back (`$panel`, `$root`, `$find`, `$query`, …); `keep_context` keeps timers and handlers alive; a `null` result from code it couldn't parse comes with a note to add `return` |
| `css_apply` / `css_clear` | live CSS applied as inline styles; persistent sheets re-apply every second as panels appear; clearing a sheet re-applies the remaining ones at once |
| `xml_load` | compile XML (plus real CSS with `:hover`, `@define`, animations; included last, so it wins ties) and load it into a panel, in about 2 s |
| `layout_live_edit` / `layout_live_revert` | edit a game stylesheet (any of the layout's includes, subdirectories too); only the changed declarations are applied live, as sheet `live:<path under styles/>` |
| `panel_delete` | remove a panel (not the UI root or the bridge) |
| `compile_source` / `list_overrides` / `remove_override` | permanent overrides of game files, packed into the dev addon VPK (restart needed); the three bridge host layouts are refused |
| `export_mod` | packs your overrides, without the bridge, into a standalone `<data dir>/mods/<name>/pak01_dir.vpk`; `install=True` copies it into `game/citadel/addons/` as the next free `pakNN_dir.vpk`, and re-installing the same name replaces that copy while it is still the file we wrote |
| `bridge_install` / `bridge_uninstall` / `addons_mount` | setup and teardown; `bridge_install` records the bridge version and game build in `mcp_addons/mcp_bridge.json` so `status` can tell when to re-run it |

Pass `ui='hud' | 'dashboard' | 'overlay'` to pick a panel tree. The default `auto` uses the first one that's ready, in that order.

[`examples/stat_boosts_row`](examples/stat_boosts_row) is a complete HUD mod built this way: a JS prototype, the final CSS, and the build steps.

## Environment overrides

| variable | default |
|---|---|
| `DEADLOCK_DIR` | auto-detected from Steam libraries |
| `DEADLOCK_MCP_DATA` | `$XDG_DATA_HOME/deadlock-mcp` (`~/.local/share/deadlock-mcp`): toolchain, Wine prefix, exported mods |
| `CSDK_DIR` | `<data dir>/tools/Reduced_CSDK_13` |
| `S2V_CLI` | `<data dir>/tools/s2v/Source2Viewer-CLI` |
| `DEADLOCK_PROTON` | Steam's *Proton - Experimental* (used to run the game) |
| `DEADLOCK_WINE` / `WINEPREFIX` | newest GE-Proton (else Proton) wine / `<data dir>/wine/pfx` (used for the compiler) |
| `CSDK_CONTENT` / `CSDK_GAME` | `<CSDK_DIR>/{content,game}/citadel_addons/mcp_bridge` (where sources are written / compiled files land) |
| `STEAM_COMPAT_CLIENT_INSTALL_PATH` | the Steam client install (`~/.local/share/Steam`, `~/.steam/steam`, the Flatpak's or the Snap's); the game's Proton prefix is taken from the library Deadlock is in |
| `DEADLOCK_RC_CMD` | custom compiler command template (`{rc}`, `{src_win}`) |
| `DEADLOCK_MOUNT` | `citadel/mcp_addons` (the dev addon) |
| `DEADLOCK_EXTRA_MOUNTS` | `citadel/addons` (comma-separated; mounted after the dev addon so installed mods load) |
| `DEADLOCK_MCP_CACHE` | `~/.cache/deadlock-mcp` (decompiled sources) |

The compiler runs under Wine and needs an X display even though it draws nothing: `DISPLAY` and `XAUTHORITY`. If the MCP client starts the server with a stripped environment, the server restores them from the session (`/tmp/.X11-unix`, then `$XDG_RUNTIME_DIR/xauth_*` as KDE's XWayland uses, `.mutter-Xwaylandauth.*`, `~/.Xauthority`). Without them every compile fails with `RenderDeviceMgr001`.

## How it works

Deadlock's client has no usable network console (`-netconport` does nothing) and no `BLoadLayoutFromString`, and its V8 blocks `eval`/`new Function`. So the bridge takes requests as files and replies through the console log:

```
MCP server ──writes──▶ citadel/mcp_addons/panorama/{layout,scripts}/h00000042.v{xml,js}_c   (loose files)
                                         │ bridge polls $.BImageFileExists("s2r://…/h00000042.vxml") every 0.1s
                                         ▼
                        bridge (in base_hud / base_dashboard / base_hud_and_db_overlay) BLoadLayout()s it
                                         │ the request script runs, replies via $.Msg("@@MCP h42 0/1 {…}")
MCP server ◀──tails── citadel/console.log  (-condebug)
```

Round trips take about 90–150 ms.

* **Fresh names per request.** Panorama caches layouts and scripts by path for the whole session.
* **No compiler on the hot path.** Request `.vjs_c` files are just a header plus raw JS, and request `.vxml_c` files are a compiled template with the path patched in place (`deadlock_mcp/resource.py`).
* **One bridge per UI root.** Each top-level window (`hud`, `dashboard`, `overlay`) is a separate panel tree with its own JS contexts, so each gets its own bridge and serial stream.
* **One request in flight per UI.** A bridge only probes for its next serial, so the server sends one request at a time per UI and only moves on when the bridge logs that it took it. A request that is never picked up is withdrawn and its serial reused; `ui='auto'` then skips that UI until it answers again.
* **The addon must be a VPK.** The engine searches every VPK before any loose directory, so overrides of game files go into `mcp_addons/pak01_dir.vpk`. Only brand-new paths (requests, live layouts) can be loose files.
* **Mounting.** The addon is mounted ahead of `citadel` in a marked block in `gameinfo.gi`, with explicit `Mod`/`Write citadel` keys so the game doesn't start writing into the addon dir. `launch_game` mounts the dev addon and starts a detached watcher (`deadlock_mcp/watchdog.py`) that unmounts it when the game exits, so a normal Steam launch doesn't load the bridge. Each launch writes a token before mounting, and the watcher checks it and unmounts under the same lock, so an old watcher can't unmount a newer launch's addon. Unmounting keeps the extra mounts (`citadel/addons`) in the block, so installed mods still load. The watcher logs to `~/.cache/deadlock-mcp/watchdog.log`.

## Known limits

* **Locale APIs crash the game.** `Intl`, `toLocale*()`, `localeCompare` and `normalize` hard-crash Deadlock, because its V8 has no ICU data. Every bridge and request context replaces them with stubs that throw a normal error. Code in your own compiled scripts is not protected.
* **Clearing `visibility: collapse` doesn't bring a panel back.** Once an inline `visibility: collapse` is applied, setting the style back to `null` leaves the panel hidden, so set `'visible'` explicitly. See `examples/stat_boosts_row/live_watcher.js`.
* **A layout's root merges into the panel you load it into.** That's why `xml_load` appends into a click-through full-size holder, so the layout's own root CSS (size, alignment) applies.
* **Reloading game layouts crashes the game.** Re-loading a game layout into its live C++ panel (`BLoadLayout` with override) causes an access violation. That's why `layout_live_edit` applies CSS diffs as inline styles by default, and why `unsafe_reload` exists only for plain `Panel`-rooted layouts. The bridge refuses the risky cases it can see: `xml_load(mode='replace')` and `panel_delete` on the UI root or the bridge panel, `replace` into anything but a plain `Panel`, and `unsafe_reload` skips such panels and lists them under `skipped`.
* **Inline styles have limits.** They can't express pseudo-classes or animations, and they can't remove a declaration from the game's stylesheet. Use `compile_source` for those.
* **Bare-type selectors are slow.** A selector with no `#id`/`.class` (e.g. `Label`) walks the whole tree, which costs about 20–60 ms in the HUD (30k panels). Anchored selectors use native lookups (≈0 ms).
* **Map loads.** `new_player_basics` works. `hero_testing` crashes even unmodded, so `launch_game` refuses it.
* **The bridge needs the game running and not frozen.** Requests time out with a hint if the game is paused, still loading, or restarting.
* **A crash leaves the addon mounted.** The watcher unmounts it when the game exits, but not after a reboot or if the watcher is killed. `status` warns about it; run `addons_mount(False)`.

## Development

`pytest`: the unit tests cover script generation, the CSS diff, layout edits, VPK round trips, config detection, an end-to-end mailbox test against a fake game (`test_bridge.py`), the tool layer (`test_server.py`: error replies, launch/mount ordering, `status`) and the unmount watcher (`test_watchdog.py`). `test_js.py` runs the in-game JS in embedded V8 (the `mini-racer` dev dependency) against a fake Panorama API (`tests/panorama_v8.py`), and `test_e2e.py` runs the real bridge poll loop and request scripts there against the real `Mailbox`. None of them need the game, so CI runs them. `scripts/smoke.py` and `scripts/exercise.py` exercise a real running game. `scripts/make_templates.py` regenerates the compiled request templates in `deadlock_mcp/templates/` (needs CSDK; only if the resource format changes).

Code layout: `server.py` (tool definitions; each tool is `async` behind one `@tool` error boundary), `game.py` (launch, quit, screenshots), `env.py` (desktop-session variables), `watchdog.py`, `bridge.py` (transport), `assets.py` (decompile, compile, VPK, `gameinfo.gi`), `css.py` / `layout.py` (text edits).
