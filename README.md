<p align="center"><img src="https://raw.githubusercontent.com/johnsusek/DeadlockMCP/main/logo.svg" alt="deadlock-mcp logo" width="160"></p>
<!-- mcp-name: io.github.johnsusek/deadlock-mcp -->

# DeadlockMCP

An [MCP](https://modelcontextprotocol.io) server for building Deadlock UI mods.

Live-edit Deadlock's Panorama UI (XML/CSS/JS) while the game runs.

Connect it to an MCP client with an agent and ask for things like "find the panel that shows my stat boosts and move it to bottom-center".

The server installs an addon that uses the dev tools to update the UI. The agent inspects the live panel tree, tries CSS and JS in the running game, takes screenshots to check the result, and then compiles the change into a mod VPK.

> ⚠️ **Status: Alpha.** Tested only on Bazzite (KDE Wayland) with Proton, Deadlock build #6,861 (Nov 2026). Other Linux setups may work but are untested. Windows and macOS aren't supported yet; help supporting other platforms is welcome.

## Getting started

You need Linux with Deadlock installed through Steam (running under Proton) and [uv](https://docs.astral.sh/uv/). Steam must be running when the game launches, so the game can log in.

1. **Add the server to your MCP client** (stdio). It must run on the same Linux desktop session as the game:
   ```json
   {
     "mcpServers": {
       "deadlock": {
         "command": "uvx",
         "args": ["deadlock-mcp"]
       }
     }
   }
   ```
   With Claude Code: `claude mcp add deadlock -- uvx deadlock-mcp`. Paths are auto-detected; [TECH.md](TECH.md#environment-overrides) lists the environment variables to override them.
2. **Ask your agent to set it up.** It runs `setup`, which downloads [Source 2 Viewer CLI](https://github.com/ValveResourceFormat/ValveResourceFormat) and [CSDK 13](https://deadlockmodding.pages.dev/modding-tools/csdk-13) (about 1.6 GB) and creates a Wine prefix for the compiler. If the CSDK download fails, it asks you to download the `.7z` into `~/Downloads` yourself. `setup` also lists any missing system tools (`7z`, `xdotool`, ImageMagick, `umu-run`) with the install command for your distro.
3. **Ask it to start the game**: it runs `bridge_install`, then `launch_game(map="new_player_basics")`. The game boots straight into a sandbox match with the full HUD, ready for changes.

## Making a mod

1. **Get set up** as described above, with the game running in the sandbox.
2. **Tell your agent what you want**, for example "move my stat boosts to bottom-center" or "make the ability cooldown numbers bigger". It finds the panels, tries the change live, and takes screenshots to check. Keep giving feedback until it looks right.
3. **Ask it to package the mod**, for example "make that permanent and install it as a mod called my_hud". You get `~/.local/share/deadlock-mcp/mods/my_hud/pak01_dir.vpk`, and with install it's also copied into `game/citadel/addons/` under the next free `pakNN_dir.vpk` name.

The exported VPK is a normal mod: it doesn't include the bridge and doesn't need this tool to run.

## Tools

Most tools take `ui='hud' | 'dashboard' | 'overlay'` to pick a panel tree; the default `auto` uses the first one that's ready.

**Game**

| tool | description |
|---|---|
| `status` | Reports paths, game and bridge state, and anything that needs fixing, and pings each in-game bridge. |
| `launch_game` | Launches Deadlock with the dev addon mounted, optionally straight into the sandbox map with the full HUD. |
| `quit_game` | Closes Deadlock. |
| `console_log` | Returns recent game console output, optionally regex-filtered. |
| `screenshot` | Captures the game window (or the whole screen) as an image, with an optional crop. |

**Game files**

| tool | description |
|---|---|
| `ui_files` | Lists the Panorama files shipped in the game, with a regex filter. |
| `ui_source` | Decompiles a shipped Panorama layout, stylesheet or script to source. |

**Live editing**

| tool | description |
|---|---|
| `panel_tree` | Dumps the live panel hierarchy (id, type, layout, size, position, text) under a panel. |
| `panel_find` | Finds live panels with a CSS-like selector. |
| `js_eval` | Runs JavaScript in the game's UI and returns the result. |
| `css_apply` | Applies CSS to live panels as inline styles, re-applying it as panels are recreated. |
| `css_clear` | Removes one live CSS sheet, or all of them. |
| `xml_load` | Compiles Panorama XML (and optional CSS) and loads it into the live UI. |
| `layout_live_edit` | Live-edits an existing game layout's stylesheet, applying only the changed declarations. |
| `layout_live_revert` | Reloads the original layout into panels changed by `layout_live_edit`. |
| `panel_delete` | Deletes a live panel by id. |

**Building and exporting**

| tool | description |
|---|---|
| `compile_source` | Compiles a Panorama file into the dev addon as a permanent override (applies after a restart). |
| `list_overrides` | Lists the compiled files in the dev addon. |
| `remove_override` | Removes a compiled override from the dev addon. |
| `export_mod` | Exports the overrides, without the bridge, as a standalone mod VPK, and optionally installs it into the game. |

**Setup**

| tool | description |
|---|---|
| `setup` | Installs Source 2 Viewer CLI, CSDK 13 and the compiler's Wine prefix, and lists missing system tools. |
| `bridge_install` | Builds the in-game bridges and packs them into the dev addon; re-run after a game update. |
| `bridge_uninstall` | Removes the bridge files from the dev addon. |
| `addons_mount` | Adds or removes the dev addon's search path in `gameinfo.gi`. |

Parameters and implementation details: [TECH.md](TECH.md#tools).

## What it installs

`setup` puts everything under `~/.local/share/deadlock-mcp/` (`$DEADLOCK_MCP_DATA` to move it):

* `tools/s2v/`: [Source 2 Viewer CLI](https://github.com/ValveResourceFormat/ValveResourceFormat), to decompile game files.
* `tools/Reduced_CSDK_13/`: [CSDK 13](https://deadlockmodding.pages.dev/modding-tools/csdk-13), whose `resourcecompiler.exe` compiles Panorama files (about 5.4 GB).
* `wine/`: a Wine prefix made with Proton's wine, to run the compiler.
* `mods/`: exported mods.

It doesn't install system packages.

## What it modifies

* **`game/citadel/gameinfo.gi`**: `launch_game` adds a marked block that mounts the dev addon (and `citadel/addons`, so installed mods still load), saving the previous file as `gameinfo.gi.mcp-bak`. The addon is unmounted automatically when that game exits; if the game crashed or the PC rebooted, `status` warns and `addons_mount(False)` removes it.
* **`game/citadel/mcp_addons/`**: the dev addon, holding the bridge, your compiled overrides, and the request files the server talks to the game through.
* **`game/citadel/console.log`**: the game writes it (`-condebug`) and the server deletes it on each launch.
* **`game/citadel/addons/`**: `export_mod(install=True)` copies the mod there.
* **`~/.local/share/deadlock-mcp/tools/Reduced_CSDK_13/{content,game}/citadel_addons/mcp_bridge/`**: sources and compiler output.
* **`~/.cache/deadlock-mcp/`**: decompiled sources, launch and watcher logs.

No shipped game files are changed. Agent-launched games are for building mods: play your matches from a normal Steam launch, which doesn't load the bridge.

## License

MIT. Deadlock and its assets belong to Valve.
