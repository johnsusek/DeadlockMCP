"""Python side of the live-edit bridge.

Transport (verified against retail Deadlock, Nov 2026):
  in  — the MCP server writes a fresh compiled layout + script pair `panorama/{layout,scripts}/mNNNNNNNN`
        as loose files in the mounted addon dir. The persistent bridge (compiled into the host layout)
        probes for the next serial with $.BImageFileExists every 0.1s and BLoadLayout()s it.
        Fresh names are required: Panorama caches layouts/scripts by path for the whole session.
  out — scripts reply with $.Msg lines "@@MCP <serial> <k>/<n> <json slice>", read from console.log
        (-condebug). Engine-level script errors ("JS Exception!") are matched back to the serial.

Deadlock's Panorama V8 forbids eval/new Function, so user code for js_eval is compiled into the
request script as a real function.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import __version__
from .consolelog import ConsoleLog
from .resource import make_request_layout, make_vjs, request_name

REPLY_RX = re.compile(r"@@MCP ([a-z])(\d+) (\d+)/(\d+) (.*)$")
READY_RX = re.compile(r"@@MCPLOG ready domain=([a-z]) version=(\S+) base=(\d+)")
TOOK_RX = re.compile(r"@@MCPLOG took ([a-z])(\d+)")
SCRIPT_FILE_RX = re.compile(r"/([a-z])(\d{8})\.vjs")

# UI roots that get a bridge: domain letter -> (friendly name, host layout). Each top-level
# Panorama window is a separate panel tree, so each needs its own bridge.
HOSTS = {
    "h": ("hud", "panorama/layout/base_hud"),
    "d": ("dashboard", "panorama/layout/base_dashboard"),
    "m": ("overlay", "panorama/layout/base_hud_and_db_overlay"),
}
UI_NAMES = {name: dom for dom, (name, _) in HOSTS.items()}


class BridgeError(RuntimeError):
    pass


def _js(name: str) -> str:
    return resources.files(__package__).joinpath(f"js/{name}").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- script generation

def bridge_script(domain: str) -> str:
    return f"panorama/scripts/mcp_bridge_{domain}.js"


INBOX_LAYOUT = "panorama/layout/mcp_inbox.xml"
INBOX_LAYOUT_XML = """<root>
\t<scripts>
\t\t<include src="file://{resources}/scripts/mcp_inbox.js" />
\t</scripts>
\t<Panel />
</root>
"""
INBOX_SCRIPT = "panorama/scripts/mcp_inbox.js"


def render_bridge_js(domain: str = "m", poll: float = 0.1) -> str:
    main = (
        _js("bridge_main.js")
        .replace("__MCP_VERSION__", __version__)
        .replace("__MCP_DOMAIN__", domain)
        .replace("__MCP_POLL__", repr(float(poll)))
    )
    return _js("core.js") + "\n" + main


_STATEMENT_START = re.compile(
    r"^\s*(var|let|const|for|if|while|do|switch|try|throw|return|function|class)\b|;\s*\S", re.S
)


def user_function(code: str | None) -> str:
    """Wrap user code as `function __mcpUser($panel, $root, $find, $query, $children, $describe)`.

    A single expression is returned automatically; statement blocks must use `return`."""
    params = "$panel, $root, $find, $query, $children, $describe"
    if not code:
        return f"function __mcpUser({params}) {{ return null; }}\n"
    stripped = code.strip().rstrip(";")
    is_statements = bool(_STATEMENT_START.search(stripped)) or "\n" in stripped
    if is_statements:
        try:
            import esprima  # optional; refines the guess when available

            esprima.parseScript(f"(function(){{return ({stripped}\n);}})")
            is_statements = False
        except Exception:
            pass
    if is_statements:
        return f"function __mcpUser({params}) {{\n{_return_last_expression(code)}\n}}\n"
    return f"function __mcpUser({params}) {{\nreturn (\n{stripped}\n);\n}}\n"


def _return_last_expression(code: str) -> str:
    """REPL-style: make a trailing expression statement the return value (needs esprima; ES2017)."""
    try:
        import esprima

        body = esprima.parseScript(f"function f() {{\n{code}\n}}", {"range": True}).body[0].body.body
    except Exception:
        return code
    if not body or body[-1].type != "ExpressionStatement":
        return code
    start = body[-1].range[0] - len("function f() {\n")
    if start < 0:
        return code
    return code[:start] + "return " + code[start:]


def render_request_js(serial: int, op: str, args: dict, code: str | None = None, keep: bool = False,
                      domain: str = "m") -> str:
    req = json.dumps({"id": f"{domain}{serial}", "op": op, "args": args, "keep": keep}, ensure_ascii=True)
    return _js("core.js") + "\n" + user_function(code) + "\n" + _js("request_main.js").replace("__MCP_REQUEST__", req)


# --------------------------------------------------------------------------- transport


@dataclass
class _Pending:
    future: asyncio.Future
    parts: dict[int, str] = field(default_factory=dict)
    taken: bool = False


class Mailbox:
    def __init__(self, inbox_root: Path | None, log: ConsoleLog):
        self.root = inbox_root  # the mounted addon dir (contains panorama/)
        self.log = log
        self.sessions: dict[str, dict] = {}  # domain -> {"version", "next"}
        self._pending: dict[tuple[str, int], _Pending] = {}
        self._lock = asyncio.Lock()
        self._exc_lines: list[str] | None = None
        log.add_listener(self._on_line)
        log.on_new_file.append(self._reset)

    # ---- log parsing
    def _reset(self) -> None:
        self.sessions = {}
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(BridgeError("game restarted while waiting for a reply"))

    def _on_line(self, line: str) -> None:
        if self._exc_lines is not None:
            self._exc_lines.append(line)
            m = SCRIPT_FILE_RX.search(line)
            if m:
                self._fail((m[1], int(m[2])), "JS exception in request script:\n" + "\n".join(self._exc_lines))
                self._exc_lines = None
            elif len(self._exc_lines) > 6:
                self._exc_lines = None
        if "JS Exception" in line:
            self._exc_lines = [line]
            return
        if "@@MCP" not in line:
            return
        if m := READY_RX.search(line):
            self.sessions[m[1]] = {"version": m[2], "next": int(m[3])}
            return
        if m := TOOK_RX.search(line):
            key = (m[1], int(m[2]))
            if sess := self.sessions.get(key[0]):
                sess["next"] = max(sess["next"], key[1] + 1)
            if p := self._pending.get(key):
                p.taken = True
            return
        if m := REPLY_RX.search(line):
            key, k, n, text = (m[1], int(m[2])), int(m[3]), int(m[4]), m[5]
            p = self._pending.get(key)
            if not p or p.future.done():
                return
            p.parts[k] = text
            if len(p.parts) == n:
                raw = "".join(p.parts[i] for i in range(n))
                try:
                    p.future.set_result(json.loads(raw))
                except json.JSONDecodeError as e:
                    p.future.set_exception(BridgeError(f"garbled reply ({e}): {raw[:300]}"))

    def _fail(self, key: tuple[str, int], msg: str) -> None:
        p = self._pending.get(key)
        if p and not p.future.done():
            p.future.set_exception(BridgeError(msg))

    # ---- files
    def _paths(self, domain: str, serial: int) -> tuple[Path, Path]:
        assert self.root
        name = request_name(serial, domain)
        return (self.root / f"panorama/scripts/{name}.vjs_c", self.root / f"panorama/layout/{name}.vxml_c")

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def cleanup_all(self) -> int:
        """Remove every request file (safe when no request is in flight)."""
        if not self.root:
            return 0
        n = 0
        for d, pat in (("panorama/scripts", "[a-z][0-9]*.vjs_c"), ("panorama/layout", "[a-z][0-9]*.vxml_c")):
            for p in (self.root / d).glob(pat):
                if re.fullmatch(r"[a-z]\d{8}\.v(js|xml)_c", p.name):
                    p.unlink(missing_ok=True)
                    n += 1
        return n

    @property
    def ready(self) -> bool:
        return bool(self.sessions)

    def ready_uis(self) -> list[str]:
        return [HOSTS[d][0] for d in HOSTS if d in self.sessions]

    def resolve_ui(self, ui: str | None) -> str:
        """Map 'hud' / 'dashboard' / 'overlay' / 'auto' to a domain letter with a live bridge."""
        self.log.start()
        self.log.poll()
        if not self.log.exists:
            raise BridgeError("console.log not found — is Deadlock running with -condebug? (use launch_game)")
        if ui in (None, "", "auto"):
            for d in HOSTS:  # preference order: hud, dashboard, overlay
                if d in self.sessions:
                    return d
            raise BridgeError(
                "no bridge has reported ready in this game session. Is the game past the start screen, and "
                "was bridge_install run (then the game restarted)? See console_log(pattern='@@MCP|JS Exception')."
            )
        d = UI_NAMES.get(ui, ui)
        if d not in HOSTS:
            raise BridgeError(f"unknown ui {ui!r}; use one of {', '.join(UI_NAMES)} or 'auto'")
        if d not in self.sessions:
            raise BridgeError(f"the {HOSTS[d][0]} bridge is not loaded right now (ready: {self.ready_uis() or 'none'})")
        return d

    async def call(self, op: str, args: dict | None = None, *, ui: str | None = None, code: str | None = None,
                   keep: bool = False, timeout: float = 5.0):
        if not self.root:
            raise BridgeError("Deadlock dir not found")
        domain = self.resolve_ui(ui)
        async with self._lock:
            sess = self.sessions[domain]
            serial = sess["next"]
            sess["next"] += 1
            key = (domain, serial)
            fut = asyncio.get_running_loop().create_future()
            self._pending[key] = _Pending(fut)
            js_path, xml_path = self._paths(domain, serial)
            self._atomic_write(js_path, make_vjs(render_request_js(serial, op, args or {}, code, keep, domain)))
            self._atomic_write(xml_path, make_request_layout(serial, domain=domain))
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            p = self._pending.get(key)
            hint = (
                "the bridge loaded the request but sent no reply (script error? see console_log)"
                if p and p.taken else
                f"the {HOSTS[domain][0]} bridge never picked up the request (game paused/minimised, "
                "UI unloaded, or game restarted)"
            )
            raise BridgeError(f"timed out after {timeout}s on '{op}': {hint}") from None
        finally:
            self._pending.pop(key, None)
            for p in self._paths(domain, serial):
                p.unlink(missing_ok=True)
        if not resp.get("ok"):
            raise BridgeError(resp.get("error", "unknown error") + (f"\n{resp['stack']}" if resp.get("stack") else ""))
        return resp.get("result")


# --------------------------------------------------------------------------- CSS → rules

_COMPOUND = re.compile(r"^(\*|[A-Za-z_][\w-]*)?((?:[#.][\w-]+)*)$")


def parse_selector(sel: str) -> list[dict] | None:
    """Parse `Type#id.cls > .a .b` into compounds; None if it uses unsupported syntax."""
    tokens = re.findall(r">|[^\s>]+", sel.strip())
    out: list[dict] = []
    comb = " "
    for tok in tokens:
        if tok == ">":
            comb = ">"
            continue
        m = _COMPOUND.match(tok)
        if not m:
            return None
        c: dict = {"comb": comb}
        if m[1]:
            c["type"] = m[1]
        for kind, name in re.findall(r"([#.])([\w-]+)", m[2] or ""):
            if kind == "#":
                c["id"] = name
            else:
                c.setdefault("classes", []).append(name)
        out.append(c)
        comb = " "
    return out or None


def _strip_at_blocks(css: str) -> tuple[str, list[str]]:
    out, skipped, i = [], [], 0
    while i < len(css):
        if css[i] == "@":
            j = i
            while j < len(css) and css[j] not in "{;":
                j += 1
            skipped.append(css[i:j].strip())
            if j < len(css) and css[j] == "{":
                depth = 0
                while j < len(css):
                    depth += css[j] == "{"
                    depth -= css[j] == "}"
                    j += 1
                    if depth == 0:
                        break
            else:
                j += 1
            i = j
        else:
            out.append(css[i])
            i += 1
    return "".join(out), skipped


_DEFINE_RX = re.compile(r"@define\s+([\w-]+)\s*:\s*([^;]+);")


def css_defines(css: str) -> dict[str, str]:
    return {m[1]: m[2].strip() for m in _DEFINE_RX.finditer(re.sub(r"/\*.*?\*/", "", css, flags=re.S))}


def _substitute(value: str, defines: dict[str, str]) -> str:
    if not defines:
        return value
    rx = re.compile(r"(?<![\w#-])(" + "|".join(re.escape(k) for k in sorted(defines, key=len, reverse=True)) + r")(?![\w-])")
    for _ in range(5):  # defines may reference other defines
        new = rx.sub(lambda m: defines[m[1]], value)
        if new == value:
            break
        value = new
    return value


def css_blocks(css: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Split CSS into (selector-list, declarations) blocks; returns blocks and skipped at-rules."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css, at_rules = _strip_at_blocks(css)
    blocks = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        decl: dict[str, str] = {}
        for d in m[2].split(";"):
            if ":" in d:
                k, v = d.split(":", 1)
                if k.strip() and v.strip():
                    decl[k.strip().lower()] = v.strip()
        if decl:
            blocks.append((m[1].strip(), decl))
    return blocks, [a for a in at_rules if not a.startswith("@define")]


def _blocks_to_rules(blocks, defines, warnings) -> list[dict]:
    rules: list[dict] = []
    for sels, decl in blocks:
        decl = {k: _substitute(v, defines) for k, v in decl.items()}
        for sel in sels.split(","):
            parsed = parse_selector(sel)
            if parsed is None:
                warnings.append(f"skipped selector (only type/#id/.class with ' ' and '>' supported live): {sel.strip()}")
                continue
            rules.append({"sel": parsed, "decl": decl})
    return rules


def css_to_rules(css: str, defines: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Compile Panorama CSS text into [{sel, decl}] rules for inline application.
    @define variables (from this CSS, plus `defines`) are substituted into values."""
    blocks, at_rules = css_blocks(css)
    warnings = [f"skipped at-rule (not supported live): {a}" for a in at_rules]
    return _blocks_to_rules(blocks, {**(defines or {}), **css_defines(css)}, warnings), warnings


