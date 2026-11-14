"""Panorama CSS → structured rules the in-game bridge applies as inline styles."""

from __future__ import annotations

import re

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


# --- tokenizer -------------------------------------------------------------------------------------------------
# Values hold quoted strings and parens with `;`, `{}`, `@` and `/*` in them (`url("file://{images}/a.png")`,
# `foo@2x_png.vtex`), so every scan below skips strings and parenthesised groups.

def _string_end(s: str, i: int) -> tuple[int, bool]:
    """Index after the string starting at s[i]; False if unterminated (it then ends at the newline, as in CSS)."""
    q, i = s[i], i + 1
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1, True
        if c == "\n":
            return i, False
        i += 1
    return len(s), False


def _strip_comments(css: str, warnings: list[str]) -> str:
    out, i, n = [], 0, len(css)
    while i < n:
        c = css[i]
        if c in "\"'":
            j, ok = _string_end(css, i)
            if not ok:
                warnings.append(f"unterminated string: {css[i:j][:60]}")
            out.append(css[i:j])
            i = j
        elif c == "/" and css.startswith("*", i + 1):
            j = css.find("*/", i + 2)
            if j < 0:
                warnings.append("unterminated comment; ignored the rest of the stylesheet")
                break
            out.append(" ")
            i = j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _scan_to(s: str, i: int, stops: str) -> int:
    """Index of the first char in `stops` outside strings and parens (len(s) if none)."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c in "\"'":
            i = _string_end(s, i)[0]
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and c in stops:
            return i
        i += 1
    return i


def _split_top(s: str, sep: str) -> list[str]:
    parts, i = [], 0
    while i <= len(s):
        j = _scan_to(s, i, sep)
        parts.append(s[i:j])
        i = j + 1
    return parts


def _block_end(s: str, i: int) -> int | None:
    """Index after the `}` matching the `{` at s[i]; None if unterminated."""
    depth = 0
    while True:
        i = _scan_to(s, i, "{}")
        if i >= len(s):
            return None
        depth += 1 if s[i] == "{" else -1
        i += 1
        if depth == 0:
            return i


_PROP = re.compile(r"-?[A-Za-z_][\w-]*$")


def _declarations(body: str, warnings: list[str]) -> dict[str, str]:
    decl: dict[str, str] = {}
    for d in _split_top(body, ";"):
        if not d.strip():
            continue
        k, colon, v = d.partition(":")
        k, v = k.strip(), v.strip()
        if not colon or not _PROP.match(k) or not v:
            warnings.append(f"skipped malformed declaration: {' '.join(d.split())[:80]}")
            continue
        decl[k.lower()] = v
    return decl


_DEFINE = re.compile(r"@define\s+([\w-]+)\s*:\s*(.*\S)", re.S)


def _parse(css: str) -> tuple[list[tuple[str, dict[str, str]]], dict[str, str], list[str], list[str]]:
    """→ (blocks [(selector-list, decl)], @defines, skipped at-rule preludes, parse warnings)."""
    warnings: list[str] = []
    s = _strip_comments(css, warnings)
    blocks, defines, at_rules = [], {}, []
    i, n = 0, len(s)
    while i < n:
        if s[i].isspace() or s[i] == ";":
            i += 1
            continue
        if s[i] == "}":
            warnings.append("ignored stray '}'")
            i += 1
            continue
        at = s[i] == "@"  # at-rules are only recognised at statement start
        j = _scan_to(s, i, "{;}" if at else "{}")
        prelude = " ".join(s[i:j].split())
        if j >= n or s[j] == "}":
            warnings.append(f"ignored incomplete rule: {prelude[:80]}")
            i = j + 1
            continue
        if s[j] == ";":  # at-rule statement (`@define x: y;`, `@import ...;`)
            m = _DEFINE.fullmatch(prelude)
            if m:
                defines[m[1]] = m[2]
            else:
                at_rules.append(prelude)
            i = j + 1
            continue
        end = _block_end(s, j)
        if end is None:
            warnings.append(f"ignored unterminated block: {prelude[:80]} {{ ...")
            break
        if at:
            at_rules.append(prelude)  # @keyframes 'x' { ... } and friends
        else:
            decl = _declarations(s[j + 1: end - 1], warnings)
            if decl:
                blocks.append((prelude, decl))
        i = end
    return blocks, defines, at_rules, warnings


def css_defines(css: str) -> dict[str, str]:
    return _parse(css)[1]


def css_blocks(css: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Split CSS into (selector-list, declarations) blocks; returns blocks and skipped at-rules."""
    blocks, _, at_rules, _ = _parse(css)
    return blocks, at_rules


