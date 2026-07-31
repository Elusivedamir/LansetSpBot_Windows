"""Platform-aware filesystem locations for LansetSpBot."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    validate_private_regular_file,
)


CANONICAL_DATA_DIR_ENV = "LANSETSPBOT_DATA_DIR"
LEGACY_DATA_DIR_ENV = "MARLEN_DATA_DIR"
CANONICAL_WINDOWS_PROFILE = "LansetSpBot"
LEGACY_WINDOWS_PROFILE = "Marlen"


class ProfilePathConflictError(RuntimeError):
    """Raised when two profile locations cannot be selected safely."""


def _normalized_override(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def _resolve_profile_root(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
) -> Path:
    """Select one profile root without moving, copying, or deleting user data.

    Existing Windows installations continue using ``%APPDATA%\\Marlen`` until a
    later startup-owned migration can prove that all database, session and
    secret-store resources are closed. New installations use
    ``%APPDATA%\\LansetSpBot``.
    """

    env = os.environ if environ is None else environ
    current_platform = os.name if platform_name is None else platform_name
    current_home = Path.home() if home is None else Path(home)

    canonical_override = _normalized_override(env.get(CANONICAL_DATA_DIR_ENV))
    legacy_override = _normalized_override(env.get(LEGACY_DATA_DIR_ENV))
    if (
        canonical_override is not None
        and legacy_override is not None
        and canonical_override != legacy_override
    ):
        raise ProfilePathConflictError(
            f"{CANONICAL_DATA_DIR_ENV} and {LEGACY_DATA_DIR_ENV} point to "
            "different profile directories."
        )
    if canonical_override is not None:
        return canonical_override
    if legacy_override is not None:
        return legacy_override

    if current_platform == "nt":
        base = Path(env.get("APPDATA", current_home))
        canonical_root = base / CANONICAL_WINDOWS_PROFILE
        legacy_root = base / LEGACY_WINDOWS_PROFILE
        legacy_exists = legacy_root.exists() or legacy_root.is_symlink()
        canonical_exists = canonical_root.exists() or canonical_root.is_symlink()
        if legacy_exists and canonical_exists:
            raise ProfilePathConflictError(
                "Both legacy and canonical LansetSpBot profiles exist: "
                f"{legacy_root} and {canonical_root}. "
                "Refusing to select or merge them automatically."
            )
        if legacy_exists:
            return legacy_root
        return canonical_root

    # Non-Windows paths remain unchanged because this repository's supported
    # production target is Windows and changing them would add migration risk.
    return Path(env.get("XDG_DATA_HOME", current_home / ".local" / "share")) / "marlen"


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    logs: Path
    sessions: Path
    backups: Path

    @classmethod
    def resolve(cls) -> "AppPaths":
        root = _resolve_profile_root()

        return cls(
            root=root,
            database=root / "marlen.db",
            logs=root / "logs",
            sessions=root / "sessions",
            backups=root / "backups",
        )

    def _validate_layout(self) -> None:
        root = Path(self.root)
        for managed in (self.database, self.logs, self.sessions, self.backups):
            candidate = Path(managed)
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise LocalFileSecurityError(
                    f"Managed application path is outside the data directory: {candidate}"
                ) from exc
        if Path(self.database).parent != root:
            raise LocalFileSecurityError(
                f"SQLite database must be directly inside {root}: {self.database}"
            )

    def ensure(self) -> None:
        self._validate_layout()
        for directory in (self.root, self.logs, self.sessions, self.backups):
            ensure_private_directory(Path(directory))

        database = Path(self.database)
        try:
            exists = database.exists() or database.is_symlink()
        except OSError as exc:
            raise LocalFileSecurityError(
                f"Could not inspect SQLite database path {database}: {exc}"
            ) from exc
        if exists:
            info = database.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise LocalFileSecurityError(
                    f"Refusing symbolic-link SQLite database: {database}"
                )
            validate_private_regular_file(database)


APP_PATHS = AppPaths.resolve()
