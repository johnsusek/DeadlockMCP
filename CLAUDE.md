# deadlock-mcp

An MCP server for live-editing Deadlock's Panorama UI. Technical details: [TECH.md](TECH.md).

## Setting up for a user

When the user asks to set this up (or the `deadlock` MCP server's tools are missing because `.venv/` doesn't exist), do the steps below in order. Skip any step whose result already exists. Explain briefly what each step is for; the user may not be a developer.

1. **Check prerequisites.** Linux; Deadlock installed through Steam (look for `steamapps/common/Deadlock/game/citadel/gameinfo.gi` in the Steam libraries); `python3` ≥ 3.11; `umu-run`; `unzip`; `7z` (or `7za`). If one is missing, tell the user what to install for their distro and stop. Don't install system packages without asking.
2. **Python environment:** `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`. It must be an editable install: the server finds `tools/` and writes `mods/` relative to the checkout.
3. **Source 2 Viewer CLI** into `tools/s2v/` (decompiles game files):
   `mkdir -p tools && curl -L -o tools/s2v.zip https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/20.1/cli-linux-x64.zip && unzip tools/s2v.zip -d tools/s2v && rm tools/s2v.zip`
4. **CSDK 13 (Reduced)** into `tools/Reduced_CSDK_13/` (provides `resourcecompiler.exe`). It's a ~1.6 GB `.7z` (~5.4 GB unpacked) linked from https://deadlockmodding.pages.dev/modding-tools/csdk-13 (Google Drive, with a Mega mirror). Try downloading the Google Drive file with `.venv/bin/pip install gdown && .venv/bin/gdown 1bAM-S06aLAzX6mUOVdkhCoznj7SvwMZC -O tools/Reduced_CSDK_13.7z`. If that fails (quota, link changed), ask the user to download it from that page into `tools/` and tell you when it's there. Then `7z x tools/Reduced_CSDK_13.7z -otools/` and check that `tools/Reduced_CSDK_13/game/bin_server/win64/resourcecompiler.exe` exists. Delete the `.7z` only if the user agrees.
5. **Wine prefix for the compiler** (one time): `GAMEID=umu-default umu-run cmd /c exit`. It can take a minute the first time.
6. **Hand off.** Tell the user to run `/mcp` (or restart Claude Code) and approve the `deadlock` server. Once its tools are available, run `status` (`csdk_resourcecompiler` and `s2v_cli` must not say "missing"), then `bridge_install`, then `launch_game(map="new_player_basics")`, and check `status` again until `bridges_ready` lists the UIs. `status` lists anything that needs action under `warnings` (e.g. re-run `bridge_install` after a game update).

Steam must be running when the game launches, so the game can log in.

## Working in this repo

* Tests: `.venv/bin/pytest`. They don't need the game.
* Layout: `server.py` holds the tool definitions (every tool is `async` and wrapped by `@tool`, which turns `DeadlockError`, `OSError` and bad regexes into `ERROR: ...` replies); `game.py` launches/quits/screenshots the game; `env.py` restores desktop-session variables; `watchdog.py` unmounts the dev addon when the game exits.
* `tools/` and `mods/` are git-ignored and may contain decompiled game files; never commit them.
