import asyncio
import os
import sys
import zlib

import pytest

from deadlock_mcp.assets import AssetError, Assets
from deadlock_mcp.config import Config
from deadlock_mcp.vpk import write_vpk


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
                      extra_mounts=["citadel/addons"], cache_dir=tmp_path / "cache"))
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
    a = Assets(Config(deadlock_dir=tmp_path / "Deadlock", mount_path="citadel/mcp_addons", extra_mounts=[],
                      cache_dir=tmp_path / "cache"))
    a.set_mount(True)
    a.set_mount(False)
    assert (gi_dir / "gameinfo.gi").read_text() == original


@pytest.mark.parametrize("extra", [[], ["citadel/addons"]])
def test_mount_roundtrip_crlf_byte_exact(tmp_path, extra):
    gi_dir = tmp_path / "Deadlock/game/citadel"
    gi_dir.mkdir(parents=True)
    original = b"\t\tSearchPaths\r\n\t\t{\r\n\t\t\tGame\t\t\t\tcitadel\r\n\t\t\tGame\t\t\t\tcore\r\n\t\t}\r\n// \xe9\xff\r\n"
    gi = gi_dir / "gameinfo.gi"
    gi.write_bytes(original)
    gi.chmod(0o640)
    a = Assets(Config(deadlock_dir=tmp_path / "Deadlock", mount_path="citadel/mcp_addons", extra_mounts=extra,
                      cache_dir=tmp_path / "cache"))
    assert "mounted" in a.set_mount(True) and a.mount_status()
    data = gi.read_bytes()
    assert b"Game\t\t\t\tcitadel/mcp_addons\r\n" in data and data.count(b"\n") == data.count(b"\r\n")
    assert data.endswith(b"// \xe9\xff\r\n")  # undecodable bytes survive
    assert (gi.stat().st_mode & 0o777) == 0o640
    assert sorted(p.name for p in gi_dir.iterdir()) == ["gameinfo.gi", "gameinfo.gi.mcp-bak", "mcp_addons"]  # no temp left
    a.set_mount(False)
    if not extra:
        assert gi.read_bytes() == original


def test_set_mount_only_if(tmp_path):
    cfg, a = _mounted_install(tmp_path)
    before = cfg.gameinfo.read_bytes()
    msg = a.set_mount(False, only_if=lambda: False)
    assert "skipped" in msg and "unmounted" not in msg and cfg.gameinfo.read_bytes() == before
    assert "unmounted" in a.set_mount(False, only_if=lambda: True) and not a.mount_status()


def test_safe_paths(tmp_path):
    n = Assets.normalize
    assert n("panorama\\layout\\Foo.xml") == "panorama/layout/foo.vxml_c"
    assert n("Panorama/styles/a.css") == "panorama/styles/a.vcss_c"
    for bad in ["panorama/../../../../etc/x.css", "layout/../x.xml", "panorama//x.css", "C:/x.css",
                "panorama/./x.css", "..\\..\\x.css", "panorama/"]:
        with pytest.raises(AssetError):
            n(bad)
    cfg, a = _toolchain(tmp_path)
    escape = "panorama/../../../../../escaped.css"
    with pytest.raises(AssetError):
        asyncio.run(a.compile_many({escape: "x"}, install=False))
    with pytest.raises(AssetError):
        a.install_loose([escape])
    victim = cfg.csdk_game_dir.parent / "victim.vcss_c"
    victim.parent.mkdir(parents=True)
    victim.write_text("keep")
    with pytest.raises(AssetError):
        a.uninstall("panorama/../../victim.vcss_c")
    assert victim.exists() and not list(tmp_path.rglob("escaped*"))


# ---------------------------------------------------------------- fakes for the toolchain

FAKE_S2V = """#!/bin/sh
# Source2Viewer-CLI stand-in: -i <compiled> -o <out> -d ; "decompiles" by copying, counts runs
echo run >> "$(dirname "$0")/runs"
mkdir -p "$4" && cat "$2" > "$4/out.txt"
"""

FAKE_RC = """import pathlib, sys
# resourcecompiler stand-in: compiles the given source, and with --deps every other source in its tree
ext = {".xml": ".vxml_c", ".css": ".vcss_c", ".js": ".vjs_c"}
src = pathlib.Path(sys.argv[-1])
content = pathlib.Path(str(src).split("/panorama/")[0])
game = pathlib.Path(str(content).replace("/content/", "/game/"))
for s in (content.rglob("*.*") if "--deps" in sys.argv else [src]):
    out = game / s.relative_to(content).with_suffix(ext[s.suffix])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("compiled " + s.read_text())
print("1 compiled, 0 failed")
"""


def _game(tmp_path, files):
    citadel = tmp_path / "Deadlock/game/citadel"
    citadel.mkdir(parents=True, exist_ok=True)
    write_vpk(citadel / "pak01_dir.vpk", files)
    return citadel / "pak01_dir.vpk"


