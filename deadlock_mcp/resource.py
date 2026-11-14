"""Build compiled Panorama script resources (.vjs_c) without resourcecompiler.

A compiled .vjs_c is: 16-byte header, block table (RED2, DATA), RED2 metadata, then a DATA block
holding the raw JavaScript source. We reuse the header+RED2 from a template compiled once by
resourcecompiler and swap in new DATA.
"""

from __future__ import annotations

import struct
from importlib import resources

_TEMPLATE: bytes | None = None


def _template() -> bytes:
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = resources.files(__package__).joinpath("templates/script.vjs_c").read_bytes()
    return _TEMPLATE


def make_vjs(js: str, template: bytes | None = None) -> bytes:
    t = template or _template()
    _size, _hv, _tv, boff, count = struct.unpack_from("<IHHII", t, 0)
    head = bytearray()
    data_entry = None
    for i in range(count):
        at = 8 + boff + i * 12
        typ, off, _sz = struct.unpack_from("<4sII", t, at)
        if typ == b"DATA":
            data_entry = (at, at + 4 + off)
    if not data_entry:
        raise ValueError("template has no DATA block")
    table_at, data_at = data_entry
    if data_at + struct.unpack_from("<I", t, table_at + 8)[0] != len(t):
        raise ValueError("DATA must be the last block in the template")
    body = js.encode("utf-8")
    head = bytearray(t[:data_at])
    struct.pack_into("<I", head, table_at + 8, len(body))
    struct.pack_into("<I", head, 0, len(head) + len(body))
    return bytes(head) + body


_INBOX_TEMPLATE: bytes | None = None
INBOX_TOKEN = b"mcp_inbox"  # 9 chars; replaced by a same-length unique name per request


def request_name(serial: int, domain: str = "m") -> str:
    assert len(domain) == 1 and domain.isalpha()
    return f"{domain}{serial:08d}"


def make_request_layout(serial: int, template: bytes | None = None, domain: str = "m") -> bytes:
    """Compiled layout `panorama/layout/<name>.vxml_c` that includes `panorama/scripts/<name>.vjs`.

    Panorama caches layouts and scripts by path for the whole session, so every request gets a
    fresh name. The template was compiled once by resourcecompiler; paths are patched in place."""
    global _INBOX_TEMPLATE
    if template is None:
        if _INBOX_TEMPLATE is None:
            _INBOX_TEMPLATE = resources.files(__package__).joinpath("templates/inbox.vxml_c").read_bytes()
        template = _INBOX_TEMPLATE
    name = request_name(serial, domain).encode()
    assert len(name) == len(INBOX_TOKEN)
    if template.count(INBOX_TOKEN) != 2:
        raise ValueError("unexpected inbox layout template")
    return template.replace(INBOX_TOKEN, name)
