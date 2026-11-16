"""The game process: detecting it, building its launch command, quitting it, and capturing screenshots."""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DEADLOCK_APPID, Config
from .env import desktop_env
from .errors import DeadlockError

GAME_PATTERN = r"bin[/\\]win64[/\\]deadlock\.exe"
DEFAULT_LAUNCH_ARGS = ["-console", "-condebug", "-dev", "-novid"]
CROP_RX = re.compile(r"\d+x\d+\+\d+\+\d+")


def game_running() -> bool:
    out = subprocess.run(["pgrep", "-f", GAME_PATTERN], capture_output=True, text=True)
    return bool(out.stdout.strip())


# ------------------------------------------------------------------ launching


def launch_args(map: str | None, extra_args: str = "") -> list[str]:
    try:
        extra = shlex.split(extra_args)
    except ValueError as e:
        raise DeadlockError(f"cannot parse extra_args ({e})") from None
    return [*DEFAULT_LAUNCH_ARGS, *(["+map", map] if map else []), *extra]


def umu_command(cfg: Config, args: list[str]) -> tuple[list[str], Path, dict]:
    """(cmd, cwd, env) running deadlock.exe directly through umu-run in the game's own Proton prefix."""
    if not cfg.deadlock_dir:
        raise DeadlockError("Deadlock dir not found; set DEADLOCK_DIR")
    umu = shutil.which("umu-run")
    if not umu:
        raise DeadlockError("umu-run not found; install umu-launcher or use via_steam=True")
    if not cfg.compat_data_dir:
        raise DeadlockError("cannot locate Deadlock's Proton prefix (steamapps/compatdata/1422450); use via_steam=True")
    if not cfg.steam_root:
        raise DeadlockError("Steam client install not found; set STEAM_COMPAT_CLIENT_INSTALL_PATH or use via_steam=True")
    env = dict(
        desktop_env(),
        GAMEID=f"umu-{DEADLOCK_APPID}",
        STORE="none",
        SteamAppId=str(DEADLOCK_APPID),
        SteamGameId=str(DEADLOCK_APPID),
        # umu treats WINEPREFIX as the compat-data root and uses <root>/pfx itself
        WINEPREFIX=str(cfg.compat_data_dir),
        STEAM_COMPAT_CLIENT_INSTALL_PATH=str(cfg.steam_root),
    )
    if cfg.proton_dir:
        env["PROTONPATH"] = str(cfg.proton_dir)
    return [umu, "./deadlock.exe", *args], cfg.deadlock_dir / "game/bin/win64", env


def steam_command(args: list[str]) -> tuple[list[str], dict]:
    """`steam -applaunch` through native or Snap Steam (/snap/bin may be missing from a client's PATH), else Flatpak."""
    env = desktop_env()
    launch = ["-applaunch", str(DEADLOCK_APPID), *args]
    if steam := shutil.which("steam", path=f"{env.get('PATH', '')}:/snap/bin"):
        return [steam, *launch], env
    if shutil.which("flatpak") and (Path.home() / ".var/app/com.valvesoftware.Steam").is_dir():
        return ["flatpak", "run", "com.valvesoftware.Steam", *launch], env
    raise DeadlockError("Steam not found: no `steam` on PATH and no Flatpak Steam (com.valvesoftware.Steam)")


def spawn(cmd: list[str], log: Path | None, cwd: Path | None = None, env: dict | None = None,
          append: bool = False) -> subprocess.Popen:
    """Start a detached process, its output going to `log` (closed again in this process)."""
    if log is None:
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a" if append else "w") as out:
        return subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True)


def start_watchdog(cfg: Config, token: str) -> subprocess.Popen:
    """Unmount the dev addon when this launch's game exits (see watchdog.py)."""
    return spawn([sys.executable, "-m", "deadlock_mcp.watchdog", token], cfg.cache_dir / "watchdog.log", append=True)


async def quit_game(wait: float = 10.0) -> bool:
    """Terminate the game; True once it is gone."""
    proc = await asyncio.create_subprocess_exec("pkill", "-f", GAME_PATTERN)
    await proc.wait()
    for _ in range(int(wait / 0.25)):
        await asyncio.sleep(0.25)
        if not game_running():
            return True
    return not game_running()


