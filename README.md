# deadlock-mcp

An [MCP](https://modelcontextprotocol.io) server for inspecting and live-editing Deadlock's Panorama UI (XML / CSS / JS) while the game runs. Point an MCP client such as Claude Code at it and you can ask things like "find the panel that shows my stat boosts and move it to bottom-center". The client inspects the live panel tree, tries CSS and JS in the running game, takes screenshots to check the result, and then compiles the change into a mod VPK.

> **Status: alpha, Linux only.** Tested on Bazzite (KDE Wayland) with Proton, Deadlock build #6,861 (Nov 2026).
> Not affiliated with or endorsed by Valve.

## What it can do

Ask your AI assistant to change the in-game UI in plain language. It can:

* **Find any part of the UI** while the game is running, and show you how it's built.
* **Try changes live**: move, resize, restyle or hide panels and see the result in game right away, without restarting.
* **Take screenshots** to check how a change looks.
* **Turn the result into a mod** you can drop into `game/citadel/addons/` and share.
* **Launch and quit the game** for you, straight into a sandbox match with the full HUD.

[`examples/stat_boosts_row`](examples/stat_boosts_row) is a complete HUD mod built this way. The full list of tools is in [TECH.md](TECH.md#tools).

## Requirements

* Linux, with Deadlock installed through Steam and running under Proton. Steam must be running so the game can log in.
* Python ≥ 3.11
* [`umu-run`](https://github.com/Open-Wine-Components/umu-launcher), which launches the game and provides the wine prefix the compiler runs in.
* [Source 2 Viewer CLI](https://github.com/ValveResourceFormat/ValveResourceFormat), which decompiles game files.
* [CSDK 13 (Reduced)](https://deadlockmodding.pages.dev/modding-tools/csdk-13), which provides `resourcecompiler.exe` and is run through Proton's wine.
* Optional: `xdotool` and ImageMagick (`import`, `magick`) for window screenshots.

## Setup

Ask your AI agent to clone this repo and they'll figure it out. :) 

The technical details are in [TECH.md](TECH.md#tools).

### Connect a client

**Claude Code**: the included `.mcp.json` registers the server when you open Claude Code in this directory.

**Other clients**:

```json
{ "mcpServers": { "deadlock": { "command": "/path/to/deadlock-mcp/.venv/bin/python", "args": ["-m", "deadlock_mcp"] } } }
```

Then:

Ask your agent to install the bridge and launch the game.

## Making a mod

1. **Set up and connect your agent** as described above, then ask it to launch the game. It boots straight into the sandbox with the full match HUD.
2. **Tell it what you want**, for example "move my stat boosts to bottom-center" or "make the ability cooldown numbers bigger". It finds the panels, tries the change live, and takes screenshots to check. Keep giving feedback until it looks right.
3. **Ask it to package the mod**, for example "make that permanent and export it as a mod called my_hud". You get `mods/my_hud/pak01_dir.vpk`. Copy it into `game/citadel/addons/` like any other Deadlock mod, renaming it to `pakNN_dir.vpk` if you already have other mods there.

The exported VPK is a normal mod: it doesn't include the bridge and doesn't need this tool to run.

### Playing online

The game sessions your agent launches are for building mods, so play your matches from a normal Steam launch. The dev addon (the bridge your agent talks to) is only mounted while an agent-launched game is running: when that game exits, it's removed from `gameinfo.gi` automatically. Your mods folder (`citadel/addons`) stays mounted, so the mods you install keep working in a normal launch.

## License

MIT. Deadlock and its assets belong to Valve. 
