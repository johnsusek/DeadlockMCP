"""Desktop-session environment for GUI child processes (the game, the compiler under Wine, screenshot tools).

MCP clients may start servers with a stripped environment; desktop_env() restores the session variables from
their standard locations. No package-internal imports: assets.py and game.py both use it."""

from __future__ import annotations

import os
from pathlib import Path


def _newest(paths: list[Path]) -> Path | None:
    files = []
    for p in paths:
        try:
            if p.is_file():
                files.append((p.stat().st_mtime, p))
        except OSError:
            pass
    return max(files)[1] if files else None


def find_xauthority(run: Path, home: Path) -> Path | None:
    """The X cookie file of the running session. Wine needs it to open the display: KDE's XWayland keeps it at
    $XDG_RUNTIME_DIR/xauth_XXXXXX, mutter at $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*, X11 sessions ~/.Xauthority."""
    if run.is_dir():
        for pattern in ("xauth_*", ".mutter-Xwaylandauth.*"):
            if found := _newest(list(run.glob(pattern))):
                return found
    p = home / ".Xauthority"
    return p if p.is_file() else None


def desktop_env(base: dict | None = None) -> dict:
    """A copy of the environment (default os.environ) with the desktop-session variables GUI tools need
    (XDG_RUNTIME_DIR, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS, DISPLAY, XAUTHORITY) filled in when unset."""
    env = dict(os.environ if base is None else base)
    run = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if run.is_dir():
        env.setdefault("XDG_RUNTIME_DIR", str(run))
        if "WAYLAND_DISPLAY" not in env:
            socks = sorted(p.name for p in run.glob("wayland-[0-9]") if p.is_socket())
            if socks:
                env["WAYLAND_DISPLAY"] = socks[0]
        if (run / "bus").exists():
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run / 'bus'}")
    if "DISPLAY" not in env:
        xs = sorted(Path("/tmp/.X11-unix").glob("X*"))
        if xs:
            env["DISPLAY"] = ":" + xs[0].name[1:]
    if "XAUTHORITY" not in env:
        home = Path(env.get("HOME") or Path.home())
        if xa := find_xauthority(run, home):
            env["XAUTHORITY"] = str(xa)
    env.setdefault("XDG_SESSION_TYPE", "wayland" if env.get("WAYLAND_DISPLAY") else "x11")
    return env