_WORD = re.compile(r"[^\s,()/\"']+")
_URL = re.compile(r"url\(", re.I)


def _substitute(value: str, defines: dict[str, str], depth: int = 0) -> str:
    """Replace bare @define names (not inside strings or url(...); defines may reference defines)."""
    if not defines or depth > 8:  # depth guards against cyclic defines
        return value
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c in "\"'":
            j = _string_end(value, i)[0]
        elif (m := _URL.match(value, i)) and (i == 0 or not (value[i - 1].isalnum() or value[i - 1] in "_-")):
            j = min(_scan_to(value, m.end(), ")") + 1, len(value))
        elif m := _WORD.match(value, i):
            j = m.end()
            w = m[0]
            if w in defines and not value.startswith("(", j):  # not a function name
                out.append(_substitute(defines[w], defines, depth + 1))
                i = j
                continue
        else:
            j = i + 1
        out.append(value[i:j])
        i = j
    return "".join(out)


def _blocks_to_rules(blocks, defines, warnings) -> list[dict]:
    rules: list[dict] = []
    for sels, decl in blocks:
        decl = {k: _substitute(v, defines) for k, v in decl.items()}
        for sel in _split_top(sels, ","):
            parsed = parse_selector(sel)
            if parsed is None:
                warnings.append(f"skipped selector (only type/#id/.class with ' ' and '>' supported live): {sel.strip()}")
                continue
            rules.append({"sel": parsed, "decl": decl})
    return rules


def _at_warnings(at_rules) -> list[str]:
    return [f"skipped at-rule (not supported live): {a}" for a in at_rules]


def css_to_rules(css: str, defines: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Compile Panorama CSS text into [{sel, decl}] rules for inline application.
    @define variables (from this CSS, plus `defines`) are substituted into values."""
    blocks, own, at_rules, warnings = _parse(css)
    warnings += _at_warnings(at_rules)
    return _blocks_to_rules(blocks, {**(defines or {}), **own}, warnings), warnings


def _sel_key(sel: str) -> str:
    return " ".join(re.findall(r"[>+~]|[^\s>+~]+", sel))  # `.A>.B` == `.A > .B`


def _effective(blocks, defines) -> dict[str, dict[str, str]]:
    """Per selector, the declarations that win among its blocks (later blocks override earlier ones)."""
    out: dict[str, dict[str, str]] = {}
    for sels, decl in blocks:
        for sel in _split_top(sels, ","):
            if sel.strip():
                d = out.setdefault(_sel_key(sel), {})
                for k, v in decl.items():
                    d[k] = _substitute(v, defines)
    return out


def css_diff_rules(original: str, edited: str) -> tuple[list[dict], list[str]]:
    """Rules for declarations that the edit added or changed relative to the original stylesheet.

    Compares each selector's effective declarations (all its blocks merged, last wins), so editing a block
    that a later block overrides, or adding/reordering blocks, only applies what actually changes.
    @define values are substituted first, so editing a define applies to the declarations using it."""
    o_blocks, o_defs, o_at, _ = _parse(original)
    n_blocks, n_defs, n_at, warnings = _parse(edited)
    old, new = _effective(o_blocks, o_defs), _effective(n_blocks, n_defs)
    warnings += _at_warnings(a for a in n_at if a not in o_at)
    changed = []
    for sel, decl in new.items():
        prev = old.get(sel, {})
        diff = {k: v for k, v in decl.items() if prev.get(k) != v}
        if diff:
            changed.append((sel, diff))
    if any(k not in new.get(sel, {}) for sel, decl in old.items() for k in decl):
        warnings.append("removed declarations/blocks can't be undone live (inline styles only add/override)")
    return _blocks_to_rules(changed, {}, warnings), warnings
