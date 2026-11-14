// Live HUD tweak (run via js_eval(..., ui='hud', keep_context=True)):
// active stat boosts as one gap-free left→right row at bottom-center during play;
// the game's original 3-column list (all stats) while TAB / detail view / shop is open.
var sl = $find('StatList'), host = sl.GetParent();
var token = String(Date.now());
sl.SetAttributeString('mcp_statlist_watch', token);   // a newer watcher retires this one
var SHOW_ALL = ['gDetailView', 'gScoreboardOpen', 'gShopOpen'];
var COLS = ['WeaponColumn', 'SpiritColumn', 'VitalityColumn'];
var cols = COLS.map(function (id) { return sl.FindChildTraverse(id); });
var state = null;

function entries() {
  var out = [];
  cols.forEach(function (c) { MCP.children(c).forEach(function (e) { if (e.BHasClass('miniModifier')) { out.push(e); } }); });
  return out;
}
// Preview hook: entry ids listed (comma-separated) in StatList's "mcp_preview" attribute count as active.
function previewed(e) { return (',' + sl.GetAttributeString('mcp_preview', '') + ',').indexOf(',' + e.id + ',') >= 0; }
function isActive(e) { return e.BHasClass('isPositive') || e.BHasClass('isNegative') || previewed(e); }
// What the game's own CSS would show in the full (TAB) view. We must set this explicitly: once
// style.visibility has been 'collapse', resetting it to null leaves the panel's visible flag false.
function gameVisible(e) {
  var hiddenSecondary = e.BHasClass('secondaryStat') && (e.BHasClass('isBaseValue') || e.BHasClass('isZero'));
  return !hiddenSecondary || e.BHasClass('shouldShow');
}
function showingAll() {
  for (var i = 0; i < SHOW_ALL.length; i++) { if (host.BHasClass(SHOW_ALL[i])) { return SHOW_ALL[i]; } }
  return '';
}

function toRow() {
  sl.style.horizontalAlign = 'center';
  sl.style.marginBottom = '105px';
  sl.style.width = 'fit-children';
  sl.style.height = 'fit-children';
  cols.forEach(function (c) {
    c.style.flowChildren = 'right';
    c.style.width = 'fit-children';
    c.style.height = 'fit-children';
    c.style.verticalAlign = 'bottom';
  });
  entries().forEach(function (e) { e.style.width = 'fit-children'; e.style.marginRight = '6px'; });
}
function toOriginal() {
  ['horizontalAlign', 'marginBottom', 'width', 'height'].forEach(function (k) { MCP.resetStyle(sl, k); });
  cols.forEach(function (c) {
    ['flowChildren', 'width', 'height', 'verticalAlign'].forEach(function (k) { MCP.resetStyle(c, k); });
  });
  entries().forEach(function (e) {
    ['width', 'marginRight', 'opacity'].forEach(function (k) { MCP.resetStyle(e, k); });
  });
}

function update() {
  if (!sl.IsValid() || sl.GetAttributeString('mcp_statlist_watch', '') !== token) { return; }
  var all = showingAll();
  if (all !== state) {
    state = all;
    if (all) { toOriginal(); } else { toRow(); }
    $.Msg('@@MCPLOG statlist mode=' + (all || 'row'));
  }
  if (!all) {   // keep only active boosts in the row
    entries().forEach(function (e) {
      var want = isActive(e) ? (previewed(e) ? 'preview' : 'visible') : 'collapse';
      if (e.GetAttributeString('mcp_vis', '') !== want) {
        e.style.visibility = want === 'collapse' ? 'collapse' : 'visible';
        if (want === 'preview') { e.style.opacity = '1'; } else { MCP.resetStyle(e, 'opacity'); }
        e.SetAttributeString('mcp_vis', want);
      }
    });
  } else {     // full view: mirror the game's own visibility rules
    entries().forEach(function (e) {
      var want = gameVisible(e) ? 'tab-visible' : 'tab-collapse';
      if (e.GetAttributeString('mcp_vis', '') !== want) {
        e.style.visibility = want === 'tab-visible' ? 'visible' : 'collapse';
        e.SetAttributeString('mcp_vis', want);
      }
    });
  }
  $.Schedule(0.05, update);
}
update();
'watching: row mode during play, original layout on ' + SHOW_ALL.join('/');
