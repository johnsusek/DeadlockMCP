"""Tail Deadlock's console.log (written when launched with -condebug).

This is the output channel: everything the game prints — including the bridge's `$.Msg` replies —
lands here. The file is recreated on each game launch; we detect that and start over.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)  # stderr only: stdout is the MCP stdio channel


class ConsoleLog:
    def __init__(self, path: Path | None, history: int = 5000):
        self.path = path
        self.lines: collections.deque[tuple[int, str]] = collections.deque(maxlen=history)
        self.seq = 0
        self._pos = 0
        self._ino: tuple[int, int] | None = None
        self._buf = b""
        self._listeners: list[Callable[[str], None]] = []
        self._task: asyncio.Task | None = None
        self.on_new_file: list[Callable[[], None]] = []

    def add_listener(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    @property
    def exists(self) -> bool:
        return bool(self.path and self.path.is_file())

    def poll(self) -> int:
        """Read any new bytes; returns number of new lines."""
        if not self.path:
            return 0
        try:
            st = self.path.stat()
        except FileNotFoundError:
            if self._ino is not None:  # the file we were reading is gone (launch_game deletes it): that game is over
                self._ino, self._pos, self._buf = None, 0, b""
                self._new_file()
            return 0
        ident = (st.st_dev, st.st_ino)
        if ident != self._ino or st.st_size < self._pos:
            self._ino, self._pos, self._buf = ident, 0, b""
            self._new_file()
        if st.st_size == self._pos:
            return 0
        with open(self.path, "rb") as f:
            f.seek(self._pos)
            chunk = f.read()
        self._pos += len(chunk)
        data = self._buf + chunk
        *complete, self._buf = data.split(b"\n")
        n = 0
        for raw in complete:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if not line:
                continue
            self.seq += 1
            n += 1
            self.lines.append((self.seq, line))
            for fn in list(self._listeners):
                try:
                    fn(line)
                except Exception:
                    _log.exception("console.log listener failed on line %r", line[:200])
        return n

    def _new_file(self) -> None:
        for fn in list(self.on_new_file):
            try:
                fn()
            except Exception:
                _log.exception("console.log new-file callback failed")

    def start(self, interval: float = 0.03) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(interval))

    async def _loop(self, interval: float) -> None:
        while True:
            try:
                self.poll()
            except OSError:
                pass
            await asyncio.sleep(interval)

    def tail(self, n: int = 100, pattern: str | None = None) -> list[str]:
        rx = re.compile(pattern, re.I) if pattern else None
        return [l for _, l in self.lines if not rx or rx.search(l)][-n:]
