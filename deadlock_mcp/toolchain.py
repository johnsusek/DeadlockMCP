"""Toolchain setup: Source 2 Viewer CLI, CSDK 13 and the compiler's Wine prefix, plus install hints for the
system tools the server shells out to. Long steps run as background tasks; each setup() call reports progress."""

from __future__ import annotations

import asyncio
import io
import re
import shutil
import urllib.request
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import gdown

from .assets import _run
from .config import Config
from .env import desktop_env
from .errors import AssetError

S2V_URL = "https://github.com/ValveResourceFormat/ValveResourceFormat/releases/download/21.1/cli-linux-x64.zip"
CSDK_DRIVE_ID = "1bAM-S06aLAzX6mUOVdkhCoznj7SvwMZC"
CSDK_PAGE = "https://deadlockmodding.pages.dev/modding-tools/csdk-13"
CSDK_ARCHIVE = "Reduced_CSDK_13.7z"
UMU_PAGE = "https://github.com/Open-Wine-Components/umu-launcher"

# name: (binaries, what it is for, {package manager: package})
SYSTEM_TOOLS = {
    "7z": (("7z", "7zz", "7za"), "extracting CSDK during setup",
           {"apt": "7zip", "dnf": "7zip", "pacman": "7zip", "zypper": "7zip"}),
    "xdotool": (("xdotool",), "finding the game window for screenshots",
                {"apt": "xdotool", "dnf": "xdotool", "pacman": "xdotool", "zypper": "xdotool"}),
    "imagemagick": (("import",), "capturing, cropping and resizing screenshots",
                    {"apt": "imagemagick", "dnf": "ImageMagick", "pacman": "imagemagick", "zypper": "ImageMagick"}),
    "umu-run": (("umu-run",), "launching the game directly (launch_game(via_steam=True) works without it)",
                {"dnf": "umu-launcher", "pacman": "umu-launcher"}),
}
INSTALL_CMD = {"apt": "sudo apt install", "dnf": "sudo dnf install", "pacman": "sudo pacman -S",
               "zypper": "sudo zypper install", "rpm-ostree": "rpm-ostree install"}

_jobs: dict[str, asyncio.Task] = {}
_progress: dict[str, str] = {}
_gdown_error: str | None = None


def package_manager(os_release: Path = Path("/etc/os-release"), ostree: Path = Path("/run/ostree-booted")) -> str | None:
    """The distro's package manager, from /etc/os-release (ID and ID_LIKE). Atomic Fedoras (Bazzite, Silverblue)
    layer packages with rpm-ostree."""
    try:
        text = os_release.read_text()
    except OSError:
        return None
    ids = set(" ".join(re.findall(r'^(?:ID|ID_LIKE)="?([^"\n]*)', text, re.M)).split())
    families = {"apt": {"debian", "ubuntu"}, "dnf": {"fedora", "rhel"}, "pacman": {"arch"},
                "zypper": {"suse", "opensuse"}}
    manager = next((m for m, names in families.items() if ids & names), None)
    if manager == "dnf" and ostree.exists():
        return "rpm-ostree"
    return manager


def missing_tools(manager: str | None) -> dict[str, str]:
    """{tool: what it is for and how to install it} for every system tool not on PATH."""
    out = {}
    for name, (binaries, purpose, packages) in SYSTEM_TOOLS.items():
        if any(shutil.which(b) for b in binaries):
            continue
        package = packages.get("dnf" if manager == "rpm-ostree" else manager or "")
        if package:
            how = f"install: {INSTALL_CMD[manager]} {package}"
        elif name == "umu-run":
            how = f"install: see {UMU_PAGE}"
        else:
            how = f"install the {next(iter(packages.values()))} package"
        out[name] = f"needed for {purpose}; {how}"
    return out


def _job(name: str, start: Callable[[], Awaitable[None]]) -> str:
    """Start background job `name` unless it is running; report a failure of its last run once."""
    task = _jobs.get(name)
    if task and not task.done():
        return "running" + (f": {_progress[name]}" if name in _progress else "")
    if task and not task.cancelled() and task.exception():
        del _jobs[name]
        return f"failed: {task.exception()}; run setup again to retry"
    _jobs[name] = asyncio.create_task(start())
    return "started"


# ------------------------------------------------------------------ Source 2 Viewer


async def _install_s2v(cli: Path) -> None:
    def fetch() -> None:
        with urllib.request.urlopen(S2V_URL, timeout=60) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(cli.parent)
        cli.chmod(0o755)

    await asyncio.to_thread(fetch)
    if not cli.is_file():
        raise AssetError(f"{S2V_URL} did not contain {cli.name}")


def _s2v_step(cfg: Config) -> str:
    if not cfg.s2v_cli:
        return "S2V_CLI is unset"
    if cfg.s2v_cli.is_file():
        return f"done: {cfg.s2v_cli}"
    return _job("s2v", lambda: _install_s2v(cfg.s2v_cli))


# ------------------------------------------------------------------ CSDK 13


