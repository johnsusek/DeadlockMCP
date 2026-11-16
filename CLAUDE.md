# deadlock-mcp

An MCP server for live-editing Deadlock's Panorama UI. Technical details: [TECH.md](TECH.md).

## Setting up for a user

Users install from PyPI (`uvx deadlock-mcp`, see the README). When someone runs this checkout instead (the `deadlock` server from `.mcp.json` is missing because `.venv/` doesn't exist):

1. **Python environment:** `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
2. **Hand off.** Tell the user to run `/mcp` (or restart Claude Code) and approve the `deadlock` server.
3. **Setup tool.** Run `setup` and repeat it until every step says done; relay any step that needs the user (the manual CSDK download, missing system tools: don't install system packages without asking). Then `bridge_install`, `launch_game(map="new_player_basics")`, and `status` until `bridges_ready` lists the UIs. `status` lists anything that needs action under `warnings` (e.g. re-run `bridge_install` after a game update).

Steam must be running when the game launches, so the game can log in.

## Working in this repo

* Tests: `.venv/bin/pytest`. They don't need the game.
* Layout: `server.py` holds the tool definitions (every tool is `async` and wrapped by `@tool`, which turns `DeadlockError`, `OSError` and bad regexes into `ERROR: ...` replies); `game.py` launches/quits/screenshots the game; `env.py` restores desktop-session variables; `watchdog.py` unmounts the dev addon when the game exits; `toolchain.py` is the `setup` tool and the system-tool install hints.
* `tools/` and `mods/` are git-ignored and may contain decompiled game files; never commit them.
* Releasing: bump `__version__` and both versions in `server.json`, then push a `vX.Y.Z` tag; `release.yml` publishes to PyPI and the MCP Registry.
