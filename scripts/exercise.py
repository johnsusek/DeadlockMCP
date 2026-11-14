"""Exercise many tool paths against a running game (HUD visible). Prints each result."""
import asyncio, json, sys, time
from deadlock_mcp import server as s

async def step(title, coro):
    t = time.monotonic()
    try:
        out = await coro
    except Exception as e:  # tool functions should never raise
        out = f"!!! RAISED {type(e).__name__}: {e}"
    print(f"\n=== {title} ({(time.monotonic()-t)*1000:.0f} ms)\n{str(out)[:1200]}")
    return out

WIDGET_XML = """<root>
  <Panel class="McpCard">
    <Label id="McpTitle" class="McpTitle" text="MCP ★ Stoppuhr — 測試" />
    <Label id="McpTicker" class="McpTicker" text="0" />
    <Button id="McpBtn" class="McpBtn"><Label text="Click me" /></Button>
  </Panel>
</root>"""
WIDGET_CSS = """@define accent: #39e0ff;
.McpCard { horizontal-align: right; vertical-align: center; margin-right: 40px; width: 420px; flow-children: down;
  padding: 18px; background-color: #0b1620e6; border: 2px solid accent; border-radius: 12px; }
.McpTitle { color: accent; font-size: 26px; font-weight: bold; }
.McpTicker { color: white; font-size: 40px; }
.McpBtn { margin-top: 10px; padding: 8px 16px; background-color: #1d3a4a; border-radius: 6px;
  transition-property: background-color; transition-duration: 0.2s; }
.McpBtn:hover { background-color: accent; }
.McpBtn Label { color: white; font-size: 20px; }
"""

async def main(steps):
    if "xml" in steps:
        await step("xml_load widget", s.xml_load(WIDGET_XML, target="Hud", id="McpWidget", css=WIDGET_CSS, ui="hud"))
        await step("xml_load same id again (replace)", s.xml_load(WIDGET_XML, target="Hud", id="McpWidget", css=WIDGET_CSS, ui="hud"))
        await step("count widgets", s.js_eval("$query([{comb:' ', id:'McpWidget'}]).length", ui="hud"))
    if "timer" in steps:
        await step("keep_context timer + click handler", s.js_eval("""
var start = Date.now();
function tick() {
  var t = $find('McpTicker');
  if (!t || !t.IsValid()) { return; }
  t.text = ((Date.now() - start) / 1000).toFixed(1) + ' s';
  $.Schedule(0.1, tick);
}
tick();
$find('McpBtn').SetPanelEvent('onactivate', function () { $find('McpTitle').text = 'clicked @ ' + new Date().toISOString().substr(11, 8); });
'ok'""", ui="hud", keep_context=True))
        await asyncio.sleep(1.5)
        await step("ticker advanced?", s.js_eval("$find('McpTicker').text", ui="hud"))
    if "css" in steps:
        await step("sheet A", s.css_apply(".McpTicker { color: #ff5050; }", sheet="A", ui="hud"))
        await step("sheet B overrides", s.css_apply(".McpTicker { color: #50ff50; }", sheet="B", ui="hud"))
        await step("color now (B)", s.js_eval("$find('McpTicker').style.color", ui="hud"))
        await step("clear B", s.css_clear("B", ui="hud"))
        await asyncio.sleep(1.3)
        await step("color after clear B (expect A red)", s.js_eval("$find('McpTicker').style.color", ui="hud"))
        await step("invalid value", s.css_apply(".McpTicker { color: notacolor; font-size: 12qq; opacity: 0.9; }", sheet="bad", ui="hud"))
        await step("non-persist", s.css_apply("#McpTitle { letter-spacing: 3px; }", sheet="np", persist=False, ui="hud"))
        await step("clear all", s.css_clear(ui="hud"))
    if "errors" in steps:
        await step("broken xml", s.xml_load("<root><Panel><Label text='x'></Panel></root>", target="Hud", ui="hud"))
        await step("bad target", s.js_eval("1", target="NoSuchPanel", ui="hud"))
        await step("throw string", s.js_eval("throw 'plain string'", ui="hud"))
        await step("cyclic", s.js_eval("var a = {n: 1}; a.self = a; return a;", ui="hud"))
        await step("undefined", s.js_eval("undefined", ui="hud"))
        await step("panel array", s.js_eval("$children($find('McpWidget'))", ui="hud"))
        await step("bad ui", s.js_eval("1", ui="nope"))
        await step("Intl guard (must NOT crash)", s.js_eval("new Date().toLocaleTimeString()", ui="hud"))
        await step("Intl proxy guard", s.js_eval("Intl.NumberFormat().format(1)", ui="hud"))
        await step("still alive", s.js_eval("'alive'", ui="hud"))
        await step("unsupported selector", s.panel_find("Label:hover"))
    if "tree" in steps:
        out = await step("big tree", s.panel_tree("Hud", depth=6, max_nodes=1500, ui="hud"))
        try:
            d = json.loads(out); print("tree json ok, truncated =", d["truncated"], "bytes", len(out))
        except Exception as e:
            print("tree parse failed", e)

asyncio.run(main(sys.argv[1:] or ["xml", "timer", "css", "errors", "tree"]))
