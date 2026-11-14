"""Game asset access: list/decompile shipped Panorama files, compile sources via CSDK, mount the addon."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from .config import Config
from .vpk import VPK, write_vpk

COMPILED_EXT = {".xml": ".vxml_c", ".css": ".vcss_c", ".js": ".vjs_c"}
SOURCE_EXT = {v: k for k, v in COMPILED_EXT.items()}


class AssetError(RuntimeError):
    pass


def winpath(p: Path) -> str:
    return "Z:" + str(p.resolve()).replace("/", "\\")


async def _run(cmd: list[str], timeout: float, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str]:
    """Run a process, capturing output via a temp file and waiting on the process itself (a
    persistent wineserver inherits pipes, so waiting for pipe EOF would hang)."""
    import tempfile

    with tempfile.TemporaryFile() as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise AssetError(f"timed out after {timeout}s: {' '.join(cmd)}") from None
        out.seek(0)
        return proc.returncode or 0, out.read().decode("utf-8", "replace")


class Assets:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._vpk: VPK | None = None

    # ------------------------------------------------------------------ shipped files
    @property
    def vpk(self) -> VPK:
        if self._vpk is None:
            if not self.cfg.pak_dir_vpk or not self.cfg.pak_dir_vpk.is_file():
                raise AssetError("Deadlock pak01_dir.vpk not found; set DEADLOCK_DIR")
            self._vpk = VPK(self.cfg.pak_dir_vpk)
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
        p = path.strip().lstrip("/").lower()
        if not p.startswith("panorama/"):
            p = "panorama/" + p
        stem, ext = os.path.splitext(p)
        if ext in COMPILED_EXT:
            return stem + COMPILED_EXT[ext]
        if ext in SOURCE_EXT:
            return p
        kind = p.split("/")[1] if p.count("/") >= 2 else ""
        return p + {"layout": ".vxml_c", "styles": ".vcss_c", "scripts": ".vjs_c"}.get(kind, ".vxml_c")

    async def decompile(self, path: str, refresh: bool = False) -> str:
        """Return decompiled source text for a shipped Panorama file (via Source2Viewer-CLI)."""
        name = self.normalize(path)
        if name not in self.vpk:
            raise AssetError(f"{name} is not in pak01_dir.vpk")
        src_ext = SOURCE_EXT[os.path.splitext(name)[1]]
        cache = self.cfg.cache_dir / "decompiled" / (os.path.splitext(name)[0] + src_ext)
        if cache.is_file() and not refresh:
            return cache.read_text(encoding="utf-8", errors="replace")

        cli = self.cfg.s2v_cli
        if not cli or not cli.is_file():
            raise AssetError(
                "Source2Viewer-CLI not found. Download cli-linux-x64.zip from "
                "https://github.com/ValveResourceFormat/ValveResourceFormat/releases into tools/s2v/ or set S2V_CLI."
            )
        work = self.cfg.cache_dir / "work" / str(time.time_ns())
        work.mkdir(parents=True)
        try:
            compiled = work / os.path.basename(name)
            compiled.write_bytes(self.vpk.read(name))
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
        cache.write_text(text, encoding="utf-8")
        return text

    # ------------------------------------------------------------------ compiling (CSDK 13)
    @property
    def csdk_content_root(self) -> Path:
        if not self.cfg.csdk_dir:
            raise AssetError("CSDK_DIR is not set (path to your CSDK 13 install)")
        return Path(os.environ.get("CSDK_CONTENT", self.cfg.csdk_dir / "content/citadel_addons" / self.cfg.addon_name))

    @property
    def csdk_game_root(self) -> Path:
        if not self.cfg.csdk_dir:
            raise AssetError("CSDK_DIR is not set (path to your CSDK 13 install)")
        return Path(os.environ.get("CSDK_GAME", self.cfg.csdk_dir / "game/citadel_addons" / self.cfg.addon_name))

    _wineserver_started = 0.0

    def _ensure_wineserver(self, env: dict) -> None:
        """Keep a persistent wineserver so each compile skips Wine start-up (~2.5s → ~1s)."""
        if not self.cfg.wine or time.time() - self._wineserver_started < 500:
            return
        ws = self.cfg.wine.with_name("wineserver")
        if ws.is_file():
            import subprocess

            subprocess.run([str(ws), "-p", "600"], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
            Assets._wineserver_started = time.time()

    def _rc_command(self, rc: Path, src: Path) -> tuple[list[str], dict]:
        env = dict(os.environ, WINEDEBUG="-all", WINEPREFIX=str(self.cfg.wineprefix))
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
        rc = self.cfg.resourcecompiler
        if not rc or not rc.is_file():
            raise AssetError(f"resourcecompiler.exe not found (looked at {rc}); check CSDK_DIR")
        results = []
        norm = lambda r: (r if r.strip().lstrip("/").startswith("panorama/") else "panorama/" + r.strip().lstrip("/"))
        prepared = []
        for rel_source, text in files.items():
            rel = norm(rel_source.strip().lstrip("/").replace("\\", "/"))
            ext = os.path.splitext(rel)[1].lower()
            if ext not in COMPILED_EXT:
                raise AssetError(f"unsupported source type {ext!r} (use .xml, .css or .js)")
            src = self.csdk_content_root / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(text, encoding="utf-8")
            prepared.append((rel, src))
        root_set = {norm(r) for r in roots} if roots else None
        for rel, src in prepared:
            if root_set is not None and rel not in root_set:
                continue
            ext = os.path.splitext(rel)[1].lower()
            rel_c = os.path.splitext(rel)[0] + COMPILED_EXT[ext]
            out = self.csdk_game_root / rel_c
            before = out.stat().st_mtime if out.exists() else 0.0

            cmd, env = self._rc_command(rc, src)
            self._ensure_wineserver(env)
            code, log = await _run(cmd, timeout=300, cwd=rc.parent, env=env)
            log = "\n".join(
                l for l in log.splitlines()
                if l.strip() and not re.match(r"(ntsync|Creating render device|PIX|Disable asynchronous|Leaked KeyValues)", l)
            )
            m = re.search(r"(\d+) compiled, (\d+) failed", log)
            ok = bool(m and int(m[1]) >= 1 and int(m[2]) == 0) and out.exists() and out.stat().st_mtime > before
            if not ok:
                errs = [l.strip() for l in log.splitlines()
                        if ("COMPILE ERROR" in l or "error" in l.lower() and ":" in l)
                        and not l.strip().startswith(("Look for", "ERROR: 0 compiled"))]
                errs = [re.sub(r"^Z:\\.*?\\citadel_addons\\[^\\]+\\", "", e).replace("\\", "/") for e in errs]
                detail = "\n".join(dict.fromkeys(errs)) if errs else log[-4000:]
                raise AssetError(f"compile failed for {rel}:\n{detail}")
            results.append({"source": rel, "compiled": str(out), "log": log[-1500:]})
        out = {"files": results}
        if install:
            out["vpk"] = self.pack_addon()
        return out

    async def compile(self, rel_source: str, text: str, install: bool = True) -> dict:
        return (await self.compile_many({rel_source: text}, install))["files"][0]

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
        roots = [self.cfg.addons_dir] if self.cfg.addons_dir else []
        if self.cfg.csdk_dir:
            roots += [self.csdk_content_root, self.csdk_game_root]
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
            rel_c = os.path.splitext(rel)[0] + COMPILED_EXT[os.path.splitext(rel)[1].lower()]
            src = self.csdk_game_root / rel_c
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
        files = self.addon_files()
        self.cfg.addons_dir.mkdir(parents=True, exist_ok=True)
        dest = self.cfg.addons_dir / "pak01_dir.vpk"
        write_vpk(dest, {rel: p.read_bytes() for rel, p in files.items()})
        return {"vpk": str(dest), "files": sorted(files)}

    def uninstall(self, rel_compiled: str) -> bool:
        p = self.csdk_game_root / rel_compiled
        if not p.is_file():
            return False
        p.unlink()
        self.pack_addon()
        return True

    # ------------------------------------------------------------------ gameinfo mounting
    BEGIN, END = "// deadlock-mcp begin", "// deadlock-mcp end"

    def _block_rx(self) -> re.Pattern:
        return re.compile(r"^[ \t]*" + re.escape(self.BEGIN) + r".*?" + re.escape(self.END) + r"[ \t]*\r?\n", re.M | re.S)

    def mount_status(self) -> bool:
        gi = self.cfg.gameinfo
        if not (gi and gi.is_file()):
            return False
        m = self._block_rx().search(gi.read_text(errors="replace"))
        # an outdated block (e.g. missing a newly configured extra mount) counts as not mounted
        return bool(m and all(p in m[0] for p in [self.cfg.mount_path, *self.cfg.extra_mounts]))

    def set_mount(self, enable: bool) -> str:
        """Insert our search path ahead of `Game citadel`. The first Game path normally also becomes
        the MOD and DEFAULT_WRITE_PATH, so pin those back to citadel with explicit Mod/Write keys.

        Disabling removes only the dev addon: the extra mounts (the standard mod folder) stay, so
        installed mods keep loading in a normal Steam launch."""
        gi = self.cfg.gameinfo
        if not gi or not gi.is_file():
            raise AssetError("gameinfo.gi not found")
        original = gi.read_text(errors="replace")
        text = self._block_rx().sub("", original)
        paths = [self.cfg.mount_path, *self.cfg.extra_mounts] if enable else list(self.cfg.extra_mounts)
        if paths:
            m = re.search(r"^([ \t]*)Game([ \t]+)citadel[ \t]*$", text, re.M)
            if not m:
                raise AssetError("could not find the 'Game citadel' search path in gameinfo.gi")
            ind, sep = m[1], m[2]
            block = (
                f"{ind}{self.BEGIN}\n"
                + "".join(f"{ind}Game{sep}{p}\n" for p in paths)
                + f"{ind}Mod{sep}citadel\n"
                f"{ind}Write{sep}citadel\n"
                f"{ind}{self.END}\n"
            )
            text = text[: m.start()] + block + text[m.start() :]
        if text == original:
            return f"{self.cfg.mount_path} already {'mounted' if enable else 'unmounted'}"
        backup = gi.with_name("gameinfo.gi.mcp-bak")
        shutil.copy2(gi, backup)
        if enable and self.cfg.addons_dir:
            self.cfg.addons_dir.mkdir(parents=True, exist_ok=True)
        gi.write_text(text)
        kept = f"; kept {', '.join(self.cfg.extra_mounts)}" if not enable and self.cfg.extra_mounts else ""
        return (
            f"{'mounted' if enable else 'unmounted'} {self.cfg.mount_path} in gameinfo.gi{kept} "
            f"(previous version saved as {backup.name}). Restart the game to apply."
        )
