from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from core.local_security import (
    LocalFileSecurityError,
    harden_private_file,
    validate_private_regular_file,
)

log = logging.getLogger(__name__)


class TelegramSessionMixin:
    """Integrity checks and revocation for Telethon's SQLite session file.

    The session is never copied anywhere: a copy carries the same Telegram
    authorization key as the original, and the product no longer keeps one.
    What remains is verification, quarantine of a corrupt file and removal of
    authorization material on logout - including any backups left behind by an
    older version.

    The mixin deliberately has no dependency on ``TelegramService``.  Its host
    only needs a ``client`` attribute whose session exposes ``filename``.
    """

    @staticmethod
    def _session_is_healthy(path: Path) -> bool:
        """Validate a SQLite session without taking a Windows file lock."""
        try:
            if not path.is_file() or path.stat().st_size < 16:
                return False
            with path.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    return False
        except OSError:
            return False

        connection: sqlite3.Connection | None = None
        cursor: sqlite3.Cursor | None = None
        try:
            uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            cursor = connection.cursor()
            cursor.execute("PRAGMA quick_check")
            row = cursor.fetchone()
            return bool(row and str(row[0]).lower() == "ok")
        except (OSError, ValueError, sqlite3.Error):
            return False
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except sqlite3.Error:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _replace_with_windows_retry(source: Path, destination: Path) -> bool:
        """Rename a file, tolerating short-lived Windows sharing violations."""
        attempts = 6 if os.name == "nt" else 1
        for attempt in range(attempts):
            try:
                source.replace(destination)
                return True
            except PermissionError:
                if attempt + 1 >= attempts:
                    return False
                time.sleep(0.05 * (attempt + 1))
            except OSError:
                return False
        return False

    @staticmethod
    def _unlink_with_windows_retry(path: Path) -> bool:
        attempts = 8 if os.name == "nt" else 1
        for attempt in range(attempts):
            try:
                path.unlink(missing_ok=True)
                return not path.exists()
            except PermissionError:
                if attempt + 1 >= attempts:
                    return False
                time.sleep(0.05 * (attempt + 1))
            except OSError:
                return False
        return False

    @staticmethod
    def _overwrite_file_with_retry(source: Path, destination: Path) -> bool:
        """Restore bytes without deleting/replacing the destination inode."""
        attempts = 8 if os.name == "nt" else 1
        for attempt in range(attempts):
            try:
                mode = "r+b" if destination.exists() else "wb"
                with (
                    source.open("rb") as input_stream,
                    destination.open(mode) as output_stream,
                ):
                    output_stream.seek(0)
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    output_stream.truncate()
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                return True
            except PermissionError:
                if attempt + 1 >= attempts:
                    return False
                time.sleep(0.05 * (attempt + 1))
            except OSError:
                return False
        return False

    @classmethod
    def _backup_directory(cls, source: Path) -> Path:
        return source.parent / "backups"

    @classmethod
    def _secure_session_file(cls, source: Path) -> None:
        """Reject link substitution and require owner-only session permissions."""

        source = Path(source)
        try:
            exists = source.exists() or source.is_symlink()
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect Telegram session {source}: {exc}"
            ) from exc
        if not exists:
            return
        try:
            validate_private_regular_file(source)
        except LocalFileSecurityError as exc:
            raise RuntimeError(f"Unsafe Telegram session file: {exc}") from exc

    @classmethod
    def _harden_or_remove_private_artifact(
        cls, path: Path, *, description: str, required: bool
    ) -> bool:
        """Never retain a session artifact when owner-only permissions fail."""

        if harden_private_file(path):
            return True
        if not cls._unlink_with_windows_retry(path):
            raise RuntimeError(f"Could not restrict or remove {description}: {path}")
        if required:
            raise RuntimeError(
                f"Could not restrict {description}; the unsafe copy was removed"
            )
        log.warning("Discarded %s because owner-only permissions failed", description)
        return False

    @classmethod
    def _quarantine_session_sidecars(cls, source: Path, timestamp: str) -> None:
        """Move WAL/journal sidecars away before replacing a corrupt database."""
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{source}{suffix}")
            if not sidecar.exists():
                continue
            quarantine = sidecar.with_name(f"{sidecar.name}.corrupt.{timestamp}")
            if cls._replace_with_windows_retry(sidecar, quarantine):
                cls._harden_or_remove_private_artifact(
                    quarantine,
                    description="quarantined Telegram session sidecar",
                    required=False,
                )
                continue
            try:
                shutil.copyfile(sidecar, quarantine)
                cls._harden_or_remove_private_artifact(
                    quarantine,
                    description="quarantined Telegram session sidecar",
                    required=False,
                )
            except OSError as exc:
                raise RuntimeError(
                    "Telegram session sidecar is corrupt and could not be quarantined"
                ) from exc
            if not cls._unlink_with_windows_retry(sidecar):
                raise RuntimeError(
                    "Telegram session sidecar is locked; close other LansetSpBot instances"
                )

    @classmethod
    def _quarantine_corrupt_session(cls, source: Path, timestamp: str) -> Path:
        """Move bad bytes away so Telethon can create a clean SQLite session.

        Returning with the corrupt source still in place would make
        ``TelegramClient`` fail in its constructor before the GUI can offer a
        fresh authorization flow.  Failure to remove it is therefore explicit.
        """
        quarantine = source.with_name(f"{source.name}.corrupt.{timestamp}")
        if cls._replace_with_windows_retry(source, quarantine):
            cls._harden_or_remove_private_artifact(
                quarantine,
                description="quarantined Telegram session file",
                required=True,
            )
            return quarantine

        try:
            shutil.copyfile(source, quarantine)
            cls._harden_or_remove_private_artifact(
                quarantine,
                description="quarantined Telegram session file",
                required=True,
            )
        except OSError as exc:
            raise RuntimeError(
                "Telegram session is corrupt and could not be quarantined"
            ) from exc

        if not cls._unlink_with_windows_retry(source):
            raise RuntimeError(
                "Telegram session is corrupt and locked; close other LansetSpBot instances"
            )
        return quarantine

    @classmethod
    def _prepare_session_file(cls, source: Path) -> None:
        """Quarantine a corrupt session before Telethon opens it.

        Nothing is ever restored: session backups were removed from the product,
        so a corrupt session means reauthorization. A known-broken database is
        never left at the path Telethon opens.
        """

        try:
            exists = source.exists() or source.is_symlink()
        except OSError as exc:
            raise RuntimeError(
                f"Could not inspect Telegram session {source}: {exc}"
            ) from exc
        if not exists:
            return
        cls._secure_session_file(source)
        if cls._session_is_healthy(source):
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        # A stale WAL/journal can belong to the corrupt database and must not be
        # replayed against a clean session file.
        cls._quarantine_session_sidecars(source, timestamp)
        quarantine = cls._quarantine_corrupt_session(source, timestamp)
        log.error(
            "Telegram session was corrupt and was quarantined as %s. "
            "Reauthorization is required.",
            quarantine,
        )

    @classmethod
    def purge_session_backups(cls, source: Path) -> None:
        """Revoke rotating authorization backups without deleting the live session."""

        source = Path(source)
        backup_dir = cls._backup_directory(source)
        if not backup_dir.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        revoked = backup_dir.with_name(f"{backup_dir.name}.revoked.{timestamp}")
        if cls._replace_with_windows_retry(backup_dir, revoked):
            try:
                shutil.rmtree(revoked)
            except OSError:
                log.warning("Could not delete revoked session backups %s", revoked)
            return
        try:
            shutil.rmtree(backup_dir)
        except OSError as exc:
            raise RuntimeError(
                "Не удалось удалить резервные копии Telegram-сессии"
            ) from exc

    @classmethod
    def purge_session_artifacts(cls, source: Path) -> None:
        """Remove local authorization material after an explicit logout.

        Rotating backups contain the same Telegram authorization key as the live
        session. Keeping them after ``log_out()`` could let later corruption
        recovery restore the account that the user deliberately disconnected.
        """
        source = Path(source)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = cls._backup_directory(source)
        revoked_dir: Path | None = None
        if backup_dir.exists():
            revoked_dir = backup_dir.with_name(f"{backup_dir.name}.revoked.{timestamp}")
            if not cls._replace_with_windows_retry(backup_dir, revoked_dir):
                try:
                    shutil.rmtree(backup_dir)
                except OSError as exc:
                    raise RuntimeError(
                        "Telegram logout succeeded, but session backups could not be revoked"
                    ) from exc

        patterns = (
            source.name,
            f"{source.name}-wal",
            f"{source.name}-shm",
            f"{source.name}-journal",
            f"{source.name}.corrupt.*",
            f".{source.name}.restore.*.tmp",
        )
        for pattern in patterns:
            for artifact in source.parent.glob(pattern):
                if artifact.is_dir():
                    try:
                        shutil.rmtree(artifact)
                    except OSError as exc:
                        raise RuntimeError(
                            f"Could not remove revoked Telegram session artifact {artifact}"
                        ) from exc
                elif not cls._unlink_with_windows_retry(artifact):
                    raise RuntimeError(
                        f"Could not remove revoked Telegram session artifact {artifact}"
                    )

        # The directory was renamed first, so failed cleanup cannot make old
        # backups eligible for automatic recovery. Removal is best effort only.
        if revoked_dir is not None:
            try:
                shutil.rmtree(revoked_dir)
            except OSError:
                log.warning(
                    "Could not delete revoked session backup directory %s", revoked_dir
                )

