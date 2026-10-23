"""Cache-busting for static files: a version token that changes whenever the file changes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def asset_version(static_dir: Path) -> Callable[[str], str]:
    """Return a template helper giving a short token for a file under static_dir.

    The token is the file's modification time, so browsers fetch a stylesheet again after it is edited
    instead of pairing new pages with an old cached copy.
    """
    root = static_dir.resolve()

    def version(name: str) -> str:
        path = (root / name).resolve()
        if root not in path.parents:
            return "0"
        try:
            return format(path.stat().st_mtime_ns // 1_000_000, "x")
        except OSError:
            return "0"

    return version
