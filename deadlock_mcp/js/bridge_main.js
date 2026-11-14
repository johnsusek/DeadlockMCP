// Deadlock MCP — persistent bridge (appended to core.js, compiled into the host layout's script).
// Polls for request layouts written by the MCP server and loads each into a hidden panel.
(function () {
  var VERSION = "__MCP_VERSION__";
  var DOMAIN = "__MCP_DOMAIN__";
  var POLL = __MCP_POLL__;
  var ctx = $.GetContextPanel();
  ctx.SetAttributeString("mcp_bridge", "1");

  // Serials are time-based per session so stale request files from older sessions never match.
  var base = (Math.floor(Date.now() / 1000) % 900000) * 100 + 1;
  var next = base;
  var hosts = [];
  var KEEP_HOSTS = 64;

  function pad8(n) { var t = String(n); while (t.length < 8) { t = "0" + t; } return t; }

  var lastPollError = "";

  function poll() {
    try {
      // Drain everything that is ready this tick (bounded).
      for (var guard = 0; guard < 16; guard++) {
        var name = DOMAIN + pad8(next);
        if (!$.BImageFileExists("s2r://panorama/layout/" + name + ".vxml")) { break; }
        $.Msg("@@MCPLOG took " + DOMAIN + next);
        next++;
        var host = $.CreatePanel("Panel", ctx, "McpReq" + name);
        host.visible = false;
        host.hittest = false;
        host.BLoadLayout("file://{resources}/layout/" + name + ".xml", false, false);
        if (host.GetAttributeString("mcp_keep", "") !== "1") {
          hosts.push(host);
          while (hosts.length > KEEP_HOSTS) {
            var old = hosts.shift();
            if (old.IsValid() && old.GetAttributeString("mcp_keep", "") !== "1") { old.DeleteAsync(0); }
          }
        }
      }
      lastPollError = "";
    } catch (e) {
      // Log each distinct error once instead of every tick; a recurrence after recovery is logged again.
      var msg = String(e);
      if (msg !== lastPollError) { $.Msg("@@MCPLOG poll error: " + msg); }
      lastPollError = msg;
    }
    $.Schedule(POLL, poll);
  }

  // selector → matched panels for the re-apply loop; entries idle for 30s (e.g. selectors of
  // cleared sheets) are dropped so they don't keep panels alive.
  var matchCache = {};
  function reapplyLoop() {
    try { MCP.reapplySheets(matchCache); } catch (e) {}
    try { MCP.pruneCache(matchCache, 30000); } catch (e) {}
    $.Schedule(1.0, reapplyLoop);
  }

  $.Msg("@@MCPLOG ready domain=" + DOMAIN + " version=" + VERSION + " base=" + base);
  $.Schedule(POLL, poll);
  $.Schedule(0.5, reapplyLoop);
})();