def css_diff_rules(original: str, edited: str) -> tuple[list[dict], list[str]]:
    """Rules for declarations that the edit added or changed relative to the original stylesheet.

    Blocks are matched by (selector, n-th occurrence), since stylesheets often repeat a selector."""
    def keyed(blocks):
        seen: dict[str, int] = {}
        out: dict[tuple[str, int], dict[str, str]] = {}
        for sels, decl in blocks:
            for sel in sels.split(","):
                k = " ".join(sel.split())
                i = seen.get(k, 0)
                seen[k] = i + 1
                out[(k, i)] = decl
        return out

    orig_blocks, _ = css_blocks(original)
    new_blocks, at_rules = css_blocks(edited)
    old_map, new_map = keyed(orig_blocks), keyed(new_blocks)
    defines = {**css_defines(original), **css_defines(edited)}
    warnings = [f"skipped at-rule (not supported live): {a}" for a in at_rules if a not in original]
    changed = []
    removed = False
    for (sel, _i), decl in new_map.items():
        old = old_map.get((sel, _i), {})
        diff = {k: v for k, v in decl.items() if old.get(k) != v}
        removed = removed or any(k not in decl for k in old)
        if diff:
            changed.append((sel, diff))
    if removed or any(k not in new_map for k in old_map):
        warnings.append("removed declarations/blocks can't be undone live (inline styles only add/override)")
    return _blocks_to_rules(changed, defines, warnings), warnings


