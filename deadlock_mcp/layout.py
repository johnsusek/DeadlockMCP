"""Text edits on Panorama layout XML: wrap fragments, add stylesheet includes, inject the bridge.

Layouts are XML-ish (decompiled ones aren't always well-formed), so these are regex edits. Tags are searched in
a copy with comments blanked out (same offsets), so a commented-out <styles>/<scripts>/<root> is never used."""

from __future__ import annotations

import re

from .errors import LayoutError


def _mask(xml: str) -> str:
    """xml with every comment (and an unterminated trailing one) replaced by spaces of the same length."""
    return re.sub(r"<!--.*?(?:-->|\Z)", lambda m: " " * len(m[0]), xml, flags=re.S)


def _open_tag(name: str) -> re.Pattern:
    # <name>, <name attr="..">, <name /> — group 1 is "/" for a self-closing tag; trailing whitespace is included.
    return re.compile(rf"<{name}(?:\s[^>]*?)?(/?)>(\s*)", re.I)


_ROOT, _STYLES, _SCRIPTS = _open_tag("root"), _open_tag("styles"), _open_tag("scripts")


def _insert_child(xml: str, mask: str, tag: re.Pattern, name: str, child: str) -> str | None:
    """Insert `child` as the first child of the first live <name> element (None if there is none)."""
    m = tag.search(mask)
    if not m:
        return None
    if m[1]:  # self-closing <name />: expand it
        return xml[: m.start()] + f"<{name}>\n\t\t{child}\n\t</{name}>" + m[2] + xml[m.end():]
    ws = m[2] or "\n\t\t"
    return xml[: m.end()] + child + ws + xml[m.end():]


def _insert_section(xml: str, after: re.Match, name: str, child: str) -> str:
    return xml[: after.end()] + f"<{name}>\n\t\t{child}\n\t</{name}>\n\t" + xml[after.end():]


def ensure_root(xml: str) -> str:
    """Wrap bare fragments in a full <root> layout document."""
    if _ROOT.search(_mask(xml)):
        return xml
    return f'<root>\n\t<Panel style="width: 100%; height: 100%;" hittest="false">\n{xml}\n\t</Panel>\n</root>\n'


_STYLES_CLOSE = re.compile(r"[ \t]*</styles\s*>", re.I)


def add_stylesheet(xml: str, style_src: str) -> str:
    """Add a stylesheet include as the last one in <styles>: later includes win ties in the cascade, so the
    added sheet can override the stylesheets the layout already includes."""
    inc = f'<include src="{style_src}" />'
    mask = _mask(xml)
    m = _STYLES.search(mask)
    close = _STYLES_CLOSE.search(mask, m.end()) if m and not m[1] else None
    if close:
        lead = "" if xml[: close.start()].endswith("\n") else "\n"
        return xml[: close.start()] + f"{lead}\t\t{inc}\n" + xml[close.start():]
    out = _insert_child(xml, mask, _STYLES, "styles", inc)
    if out is not None:
        return out
    m = _ROOT.search(mask)
    if not m or m[1]:
        raise LayoutError("layout has no <root>")
    return _insert_section(xml, m, "styles", inc)


def inject_bridge(layout_xml: str, domain: str = "m") -> str:
    """Add a <scripts> include of the bridge script to a decompiled Panorama layout."""
    BRIDGE_INCLUDE = f'<include src="file://{{resources}}/scripts/mcp_bridge_{domain}.js" />'
    xml = re.sub(r"^\s*<!--.*?-->\s*", "", layout_xml, count=1, flags=re.S)  # Source 2 Viewer banner
    mask = _mask(xml)
    if "mcp_bridge" in mask:
        raise LayoutError("layout already includes the MCP bridge")
    out = _insert_child(xml, mask, _SCRIPTS, "scripts", BRIDGE_INCLUDE)
    if out is not None:
        return out
    root = _ROOT.search(mask)
    if not root or root[1]:
        raise LayoutError("host layout has no <root>; cannot inject")
    m = re.compile(r"</styles\s*>\s*", re.I).search(mask, root.end()) or root
    return _insert_section(xml, m, "scripts", BRIDGE_INCLUDE)


# --- stylesheet includes ---------------------------------------------------------------------------------------

_INCLUDE_SRC = re.compile(r"""<include\s[^>]*?\bsrc\s*=\s*(["'])(.*?)\1""", re.I | re.S)
_STYLES_SECTION = re.compile(r"<styles(?:\s[^>]*?)?(?<!/)>(.*?)(?:</styles\s*>|\Z)", re.I | re.S)


def normalize_stylesheet(src: str) -> str:
    """`s2r://panorama/styles/X.vcss` / `file://{resources}/styles/x.css` / `panorama/styles/x.vcss_c`
    → `styles/x.css` (relative to panorama/, source-style, lower-case)."""
    s = src.strip().replace("\\", "/").lower()
    s = re.sub(r"^(?:s2r://|file://\{resources\}/)", "", s)
    s = re.sub(r"^/?panorama/", "", s)
    return re.sub(r"\.(?:vcss_c|vcss|css)$", ".css", s)


def _style_includes(xml: str) -> list[re.Match]:
    """src matches (group 2, offsets into xml) of the live includes inside <styles> sections."""
    mask = _mask(xml)
    out = []
    for sec in _STYLES_SECTION.finditer(mask):
        out += _INCLUDE_SRC.finditer(mask, sec.start(1), sec.end(1))
    return out


def stylesheet_includes(xml: str) -> list[str]:
    """The layout's stylesheet includes, e.g. ['styles/citadel_base_styles.css', 'styles/hud/x.css']."""
    return [normalize_stylesheet(m[2]) for m in _style_includes(xml)]


def replace_stylesheet(xml: str, sheet: str, new_src: str) -> str:
    """Point the include of stylesheet `sheet` (e.g. 'styles/hud_common.css') at `new_src` instead."""
    want = normalize_stylesheet(sheet)
    hits = [m for m in _style_includes(xml) if normalize_stylesheet(m[2]) == want]
    if not hits:
        have = ", ".join(stylesheet_includes(xml)) or "none"
        raise LayoutError(f"layout does not include stylesheet {want!r}; its stylesheets: {have}")
    for m in reversed(hits):
        xml = xml[: m.start(2)] + new_src + xml[m.end(2):]
    return xml
