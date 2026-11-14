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
    a = Assets(Config(deadlock_dir=tmp_path / "Deadlock", mount_path="citadel/mcp_addons"))
    assert not a.mount_status()
    a.set_mount(True)
    text = (gi_dir / "gameinfo.gi").read_text()
    assert text.index("Game\t\t\t\tcitadel/mcp_addons\n") < text.index("Game\t\t\t\tcitadel\n")
    assert (tmp_path / "Deadlock/game/citadel/mcp_addons").is_dir()
    assert "Write\t\t\t\tcitadel\n" in text and "Mod\t\t\t\tcitadel\n" in text
    assert a.mount_status()
    assert "already" in a.set_mount(True)
    a.set_mount(False)
    assert (gi_dir / "gameinfo.gi").read_text() == original
    assert "mcp_addons" in (gi_dir / "gameinfo.gi.mcp-bak").read_text()  # backup = state before last change


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
