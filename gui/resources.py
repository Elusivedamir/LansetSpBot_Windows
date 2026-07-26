"""Frozen-aware resolution of bundled GUI resources.

In a source checkout ``__file__`` points at the real package directory, so a
path relative to it resolves correctly.  In a PyInstaller build the Python
modules live inside the archive and ``__file__`` is a virtual path, while the
bundled data files are extracted next to ``sys._MEIPASS``.  ``main.py`` already
resolves its own icon through ``sys._MEIPASS``; the GUI must use the same rule
so packaged builds do not silently fall back to missing pixmaps.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent


def resource_root() -> Path:
    """Return the directory that owns the bundled ``gui`` resource tree."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "gui"
    return _PACKAGE_ROOT


def asset_path(*parts: str) -> Path:
    """Return one path inside ``gui/assets`` for source and frozen builds."""

    return resource_root().joinpath("assets", *parts)


__all__ = ["asset_path", "resource_root"]
