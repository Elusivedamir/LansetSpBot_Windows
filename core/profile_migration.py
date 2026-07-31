"""Transactional legacy-profile migration for LansetSpBot.

This module is deliberately independent from application startup.  The caller
must ensure that no LansetSpBot process has open database, session, logging, or
secret-store handles before invoking migration or recovery.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


MIGRATION_TRANSACTION_NAME = ".LansetSpBot-profile-migration"


class ProfileMigrationError(RuntimeError):
    """Raised when a profile cannot be migrated or recovered without data loss."""


@dataclass(frozen=True, slots=True)
class ProfileManifestEntry:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProfileMigrationResult:
    source: Path
    destination: Path
    files_verified: int
    bytes_verified: int


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _assert_plain_directory(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise ProfileMigrationError(f"{label} must not be a symbolic link: {path}")
    if not path.is_dir():
        raise ProfileMigrationError(f"{label} is not a directory: {path}")


def _iter_regular_files(root: Path) -> Iterator[Path]:
    for current_root, directory_names, file_names in os.walk(root, topdown=True):
        current = Path(current_root)
        for name in sorted(directory_names):
            candidate = current / name
            if candidate.is_symlink():
                raise ProfileMigrationError(
                    f"Profile contains a symbolic-link directory: {candidate}"
                )
        for name in sorted(file_names):
            candidate = current / name
            if candidate.is_symlink():
                raise ProfileMigrationError(
                    f"Profile contains a symbolic-link file: {candidate}"
                )
            if not candidate.is_file():
                raise ProfileMigrationError(
                    f"Profile contains a non-regular file: {candidate}"
                )
            yield candidate


def build_profile_manifest(
    root: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> tuple[ProfileManifestEntry, ...]:
    """Hash every regular file without opening SQLite or understanding its format."""

    root = Path(root)
    _assert_plain_directory(root, label="Profile")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    entries: list[ProfileManifestEntry] = []
    for candidate in _iter_regular_files(root):
        digest = hashlib.sha256()
        size = 0
        with candidate.open("rb") as stream:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        entries.append(
            ProfileManifestEntry(
                relative_path=candidate.relative_to(root).as_posix(),
                size=size,
                sha256=digest.hexdigest(),
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.relative_path))


def _transaction_path(source: Path, destination: Path) -> Path:
    if source.parent != destination.parent:
        raise ProfileMigrationError(
            "Legacy and canonical profiles must have the same parent directory"
        )
    return source.parent / MIGRATION_TRANSACTION_NAME


def _rename(source: Path, destination: Path) -> None:
    source.rename(destination)


def migrate_legacy_profile(
    source: Path,
    destination: Path,
    *,
    rename: Callable[[Path, Path], None] = _rename,
) -> ProfileMigrationResult:
    """Atomically rename one closed profile and verify every byte afterward.

    The operation uses an intermediate transaction directory:

    ``Marlen -> .LansetSpBot-profile-migration -> LansetSpBot``

    If the second rename or post-rename verification fails, the original
    ``Marlen`` path is restored whenever the filesystem permits it.
    """

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    transaction = _transaction_path(source, destination)

    _assert_plain_directory(source, label="Legacy profile")
    if _exists(destination):
        raise ProfileMigrationError(
            f"Canonical profile already exists; refusing to merge: {destination}"
        )
    if _exists(transaction):
        raise ProfileMigrationError(
            f"An unfinished profile migration exists: {transaction}"
        )

    before = build_profile_manifest(source)
    source_moved = False
    destination_created = False

    try:
        rename(source, transaction)
        source_moved = True
        rename(transaction, destination)
        destination_created = True

        after = build_profile_manifest(destination)
        if after != before:
            raise ProfileMigrationError(
                "Profile verification failed after migration; file manifest changed"
            )
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            if destination_created and _exists(destination) and not _exists(source):
                rename(destination, source)
            elif source_moved and _exists(transaction) and not _exists(source):
                rename(transaction, source)
        except Exception as rollback_exc:  # noqa: BLE001 - preserve both failures
            rollback_error = rollback_exc

        if rollback_error is not None:
            raise ProfileMigrationError(
                "Profile migration failed and automatic rollback also failed. "
                f"Migration error: {type(exc).__name__}: {exc}; "
                f"rollback error: {type(rollback_error).__name__}: {rollback_error}"
            ) from exc
        if isinstance(exc, ProfileMigrationError):
            raise
        raise ProfileMigrationError(
            f"Profile migration failed; original profile restored: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    return ProfileMigrationResult(
        source=source,
        destination=destination,
        files_verified=len(before),
        bytes_verified=sum(entry.size for entry in before),
    )


def recover_incomplete_profile_migration(
    source: Path,
    destination: Path,
    *,
    rename: Callable[[Path, Path], None] = _rename,
) -> bool:
    """Restore the legacy path after a crash between the two atomic renames.

    Returns ``True`` only when the transaction directory was restored.  Any
    ambiguous state fails closed and leaves all paths untouched.
    """

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    transaction = _transaction_path(source, destination)

    transaction_exists = _exists(transaction)
    if not transaction_exists:
        return False

    if _exists(source) or _exists(destination):
        raise ProfileMigrationError(
            "Cannot recover profile migration because transaction, legacy, and/or "
            "canonical profile paths coexist. No path was changed."
        )

    _assert_plain_directory(transaction, label="Migration transaction")
    rename(transaction, source)
    return True
