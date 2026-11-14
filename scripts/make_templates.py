"""Regenerate the compiled request templates in deadlock_mcp/templates/ (needs CSDK 13). Usage:
    .venv/bin/python scripts/make_templates.py

Requests are written without the compiler at runtime (deadlock_mcp/resource.py): script.vjs_c is a compiled
script whose DATA block gets swapped for the request JS, inbox.vxml_c a compiled layout whose two `mcp_inbox`
paths get patched to the request's name. Only needed if Valve changes the resource format.
"""

import asyncio
from pathlib import Path

from deadlock_mcp.assets import Assets
from deadlock_mcp.config import Config
from deadlock_mcp.resource import INBOX_TOKEN, make_request_layout, make_vjs

INBOX_LAYOUT = "panorama/layout/mcp_inbox.xml"
INBOX_SCRIPT = "panorama/scripts/mcp_inbox.js"
INBOX_LAYOUT_XML = """<root>
\t<scripts>
\t\t<include src="file://{resources}/scripts/mcp_inbox.js" />
\t</scripts>
\t<Panel />
</root>
"""

OUT = Path(__file__).resolve().parent.parent / "deadlock_mcp/templates"


async def main() -> None:
    assets = Assets(Config())
    files = {INBOX_SCRIPT: "// template\n", INBOX_LAYOUT: INBOX_LAYOUT_XML}
    await assets.compile_many(files, install=False, roots=[INBOX_LAYOUT])  # the layout's run builds the script
    game = assets.csdk_game_root
    layout = (game / "panorama/layout/mcp_inbox.vxml_c").read_bytes()
    script = (game / "panorama/scripts/mcp_inbox.vjs_c").read_bytes()
    assert layout.count(INBOX_TOKEN) == 2, "layout template must name mcp_inbox exactly twice"
    make_request_layout(1, layout, "m")
    make_vjs("1", script)  # raises if DATA is not the last block
    for name, data in (("inbox.vxml_c", layout), ("script.vjs_c", script)):
        dest = OUT / name  # tracked in git, so no backup here (templates/* would ship it)
        dest.write_bytes(data)
        print(f"wrote {dest} ({len(data)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
