"""The setup tool's decisions: distro detection, install hints, where the CSDK archive comes from. No downloads."""

import asyncio

import pytest

from deadlock_mcp import toolchain
from deadlock_mcp.config import Config


@pytest.mark.parametrize("text, ostree, manager", [
    ('ID=ubuntu\nID_LIKE=debian\n', False, "apt"),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', False, "apt"),
    ('ID=fedora\n', False, "dnf"),
    ('ID=bazzite\nID_LIKE="fedora"\n', True, "rpm-ostree"),
    ('ID=cachyos\nID_LIKE=arch\n', False, "pacman"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', False, "zypper"),
    ('ID=nixos\n', False, None),
])
def test_package_manager(tmp_path, text, ostree, manager):
    (tmp_path / "os-release").write_text(text)
    if ostree:
        (tmp_path / "ostree-booted").write_text("")
    assert toolchain.package_manager(tmp_path / "os-release", tmp_path / "ostree-booted") == manager


def test_missing_tools_hints(monkeypatch):
    monkeypatch.setattr(toolchain.shutil, "which", lambda name: "/usr/bin/7za" if name == "7za" else None)
    hints = toolchain.missing_tools("rpm-ostree")
    assert "7z" not in hints  # any 7-Zip binary will do
    assert hints["imagemagick"].endswith("rpm-ostree install ImageMagick")
    assert toolchain.UMU_PAGE in toolchain.missing_tools("apt")["umu-run"]
    assert toolchain.missing_tools(None)["xdotool"].endswith("install the xdotool package")


def test_find_archive_prefers_ours_then_newest_download(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config(deadlock_dir=None, data_dir=tmp_path / "data")
    assert toolchain._find_archive(cfg) is None
    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / "Reduced_CSDK_13.7z.part").write_text("")  # browser still downloading
    (dl / "other.7z").write_text("")
    assert toolchain._find_archive(cfg) is None
    (dl / "Reduced_CSDK_13.7z").write_text("")
    assert toolchain._find_archive(cfg) == dl / "Reduced_CSDK_13.7z"
    ours = cfg.data_dir / "downloads" / toolchain.CSDK_ARCHIVE
    ours.parent.mkdir(parents=True)
    ours.write_text("")
    assert toolchain._find_archive(cfg) == ours


async def test_failed_download_asks_for_a_manual_one_then_extracts_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(toolchain, "_jobs", {})
    monkeypatch.setattr(toolchain, "_gdown_error", None)

    def quota(**kwargs):
        raise RuntimeError("Too many users have viewed or downloaded this file recently.\nmore")

    monkeypatch.setattr(toolchain.gdown, "download", quota)
    extracted = []

    async def extract(cfg, archive):
        extracted.append(archive)

    monkeypatch.setattr(toolchain, "_extract_csdk", extract)
    cfg = Config(deadlock_dir=None, data_dir=tmp_path / "data", csdk_dir=tmp_path / "data/tools/Reduced_CSDK_13")
    assert toolchain._csdk_step(cfg) == "started"
    await toolchain._jobs["csdk"]
    state = toolchain._csdk_step(cfg)
    assert state.startswith("needs action") and "Too many users" in state and "more" not in state
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads/Reduced_CSDK_13.7z").write_text("")
    assert toolchain._csdk_step(cfg) == "started"
    await toolchain._jobs["csdk"]
    assert extracted == [tmp_path / "Downloads/Reduced_CSDK_13.7z"]


async def test_failed_job_is_reported_once_then_retried(monkeypatch):
    monkeypatch.setattr(toolchain, "_jobs", {})

    async def boom():
        raise toolchain.AssetError("no network")

    assert toolchain._job("x", boom) == "started"
    await asyncio.gather(toolchain._jobs["x"], return_exceptions=True)
    assert toolchain._job("x", boom).startswith("failed: no network")
    assert toolchain._job("x", boom) == "started"
    await asyncio.gather(toolchain._jobs["x"], return_exceptions=True)
