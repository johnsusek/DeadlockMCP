"""Unmount the dev addon when a game started by launch_game exits, so a later normal Steam launch
doesn't load the bridge. launch_game runs this detached: `python -m deadlock_mcp.watchdog <token>`.

The token ties the watcher to one launch: a newer launch_game overwrites it (under the gameinfo.gi lock,
before mounting), and a superseded watcher leaves gameinfo.gi alone. The token check and the unmount happen
under that same lock, so an old watcher can't unmount the addon a new launch just mounted.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .assets import Assets
from .config import Config
from .game import GAME_PATTERN, game_running  # noqa: F401  (GAME_PATTERN re-exported for callers)


def token_file(cfg: Config) -> Path:
    return cfg.cache_dir / "launch-session"


def release_token(cfg: Config, token: str) -> bool:
    """If the launch token is still `token`, remove it and return True. Call under the gameinfo lock."""
    tf = token_file(cfg)
    try:
        if tf.read_text().strip() != token:
            return False
    except OSError:
        return False
    tf.unlink(missing_ok=True)
    return True


def watch(token: str, cfg: Config | None = None, poll: float = 2.0, startup_timeout: float = 180.0,
          running=game_running) -> str:
    cfg = cfg or Config()
    deadline = time.monotonic() + startup_timeout
    while not running() and time.monotonic() < deadline:  # wait for the game to appear
        time.sleep(poll)
    while running():
        time.sleep(poll)
    msg = Assets(cfg).set_mount(False, only_if=lambda: release_token(cfg, token))
    if msg.startswith("skipped"):
        return "superseded by a newer launch; leaving gameinfo.gi alone"
    return msg


def main() -> None:
    try:
        msg = watch(sys.argv[1])
    except Exception as e:  # the log is all anyone sees of this process
        msg = f"ERROR: {type(e).__name__}: {e}"
    print(time.strftime("%F %T"), msg, flush=True)  # stamped when it finishes, not at launch


if __name__ == "__main__":
    main()