def _find_archive(cfg: Config) -> Path | None:
    """A complete CSDK archive: the one setup downloads, or one the user saved to ~/Downloads (browsers and gdown
    write to a temporary name and rename when done, so a file with the final name is complete)."""
    ours = cfg.data_dir / "downloads" / CSDK_ARCHIVE
    if ours.is_file():
        return ours
    downloads = Path.home() / "Downloads"
    found = [p for p in downloads.glob("*.7z") if "csdk" in p.name.lower() and "13" in p.name] if downloads.is_dir() else []
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


async def _download_csdk(cfg: Config) -> None:
    """Fetch the archive from Google Drive. A failure is kept in _gdown_error (setup then asks the user to download it
    by hand) instead of failing the job, so the extract job can start once the user's download shows up."""
    global _gdown_error
    folder = cfg.data_dir / "downloads"
    folder.mkdir(parents=True, exist_ok=True)

    def progress(done: int, total: int | None) -> None:
        _progress["csdk"] = f"downloading, {done / 1e6:.0f} of {total / 1e6 if total else 1600:.0f} MB"

    try:
        await asyncio.to_thread(gdown.download, id=CSDK_DRIVE_ID, output=str(folder / CSDK_ARCHIVE), quiet=True,
                                resume=True, progress=progress)
    except Exception as e:  # gdown raises plain exceptions for quota and changed links; all mean "download it by hand"
        _gdown_error = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    finally:
        _progress.pop("csdk", None)


async def _extract_csdk(cfg: Config, archive: Path) -> None:
    seven = next(filter(None, map(shutil.which, SYSTEM_TOOLS["7z"][0])), None)
    if not seven:
        raise AssetError("7z is not installed (see missing_system_tools)")
    _progress["csdk"] = f"extracting {archive} (about 5.4 GB)"
    try:
        code, out = await _run([seven, "x", "-y", str(archive), f"-o{cfg.csdk_dir.parent}"], timeout=1800)
    finally:
        _progress.pop("csdk", None)
    if code != 0:
        raise AssetError(f"7z failed (exit {code}): {out[-400:].strip()}")
    if not cfg.resourcecompiler.is_file():
        raise AssetError(f"{archive.name} did not contain {cfg.resourcecompiler}")
    if archive.parent == cfg.data_dir / "downloads":  # ours to delete; a file in ~/Downloads is the user's
        archive.unlink()


def _csdk_step(cfg: Config) -> str:
    if not cfg.csdk_dir:
        return "CSDK_DIR is unset"
    if cfg.resourcecompiler.is_file():
        return f"done: {cfg.csdk_dir}"
    if archive := _find_archive(cfg):
        return _job("csdk", lambda: _extract_csdk(cfg, archive))
    if _gdown_error is None:
        return _job("csdk", lambda: _download_csdk(cfg))
    return (f"needs action: the automatic download failed ({_gdown_error}). Download Reduced CSDK 13 (.7z, about "
            f"1.6 GB) from {CSDK_PAGE} into ~/Downloads, then run setup again")


# ------------------------------------------------------------------ Wine prefix


async def _make_prefix(cfg: Config, proton: Path) -> None:
    """Let the compiler's own Proton build the prefix: a bare `wineboot` prefix lacks the DXVK setup without which
    resourcecompiler can't load rendersystemdx11.dll. Proton puts the prefix in <STEAM_COMPAT_DATA_PATH>/pfx."""
    compat = cfg.wineprefix.parent
    compat.mkdir(parents=True, exist_ok=True)
    env = dict(desktop_env(), STEAM_COMPAT_DATA_PATH=str(compat))
    if cfg.steam_root:
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(cfg.steam_root)
    code, out = await _run([str(proton), "run", "cmd", "/c", "exit"], timeout=600, env=env)
    if not (cfg.wineprefix / "system.reg").is_file():
        raise AssetError(f"Proton failed to create the prefix (exit {code}): {out[-400:].strip()}")


def _prefix_step(cfg: Config) -> str:
    if cfg.rc_cmd:
        return "skipped: DEADLOCK_RC_CMD runs the compiler"
    if (cfg.wineprefix / "system.reg").is_file():
        return f"done: {cfg.wineprefix}"
    if not cfg.wine or not cfg.wine.is_file():
        return "needs action: no Proton found; install Proton through Steam (or GE-Proton), or set DEADLOCK_WINE"
    proton = cfg.wine.parents[2] / "proton"  # <proton>/files/bin/wine
    if not proton.is_file():
        return f"needs action: {cfg.wine} is not inside a Proton build (no {proton}); set WINEPREFIX to a ready prefix"
    if cfg.wineprefix.name != "pfx":
        return f"needs action: Proton can only create a prefix named pfx, not {cfg.wineprefix}"
    return _job("prefix", lambda: _make_prefix(cfg, proton))


async def setup(cfg: Config) -> dict:
    info = {
        "source2viewer": _s2v_step(cfg),
        "csdk": _csdk_step(cfg),
        "wine_prefix": _prefix_step(cfg),
    }
    ready = all(v.startswith(("done", "skipped")) for v in info.values())
    if missing := missing_tools(package_manager()):
        info["missing_system_tools"] = missing
    info["next"] = "bridge_install, then launch_game(map='new_player_basics')" if ready else "run setup again to check progress"
    return info
