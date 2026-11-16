from pathlib import Path

import pytest

from deadlock_mcp.config import DEADLOCK_APPID, Config


@pytest.fixture
def steam(tmp_path, monkeypatch):
    """Steam client in ~/.local/share/Steam, Deadlock in a secondary library, Proton in the main one."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("DEADLOCK_DIR", "DEADLOCK_PROTON", "STEAM_COMPAT_CLIENT_INSTALL_PATH", "CSDK_DIR", "CSDK_CONTENT",
                "CSDK_GAME", "DEADLOCK_ADDON"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / ".local/share/Steam"
    lib2 = tmp_path / "games/SteamLibrary"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n "0"\n {{\n  "path" "{root}"\n }}\n "1"\n {{\n  "path" "{lib2}"\n }}\n}}\n')
    gi = lib2 / "steamapps/common/Deadlock/game/citadel/gameinfo.gi"
    gi.parent.mkdir(parents=True)
    gi.write_text("")
    proton = root / "steamapps/common/Proton - Experimental"
    proton.mkdir(parents=True)
    (proton / "proton").write_text("")
    return root, lib2, proton


def test_secondary_library_paths(steam):
    root, lib2, proton = steam
    cfg = Config()
    assert cfg.deadlock_dir == lib2 / "steamapps/common/Deadlock"
    assert cfg.steam_root == root
    assert cfg.compat_data_dir == lib2 / "steamapps/compatdata" / str(DEADLOCK_APPID)
    assert cfg.proton_dir == proton


def test_proton_prefers_game_library_and_env_overrides(steam, monkeypatch, tmp_path):
    root, lib2, _ = steam
    local = lib2 / "steamapps/common/Proton - Experimental"
    local.mkdir()
    (local / "proton").write_text("")
    assert Config().proton_dir == local
    monkeypatch.setenv("DEADLOCK_PROTON", str(tmp_path / "myproton"))
    monkeypatch.setenv("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(tmp_path / "client"))
    cfg = Config()
    assert cfg.proton_dir == tmp_path / "myproton" and cfg.steam_root == tmp_path / "client"


def test_no_steam(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("STEAM_COMPAT_CLIENT_INSTALL_PATH", raising=False)
    monkeypatch.delenv("DEADLOCK_PROTON", raising=False)
    cfg = Config(deadlock_dir=None)
    assert cfg.steam_root is None and cfg.compat_data_dir is None and cfg.proton_dir is None


def test_csdk_dirs(steam, monkeypatch, tmp_path):
    cfg = Config(csdk_dir=Path("/csdk"), addon_name="x")
    assert cfg.csdk_content_dir == Path("/csdk/content/citadel_addons/x")
    assert cfg.csdk_game_dir == Path("/csdk/game/citadel_addons/x")
    monkeypatch.setenv("CSDK_GAME", str(tmp_path / "out"))
    assert Config(csdk_dir=Path("/csdk")).csdk_game_dir == tmp_path / "out"
    assert Config(csdk_dir=None, csdk_content_dir=None).csdk_content_dir is None


def test_data_dir_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("DEADLOCK_MCP_DATA", "XDG_DATA_HOME", "CSDK_DIR", "S2V_CLI", "WINEPREFIX"):
        monkeypatch.delenv(var, raising=False)
    data = tmp_path / ".local/share/deadlock-mcp"
    cfg = Config()
    assert cfg.data_dir == data and cfg.mods_dir == data / "mods" and cfg.wineprefix == data / "wine"
    assert cfg.csdk_dir == data / "tools/Reduced_CSDK_13" and cfg.s2v_cli == data / "tools/s2v/Source2Viewer-CLI"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert Config().data_dir == tmp_path / "xdg/deadlock-mcp"
    monkeypatch.setenv("DEADLOCK_MCP_DATA", str(tmp_path / "mine"))
    assert Config().csdk_dir == tmp_path / "mine/tools/Reduced_CSDK_13"


def test_snap_steam_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("STEAM_COMPAT_CLIENT_INSTALL_PATH", raising=False)
    root = tmp_path / "snap/steam/common/.local/share/Steam"
    (root / "steamapps").mkdir(parents=True)
    assert Config(deadlock_dir=None).steam_root == root
