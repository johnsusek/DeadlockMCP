"""Minimal read-only VPK (v1/v2) directory reader."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    archive: int
    offset: int
    length: int
    preload: bytes
    data_base: int


class VPK:
    def __init__(self, dir_path: Path):
        self.path = Path(dir_path)
        self.entries: dict[str, Entry] = {}
        self._load()

    def _load(self) -> None:
        data = self.path.read_bytes()
        sig, ver, tree_size = struct.unpack_from("<III", data, 0)
        if sig != 0x55AA1234:
            raise ValueError(f"{self.path} is not a VPK directory file")
        pos = 12 + (16 if ver == 2 else 0)
        data_base = pos + tree_size

        def cstr() -> str:
            nonlocal pos
            end = data.index(b"\0", pos)
            s = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
            return s

        while ext := cstr():
            while d := cstr():
                while name := cstr():
                    _crc, pre, arc, off, ln, _term = struct.unpack_from("<IHHIIH", data, pos)
                    pos += 18
                    preload = data[pos : pos + pre]
                    pos += pre
                    d2 = d.strip()
                    fn = f"{name}.{ext}" if ext.strip() else name
                    p = f"{d2}/{fn}" if d2 else fn
                    self.entries[p.lower()] = Entry(arc, off, ln, preload, data_base)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self.entries

    def names(self) -> list[str]:
        return list(self.entries)

    def read(self, name: str) -> bytes:
        e = self.entries[name.lower()]
        if e.length == 0:
            return e.preload
        if e.archive == 0x7FFF:
            path, off = self.path, e.data_base + e.offset
        else:
            path = self.path.with_name(self.path.name.replace("_dir.vpk", f"_{e.archive:03d}.vpk"))
            off = e.offset
        with open(path, "rb") as f:
            f.seek(off)
            return e.preload + f.read(e.length)


def write_vpk(path: Path, files: dict[str, bytes]) -> None:
    """Write a single-file VPK v2 (all data embedded in the _dir file, archive index 0x7fff)."""
    import hashlib
    import zlib

    tree: dict[str, dict[str, list[tuple[str, bytes]]]] = {}
    for name, data in files.items():
        name = name.replace("\\", "/").lower().lstrip("/")
        d, _, fn = name.rpartition("/")
        stem, dot, ext = fn.rpartition(".")
        if not dot:
            stem, ext = fn, ""
        tree.setdefault(ext or " ", {}).setdefault(d or " ", []).append((stem, data))

    t = bytearray()
    blob = bytearray()
    for ext in sorted(tree):
        t += ext.encode() + b"\0"
        for d in sorted(tree[ext]):
            t += d.encode() + b"\0"
            for stem, data in sorted(tree[ext][d]):
                t += stem.encode() + b"\0"
                t += struct.pack("<IHHIIH", zlib.crc32(data) & 0xFFFFFFFF, 0, 0x7FFF, len(blob), len(data), 0xFFFF)
                blob += data
            t += b"\0"
        t += b"\0"
    t += b"\0"

    header = struct.pack("<IIIIIII", 0x55AA1234, 2, len(t), len(blob), 0, 48, 0)
    tree_md5 = hashlib.md5(t).digest()
    archive_md5_md5 = hashlib.md5(b"").digest()
    whole = hashlib.md5(header + t + blob + tree_md5 + archive_md5_md5).digest()
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(header + t + blob + tree_md5 + archive_md5_md5 + whole)
    tmp.replace(path)