# ------------------------------------------------------------------ screenshots


async def _run(cmd: list[str], env: dict, timeout: float = 20) -> tuple[int, str]:
    """Run a process asynchronously; output via a temp file (screenshot tools may leave helpers holding pipes)."""
    with tempfile.TemporaryFile() as out:
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=out,
                                                        stderr=out, env=env)
        except OSError as e:
            return 127, str(e)
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, f"timed out after {timeout}s"
        out.seek(0)
        return proc.returncode or 0, out.read().decode("utf-8", "replace")


def check_crop(crop: str | None) -> None:
    if crop is not None and not CROP_RX.fullmatch(crop.strip()):
        raise DeadlockError(f"crop must look like 'WxH+X+Y' (e.g. '800x800+0+1100'), got {crop!r}")


async def screenshot(max_width: int = 1600, crop: str | None = None, full_screen: bool = False) -> tuple[bytes, str]:
    """(image bytes, 'jpeg' | 'png'). Captures the game window via X11, or the whole screen with full_screen.

    A game window that can't be captured is an error, never a silent switch to the whole screen: that would
    hand the caller a picture of the user's other windows when it asked for the game."""
    check_crop(crop)
    if max_width < 1:
        raise DeadlockError("max_width must be positive")
    env = desktop_env()
    whole = "pass full_screen=True to capture the whole screen instead"
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "shot.png"
        if full_screen:
            tools = [[t, "-b", "-n", "-f", "-o", str(raw)] for t in [shutil.which("spectacle")] if t]
            tools += [[t, str(raw)] for t in [shutil.which("grim")] if t]
            if not tools:
                raise DeadlockError("no full-screen screenshot tool found (spectacle or grim)")
            errors = []
            for cmd in tools:
                code, out = await _run(cmd, env)
                if raw.is_file():
                    break
                errors.append(f"{Path(cmd[0]).name} failed (exit {code}): {out[-400:]}")
            if not raw.is_file():
                raise DeadlockError("screenshot failed:\n" + "\n".join(errors))
        else:
            if not (shutil.which("xdotool") and shutil.which("import")):
                raise DeadlockError(f"capturing the game window needs xdotool and ImageMagick (import); {whole}")
            find = ["xdotool", "search", "--name", "^Deadlock$"]
            code, out = await _run(find, env, timeout=10)
            win = (out.split() or [None])[0] if code == 0 else None
            if not win:
                raise DeadlockError(f"no Deadlock window found on the X display (is the game running?); {whole}")
            code, out = await _run(["import", "-window", win, str(raw)], env)
            if not raw.is_file():
                # import cannot read an unmapped window and fails with an unrelated message, so say what is wrong
                code, visible = await _run([*find[:2], "--onlyvisible", *find[2:]], env, timeout=10)
                if not (code == 0 and visible.split()):
                    raise DeadlockError("the Deadlock window is minimised or hidden, so it can't be captured (in fullscreen "
                                        f"it minimises itself when it loses focus); restore it, or {whole}")
                raise DeadlockError(f"capturing the Deadlock window failed: {out[-400:].strip()}; {whole}")
        magick = shutil.which("magick") or shutil.which("convert")
        if not magick:
            if crop:
                raise DeadlockError("crop needs ImageMagick (magick or convert)")
            return raw.read_bytes(), "png"
        small = Path(td) / "shot.jpg"
        ops = (["-crop", crop.strip(), "+repage"] if crop else []) + ["-resize", f"{max_width}x>", "-quality", "85"]
        code, out = await _run([magick, str(raw), *ops, str(small)], env, timeout=60)
        # a crop outside the image only warns ("geometry does not contain image") and yields a 1x1 picture
        if code != 0 or not small.is_file() or small.stat().st_size == 0 or "does not contain" in out:
            if crop:  # an uncropped full-size image is not what was asked for
                raise DeadlockError(f"cropping the screenshot failed (is {crop!r} inside the image?): {out[-400:]}")
            return raw.read_bytes(), "png"
        return small.read_bytes(), "jpeg"
