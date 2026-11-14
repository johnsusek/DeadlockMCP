"""Python side of the live-edit bridge.

Transport (verified against retail Deadlock, Nov 2026):
  in  — the MCP server writes a fresh compiled layout + script pair `panorama/{layout,scripts}/mNNNNNNNN`
        as loose files in the mounted addon dir. The persistent bridge (compiled into the host layout)
        probes for the next serial with $.BImageFileExists every 0.1s and BLoadLayout()s it.
        Fresh names are required: Panorama caches layouts/scripts by path for the whole session.
  out — scripts reply with $.Msg lines "@@MCP <id> <k>/<n> <json slice>", read from console.log
        (-condebug). Engine-level script errors ("JS Exception!") are matched back to the serial.

The bridge only ever probes for its one `next` serial, so Python must never run ahead of it: the
expected serial moves only on the bridge's own "took"/"ready" lines, and each domain has one request
in flight. A request that timed out untaken is simply rewritten under the same serial next time.

Deadlock's Panorama V8 forbids eval/new Function, so user code for js_eval is compiled into the
request script as a real function.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

from . import __version__
from .consolelog import ConsoleLog
from .errors import BridgeError
from .resource import make_request_layout, make_vjs, request_name

REPLY_RX = re.compile(r"@@MCP ([a-z])(\d+)(?:-([0-9a-z]+))? (\d+)/(\d+) (.*)$")
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


def _js(name: str) -> str:
    return resources.files(__package__).joinpath(f"js/{name}").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- script generation

def bridge_script(domain: str) -> str:
    return f"panorama/scripts/mcp_bridge_{domain}.js"


def bridge_version() -> str:
    """Package version plus a hash of the bridge JS, so a stale in-game bridge shows up after a code change."""
    digest = hashlib.sha1((_js("core.js") + "\0" + _js("bridge_main.js")).encode("utf-8")).hexdigest()
    return f"{__version__}+{digest[:8]}"


def render_bridge_js(domain: str = "m", poll: float = 0.1) -> str:
    main = (
        _js("bridge_main.js")
        .replace("__MCP_VERSION__", bridge_version())
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
    return analyze_user_code(code)[0]


def analyze_user_code(code: str | None) -> tuple[str, str | None]:
    """Like user_function, plus a note for the caller when the code could not be analysed (so a
    trailing expression was not turned into the return value and the result will be null)."""
    params = "$panel, $root, $find, $query, $children, $describe"
    if not code:
        return f"function __mcpUser({params}) {{ return null; }}\n", None
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
        body, ok = _return_last_expression(code)
        note = None if ok else ("could not analyse the code (syntax newer than ES2017?), so its last expression "
                                "is not returned automatically; add an explicit `return`")
        return f"function __mcpUser({params}) {{\n{body}\n}}\n", note
    return f"function __mcpUser({params}) {{\nreturn (\n{stripped}\n);\n}}\n", None


def _return_last_expression(code: str) -> tuple[str, bool]:
    """REPL-style: make a trailing expression statement the return value (needs esprima; ES2017).
    Returns (code, analysed); analysed is False when esprima is missing or cannot parse the code."""
    try:
        import esprima

        body = esprima.parseScript(f"function f() {{\n{code}\n}}", {"range": True}).body[0].body.body
    except Exception:
        return code, False
    if not body or body[-1].type != "ExpressionStatement":
        return code, True
    start = body[-1].range[0] - len("function f() {\n")
    if start < 0:
        return code, True
    return code[:start] + "return " + code[start:], True


def request_id(domain: str, serial: int, nonce: str | None = None) -> str:
    """The id a request script echoes in its replies: `<domain><serial>[-<nonce>]`, one token."""
    if nonce is not None and not re.fullmatch(r"[0-9a-z]+", nonce):
        raise ValueError(f"bad nonce {nonce!r}")
    return f"{domain}{serial}" + (f"-{nonce}" if nonce else "")


def render_request_js(serial: int, op: str, args: dict, code: str | None = None, keep: bool = False,
                      domain: str = "m", nonce: str | None = None) -> str:
    req = json.dumps({"id": request_id(domain, serial, nonce), "op": op, "args": args, "keep": keep},
                     ensure_ascii=True)
    return _js("core.js") + "\n" + user_function(code) + "\n" + _js("request_main.js").replace("__MCP_REQUEST__", req)




# --------------------------------------------------------------------------- transport


@dataclass
class _Pending:
    serial: int
    nonce: str
    future: asyncio.Future
    parts: dict[int, str] = field(default_factory=dict)
    taken: bool = False


class Mailbox:
    def __init__(self, inbox_root: Path | None, log: ConsoleLog, game_running: Callable[[], bool] | None = None):
        self.root = inbox_root  # the mounted addon dir (contains panorama/)
        self.log = log
        self.game_running = game_running
        # domain -> {"version", "next" (the serial the bridge probes for), "stalled" (last request went untaken)}
        self.sessions: dict[str, dict] = {}
        self._pending: dict[str, _Pending] = {}  # domain -> the one request in flight
        self._locks: dict[str, asyncio.Lock] = {}
        self._exc_lines: list[str] | None = None
        log.add_listener(self._on_line)
        log.on_new_file.append(self.reset)

    def reset(self, reason: str = "the game session ended (console.log was recreated or removed)") -> None:
        """Forget every bridge (game exited or restarted); fails any request still waiting."""
        self.sessions = {}
        self._exc_lines = None
        for p in self._pending.values():
            if not p.future.done():
                p.future.set_exception(BridgeError(f"{reason} while waiting for a reply"))

    # ---- log parsing
    def _on_line(self, line: str) -> None:
        # Protocol lines first: reply text is arbitrary (it may say "JS Exception" or name a request file).
        if "@@MCP" in line and self._on_protocol(line):
            return
        if self._exc_lines is not None:
            self._exc_lines.append(line)
            m = SCRIPT_FILE_RX.search(line)
            if m:
                p = self._pending.get(m[1])
                if p and p.serial == int(m[2]) and not p.future.done():
                    p.future.set_exception(BridgeError("JS exception in request script:\n" + "\n".join(self._exc_lines)))
                self._exc_lines = None
            elif len(self._exc_lines) > 6:
                self._exc_lines = None
        if "JS Exception" in line:
            self._exc_lines = [line]

    def _on_protocol(self, line: str) -> bool:
        if m := REPLY_RX.search(line):
            d, serial, nonce, k, n, text = m[1], int(m[2]), m[3], int(m[4]), int(m[5]), m[6]
            p = self._pending.get(d)
            # The nonce rejects replies meant for another server process sharing this bridge.
            if not p or p.serial != serial or p.nonce != nonce or p.future.done():
                return True
            p.parts[k] = text
            if len(p.parts) == n:
                raw = "".join(p.parts.get(i, "") for i in range(n))
                try:
                    p.future.set_result(json.loads(raw))
                except json.JSONDecodeError as e:
                    p.future.set_exception(BridgeError(f"garbled reply ({e}): {raw[:300]}"))
            return True
        if m := TOOK_RX.search(line):
            d, serial = m[1], int(m[2])
            if sess := self.sessions.get(d):  # also for late "took"s of requests we already gave up on
                sess["next"] = max(sess["next"], serial + 1)
                sess["stalled"] = False
            if (p := self._pending.get(d)) and p.serial == serial:
                p.taken = True
            return True
        if m := READY_RX.search(line):
            d = m[1]
            self.sessions[d] = {"version": m[2], "next": int(m[3]), "stalled": False}
            p = self._pending.get(d)
            if p and not p.future.done():
                p.future.set_exception(BridgeError(
                    f"the {HOSTS[d][0]} UI was reloaded (its bridge restarted) while the request was pending; retry"))
            return True
        return False

    def _refresh(self) -> None:
        try:
            self.log.poll()
        except OSError:
            pass

    # ---- files
    def _paths(self, domain: str, serial: int) -> tuple[Path, Path]:
        assert self.root
        name = request_name(serial, domain)
        return (self.root / f"panorama/scripts/{name}.vjs_c", self.root / f"panorama/layout/{name}.vxml_c")

    def _unlink(self, domain: str, serial: int) -> None:
        for p in self._paths(domain, serial):
            p.unlink(missing_ok=True)

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

    # ---- state
    @property
    def ready(self) -> bool:
        return bool(self.sessions)

    def ready_uis(self) -> list[str]:
        self._refresh()
        return [HOSTS[d][0] for d in HOSTS if d in self.sessions]

    def ui_states(self) -> dict[str, dict]:
        """{ui name: {"version", "current" (matches this code's bridge_version), "stalled"}} for loaded bridges."""
        self._refresh()
        want = bridge_version()
        return {HOSTS[d][0]: {"version": s["version"], "current": s["version"] == want, "stalled": s["stalled"]}
                for d in HOSTS if (s := self.sessions.get(d))}

    def resolve_ui(self, ui: str | None) -> str:
        """Map 'hud' / 'dashboard' / 'overlay' / 'auto' to a domain letter with a live bridge."""
        self.log.start()
        self._refresh()
        if not self.log.exists:
            raise BridgeError("console.log not found — is Deadlock running with -condebug? (use launch_game)")
        if ui in (None, "", "auto"):
            live = [d for d in HOSTS if d in self.sessions]  # preference order: hud, dashboard, overlay
            if not live:
                raise BridgeError(
                    "no bridge has reported ready in this game session. Is the game past the start screen, and "
                    "was bridge_install run (then the game restarted)? See console_log(pattern='@@MCP|JS Exception')."
                )
            # A UI whose last request went untaken is probably unloaded; don't burn a timeout on it every call.
            return next((d for d in live if not self.sessions[d]["stalled"]), live[0])
        d = UI_NAMES.get(ui, ui)
        if d not in HOSTS:
            raise BridgeError(f"unknown ui {ui!r}; use one of {', '.join(UI_NAMES)} or 'auto'")
        if d not in self.sessions:
            raise BridgeError(f"the {HOSTS[d][0]} bridge is not loaded right now (ready: {self.ready_uis() or 'none'})")
        return d

    # ---- requests
    async def call(self, op: str, args: dict | None = None, *, ui: str | None = None, code: str | None = None,
                   keep: bool = False, timeout: float = 5.0):
        if not self.root:
            raise BridgeError("Deadlock dir not found")
        domain = self.resolve_ui(ui)
        name = HOSTS[domain][0]
        # One request in flight per bridge: it only probes for its next serial, so a second request
        # would just sit behind the first anyway, and a failed one must hand its serial back.
        async with self._locks.setdefault(domain, asyncio.Lock()):
            self._refresh()
            sess = self.sessions.get(domain)
            if sess is None:  # the game restarted while we waited for the lock
                raise BridgeError(f"the {name} bridge went away before the request was sent (game restarted?)")
            serial = sess["next"]
            p = _Pending(serial, secrets.token_hex(4), asyncio.get_running_loop().create_future())
            self._pending[domain] = p
            try:
                js = render_request_js(serial, op, args or {}, code, keep, domain, nonce=p.nonce)
                self._atomic_write(self._paths(domain, serial)[0], make_vjs(js))
                self._atomic_write(self._paths(domain, serial)[1], make_request_layout(serial, domain=domain))
                await asyncio.wait({p.future}, timeout=timeout)
                if not p.future.done():
                    # Withdraw the request first so the bridge can't take it after we give up, then read what
                    # it logged until now: it may have taken it (or even replied) right at the deadline.
                    self._unlink(domain, serial)
                    self._refresh()
                if not p.future.done():
                    raise self._timeout_error(domain, p, op, timeout)
                resp = p.future.result()
            finally:
                if not p.future.done():
                    p.future.cancel()  # cancelled call: nothing must set an exception nobody retrieves
                if self._pending.get(domain) is p:
                    del self._pending[domain]
                self._unlink(domain, serial)
        if not resp.get("ok"):
            raise BridgeError(resp.get("error", "unknown error") + (f"\n{resp['stack']}" if resp.get("stack") else ""))
        return resp.get("result")

    def _timeout_error(self, domain: str, p: _Pending, op: str, timeout: float) -> BridgeError:
        name = HOSTS[domain][0]
        prefix = f"timed out after {timeout}s on '{op}': "
        if p.taken:
            return BridgeError(prefix + "the bridge loaded the request but sent no reply (script error? see console_log)")
        # Untaken: sess["next"] did not move, so the next request reuses this serial.
        self._pending.pop(domain, None)
        running = True
        if self.game_running is not None:
            try:
                running = bool(self.game_running())
            except Exception:
                pass
        if not running:
            self.reset("the game is not running")
            return BridgeError(prefix + "the game is not running (it exited or crashed); use launch_game")
        if sess := self.sessions.get(domain):
            sess["stalled"] = True
        return BridgeError(
            prefix + f"the {name} bridge never picked up the request (map loading, game paused or minimised, or "
            "that UI unloaded). The request was withdrawn; retry, or try another ui."
        )
