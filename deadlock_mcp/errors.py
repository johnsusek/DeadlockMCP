"""Exception types. Every expected failure derives from DeadlockError, so the tool layer can turn
it into an error reply in one place."""

from __future__ import annotations


class DeadlockError(RuntimeError):
    pass


class BridgeError(DeadlockError):
    """The in-game bridge could not be reached, or a request to it failed."""


class AssetError(DeadlockError):
    """Reading, decompiling, compiling or installing game files failed."""


class LayoutError(DeadlockError):
    """A Panorama layout document could not be edited."""
