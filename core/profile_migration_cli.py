"""Explicit, pre-Qt command for migrating the Windows profile directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, TextIO

from core.profile_bootstrap import (
    CANONICAL_DATA_DIR_ENV,
    CANONICAL_WINDOWS_PROFILE,
    LEGACY_DATA_DIR_ENV,
    LEGACY_WINDOWS_PROFILE,
)
from core.profile_migration import (
    MIGRATION_TRANSACTION_NAME,
    ProfileMigrationError,
    migrate_legacy_profile,
    recover_incomplete_profile_migration,
)


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _instance_locks(root: Path) -> tuple[Path, ...]:
    if not root.is_dir() or root.is_symlink():
        return ()
    return tuple(sorted(root.glob(".instance-*.lock")))


def run_profile_migration_command(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Migrate ``%APPDATA%\\Marlen`` only when explicitly requested.

    Exit codes:
      0 - migrated or recovered successfully;
      2 - unsupported/unsafe state;
      3 - migration attempted but failed.
    """

    env = os.environ if environ is None else environ
    current_platform = os.name if platform_name is None else platform_name
    current_home = Path.home() if home is None else Path(home)
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    if current_platform != "nt":
        print("Profile migration is supported only on Windows.", file=err)
        return 2

    configured_overrides = [
        name
        for name in (CANONICAL_DATA_DIR_ENV, LEGACY_DATA_DIR_ENV)
        if str(env.get(name, "")).strip()
    ]
    if configured_overrides:
        print(
            "Profile migration refused because path overrides are configured: "
            + ", ".join(configured_overrides),
            file=err,
        )
        return 2

    appdata = Path(env.get("APPDATA", current_home))
    source = appdata / LEGACY_WINDOWS_PROFILE
    destination = appdata / CANONICAL_WINDOWS_PROFILE
    transaction = appdata / MIGRATION_TRANSACTION_NAME

    locks = tuple(
        lock
        for root in (source, destination, transaction)
        for lock in _instance_locks(root)
    )
    if locks:
        print(
            "Profile migration refused because instance lock files exist. "
            "Close every LansetSpBot/Marlen process and remove only proven-stale "
            "locks before retrying:\n"
            + "\n".join(str(lock) for lock in locks),
            file=err,
        )
        return 2

    try:
        recovered = recover_incomplete_profile_migration(source, destination)
        if recovered:
            print(
                f"Recovered unfinished migration back to legacy profile: {source}",
                file=out,
            )

        if not _path_exists(source):
            if _path_exists(destination):
                print(
                    f"Canonical profile already exists; nothing to migrate: {destination}",
                    file=out,
                )
                return 0
            print(f"Legacy profile does not exist: {source}", file=err)
            return 2

        if _path_exists(destination):
            print(
                "Both legacy and canonical profiles exist. No data was changed.",
                file=err,
            )
            return 2

        result = migrate_legacy_profile(source, destination)
    except ProfileMigrationError as exc:
        print(f"Profile migration failed: {exc}", file=err)
        return 3
    except OSError as exc:
        print(f"Profile migration failed: {type(exc).__name__}: {exc}", file=err)
        return 3

    print(
        "Profile migration completed successfully: "
        f"{result.files_verified} files, {result.bytes_verified} bytes verified.\n"
        f"{result.source} -> {result.destination}",
        file=out,
    )
    return 0
