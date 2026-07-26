"""Fail-closed validation for Marlen-owned local files and directories."""

from __future__ import annotations

import getpass
import os
import stat
import subprocess
from pathlib import Path


class LocalFileSecurityError(RuntimeError):
    """A Marlen-owned path cannot be used without crossing a trust boundary."""


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise LocalFileSecurityError(
            f"Could not inspect local path {path}: {exc}"
        ) from exc


def ensure_private_directory(path: Path) -> None:
    """Create one private directory and reject symlink/non-directory targets."""

    path = Path(path)
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:
        raise LocalFileSecurityError(
            f"Could not inspect directory {path}: {exc}"
        ) from exc
    if exists:
        info = _lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise LocalFileSecurityError(f"Refusing symbolic-link directory: {path}")
        if not stat.S_ISDIR(info.st_mode):
            raise LocalFileSecurityError(f"Expected a directory at {path}")
    else:
        try:
            path.mkdir(parents=True, mode=0o700, exist_ok=False)
        except FileExistsError:
            pass
        except OSError as exc:
            raise LocalFileSecurityError(
                f"Could not create directory {path}: {exc}"
            ) from exc
        info = _lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise LocalFileSecurityError(f"Unsafe directory appeared at {path}")

    if os.name != "nt":
        info = _lstat(path)
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise LocalFileSecurityError(f"Directory is owned by another user: {path}")
        try:
            os.chmod(path, 0o700, follow_symlinks=False)
        except (NotImplementedError, OSError) as exc:
            raise LocalFileSecurityError(
                f"Could not restrict application directory {path}: {exc}"
            ) from exc
        if stat.S_IMODE(_lstat(path).st_mode) != 0o700:
            raise LocalFileSecurityError(
                f"Application directory is not private: {path}"
            )


def harden_private_file(path: Path) -> bool:
    """Restrict a regular file to the current OS account without following links."""

    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        return False
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        return False

    if os.name == "nt":
        username = os.environ.get("USERNAME") or getpass.getuser()
        domain = os.environ.get("USERDOMAIN", "").strip()
        principal = f"{domain}\\{username}" if domain else username
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        icacls = system_root / "System32" / "icacls.exe"
        if not icacls.is_file():
            return False
        command = str(icacls)
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            result = subprocess.run(
                [command, str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return False
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except (NotImplementedError, OSError):
        return False
    try:
        verified = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(verified.st_mode)
        and not stat.S_ISLNK(verified.st_mode)
        and stat.S_IMODE(verified.st_mode) == 0o600
    )


def validate_private_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    harden: bool = True,
) -> os.stat_result:
    """Validate an existing owner-controlled regular file without following links."""

    path = Path(path)
    info = _lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise LocalFileSecurityError(f"Refusing symbolic-link file: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise LocalFileSecurityError(f"Expected a regular file at {path}")
    if info.st_nlink != 1:
        raise LocalFileSecurityError(f"Refusing hard-linked private file: {path}")
    if os.name != "nt" and hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LocalFileSecurityError(f"File is owned by another user: {path}")
    if max_bytes is not None and info.st_size > max(0, int(max_bytes)):
        raise LocalFileSecurityError(
            f"Local file is too large: {path} ({info.st_size} bytes)"
        )
    if harden and not harden_private_file(path):
        raise LocalFileSecurityError(f"Could not restrict private file {path}")
    return _lstat(path)
