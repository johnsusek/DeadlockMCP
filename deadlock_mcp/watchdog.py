"""Unmount the dev addon when a game started by launch_game exits, so a later normal Steam launch
doesn't load the bridge. launch_game runs this detached: `python -m deadlock_mcp.watchdog <token>`.

The token ties the watcher to one launch: a newer launch_game overwrites it, and a superseded
watcher exits without touching gameinfo.gi.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from .assets import Assets
from .config import Config

GAME_PATTERN = r"bin[/\\]win64[/\\]deadlock\.exe"


def game_running() -> bool:
    out = subprocess.run(["pgrep", "-f", GAME_PATTERN], capture_output=True, text=True)
    return bool(out.stdout.strip())


def token_file(cfg: Config) -> Path:
    return cfg.cache_dir / "launch-session"


def watch(token: str, cfg: Config | None = None, poll: float = 2.0, startup_timeout: float = 180.0,
          running=game_running) -> str:
    cfg = cfg or Config()
    deadline = time.monotonic() + startup_timeout
    while not running() and time.monotonic() < deadline:  # wait for the game to appear
        time.sleep(poll)
    while running():
        time.sleep(poll)
    tf = token_file(cfg)
    try:
        current = tf.read_text().strip()
    except OSError:
        current = None
    if current != token:
        return "superseded by a newer launch; leaving gameinfo.gi alone"
    tf.unlink(missing_ok=True)
    return Assets(cfg).set_mount(False)


def main() -> None:
    print(time.strftime("%F %T"), watch(sys.argv[1]), flush=True)


if __name__ == "__main__":
    main()
