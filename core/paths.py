"""Platform-aware filesystem locations for Marlen."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    validate_private_regular_file,
)


@dataclass(frozen=True)
class AppPaths:
    root: Path
    database: Path
    logs: Path
    sessions: Path
    backups: Path

    @classmethod
    def resolve(cls) -> "AppPaths":
        override = os.getenv("MARLEN_DATA_DIR")
        if override:
            root = Path(override).expanduser().resolve()
        elif os.name == "nt":
            root = Path(os.getenv("APPDATA", Path.home())) / "Marlen"
        else:
            root = (
                Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
                / "marlen"
            )

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
