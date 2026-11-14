# deadlock-mcp: technical notes

## Setup

```bash
git clone <this repo> deadlock-mcp && cd deadlock-mcp
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

# Source 2 Viewer CLI → tools/s2v
mkdir -p tools/s2v && curl -L -o /tmp/s2v.zip \
  https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/20.1/cli-linux-x64.zip \
  && unzip /tmp/s2v.zip -d tools/s2v

# CSDK 13 (Reduced) → extract to tools/Reduced_CSDK_13

# one-time wine prefix for the compiler (~/Games/umu/umu-default)
GAMEID=umu-default umu-run cmd /c exit
```

Use an editable install (`-e`). The server finds `tools/` and writes `mods/` relative to the checkout.

> On first time use, the bridge should be installed with `bridge_install` which decompiles the three UI host layouts, injects the bridge, compiles them, and mounts the addon.

## Tools

| tool | what it does |
|---|---|
| `status` | game, addon and bridge state; pings each loaded UI |
| `launch_game` / `quit_game` | runs `deadlock.exe` directly via `umu-run` in Steam's prefix (no launch-options prompt, no shader pre-cache), adding `-console -condebug -dev -novid`; `map="new_player_basics"` boots into the sandbox with the full match HUD |
| `console_log` | tail of `console.log` (Panorama errors, `$.Msg` output) |
| `screenshot` | captures the game window (or the full screen) as an image, with optional crop |
| `ui_files` / `ui_source` | list the game's Panorama files / decompile any of them |
| `panel_tree` / `panel_find` | inspect the live panel tree with CSS-like selectors |
| `js_eval` | run JS in a UI context and get the value back (`$panel`, `$root`, `$find`, `$query`, …); `keep_context` keeps timers and handlers alive |
| `css_apply` / `css_clear` | live CSS applied as inline styles; persistent sheets re-apply every second as panels appear |
| `xml_load` | compile XML (plus real CSS with `:hover`, `@define`, animations) and load it into a panel, in about 2 s |
| `layout_live_edit` / `layout_live_revert` | edit a game stylesheet; only the changed declarations are applied live |
| `panel_delete` | remove a panel |
| `compile_source` / `list_overrides` / `remove_override` | permanent overrides of game files, packed into the dev addon VPK (restart needed) |
| `export_mod` | packs your overrides, without the bridge, into a standalone `mods/<name>/pak01_dir.vpk` for `game/citadel/addons/` |
| `bridge_install` / `bridge_uninstall` / `addons_mount` | setup and teardown |

Pass `ui='hud' | 'dashboard' | 'overlay'` to pick a panel tree. The default `auto` uses the first one that's ready, in that order.

[`examples/stat_boosts_row`](examples/stat_boosts_row) is a complete HUD mod built this way: a JS prototype, the final CSS, and the build steps.

## Environment overrides

| variable | default |
|---|---|
| `DEADLOCK_DIR` | auto-detected from Steam libraries |
| `CSDK_DIR` | `tools/Reduced_CSDK_13` |
| `S2V_CLI` | `tools/s2v/Source2Viewer-CLI` |
| `DEADLOCK_PROTON` | Steam's *Proton - Experimental* (used to run the game) |
| `DEADLOCK_WINE` / `WINEPREFIX` | newest GE-Proton (else Proton) wine / `~/Games/umu/umu-default/pfx` (used for the compiler) |
| `DEADLOCK_RC_CMD` | custom compiler command template (`{rc}`, `{src_win}`) |
| `DEADLOCK_MOUNT` | `citadel/mcp_addons` (the dev addon) |
| `DEADLOCK_EXTRA_MOUNTS` | `citadel/addons` (comma-separated; mounted after the dev addon so installed mods load) |
| `DEADLOCK_MCP_CACHE` | `~/.cache/deadlock-mcp` (decompiled sources) |

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
* **The addon must be a VPK.** The engine searches every VPK before any loose directory, so overrides of game files go into `mcp_addons/pak01_dir.vpk`. Only brand-new paths (requests, live layouts) can be loose files.
* **Mounting.** The addon is mounted ahead of `citadel` in a marked block in `gameinfo.gi`, with explicit `Mod`/`Write citadel` keys so the game doesn't start writing into the addon dir.

## Known limits

* **Locale APIs crash the game.** `Intl`, `toLocale*()`, `localeCompare` and `normalize` hard-crash Deadlock, because its V8 has no ICU data. Every bridge and request context replaces them with stubs that throw a normal error. Code in your own compiled scripts is not protected.
* **Clearing `visibility: collapse` doesn't bring a panel back.** Once an inline `visibility: collapse` is applied, setting the style back to `null` leaves the panel hidden, so set `'visible'` explicitly. See `examples/stat_boosts_row/live_watcher.js`.
* **A layout's root merges into the panel you load it into.** That's why `xml_load` appends into a click-through full-size holder, so the layout's own root CSS (size, alignment) applies.
* **Reloading game layouts crashes the game.** Re-loading a game layout into its live C++ panel (`BLoadLayout` with override) causes an access violation. That's why `layout_live_edit` applies CSS diffs as inline styles by default, and why `unsafe_reload` exists only for plain `Panel`-rooted layouts.
* **Inline styles have limits.** They can't express pseudo-classes or animations, and they can't remove a declaration from the game's stylesheet. Use `compile_source` for those.
* **Bare-type selectors are slow.** A selector with no `#id`/`.class` (e.g. `Label`) walks the whole tree, which costs about 20–60 ms in the HUD (30k panels). Anchored selectors use native lookups (≈0 ms).
* **Map loads.** `new_player_basics` works. `hero_testing` crashes even unmodded, so `launch_game` refuses it.
* **The bridge needs the game running and not frozen.** Requests time out with a hint if the game is paused, still loading, or restarting.

## Development

`pytest`: the unit tests cover script generation, the CSS diff, VPK round trips, and an end-to-end mailbox test against a fake game. They don't need the game, so CI runs them. `scripts/smoke.py` and `scripts/exercise.py` exercise a real running game.
