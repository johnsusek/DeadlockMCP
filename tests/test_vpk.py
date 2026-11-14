import os
import threading
import zlib

import pytest

from deadlock_mcp.vpk import VPK, write_vpk


def test_entries_keep_crc(tmp_path):
    write_vpk(tmp_path / "pak01_dir.vpk", {"panorama/a.vxml_c": b"hello", "b.txt": b""})
    v = VPK(tmp_path / "pak01_dir.vpk")
    assert v.crc("panorama/A.vxml_c") == zlib.crc32(b"hello") and v.crc("b.txt") == zlib.crc32(b"")
    st = (tmp_path / "pak01_dir.vpk").stat()
    assert v.stamp == (st.st_mtime_ns, st.st_size)


def test_concurrent_writes_use_unique_temps(tmp_path):
    dest = tmp_path / "pak01_dir.vpk"
    payloads = [{f"f{i}.txt": bytes([i]) * (1000 + i)} for i in range(8)]
    threads = [threading.Thread(target=write_vpk, args=(dest, p)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    v = VPK(dest)  # whichever won, the file is whole
    (name,) = v.names()
    assert {name: v.read(name)} in payloads
    assert os.listdir(tmp_path) == ["pak01_dir.vpk"] and (dest.stat().st_mode & 0o777) == 0o644


def test_failed_write_leaves_no_temp(tmp_path, monkeypatch):
    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_vpk(tmp_path / "pak01_dir.vpk", {"a.txt": b"a"})
    assert os.listdir(tmp_path) == []