# --------------------------------------------------------------------------- XML helpers


def ensure_root(xml: str) -> str:
    """Wrap bare fragments in a full <root> layout document."""
    if re.search(r"<root[\s>]", xml):
        return xml
    return f'<root>\n\t<Panel style="width: 100%; height: 100%;" hittest="false">\n{xml}\n\t</Panel>\n</root>\n'


def add_stylesheet(xml: str, style_src: str) -> str:
    inc = f'<include src="{style_src}" />'
    m = re.search(r"<styles>\s*", xml)
    if m:
        return xml[: m.end()] + inc + "\n\t\t" + xml[m.end():]
    m = re.search(r"<root>\s*", xml)
    if not m:
        raise BridgeError("layout has no <root>")
    return xml[: m.end()] + f"<styles>\n\t\t{inc}\n\t</styles>\n\t" + xml[m.end():]


def inject_bridge(layout_xml: str, domain: str = "m") -> str:
    """Add a <scripts> include of the bridge script to a decompiled Panorama layout."""
    BRIDGE_INCLUDE = f'<include src="file://{{resources}}/scripts/mcp_bridge_{domain}.js" />'
    if "mcp_bridge" in layout_xml:
        raise BridgeError("layout already includes the MCP bridge")
    xml = re.sub(r"^\s*<!--.*?-->\s*", "", layout_xml, count=1, flags=re.S)
    m = re.search(r"<scripts>\s*", xml)
    if m:
        return xml[: m.end()] + BRIDGE_INCLUDE + "\n\t\t" + xml[m.end():]
    m = re.search(r"</styles>\s*", xml) or re.search(r"<root>\s*", xml)
    if not m:
        raise BridgeError("host layout has no <root>; cannot inject")
    return xml[: m.end()] + f"<scripts>\n\t\t{BRIDGE_INCLUDE}\n\t</scripts>\n\t" + xml[m.end():]
