"""Paths and settings, resolved from environment variables with sensible autodetection."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEADLOCK_APPID = 1422450


def data_home() -> Path:
    """Where setup installs the toolchain and export_mod writes mods (env DEADLOCK_MCP_DATA)."""
    env = os.environ.get("DEADLOCK_MCP_DATA")
    if env:
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser() / "deadlock-mcp"


def _steam_roots() -> list[Path]:
    """Where the Steam client itself may be installed (native, legacy symlink, Flatpak, Snap)."""
    return [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        Path.home() / "snap/steam/common/.local/share/Steam",
    ]


def _steam_libraries() -> list[Path]:
    libs: list[Path] = []
    for root in _steam_roots():
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                libs.append(Path(m.group(1)))
        libs.append(root)
    seen, out = set(), []
    for lib in libs:
        key = str(lib.resolve()) if lib.exists() else str(lib)
        if key not in seen:
            seen.add(key)
            out.append(lib)
    return out


def find_deadlock_dir() -> Path | None:
    env = os.environ.get("DEADLOCK_DIR")
    if env:
        return Path(env).expanduser()
    for lib in _steam_libraries():
        cand = lib / "steamapps/common/Deadlock"
        if (cand / "game/citadel/gameinfo.gi").is_file():
            return cand
    return None


def find_proton_wine() -> Path | None:
    """Pick a Proton build's wine binary (GE-Proton preferred, newest first)."""
    cands: list[Path] = []
    for lib in _steam_libraries():
        for sub in ("compatibilitytools.d", "steamapps/common"):
            d = lib / sub
            if d.is_dir():
                cands += [p / "files/bin/wine" for p in d.iterdir() if "proton" in p.name.lower()]
    cands = [c for c in cands if c.is_file()]
    cands.sort(key=lambda c: ("ge-proton" in str(c).lower(), c.stat().st_mtime), reverse=True)
    return cands[0] if cands else None


def _env_path(name: str, default: Path | None) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


@dataclass
class Config:
    deadlock_dir: Path | None = field(default_factory=find_deadlock_dir)
    data_dir: Path = field(default_factory=data_home)
    s2v_cli: Path | None = field(
        default_factory=lambda: _env_path("S2V_CLI", data_home() / "tools/s2v/Source2Viewer-CLI")
    )
    csdk_dir: Path | None = field(
        default_factory=lambda: _env_path("CSDK_DIR", data_home() / "tools/Reduced_CSDK_13")
    )
    # Optional command template overriding how resourcecompiler is run. Placeholders: {rc} {src_win}.
    # Default: Proton's wine binary directly (fast, keeps stdout), in the prefix setup creates.
    rc_cmd: str | None = field(default_factory=lambda: os.environ.get("DEADLOCK_RC_CMD") or None)
    wine: Path | None = field(default_factory=lambda: _env_path("DEADLOCK_WINE", find_proton_wine()))
    wineprefix: Path = field(
        default_factory=lambda: _env_path("WINEPREFIX", data_home() / "wine")  # type: ignore[arg-type]
    )
    # Search path (relative to game/) mounted ahead of `citadel` in gameinfo.gi; compiled files go here.
    mount_path: str = field(default_factory=lambda: os.environ.get("DEADLOCK_MOUNT", "citadel/mcp_addons"))
    # Additional search paths mounted after ours (e.g. the standard mod folder where installed mod VPKs live).
    extra_mounts: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.environ.get("DEADLOCK_EXTRA_MOUNTS", "citadel/addons").split(",") if m.strip()])
    addon_name: str = field(default_factory=lambda: os.environ.get("DEADLOCK_ADDON", "mcp_bridge"))
    cache_dir: Path = field(
        default_factory=lambda: _env_path("DEADLOCK_MCP_CACHE", Path.home() / ".cache/deadlock-mcp")  # type: ignore[arg-type]
    )
    # CSDK addon trees: sources are written to csdk_content_dir, resourcecompiler writes to csdk_game_dir.
    # Default (None): <csdk_dir>/{content,game}/citadel_addons/<addon_name>.
    csdk_content_dir: Path | None = field(default_factory=lambda: _env_path("CSDK_CONTENT", None))
    csdk_game_dir: Path | None = field(default_factory=lambda: _env_path("CSDK_GAME", None))

    def __post_init__(self) -> None:
        if self.csdk_dir:
            if self.csdk_content_dir is None:
                self.csdk_content_dir = self.csdk_dir / "content/citadel_addons" / self.addon_name
            if self.csdk_game_dir is None:
                self.csdk_game_dir = self.csdk_dir / "game/citadel_addons" / self.addon_name

    # --- derived paths -------------------------------------------------
    @property
    def mods_dir(self) -> Path: return self.data_dir / "mods"

    @property
    def citadel_dir(self) -> Path | None:
        return self.deadlock_dir / "game/citadel" if self.deadlock_dir else None

    @property
    def pak_dir_vpk(self) -> Path | None:
        return self.citadel_dir / "pak01_dir.vpk" if self.citadel_dir else None

    @property
    def gameinfo(self) -> Path | None:
        return self.citadel_dir / "gameinfo.gi" if self.citadel_dir else None

    @property
    def addons_dir(self) -> Path | None:
        return self.deadlock_dir / "game" / self.mount_path if self.deadlock_dir else None

    @property
    def game_library(self) -> Path | None:
        """The Steam library holding Deadlock (<lib>/steamapps/common/Deadlock), if it is laid out like one."""
        if self.deadlock_dir and self.deadlock_dir.parent.parent.name == "steamapps":
            return self.deadlock_dir.parent.parent.parent
        return None

    @property
    def steam_root(self) -> Path | None:
        """The Steam client install (env STEAM_COMPAT_CLIENT_INSTALL_PATH). Not necessarily Deadlock's
        library: the game may live in a secondary library."""
        env = os.environ.get("STEAM_COMPAT_CLIENT_INSTALL_PATH")
        if env:
            return Path(env).expanduser()
        return next((r for r in _steam_roots() if (r / "steamapps").is_dir()), None)

    @property
    def compat_data_dir(self) -> Path | None:
        """Deadlock's Proton compat data; Steam keeps it in the game's own library."""
        lib = self.game_library or self.steam_root
        return lib / "steamapps/compatdata" / str(DEADLOCK_APPID) if lib else None

    @property
    def proton_dir(self) -> Path | None:
        """Proton used to run the game directly (env DEADLOCK_PROTON, else Steam's Proton - Experimental,
        looked up in Deadlock's library first, then every other library)."""
        env = os.environ.get("DEADLOCK_PROTON")
        if env:
            return Path(env).expanduser()
        libs = _steam_libraries()
        if self.game_library:
            libs.insert(0, self.game_library)
        for lib in libs:
            p = lib / "steamapps/common/Proton - Experimental"
            if (p / "proton").is_file():
                return p
        return None

    @property
    def resourcecompiler(self) -> Path | None:
        if not self.csdk_dir:
            return None
        for rel in ("game/bin_server/win64/resourcecompiler.exe", "game/bin/win64/resourcecompiler.exe"):
            p = self.csdk_dir / rel
            if p.is_file():
                return p
        return self.csdk_dir / "game/bin_server/win64/resourcecompiler.exe"
