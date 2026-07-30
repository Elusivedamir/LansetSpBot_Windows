"""Fail-closed SQLCipher driver and plaintext-to-encrypted migration helpers.

Production database connections always use SQLCipher with a 256-bit raw key
that is derived from the OS-bound master key. Standard-library sqlite3 is only
available through an explicit test gate; it is never a production fallback.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import sqlite3 as _stdlib_sqlite3

from core.crypto_vault import EncryptedBlobCodec, OSBoundMasterKeyProvider
from core.secure_memory import secure_memory_available
from core.local_security import (
    LocalFileSecurityError,
    ensure_private_directory,
    harden_private_file,
    validate_private_regular_file,
)

SQLITE_HEADER: Final[bytes] = b"SQLite format 3\x00"
DATABASE_KEY_PURPOSE: Final[str] = "database.sqlcipher.v1"
TEST_PLAINTEXT_ENV: Final[str] = "LANSETSPBOT_ALLOW_PLAINTEXT_TEST_DB"
MIGRATION_VERSION: Final[int] = 1
MIGRATION_SUFFIX: Final[str] = ".sqlcipher-migration.json"
MIGRATION_TEMP_SUFFIX: Final[str] = ".sqlcipher-migration.tmp"
MIGRATION_ROLLBACK_SUFFIX: Final[str] = ".plaintext-migration.rollback"

_DATABASE_KEY_REGISTRY: dict[str, bytes] = {}
_DATABASE_KEY_REGISTRY_LOCK = threading.RLock()


class SQLCipherError(RuntimeError):
    """Base class for encrypted-database failures."""


class SQLCipherUnavailableError(SQLCipherError):
    """The SQLCipher Python extension is not installed or not functional."""


class DatabaseKeyError(SQLCipherError):
    """The OS-bound database key cannot be loaded safely."""


class DatabaseEncryptionMigrationError(SQLCipherError):
    """A plaintext database could not be migrated atomically."""


def _load_driver() -> tuple[ModuleType, bool, BaseException | None]:
    try:
        # mypy.ini sets ignore_missing_imports, so no inline ignore is needed
        # here; warn_unused_ignores would reject one when sqlcipher3 is installed.
        from sqlcipher3 import dbapi2 as sqlcipher_dbapi

        return sqlcipher_dbapi, True, None
    except BaseException as exc:  # noqa: BLE001 - retained for a clear startup error
        return _stdlib_sqlite3, False, exc


_DRIVER, SQLCIPHER_AVAILABLE, SQLCIPHER_IMPORT_ERROR = _load_driver()




def _database_registry_key(database: str | os.PathLike[str], *, uri: bool = False) -> str | None:
    text = os.fspath(database)
    if text == ":memory:" or text.startswith("file::memory:"):
        return None
    if uri and text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
    try:
        return os.fspath(Path(text).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return os.path.abspath(text)


def register_database_key(database: str | os.PathLike[str], key: bytes) -> None:
    registry_key = _database_registry_key(database)
    if registry_key is None:
        return
    raw = bytes(key)
    if len(raw) != 32:
        raise DatabaseKeyError("SQLCipher key must contain exactly 32 bytes")
    with _DATABASE_KEY_REGISTRY_LOCK:
        _DATABASE_KEY_REGISTRY[registry_key] = raw


def forget_database_key(database: str | os.PathLike[str]) -> None:
    registry_key = _database_registry_key(database)
    if registry_key is None:
        return
    with _DATABASE_KEY_REGISTRY_LOCK:
        _DATABASE_KEY_REGISTRY.pop(registry_key, None)


def _registered_database_key(
    database: str | os.PathLike[str], *, uri: bool = False
) -> bytes | None:
    registry_key = _database_registry_key(database, uri=uri)
    if registry_key is None:
        return None
    with _DATABASE_KEY_REGISTRY_LOCK:
        value = _DATABASE_KEY_REGISTRY.get(registry_key)
    return bytes(value) if value is not None else None


def plaintext_test_mode_enabled() -> bool:
    """Permit stdlib SQLite only inside a live, non-frozen pytest process.

    The environment flag remains an explicit opt-in for the test suite, but it
    is not sufficient on its own.  This prevents an ordinary source launch (or
    a packaged application inheriting the variable) from silently disabling
    SQLCipher.
    """

    return (
        os.getenv(TEST_PLAINTEXT_ENV, "").strip() == "1"
        and "pytest" in sys.modules
        and not bool(getattr(sys, "frozen", False))
    )


def _require_driver() -> None:
    if SQLCIPHER_AVAILABLE:
        return
    if plaintext_test_mode_enabled():
        return
    detail = (
        f" ({type(SQLCIPHER_IMPORT_ERROR).__name__}: {SQLCIPHER_IMPORT_ERROR})"
        if SQLCIPHER_IMPORT_ERROR is not None
        else ""
    )
    raise SQLCipherUnavailableError(
        "SQLCipher runtime is unavailable; refusing to open marlen.db with "
        f"unencrypted sqlite3{detail}"
    )



def default_database_key_storage_dir(database: str | os.PathLike[str]) -> Path:
    """Return the profile root that owns the OS-bound SQLCipher key.

    The live database is stored directly in the profile root. Durable
    pre-restore snapshots live in its ``backups`` child and intentionally use
    the same key so they remain readable after a restart.
    """

    path = Path(os.fspath(database)).expanduser().resolve(strict=False)
    parent = path.parent
    if parent.name == "backups":
        return parent.parent
    return parent

def derive_database_key(storage_dir: Path) -> bytes:
    """Derive the SQLCipher key without writing it beside the database."""

    try:
        codec = EncryptedBlobCodec(OSBoundMasterKeyProvider(Path(storage_dir)))
        return codec.derive_key(purpose=DATABASE_KEY_PURPOSE)
    except Exception as exc:  # noqa: BLE001 - normalized at the storage boundary
        raise DatabaseKeyError(
            "Could not obtain the OS-bound encryption key for marlen.db"
        ) from exc


def migration_artifacts(database_path: Path) -> tuple[Path, Path, Path]:
    path = Path(database_path)
    return (
        path.with_name(path.name + MIGRATION_SUFFIX),
        path.with_name(path.name + MIGRATION_TEMP_SUFFIX),
        path.with_name(path.name + MIGRATION_ROLLBACK_SUFFIX),
    )


def is_plaintext_sqlite(path: Path) -> bool:
    candidate = Path(path)
    try:
        if not candidate.exists() or candidate.stat().st_size < len(SQLITE_HEADER):
            return False
        with candidate.open("rb") as stream:
            return stream.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError as exc:
        raise SQLCipherError(f"Could not inspect database header {candidate}: {exc}") from exc


def _raw_key_pragma(key: bytes) -> str:
    key = bytes(key)
    if len(key) != 32:
        raise DatabaseKeyError("SQLCipher key must contain exactly 32 bytes")
    return f'PRAGMA key = "x\'{key.hex()}\'"'


def _apply_key(connection: Any, key: bytes) -> None:
    connection.execute(_raw_key_pragma(key))
    # Locked memory is enabled only where the OS can honour it. SQLCipher
    # 4.12.0 overflows the C stack instead of degrading when VirtualLock is
    # refused, which kills the process outright; see core.secure_memory.
    if secure_memory_available():
        connection.execute("PRAGMA cipher_memory_security = ON")
    version_row = connection.execute("PRAGMA cipher_version").fetchone()
    version = str(version_row[0] if version_row else "").strip()
    if not version:
        raise SQLCipherUnavailableError(
            "Loaded sqlite driver does not expose SQLCipher; refusing plaintext fallback"
        )


def connect_encrypted_database(
    database: str | os.PathLike[str],
    *args: Any,
    key: bytes | None = None,
    key_storage_dir: Path | None = None,
    validate: bool = True,
    **kwargs: Any,
):
    """Open one keyed SQLCipher connection and verify the key immediately."""

    _require_driver()
    if not SQLCIPHER_AVAILABLE:
        # Tests use standard sqlite3 only behind an explicit process-level gate.
        return _stdlib_sqlite3.connect(database, *args, **kwargs)

    path_text = os.fspath(database)
    if key is None:
        key = _registered_database_key(database, uri=bool(kwargs.get("uri")))
    if key is None:
        if key_storage_dir is None:
            if path_text == ":memory:" or path_text.startswith("file::memory:"):
                key_storage_dir = Path(tempfile.gettempdir()) / "lansetspbot-sqlcipher-test"
            else:
                # URI connections are used only for read-only backup verification.
                plain_path = path_text
                if kwargs.get("uri") and plain_path.startswith("file:"):
                    plain_path = plain_path[5:].split("?", 1)[0]
                key_storage_dir = default_database_key_storage_dir(plain_path)
        key = derive_database_key(Path(key_storage_dir))
    if _database_registry_key(database, uri=bool(kwargs.get("uri"))) is not None:
        register_database_key(
            _database_registry_key(database, uri=bool(kwargs.get("uri"))) or database,
            key,
        )

    connection = _DRIVER.connect(database, *args, **kwargs)
    try:
        _apply_key(connection, key)
        if validate:
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except BaseException:
        connection.close()
        raise


def connect_plaintext_database(database: str | os.PathLike[str], *args: Any, **kwargs: Any):
    """Open an existing plaintext SQLite file through the SQLCipher library."""

    _require_driver()
    if not SQLCIPHER_AVAILABLE:
        if not plaintext_test_mode_enabled():
            raise SQLCipherUnavailableError("SQLCipher is required for migration")
        return _stdlib_sqlite3.connect(database, *args, **kwargs)
    connection = _DRIVER.connect(database, *args, **kwargs)
    try:
        version_row = connection.execute("PRAGMA cipher_version").fetchone()
        if not str(version_row[0] if version_row else "").strip():
            raise SQLCipherUnavailableError("Migration driver is not SQLCipher")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except BaseException:
        connection.close()
        raise


def verify_encrypted_database(
    path: Path,
    *,
    key: bytes,
    foreign_keys: bool = True,
    timeout: float = 5.0,
) -> int:
    """Verify SQLCipher, integrity, foreign keys and schema version."""

    path = Path(path)
    validate_private_regular_file(path)
    if SQLCIPHER_AVAILABLE and is_plaintext_sqlite(path):
        raise SQLCipherError(f"Database is still plaintext: {path}")
    connection = connect_encrypted_database(path, timeout=timeout, key=key)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            detail = "; ".join(str(row[0]) for row in integrity[:10])
            raise SQLCipherError(f"Encrypted database integrity_check failed: {detail}")
        if foreign_keys:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise SQLCipherError(
                    f"Encrypted database foreign_key_check failed: {violations[:5]!r}"
                )
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, *, state: str, database: Path) -> None:
    ensure_private_directory(path.parent)
    payload = json.dumps(
        {
            "version": MIGRATION_VERSION,
            "state": state,
            "database": database.name,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if not harden_private_file(temporary):
            raise DatabaseEncryptionMigrationError(
                f"Could not protect database migration journal {temporary}"
            )
        os.replace(temporary, path)
        if not harden_private_file(path):
            raise DatabaseEncryptionMigrationError(
                f"Could not protect database migration journal {path}"
            )
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _read_journal(path: Path, database: Path) -> str:
    try:
        validate_private_regular_file(path, max_bytes=64 * 1024)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, LocalFileSecurityError) as exc:
        raise DatabaseEncryptionMigrationError(
            f"Database migration journal is corrupted: {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or int(payload.get("version", -1)) != MIGRATION_VERSION
        or str(payload.get("database") or "") != database.name
    ):
        raise DatabaseEncryptionMigrationError(
            "Database migration journal belongs to another database or version"
        )
    state = str(payload.get("state") or "")
    if state not in {"prepared", "source_moved", "encrypted_active"}:
        raise DatabaseEncryptionMigrationError(
            f"Unknown database migration state: {state!r}"
        )
    return state


def _safe_unlink(path: Path) -> None:
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:
        raise DatabaseEncryptionMigrationError(f"Could not inspect {path}: {exc}") from exc
    if not exists:
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DatabaseEncryptionMigrationError(
            f"Refusing unsafe database migration artifact: {path}"
        )
    path.unlink()


def recover_database_encryption_migration(path: Path, *, key: bytes) -> None:
    """Recover every crash point of the atomic plaintext migration."""

    database = Path(path)
    journal, temporary, rollback = migration_artifacts(database)
    if not journal.exists():
        # Unjournaled artifacts are never trusted or silently removed.
        for candidate in (temporary, rollback):
            if candidate.exists() or candidate.is_symlink():
                raise DatabaseEncryptionMigrationError(
                    f"Unjournaled database migration artifact requires manual review: {candidate}"
                )
        return

    _read_journal(journal, database)
    active_exists = database.exists() or database.is_symlink()
    temp_exists = temporary.exists() or temporary.is_symlink()
    rollback_exists = rollback.exists() or rollback.is_symlink()

    # If an encrypted active database is already valid, migration committed even
    # if the process crashed before deleting the rollback copy or journal.
    if active_exists and not is_plaintext_sqlite(database):
        verify_encrypted_database(database, key=key)
        _safe_unlink(temporary)
        _safe_unlink(rollback)
        _safe_unlink(journal)
        _fsync_directory(database.parent)
        return

    # If the source was moved away but the verified encrypted temp remains, finish
    # the atomic activation. Otherwise restore the plaintext rollback exactly.
    if not active_exists and temp_exists:
        try:
            verify_encrypted_database(temporary, key=key)
        except Exception:
            if rollback_exists:
                os.replace(rollback, database)
                _safe_unlink(temporary)
                _safe_unlink(journal)
                _fsync_directory(database.parent)
                return
            raise
        os.replace(temporary, database)
        verify_encrypted_database(database, key=key)
        _safe_unlink(rollback)
        _safe_unlink(journal)
        _fsync_directory(database.parent)
        return

    if rollback_exists:
        if active_exists:
            _safe_unlink(database)
        os.replace(rollback, database)
        _safe_unlink(temporary)
        _safe_unlink(journal)
        _fsync_directory(database.parent)
        return

    # A prepared journal with the original plaintext database still present is
    # safe to restart from scratch.
    if active_exists and is_plaintext_sqlite(database):
        _safe_unlink(temporary)
        _safe_unlink(journal)
        _fsync_directory(database.parent)
        return

    raise DatabaseEncryptionMigrationError(
        "Database encryption migration cannot be recovered automatically"
    )


def migrate_plaintext_database(path: Path, *, key: bytes, timeout: float = 30.0) -> None:
    """Atomically convert one ordinary SQLite database into SQLCipher."""

    database = Path(path)
    if not is_plaintext_sqlite(database):
        return
    if not SQLCIPHER_AVAILABLE:
        if plaintext_test_mode_enabled():
            return
        _require_driver()

    journal, temporary, rollback = migration_artifacts(database)
    recover_database_encryption_migration(database, key=key)
    if not is_plaintext_sqlite(database):
        return

    validate_private_regular_file(database)
    _write_journal(journal, state="prepared", database=database)
    _safe_unlink(temporary)
    _safe_unlink(rollback)

    source = connect_plaintext_database(database, timeout=timeout)
    attached = False
    try:
        quick = str(source.execute("PRAGMA quick_check").fetchone()[0]).strip().lower()
        if quick != "ok":
            raise DatabaseEncryptionMigrationError(
                f"Plaintext database quick_check failed before migration: {quick}"
            )
        # Replay and checkpoint any durable WAL before exporting.
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        user_version = int(source.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(source.execute("PRAGMA application_id").fetchone()[0])
        key_literal = f'"x\'{bytes(key).hex()}\'"'
        source.execute(
            f"ATTACH DATABASE ? AS encrypted KEY {key_literal}",
            (str(temporary),),
        )
        attached = True
        source.execute("PRAGMA encrypted.cipher_page_size = 4096")
        source.execute("SELECT sqlcipher_export('encrypted')").fetchone()
        source.execute(f"PRAGMA encrypted.user_version = {user_version}")
        source.execute(f"PRAGMA encrypted.application_id = {application_id}")
        source.execute("DETACH DATABASE encrypted")
        attached = False
    except Exception as exc:
        if attached:
            try:
                source.execute("DETACH DATABASE encrypted")
            except Exception:
                pass
        raise DatabaseEncryptionMigrationError(
            f"Could not export plaintext marlen.db into SQLCipher: {exc}"
        ) from exc
    finally:
        source.close()

    # The source connection has checkpointed and closed. Remove plaintext
    # rollback sidecars before activating the encrypted file so neither leaked
    # WAL pages nor stale shared-memory state can survive the migration.
    for sidecar in (
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    ):
        _safe_unlink(sidecar)

    if not harden_private_file(temporary):
        raise DatabaseEncryptionMigrationError(
            f"Could not restrict encrypted migration file {temporary}"
        )
    verify_encrypted_database(temporary, key=key)
    # ``os.fsync`` maps to the MSVCRT ``_commit`` call on Windows.  Open the
    # exported file for update rather than read-only so the durability barrier
    # is valid on both Windows and POSIX before the atomic activation.
    with temporary.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(database.parent)

    os.replace(database, rollback)
    _write_journal(journal, state="source_moved", database=database)
    os.replace(temporary, database)
    _write_journal(journal, state="encrypted_active", database=database)
    verify_encrypted_database(database, key=key)
    _safe_unlink(rollback)
    _safe_unlink(journal)
    _fsync_directory(database.parent)


def prepare_encrypted_database(path: Path, *, key_storage_dir: Path | None = None) -> bytes:
    """Recover/migrate the database and return its derived SQLCipher key."""

    database = Path(path)
    storage_dir = Path(key_storage_dir or database.parent)
    if database.exists():
        validate_private_regular_file(database)
        if database.stat().st_size == 0:
            raise SQLCipherError(
                "Existing marlen.db is empty; refusing to initialize it as a new database"
            )
    key = derive_database_key(storage_dir)
    register_database_key(database, key)
    recover_database_encryption_migration(database, key=key)
    if database.exists() and is_plaintext_sqlite(database):
        migrate_plaintext_database(database, key=key)
    return key


class _DBAPIProxy:
    """Expose sqlite-compatible names while forcing keyed production connects."""

    def connect(self, database: str | os.PathLike[str], *args: Any, **kwargs: Any):
        return connect_encrypted_database(database, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(_DRIVER, name)


# Production storage modules import this as ``sqlite3`` so their existing Row,
# Error and Connection references remain compatible with the selected driver.
dbapi = _DBAPIProxy()
