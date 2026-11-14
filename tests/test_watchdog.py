from deadlock_mcp import watchdog
from deadlock_mcp.assets import Assets
from deadlock_mcp.config import Config


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


def test_watchdog_unmounts_when_game_exits(tmp_path):
    cfg, a = _mounted_install(tmp_path)
    watchdog.token_file(cfg).write_text("abc")
    states = iter([True, True, False])  # game seen running, still running, then exits
    msg = watchdog.watch("abc", cfg, poll=0, running=lambda: next(states))
    assert "unmounted" in msg and not a.mount_status()
    assert not watchdog.token_file(cfg).exists()


def test_watchdog_leaves_newer_launch_alone(tmp_path):
    cfg, a = _mounted_install(tmp_path)
    watchdog.token_file(cfg).write_text("newer")
    msg = watchdog.watch("abc", cfg, poll=0, startup_timeout=0, running=lambda: False)
    assert "superseded" in msg and a.mount_status()
    assert watchdog.token_file(cfg).read_text() == "newer"


def test_watchdog_superseded_after_game_exit(tmp_path):
    # A new launch claims the token after this watcher saw its game exit but before it unmounts:
    # the token is checked again under the gameinfo lock, so the new launch's mount survives.
    cfg, a = _mounted_install(tmp_path)
    watchdog.token_file(cfg).write_text("abc")

    def running():
        watchdog.token_file(cfg).write_text("newer")
        return False

    msg = watchdog.watch("abc", cfg, poll=0, startup_timeout=0, running=running)
    assert "superseded" in msg and a.mount_status()


def test_release_token(tmp_path):
    cfg = Config(deadlock_dir=tmp_path, cache_dir=tmp_path / "cache")
    assert not watchdog.release_token(cfg, "abc")  # no token file
    cfg.cache_dir.mkdir()
    watchdog.token_file(cfg).write_text("abc\n")
    assert not watchdog.release_token(cfg, "other") and watchdog.token_file(cfg).exists()
    assert watchdog.release_token(cfg, "abc") and not watchdog.token_file(cfg).exists()
