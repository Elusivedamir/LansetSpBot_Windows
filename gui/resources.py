"""Frozen-aware resolution of bundled GUI resources.

In a source checkout ``__file__`` points at the real package directory, so a
path relative to it resolves correctly.  In a PyInstaller build the Python
modules live inside the archive and ``__file__`` is a virtual path, while the
bundled data files are extracted next to ``sys._MEIPASS``.  ``main.py`` already
resolves its own icon through ``sys._MEIPASS``; the GUI must use the same rule
so packaged builds do not silently fall back to missing pixmaps.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
INSTRUCTION_ASSET_OVERRIDE_ENV = "LANSETSPBOT_INSTRUCTION_ASSETS_DIR"
INSTRUCTION_ASSET_STATUS_ENV = "LANSETSPBOT_INSTRUCTION_ASSETS_STATUS"


def resource_root() -> Path:
    """Return the directory that owns the bundled ``gui`` resource tree."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "gui"
    return _PACKAGE_ROOT


def asset_path(*parts: str) -> Path:
    """Return one path inside ``gui/assets`` for source and frozen builds.

    Source runs may render instruction screenshots into the protected user
    profile instead of mutating the Git checkout. Only that one resource
    subtree accepts an override; every other bundled asset remains immutable.
    """

    if parts and parts[0] == "instructions":
        override = os.environ.get(INSTRUCTION_ASSET_OVERRIDE_ENV, "").strip()
        if override:
            safe_parts = parts[1:]
            if any(
                part in {"", ".", ".."}
                or "/" in part
                or "\\" in part
                or Path(part).is_absolute()
                for part in safe_parts
            ):
                raise ValueError("invalid instruction asset path")
            return Path(override).joinpath(*safe_parts)
    return resource_root().joinpath("assets", *parts)


__all__ = [
    "INSTRUCTION_ASSET_OVERRIDE_ENV",
    "INSTRUCTION_ASSET_STATUS_ENV",
    "asset_path",
    "resource_root",
]
