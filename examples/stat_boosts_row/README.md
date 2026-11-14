# Example: stat boosts row

A HUD mod made with this server. It moves the active stat-boost indicators (the small stats above the
Weapon / Spirit / Vitality bars) to a gap-free row at bottom-center, showing only active boosts.
Holding TAB (or opening the shop / detail view) restores the game's original three-column list.

* `stat_boosts_row.css`: the mod's stylesheet. It is driven by the classes the HUD already sets
  (`gScoreboardOpen`, `gDetailView`, `gShopOpen`, `isPositive`, …), so it needs no script.
* `live_watcher.js`: the JS-only prototype it started as. Run it with
  `js_eval(<file contents>, ui="hud", keep_context=True)` while in a match to try the layout without compiling anything.

## Building it

The game layout is not included here because it's Valve's file. To rebuild:

1. `ui_source("panorama/layout/citadel_hud_active_player_stats.xml")` returns the decompiled layout.
2. Add one line at the end of its `<styles>` block:
   `<include src="file://{resources}/styles/mods/stat_boosts_row.css" />`
3. `compile_source("panorama/styles/mods/stat_boosts_row.css", <css>)`, then
   `compile_source("panorama/layout/citadel_hud_active_player_stats.xml", <edited xml>)`.
4. `export_mod("stat_boosts_row")` writes `mods/stat_boosts_row/pak01_dir.vpk`. Copy that file into
   `game/citadel/addons/` (rename it to `pakNN_dir.vpk` if you have other mods).

Every game update that changes this layout means repeating step 1–4.
