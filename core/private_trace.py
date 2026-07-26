"""Private helper-trace files for detached reset and restore processes."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

from core.config import Config
from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    harden_private_file,
    validate_private_regular_file,
)


def helper_trace_path(kind: str, parent_pid: int) -> Path:
    """Return a deterministic path inside the application's private log tree."""

    safe_kind = "".join(char for char in str(kind).lower() if char.isalnum() or char == "-")
    if not safe_kind:
        raise ValueError("Helper trace kind is required")
    directory = Config().paths.logs / "helpers"
    ensure_private_directory(directory)
    return directory / f"{safe_kind}-{int(parent_pid)}.log"


def open_helper_trace(path: Path) -> BinaryIO:
    """Open one owner-only trace without following a final-component symlink."""

    path = Path(path)
    ensure_private_directory(path.parent)
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:
        raise LocalFileSecurityError(f"Could not inspect helper trace {path}: {exc}") from exc
    if exists:
        validate_private_regular_file(path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise LocalFileSecurityError(f"Unsafe helper trace file: {path}")
        if os.name != "nt" and hasattr(os, "getuid") and opened.st_uid != os.getuid():
            raise LocalFileSecurityError(f"Helper trace is owned by another user: {path}")
        if not harden_private_file(path):
            raise LocalFileSecurityError(f"Could not restrict helper trace {path}")
        return os.fdopen(descriptor, "ab", buffering=0, closefd=True)
    except Exception:
        os.close(descriptor)
        raise