def _toolchain(tmp_path, deps=False):
    _game(tmp_path, {})
    s2v = tmp_path / "s2v/Source2Viewer-CLI"
    s2v.parent.mkdir()
    s2v.write_text(FAKE_S2V)
    s2v.chmod(0o755)
    csdk = tmp_path / "csdk"
    rc = csdk / "game/bin_server/win64/resourcecompiler.exe"
    rc.parent.mkdir(parents=True)
    rc.write_text("")
    (tmp_path / "fake_rc.py").write_text(FAKE_RC)
    cfg = Config(deadlock_dir=tmp_path / "Deadlock", cache_dir=tmp_path / "cache", s2v_cli=s2v, csdk_dir=csdk,
                 wine=None, rc_cmd=f"{sys.executable} {tmp_path / 'fake_rc.py'} {'--deps' if deps else ''} {{src}}")
    return cfg, Assets(cfg)


def test_decompile_cache_follows_shipped_crc(tmp_path):
    cfg, a = _toolchain(tmp_path)
    pak = _game(tmp_path, {"panorama/layout/a.vxml_c": b"v1", "panorama/layout/b.vxml_c": b"bb"})
    runs = tmp_path / "s2v/runs"
    # a cache file in the old, un-keyed format is stale by definition
    old = cfg.cache_dir / "decompiled/panorama/layout/a.xml"
    old.parent.mkdir(parents=True)
    old.write_text("from an older game build")
    assert asyncio.run(a.decompile("layout/a")) == "v1"
    assert asyncio.run(a.decompile("layout/a")) == "v1" and len(runs.read_text().split()) == 1  # cached
    assert a.shipped_crc("panorama/layout/a.xml") == zlib.crc32(b"v1")
    assert not old.exists()

    # game update: same size, new content; the server keeps running with the old VPK loaded
    first = a.vpk
    write_vpk(pak, {"panorama/layout/a.vxml_c": b"v2", "panorama/layout/b.vxml_c": b"bb"})
    st = pak.stat()
    os.utime(pak, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert a.vpk is not first
    assert asyncio.run(a.decompile("layout/a")) == "v2"
    assert a.shipped_crc("layout/a") == zlib.crc32(b"v2")
    assert [p.name for p in old.parent.iterdir()] == [f"a.{zlib.crc32(b'v2'):08x}.xml"]
    with pytest.raises(AssetError):
        a.shipped_crc("layout/missing")


def test_compile_many_checks_dependencies(tmp_path):
    files = {"panorama/layout/mcp_live/x.xml": "<root/>", "panorama/styles/mcp_live/x.css": ".a{}"}
    cfg, a = _toolchain(tmp_path)  # compiler builds only what it is given
    with pytest.raises(AssetError, match="styles/mcp_live/x.css"):
        asyncio.run(a.compile_many(files, install=False, roots=["panorama/layout/mcp_live/x.xml"]))
    cfg, a = _toolchain(tmp_path / "deps", deps=True)
    res = asyncio.run(a.compile_many(files, install=False, roots=["layout/mcp_live/x.xml"]))
    assert sorted(f["source"] for f in res["files"]) == sorted(files)
    with pytest.raises(AssetError, match="roots"):
        asyncio.run(a.compile_many(files, install=False, roots=["layout/other.xml"]))
    with pytest.raises(AssetError, match="nope.vcss_c"):
        a.install_loose(["panorama/styles/nope.css"])
    assert len(a.install_loose(list(files))) == 2


def test_uninstall_many_and_installed_names(tmp_path):
    cfg, a = _toolchain(tmp_path)
    assert a.installed_names() == set()
    names = ["panorama/styles/a.vcss_c", "panorama/styles/b.vcss_c", "panorama/layout/c.vxml_c"]
    for n in names:
        (cfg.csdk_game_dir / n).parent.mkdir(parents=True, exist_ok=True)
        (cfg.csdk_game_dir / n).write_bytes(n.encode())
    a.pack_addon()
    assert a.installed_names() == set(names)
    packs = []
    orig = a.pack_addon
    a.pack_addon = lambda: packs.append(1) or orig()
    assert a.uninstall_many([names[0], names[1], "panorama/styles/gone.vcss_c"]) == names[:2]
    assert len(packs) == 1 and a.installed_names() == {names[2]}
    assert a.uninstall_many(["panorama/styles/gone.vcss_c"]) == [] and len(packs) == 1
    assert a.uninstall(names[2]) and not a.uninstall(names[2])


def test_corrupt_vpk_is_asset_error(tmp_path):
    cfg, a = _toolchain(tmp_path)
    pak = _game(tmp_path, {f"panorama/layout/f{i}.vxml_c": b"x" * 50 for i in range(20)})
    data = pak.read_bytes()
    for bad in (data[:100], data[:5], b"not a vpk at all"):
        pak.write_bytes(bad)
        with pytest.raises(AssetError):
            a.vpk
    cfg.addons_dir.mkdir(parents=True)
    (cfg.addons_dir / "pak01_dir.vpk").write_bytes(data[:100])
    with pytest.raises(AssetError):
        a.installed_names()


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
    cfg.cache_dir.mkdir(exist_ok=True)
    return cfg, a
