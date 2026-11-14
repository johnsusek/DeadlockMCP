# deadlock-mcp

An [MCP](https://modelcontextprotocol.io) server for building Deadlock UI mods.

Live-edit Deadlock's Panorama UI (XML/CSS/JS) while the game runs. 

Point an agent such as Claude Code and do things like: "find the panel that shows my stat boosts and move it to bottom-center". 

The client inspects the live panel tree, tries CSS and JS in the running game, takes screenshots to check the result, and then compiles the change into a mod VPK.

> ⚠️ **Status: Alpha, Linux only.** Tested on Bazzite (KDE Wayland) with Proton, Deadlock build #6,861 (Nov 2026).

## Getting started

You need Linux with Deadlock installed through Steam (running under Proton), and [Claude Code](https://claude.com/claude-code).

1. **Clone this repo** and open Claude Code in it:
   ```bash
   git clone https://github.com/johnsusek/DeadlockMCP.git
   cd DeadlockMCP
   claude
   ```
2. **Tell it to set things up**, e.g. "set this up". It downloads and installs the modding tools it needs (about 2 GB to download, 6 GB on disk) and tells you if anything is missing on your system.
3. **Connect to the MCP server**: run `/mcp` (or restart Claude Code) and approve the `deadlock` server when asked.
4. **Ask it to launch the game.** It boots straight into a sandbox match with the full HUD, ready for changes.

Using a different MCP client, or want to set things up by hand? See [TECH.md](TECH.md).

## Making a mod

1. **Get set up** as described above, with the game running in the sandbox.
2. **Tell it what you want**, for example "move my stat boosts to bottom-center" or "make the ability cooldown numbers bigger". It finds the panels, tries the change live, and takes screenshots to check. Keep giving feedback until it looks right.
3. **Ask it to package the mod**, for example "make that permanent and export it as a mod called my_hud". You get `mods/my_hud/pak01_dir.vpk`. Copy it into `game/citadel/addons/` like any other Deadlock mod, renaming it to `pakNN_dir.vpk` if you already have other mods there.

The exported VPK is a normal mod: it doesn't include the bridge and doesn't need this tool to run.

### Playing online

The game sessions your agent launches are for building mods, so play your matches from a normal Steam launch. The dev addon (the bridge your agent talks to) is only mounted while an agent-launched game is running: when that game exits, it's removed from `gameinfo.gi` automatically.

## License

MIT. Deadlock and its assets belong to Valve. 
