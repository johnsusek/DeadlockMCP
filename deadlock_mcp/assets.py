"""Game asset access: list/decompile shipped Panorama files, compile sources via CSDK, mount the addon."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import shlex
import shutil
import stat
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .env import desktop_env
from .errors import AssetError
from .vpk import VPK, write_vpk

COMPILED_EXT = {".xml": ".vxml_c", ".css": ".vcss_c", ".js": ".vjs_c"}
SOURCE_EXT = {v: k for k, v in COMPILED_EXT.items()}


def winpath(p: Path) -> str:
    return "Z:" + str(p.resolve()).replace("/", "\\")


async def _run(cmd: list[str], timeout: float, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    """Run a process, capturing output via a temp file and waiting on the process itself (a
    persistent wineserver inherits pipes, so waiting for pipe EOF would hang)."""
    with tempfile.TemporaryFile() as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()  # reap it, or it stays a zombie
            raise AssetError(f"timed out after {timeout}s: {' '.join(cmd)}") from None
        out.seek(0)
        return proc.returncode or 0, out.read().decode("utf-8", "replace")




def safe_rel(path: str) -> str:
    """Validate a user-supplied path and return it as 'panorama/...' (case kept). Leading slashes are
    dropped (paths are relative to the VPK root); anything that could leave the addon trees is rejected."""
    p = path.strip().replace("\\", "/").lstrip("/")
    head, _, rest = p.partition("/")
    p = "panorama/" + rest if head.lower() == "panorama" and rest else "panorama/" + p
    if "\0" in p or ":" in p or any(seg in ("", ".", "..") for seg in p.split("/")):
        raise AssetError(f"invalid path {path!r}: must be a relative path under panorama/ without '..' or empty segments")
    return p


def _open_vpk(path: Path) -> VPK:
    try:
        return VPK(path)
    except (ValueError, struct.error, IndexError, OSError) as e:
        raise AssetError(f"cannot read {path} (corrupt or truncated?): {e}") from None


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace path's contents without ever leaving a truncated file (keeps the file mode)."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Assets:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._vpk: VPK | None = None
        self._compile_lock = asyncio.Lock()  # compile_many runs on the event loop; one compile/pack at a time
        self._pack_lock = threading.Lock()  # pack_addon is also reached from sync tools in worker threads
        self._wineserver_started: float | None = None

    # ------------------------------------------------------------------ shipped files
    @property
    def vpk(self) -> VPK:
        """The game's pak01_dir.vpk, reloaded when the file changes (a game update moves every offset)."""
        path = self.cfg.pak_dir_vpk
        if not path or not path.is_file():
            raise AssetError("Deadlock pak01_dir.vpk not found; set DEADLOCK_DIR")
        st = path.stat()
        if self._vpk is None or self._vpk.stamp != (st.st_mtime_ns, st.st_size):
            self._vpk = _open_vpk(path)
        return self._vpk

    def list_panorama(self, pattern: str | None = None, limit: int = 300) -> list[str]:
        rx = re.compile(pattern, re.I) if pattern else None
        names = sorted(
            n for n in self.vpk.names()
            if n.startswith("panorama/") and n.endswith((".vxml_c", ".vcss_c", ".vjs_c"))
        )
        if rx:
            names = [n for n in names if rx.search(n)]
        return names[:limit]

    @staticmethod
    def normalize(path: str) -> str:
        """Accept 'layout/foo', 'panorama/layout/foo.xml', 'panorama/styles/foo.vcss_c', ...; return VPK name."""
        p = safe_rel(path).lower()
        stem, ext = os.path.splitext(p)
        if ext in COMPILED_EXT:
            return stem + COMPILED_EXT[ext]
        if ext in SOURCE_EXT:
            return p
        kind = p.split("/")[1] if p.count("/") >= 2 else ""
        return p + {"layout": ".vxml_c", "styles": ".vcss_c", "scripts": ".vjs_c"}.get(kind, ".vxml_c")

    def shipped_crc(self, path: str) -> int:
        """CRC32 of a shipped file, as recorded in pak01_dir.vpk (identifies the game build's version of it)."""
        name = self.normalize(path)
        vpk = self.vpk
        if name not in vpk:
            raise AssetError(f"{name} is not in pak01_dir.vpk")
        return vpk.crc(name)

    async def decompile(self, path: str, refresh: bool = False) -> str:
        """Return decompiled source text for a shipped Panorama file (via Source2Viewer-CLI)."""
        name = self.normalize(path)
        vpk = self.vpk
        if name not in vpk:
            raise AssetError(f"{name} is not in pak01_dir.vpk")
        src_ext = SOURCE_EXT[os.path.splitext(name)[1]]
        stem = os.path.splitext(name)[0]
        # keyed on the shipped CRC, so a game update invalidates it (old un-keyed cache files are never read)
        cache = self.cfg.cache_dir / "decompiled" / f"{stem}.{vpk.crc(name):08x}{src_ext}"
        if cache.is_file() and not refresh:
            return cache.read_text(encoding="utf-8", errors="replace")

        cli = self.cfg.s2v_cli
        if not cli or not cli.is_file():
            raise AssetError(
                "Source2Viewer-CLI not found. Download cli-linux-x64.zip from "
                "https://github.com/ValveResourceFormat/ValveResourceFormat/releases into tools/s2v/ or set S2V_CLI."
            )
        try:
            data = vpk.read(name)
        except OSError as e:
            raise AssetError(f"cannot read {name} from the game VPKs: {e}") from None
        work = self.cfg.cache_dir / "work" / str(time.time_ns())
        work.mkdir(parents=True)
        try:
            compiled = work / os.path.basename(name)
            compiled.write_bytes(data)
            out_dir = work / "out"
            code, log = await _run([str(cli), "-i", str(compiled), "-o", str(out_dir), "-d"], timeout=120)
            produced = [p for p in out_dir.rglob("*") if p.is_file()] if out_dir.exists() else []
            if not produced and out_dir.is_file():
                produced = [out_dir]
            if code != 0 or not produced:
                raise AssetError(f"Source2Viewer-CLI failed (exit {code}):\n{log[-2000:]}")
            text = produced[0].read_text(encoding="utf-8", errors="replace")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        stale = re.compile(re.escape(os.path.basename(stem)) + r"(\.[0-9a-f]{8})?" + re.escape(src_ext))
        for old in cache.parent.iterdir():
            if old != cache and stale.fullmatch(old.name):
                old.unlink(missing_ok=True)
        _atomic_write(cache, text.encode("utf-8"))
        return text

    # ------------------------------------------------------------------ compiling (CSDK 13)
    @property
    def csdk_content_root(self) -> Path:
        if not self.cfg.csdk_content_dir:
            raise AssetError("CSDK_DIR is not set (path to your CSDK 13 install)")
        return self.cfg.csdk_content_dir

    @property
    def csdk_game_root(self) -> Path:
        if not self.cfg.csdk_game_dir:
            raise AssetError("CSDK_DIR is not set (path to your CSDK 13 install)")
        return self.cfg.csdk_game_dir

    async def _ensure_wineserver(self, env: dict) -> None:
        """Keep a persistent wineserver so each compile skips Wine start-up (~2.5s → ~1s). Best effort:
        if it fails, compiles still work, just slower."""
        if not self.cfg.wine or (self._wineserver_started is not None
                                 and time.monotonic() - self._wineserver_started < 500):
            return
        ws = self.cfg.wine.with_name("wineserver")
        if not ws.is_file():
            return
        self._wineserver_started = time.monotonic()  # also on failure, so we don't retry every compile
        try:
            await _run([str(ws), "-p", "600"], timeout=30, env=env)
        except (AssetError, OSError):
            pass

    def _rc_command(self, rc: Path, src: Path) -> tuple[list[str], dict]:
        # desktop_env: under Wine the compiler needs an X display (DISPLAY + XAUTHORITY), which a client that starts
        # the server with a stripped environment doesn't pass on
        env = dict(desktop_env(), WINEDEBUG="-all", WINEPREFIX=str(self.cfg.wineprefix))
        if self.cfg.rc_cmd:
            return shlex.split(self.cfg.rc_cmd.format(rc=rc, src=src, src_win=winpath(src))), env
        if not self.cfg.wine or not self.cfg.wine.is_file():
            raise AssetError("No Proton wine binary found; set DEADLOCK_WINE or DEADLOCK_RC_CMD")
        if not (self.cfg.wineprefix / "system.reg").is_file():
            raise AssetError(
                f"Wine prefix {self.cfg.wineprefix} is not initialised. Run once: "
                "GAMEID=umu-default umu-run cmd /c exit   (or set WINEPREFIX)"
            )
        return [str(self.cfg.wine), str(rc), "-f", "-i", winpath(src)], env

    async def compile_many(self, files: dict[str, str], install: bool = True, roots: list[str] | None = None) -> dict:
        """Write Panorama sources ({'panorama/layout/foo.xml': text, ...}) into the CSDK content tree
        and compile them; with install, repack the addon VPK.

        roots: only invoke the compiler on these sources — resourcecompiler builds the files they
        reference (stylesheets, scripts) itself, saving a ~1s compiler run per dependency."""
        async with self._compile_lock:  # concurrent calls would overwrite each other's sources and outputs
            return await self._compile_many(files, install, roots)

    async def _compile_many(self, files: dict[str, str], install: bool, roots: list[str] | None) -> dict:
        rc = self.cfg.resourcecompiler
        if not rc or not rc.is_file():
            raise AssetError(f"resourcecompiler.exe not found (looked at {rc}); check CSDK_DIR")
        prepared = []  # (source rel, source path, compiled output, output mtime before)
        for rel_source, text in files.items():
            rel = safe_rel(rel_source)
            ext = os.path.splitext(rel)[1].lower()
            if ext not in COMPILED_EXT:
                raise AssetError(f"unsupported source type {ext!r} (use .xml, .css or .js)")
            src = self.csdk_content_root / rel
            out = self.csdk_game_root / (os.path.splitext(rel)[0] + COMPILED_EXT[ext])
            before = out.stat().st_mtime_ns if out.exists() else 0
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(text, encoding="utf-8")
            prepared.append((rel, src, out, before))
        root_set = {safe_rel(r) for r in roots} if roots else None
        if root_set and (unknown := root_set - {rel for rel, *_ in prepared}):
            raise AssetError(f"roots not among the files: {', '.join(sorted(unknown))}")
        results = []
        for rel, src, out, before in prepared:
            if root_set is not None and rel not in root_set:
                continue
            cmd, env = self._rc_command(rc, src)
            await self._ensure_wineserver(env)
            code, log = await _run(cmd, timeout=300, cwd=rc.parent, env=env)
            log = "\n".join(
                l for l in log.splitlines()
                if l.strip() and not re.match(r"(ntsync|Creating render device|PIX|Disable asynchronous|Leaked KeyValues)", l)
            )
            m = re.search(r"(\d+) compiled, (\d+) failed", log)
            ok = bool(m and int(m[1]) >= 1 and int(m[2]) == 0) and out.exists() and out.stat().st_mtime_ns > before
            if not ok:
                errs = [l.strip() for l in log.splitlines()
                        if ("COMPILE ERROR" in l or "error" in l.lower() and ":" in l)
                        and not l.strip().startswith(("Look for", "ERROR: 0 compiled"))]
                errs = [re.sub(r"^Z:\\.*?\\citadel_addons\\[^\\]+\\", "", e).replace("\\", "/") for e in errs]
                detail = "\n".join(dict.fromkeys(errs)) if errs else log[-4000:]
                if "RenderDeviceMgr" in log:
                    raise AssetError(
                        f"compile failed for {rel}: resourcecompiler could not open a display. Under Wine it needs an "
                        f"X connection: DISPLAY={env.get('DISPLAY')!r}, XAUTHORITY={env.get('XAUTHORITY')!r}. Start the "
                        "MCP server from the desktop session, or set DISPLAY and XAUTHORITY (KDE keeps the cookie at "
                        f"$XDG_RUNTIME_DIR/xauth_*).\n{detail}")
                raise AssetError(f"compile failed for {rel}:\n{detail}")
            results.append({"source": rel, "compiled": str(out), "log": log[-1500:]})
        # dependencies are built by the roots' compiler runs; a file no root references would silently stay stale
        missing = [rel for rel, _, out, before in prepared if not (out.is_file() and out.stat().st_mtime_ns > before)]
        if missing:
            raise AssetError(f"no fresh compiled output for {', '.join(missing)} (is it referenced from a root?)")
        if root_set is not None:
            results += [{"source": rel, "compiled": str(out), "log": "(built as a dependency)"}
                        for rel, _, out, _ in prepared if rel not in root_set]
        res: dict = {"files": results}
        if install:
            res["vpk"] = self.pack_addon()
        return res

    def addon_files(self) -> dict[str, Path]:
        """Compiled files in the CSDK addon output dir, keyed by game-relative path."""
        root = self.csdk_game_root
        if not root.is_dir():
            return {}
        return {
            p.relative_to(root).as_posix(): p
            for p in root.rglob("*_c")
            if p.is_file()
            and p.relative_to(root).parts[0] in ("panorama", "resource", "scripts", "materials")
            and not p.name.startswith("mcp_inbox")  # request template: never shipped
            and "mcp_live" not in p.relative_to(root).parts  # live layouts are loaded loose
        }

    def cleanup_live(self) -> int:
        """Delete live-loaded layouts/styles (only safe when the game is not running)."""
        n = 0
        roots = [r for r in (self.cfg.addons_dir, self.cfg.csdk_content_dir, self.cfg.csdk_game_dir) if r]
        for r in roots:
            for kind in ("layout", "styles", "scripts"):
                d = r / "panorama" / kind / "mcp_live"
                if d.is_dir():
                    n += sum(1 for _ in d.iterdir())
                    shutil.rmtree(d, ignore_errors=True)
        return n

    def install_loose(self, rel_sources: list[str]) -> list[str]:
        """Copy compiled outputs for the given sources into the mounted addon dir as loose files
        (for files with names the game's VPKs don't contain, e.g. live layouts)."""
        if not self.cfg.addons_dir:
            raise AssetError("Deadlock dir not found")
        out = []
        for rel in rel_sources:
            rel = safe_rel(rel)
            ext = os.path.splitext(rel)[1].lower()
            if ext not in COMPILED_EXT:
                raise AssetError(f"unsupported source type {ext!r} (use .xml, .css or .js)")
            rel_c = os.path.splitext(rel)[0] + COMPILED_EXT[ext]
            src = self.csdk_game_root / rel_c
            if not src.is_file():
                raise AssetError(f"compiled output {rel_c} not found in {self.csdk_game_root}; compile it first")
            dest = self.cfg.addons_dir / rel_c
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            out.append(str(dest))
        return out

    def pack_addon(self) -> dict:
        """(Re)build <mount>/pak01_dir.vpk from all compiled addon files.

        The engine searches every VPK before any loose directory, so overrides must ship as a VPK."""
        if not self.cfg.addons_dir:
            raise AssetError("Deadlock dir not found")
        with self._pack_lock:
            files = self.addon_files()
            self.cfg.addons_dir.mkdir(parents=True, exist_ok=True)
            dest = self.cfg.addons_dir / "pak01_dir.vpk"
            write_vpk(dest, {rel: p.read_bytes() for rel, p in files.items()})
        return {"vpk": str(dest), "files": sorted(files)}

    def installed_names(self) -> set[str]:
        """File names inside the installed addon VPK (empty when there is none)."""
        p = self.cfg.addons_dir / "pak01_dir.vpk" if self.cfg.addons_dir else None
        if not p or not p.is_file():
            return set()
        return set(_open_vpk(p).names())

    def uninstall_many(self, rel_compiled: Iterable[str]) -> list[str]:
        """Delete compiled addon files (e.g. 'panorama/styles/hud.vcss_c') and repack once; returns those removed."""
        removed = []
        for rel in [safe_rel(r) for r in rel_compiled]:  # validate all before deleting any
            p = self.csdk_game_root / rel
            if p.is_file():
                p.unlink()
                removed.append(rel)
        if removed:
            self.pack_addon()
        return removed

    def uninstall(self, rel_compiled: str) -> bool:
        return bool(self.uninstall_many([rel_compiled]))

    # ------------------------------------------------------------------ gameinfo mounting
    BEGIN, END = "// deadlock-mcp begin", "// deadlock-mcp end"

    def _block_rx(self) -> re.Pattern:
        return re.compile(r"^[ \t]*" + re.escape(self.BEGIN) + r".*?" + re.escape(self.END) + r"[ \t]*\r?\n", re.M | re.S)

    @staticmethod
    def _read_gameinfo(gi: Path) -> str:
        # surrogateescape + no newline translation: writing it back reproduces every byte we didn't change
        return gi.read_bytes().decode("utf-8", "surrogateescape")

    @contextmanager
    def _gameinfo_lock(self) -> Iterator[None]:
        """Inter-process lock: the server and the watchdog process both rewrite gameinfo.gi."""
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.cache_dir / "gameinfo.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def mount_status(self) -> bool:
        gi = self.cfg.gameinfo
        if not (gi and gi.is_file()):
            return False
        m = self._block_rx().search(self._read_gameinfo(gi))
        # an outdated block (e.g. missing a newly configured extra mount) counts as not mounted
        return bool(m and all(p in m[0] for p in [self.cfg.mount_path, *self.cfg.extra_mounts]))

    def set_mount(self, enable: bool, only_if: Callable[[], bool] | None = None) -> str:
        """Insert our search path ahead of `Game citadel`. The first Game path normally also becomes
        the MOD and DEFAULT_WRITE_PATH, so pin those back to citadel with explicit Mod/Write keys.

        Disabling removes only the dev addon: the extra mounts (the standard mod folder) stay, so
        installed mods keep loading in a normal Steam launch.

        only_if: checked while holding the gameinfo lock; when it returns False nothing is changed
        (lets the watchdog check its launch token and unmount atomically)."""
        gi = self.cfg.gameinfo
        if not gi or not gi.is_file():
            raise AssetError("gameinfo.gi not found")
        with self._gameinfo_lock():
            if only_if is not None and not only_if():
                return "skipped: condition no longer holds; gameinfo.gi left as is"
            original = self._read_gameinfo(gi)
            nl = "\r\n" if "\r\n" in original else "\n"
            text = self._block_rx().sub("", original)
            paths = [self.cfg.mount_path, *self.cfg.extra_mounts] if enable else list(self.cfg.extra_mounts)
            if paths:
                m = re.search(r"^([ \t]*)Game([ \t]+)citadel[ \t]*\r?$", text, re.M)
                if not m:
                    raise AssetError("could not find the 'Game citadel' search path in gameinfo.gi")
                ind, sep = m[1], m[2]
                lines = [self.BEGIN, *(f"Game{sep}{p}" for p in paths), f"Mod{sep}citadel", f"Write{sep}citadel", self.END]
                block = "".join(f"{ind}{line}{nl}" for line in lines)
                text = text[: m.start()] + block + text[m.start() :]
            if text == original:
                return f"{self.cfg.mount_path} already {'mounted' if enable else 'unmounted'}"
            backup = gi.with_name("gameinfo.gi.mcp-bak")
            shutil.copy2(gi, backup)
            if enable and self.cfg.addons_dir:
                self.cfg.addons_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(gi, text.encode("utf-8", "surrogateescape"))
        kept = f"; kept {', '.join(self.cfg.extra_mounts)}" if not enable and self.cfg.extra_mounts else ""
        return (
            f"{'mounted' if enable else 'unmounted'} {self.cfg.mount_path} in gameinfo.gi{kept} "
            f"(previous version saved as {backup.name}). Restart the game to apply."
        )
