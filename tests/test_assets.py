import pytest

from deadlock_mcp.assets import Assets
from deadlock_mcp.config import Config


def test_normalize():
    n = Assets.normalize
    assert n("layout/base_hud") == "panorama/layout/base_hud.vxml_c"
    assert n("panorama/styles/hud.css") == "panorama/styles/hud.vcss_c"
    assert n("/panorama/layout/Foo.vxml_c") == "panorama/layout/foo.vxml_c"


def test_mount_roundtrip(tmp_path):
    gi_dir = tmp_path / "Deadlock/game/citadel"
    gi_dir.mkdir(parents=True)
    original = "\t\tSearchPaths\n\t\t{\n\t\t\tGame\t\t\t\tcitadel\n\t\t\tGame\t\t\t\tcore\n\t\t}\n"
    (gi_dir / "gameinfo.gi").write_text(original)
    a = Assets(Config(deadlock_dir=tmp_path / "Deadlock", mount_path="citadel/mcp_addons",
                      extra_mounts=["citadel/addons"]))
    assert not a.mount_status()
    a.set_mount(True)
    text = (gi_dir / "gameinfo.gi").read_text()
    assert text.index("Game\t\t\t\tcitadel/mcp_addons\n") < text.index("Game\t\t\t\tcitadel\n")
    assert (tmp_path / "Deadlock/game/citadel/mcp_addons").is_dir()
    assert "Write\t\t\t\tcitadel\n" in text and "Mod\t\t\t\tcitadel\n" in text
    assert a.mount_status()
    assert "already" in a.set_mount(True)
    a.set_mount(False)
    text = (gi_dir / "gameinfo.gi").read_text()
    assert "mcp_addons" not in text and not a.mount_status()
    # the mod folder stays mounted (ahead of citadel) so installed mods still load
    assert text.index("Game\t\t\t\tcitadel/addons\n") < text.index("Game\t\t\t\tcitadel\n")
    assert "Write\t\t\t\tcitadel\n" in text
    assert "already" in a.set_mount(False)
    assert "mcp_addons" in (gi_dir / "gameinfo.gi.mcp-bak").read_text()  # backup = state before last change
    a.set_mount(True)
    assert a.mount_status() and (gi_dir / "gameinfo.gi").read_text().count("citadel/addons") == 1


def test_unmount_without_extra_mounts_restores_original(tmp_path):
    gi_dir = tmp_path / "Deadlock/game/citadel"
    gi_dir.mkdir(parents=True)
    original = "\t\tSearchPaths\n\t\t{\n\t\t\tGame\t\t\t\tcitadel\n\t\t\tGame\t\t\t\tcore\n\t\t}\n"
    (gi_dir / "gameinfo.gi").write_text(original)
    a = Assets(Config(deadlock_dir=tmp_path / "Deadlock", mount_path="citadel/mcp_addons", extra_mounts=[]))
    a.set_mount(True)
    a.set_mount(False)
    assert (gi_dir / "gameinfo.gi").read_text() == original


def test_real_vpk_lists_panorama():
    cfg = Config()
    if not cfg.pak_dir_vpk or not cfg.pak_dir_vpk.is_file():
        pytest.skip("Deadlock not installed")
    a = Assets(cfg)
    files = a.list_panorama("base_hud_and_db_overlay")
    assert "panorama/layout/base_hud_and_db_overlay.vxml_c" in files
    data = a.vpk.read("panorama/layout/base_hud_and_db_overlay.vxml_c")
    assert len(data) > 16


def test_vpk_roundtrip(tmp_path):
    from deadlock_mcp.vpk import VPK, write_vpk

    files = {"panorama/layout/a.vxml_c": b"AAA", "panorama/scripts/b.vjs_c": b"B" * 1000, "root.txt": b"r", "noext": b"x"}
    write_vpk(tmp_path / "pak01_dir.vpk", files)
    v = VPK(tmp_path / "pak01_dir.vpk")
    assert sorted(v.names()) == sorted(files)
    for k, val in files.items():
        assert v.read(k) == val


def _mounted_install(tmp_path):
    gi_dir = tmp_path / "Deadlock/game/citadel"
    gi_dir.mkdir(parents=True)
    (gi_dir / "gameinfo.gi").write_text("\t\tSearchPaths\n\t\t{\n\t\t\tGame\t\t\t\tcitadel\n\t\t}\n")
    cfg = Config(deadlock_dir=tmp_path / "Deadlock", mount_path="citadel/mcp_addons",
                 extra_mounts=["citadel/addons"], cache_dir=tmp_path / "cache")
    a = Assets(cfg)
    a.set_mount(True)
    cfg.cache_dir.mkdir()
    return cfg, a


def test_watchdog_unmounts_when_game_exits(tmp_path):
    from deadlock_mcp import watchdog

    cfg, a = _mounted_install(tmp_path)
    watchdog.token_file(cfg).write_text("abc")
    states = iter([True, True, False])  # game seen running, still running, then exits
    msg = watchdog.watch("abc", cfg, poll=0, running=lambda: next(states))
    assert "unmounted" in msg and not a.mount_status()
    assert not watchdog.token_file(cfg).exists()


def test_watchdog_leaves_newer_launch_alone(tmp_path):
    from deadlock_mcp import watchdog

    cfg, a = _mounted_install(tmp_path)
    watchdog.token_file(cfg).write_text("newer")
    msg = watchdog.watch("abc", cfg, poll=0, startup_timeout=0, running=lambda: False)
    assert "superseded" in msg and a.mount_status()
