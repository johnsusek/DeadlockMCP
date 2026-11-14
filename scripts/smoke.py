"""Live smoke test against a running Deadlock with the bridge installed. Usage:
    .venv/bin/python scripts/smoke.py [step ...]
Steps: status tree find eval xml css shot cleanup   (default: all but shot)
"""

import asyncio
import os
import tempfile
import sys
import time

from deadlock_mcp import server as s
from mcp.server.mcpserver import Image

SHOT = os.path.join(tempfile.gettempdir(), "deadlock_mcp_smoke.jpg")


async def main(steps):
    def show(title, out):
        print(f"\n=== {title}\n{out}"[:3000])

    async def timed(title, coro):
        t = time.monotonic()
        out = await coro
        show(f"{title}  ({(time.monotonic() - t) * 1000:.0f} ms)", out)
        return out

    if "status" in steps:
        await timed("status", s.status())
    if "tree" in steps:
        await timed("tree root depth 2", s.panel_tree("root", 2, 60))
    if "find" in steps:
        await timed("find Label (first 3)", s.panel_find("Label", max_results=3))
    if "eval" in steps:
        await timed("eval 1+1", s.js_eval("1+1"))
        await timed("eval root", s.js_eval("$root.paneltype + ' / ' + $root.id"))
        await timed("eval statements", s.js_eval("var n = 0;\n$children($root).forEach(function(){ n++; });\nreturn n;"))
        await timed("eval runtime error", s.js_eval("nope.x"))
        await timed("eval syntax error", s.js_eval("(", timeout=3))
        await timed("eval unicode", s.js_eval("'héllo ☃'.toUpperCase()"))
        await timed("eval big result", s.js_eval("'z'.repeat(3000).length + ':' + 'y'.repeat(2000)"))
        await timed("eval globals", s.js_eval("Object.getOwnPropertyNames(globalThis).sort().join(',')"))
    if "xml" in steps:
        await timed("xml_load", s.xml_load(
            '<Label id="McpSmokeLabel" class="McpSmoke" text="MCP live edit works" />',
            target="root", mode="append", id="McpSmokePanel",
            css=".McpSmoke { font-size: 56px; color: #00ff88; horizontal-align: center; vertical-align: center; "
                "transition: color 0.3s; } .McpSmoke:hover { color: #ffffff; }"))
    if "css" in steps:
        await timed("css_apply", s.css_apply("#McpSmokeLabel { color: #ff3366; font-size: 72px; }", sheet="smoke"))
        await timed("css check", s.js_eval("$find('McpSmokeLabel').style.color"))
    if "shot" in steps:
        img = await s.screenshot()
        if isinstance(img, Image):
            open(SHOT, "wb").write(img.data)
            print("\n=== screenshot saved", SHOT)
        else:
            show("screenshot", img)
    if "cleanup" in steps:
        await timed("css_clear", s.css_clear("smoke"))
        await timed("delete", s.panel_delete("McpSmokePanel"))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["status", "tree", "find", "eval", "xml", "css", "cleanup"]))
