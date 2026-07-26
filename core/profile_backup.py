"""Fail-closed profile backup and restore for LansetSpBot.

Version 3 exports only a verified encrypted SQLCipher snapshot. Credentials and live Telegram
sessions are deliberately excluded because the ZIP format is not encrypted or
authenticated. Legacy version-1 archives can be inspected and their database can
be restored, but embedded credentials and sessions are never activated.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from storage.sqlcipher_driver import dbapi as sqlite3
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from core.crypto_vault import OSBoundMasterKeyProvider
from core.local_security import ensure_private_directory, harden_private_file
from core.paths import APP_PATHS, AppPaths
from core.secret_store import SecretStore
from core.version import __version__
from storage.database import Database
from storage.sqlcipher_driver import (
    connect_encrypted_database,
    connect_plaintext_database,
    derive_database_key,
    verify_encrypted_database,
)

BACKUP_FORMAT = "marlen-profile-backup"
BACKUP_FORMAT_VERSION = 3
SUPPORTED_BACKUP_FORMAT_VERSIONS = frozenset({1, 2, 3})
BACKUP_SUFFIX = ".marlen-backup.zip"
MANIFEST_NAME = "manifest.json"
DATABASE_MEMBER = "profile/marlen.db"
SECRETS_MEMBER = "profile/secrets.json"
SESSIONS_PREFIX = "profile/sessions/"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBERS = 128
MAX_MANIFEST_BYTES = 512 * 1024
RESTORE_JOURNAL_VERSION = 1


class ProfileBackupError(RuntimeError):
    """Raised when a profile archive cannot be created or trusted."""


class ProfileRestoreError(ProfileBackupError):
    """Raised when activation or rollback of a validated profile fails."""

    def __init__(self, message: str, *, profile_restored: bool = True) -> None:
        super().__init__(message)
        self.profile_restored = bool(profile_restored)


@dataclass(frozen=True)
class ProfileBackupResult:
    path: Path
    schema_version: int
    file_count: int
    contains_sessions: bool


@dataclass(frozen=True)
class ProfileBackupInfo:
    path: Path
    schema_version: int
    created_at: str
    file_count: int
    contains_sessions: bool
    app_version: str


@dataclass(frozen=True)
class ProfileRestoreResult:
    schema_version: int
    file_count: int
    contained_sessions: bool
    previous_database_backup: Path | None


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str, max_bytes: int | None = None) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProfileBackupError(f"Не удалось проверить {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProfileBackupError(f"{label} должен быть обычным файлом: {path}")
    if max_bytes is not None and info.st_size > max_bytes:
        raise ProfileBackupError(f"{label} превышает допустимый размер: {path}")


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


def _online_sqlite_backup(source: Path, destination: Path) -> int:
    """Create an encrypted SQLCipher snapshot bound to the current OS profile."""

    _regular_file(source, label="SQLCipher-базу")
    destination.parent.mkdir(parents=True, exist_ok=True)
    key = derive_database_key(source.parent)
    source_connection = None
    destination_connection = None
    try:
        source_connection = connect_encrypted_database(
            source, timeout=30.0, check_same_thread=False, key=key
        )
        destination_connection = connect_encrypted_database(
            destination, timeout=30.0, key=key, validate=False
        )
        source_connection.backup(destination_connection)
        destination_connection.commit()
    except Exception as exc:
        raise ProfileBackupError(f"Не удалось создать зашифрованный SQLCipher-снимок: {exc}") from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
    if not harden_private_file(destination):
        destination.unlink(missing_ok=True)
        raise ProfileBackupError(
            f"Не удалось ограничить права SQLCipher-снимка: {destination}"
        )
    try:
        return verify_encrypted_database(destination, key=key, foreign_keys=True)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise ProfileBackupError(f"Проверка SQLCipher-снимка не пройдена: {exc}") from exc


def _verify_sqlite(
    path: Path,
    *,
    foreign_keys: bool,
    encrypted: bool = True,
    key_storage_dir: Path | None = None,
) -> int:
    _regular_file(path, label="SQLite-файл")
    connection = None
    try:
        if encrypted:
            key = derive_database_key(Path(key_storage_dir or path.parent))
            return verify_encrypted_database(
                path, key=key, foreign_keys=foreign_keys
            )
        connection = connect_plaintext_database(path, timeout=5.0)
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            detail = "; ".join(str(row[0]) for row in integrity[:10])
            raise ProfileBackupError(f"SQLite integrity_check не пройден: {detail}")
        if foreign_keys:
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ProfileBackupError(
                    f"SQLite foreign_key_check обнаружил нарушения: {violations[:5]!r}"
                )
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    except ProfileBackupError:
        raise
    except Exception as exc:
        raise ProfileBackupError(
            f"SQLite-файл повреждён, имеет неверный ключ или недоступен: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _quiesce_restored_operations(
    path: Path, *, key_storage_dir: Path
) -> None:
    """Require an explicit user action before any restored operation can run."""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_encrypted_database(
            path, timeout=5.0, key_storage_dir=key_storage_dir
        )
        connection.execute("BEGIN IMMEDIATE")
        reason = "Восстановлено из backup — требуется ручное продолжение"
        connection.execute(
            """UPDATE comment_campaigns
               SET status='paused', pause_reason=?, network_retry_at=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE status IN ('running','network_wait','cycle_wait','scheduled')""",
            (reason,),
        )
        connection.execute(
            """UPDATE join_campaigns
               SET status='paused', pause_reason=?, network_retry_at=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE status IN ('running','network_wait','cycle_wait','scheduled')""",
            (reason,),
        )
        connection.execute(
            """UPDATE comment_schedule
               SET status='pending', task_id=NULL, result=?, executed_at=NULL
               WHERE status IN ('queued','running')""",
            (reason,),
        )
        connection.execute(
            """UPDATE join_schedule
               SET status='pending', task_id=NULL, result=?, executed_at=NULL
               WHERE status IN ('queued','running')""",
            (reason,),
        )
        connection.execute(
            """UPDATE tasks
               SET status='cancelled', status_text=NULL, error=?, not_before=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE status IN ('pending','running','processing','paused')""",
            (reason,),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.Error as exc:
        if connection is not None:
            connection.rollback()
        raise ProfileBackupError(
            f"Не удалось безопасно остановить операции из backup: {exc}"
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _write_member(archive: ZipFile, source: Path, member: str) -> None:
    info = ZipInfo(member)
    info.date_time = datetime.now().timetuple()[:6]
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    with source.open("rb") as input_stream, archive.open(info, "w") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def _manifest_entry(path: Path, member: str, kind: str) -> dict[str, Any]:
    return {
        "path": member,
        "kind": kind,
        "size": int(path.stat().st_size),
        "sha256": _sha256(path),
    }


def _normalize_destination(destination: Path) -> Path:
    # Resolve only the parent. Resolving the full path would follow a final
    # symlink before the security check and could overwrite its target.
    raw = Path(destination).expanduser()
    parent = raw.parent.resolve()
    destination = parent / raw.name
    if destination.suffix.lower() != ".zip" or not destination.name.endswith(
        BACKUP_SUFFIX
    ):
        destination = destination.with_name(destination.stem + BACKUP_SUFFIX)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return destination
    except OSError as exc:
        raise ProfileBackupError(
            f"Не удалось проверить путь резервной копии: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ProfileBackupError("Отказано: путь резервной копии является symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ProfileBackupError("Путь резервной копии должен быть обычным файлом")
    return destination


def create_profile_backup(
    *,
    database_path: Path,
    session_dir: Path,
    secret_snapshot: dict[str, str],
    destination: Path,
    include_sessions: bool,
) -> ProfileBackupResult:
    """Create an atomic DB-only archive without exporting credentials.

    ``session_dir``, ``secret_snapshot`` and ``include_sessions`` remain in the
    signature for API compatibility. Version 3 intentionally ignores them.
    """

    del session_dir, secret_snapshot, include_sessions
    destination = _normalize_destination(destination)
    with tempfile.TemporaryDirectory(prefix="marlen-profile-backup-") as temp_name:
        temp = Path(temp_name)
        staged_db = temp / "marlen.db"
        schema_version = _online_sqlite_backup(Path(database_path), staged_db)

        sources: list[tuple[Path, str, str]] = [
            (staged_db, DATABASE_MEMBER, "database"),
        ]
        files = [_manifest_entry(path, member, kind) for path, member, kind in sources]
        manifest = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "app_version": __version__,
            "schema_version": schema_version,
            "created_at": datetime.now(UTC).isoformat(),
            "contains_secrets": False,
            "contains_sessions": False,
            "database_encrypted": True,
            "key_binding": "current_os_profile",
            "files": files,
        }
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ProfileBackupError("Manifest резервной копии неожиданно велик")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        temporary_archive = Path(temporary_name)
        temporary: Path | None = temporary_archive
        try:
            with ZipFile(
                temporary_archive, "w", compression=ZIP_DEFLATED, allowZip64=True
            ) as zf:
                for source, member, _kind in sources:
                    _write_member(zf, source, member)
                manifest_info = ZipInfo(MANIFEST_NAME)
                manifest_info.date_time = datetime.now().timetuple()[:6]
                manifest_info.compress_type = ZIP_DEFLATED
                manifest_info.external_attr = 0o600 << 16
                zf.writestr(manifest_info, manifest_bytes)
            if not harden_private_file(temporary_archive):
                raise ProfileBackupError(
                    "Не удалось ограничить права временной резервной копии"
                )
            # The final component was checked without following links above.
            # os.replace then atomically replaces the directory entry itself.
            os.replace(temporary_archive, destination)
            temporary = None
            if not harden_private_file(destination):
                destination.unlink(missing_ok=True)
                raise ProfileBackupError(
                    "Не удалось ограничить права резервной копии профиля"
                )
            _fsync_directory(destination.parent)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)

    return ProfileBackupResult(
        path=destination,
        schema_version=schema_version,
        file_count=len(files),
        contains_sessions=False,
    )


def _member_is_symlink(info: ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0o170000
    return mode == stat.S_IFLNK


def _validate_members(zf: ZipFile) -> dict[str, ZipInfo]:
    infos = zf.infolist()
    if not infos or len(infos) > MAX_MEMBERS:
        raise ProfileBackupError("Некорректное количество файлов в backup")
    by_name: dict[str, ZipInfo] = {}
    folded: set[str] = set()
    normalized: set[str] = set()
    total = 0
    for info in infos:
        name = str(info.filename)
        pure = PurePosixPath(name)
        if (
            not name
            or name.startswith(("/", "\\"))
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or "\\" in name
        ):
            raise ProfileBackupError(f"Небезопасное имя в backup: {name!r}")
        if _member_is_symlink(info):
            raise ProfileBackupError(f"Symlink запрещён в backup: {name}")
        if name in by_name:
            raise ProfileBackupError(f"Дубликат файла в backup: {name}")
        folded_name = name.casefold()
        normalized_name = unicodedata.normalize("NFC", name)
        if folded_name in folded or normalized_name in normalized:
            raise ProfileBackupError(f"Конфликтующее имя в backup: {name}")
        folded.add(folded_name)
        normalized.add(normalized_name)
        by_name[name] = info
        total += int(info.file_size)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ProfileBackupError("Backup превышает допустимый распакованный размер")
    return by_name


def _load_manifest(zf: ZipFile, members: dict[str, ZipInfo]) -> dict[str, Any]:
    info = members.get(MANIFEST_NAME)
    if info is None or info.file_size > MAX_MANIFEST_BYTES:
        raise ProfileBackupError("Manifest отсутствует или имеет неверный размер")
    try:
        payload = json.loads(zf.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise ProfileBackupError(f"Manifest повреждён: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileBackupError("Manifest должен быть JSON-объектом")
    if payload.get("format") != BACKUP_FORMAT:
        raise ProfileBackupError("Архив не является резервной копией LansetSpBot")
    try:
        format_version = int(payload.get("format_version", -1))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProfileBackupError("Некорректная версия формата backup") from exc
    if format_version not in SUPPORTED_BACKUP_FORMAT_VERSIONS:
        raise ProfileBackupError("Неподдерживаемая версия формата backup")
    payload["format_version"] = format_version
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ProfileBackupError("Manifest не содержит список файлов")
    return payload


def _validate_manifest_files(
    manifest: dict[str, Any], members: dict[str, ZipInfo]
) -> dict[str, dict[str, Any]]:
    version = int(manifest["format_version"])
    expected: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise ProfileBackupError("Некорректная запись файла в manifest")
        path = item.get("path")
        kind = item.get("kind")
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(path, str) or not isinstance(kind, str):
            raise ProfileBackupError("Manifest содержит некорректный путь или тип")
        if path in expected:
            raise ProfileBackupError(f"Manifest дублирует файл: {path}")
        if path == DATABASE_MEMBER:
            if kind != "database":
                raise ProfileBackupError("Основная база имеет неверный тип")
        elif version == 1 and path == SECRETS_MEMBER:
            if kind != "secrets":
                raise ProfileBackupError("Файл секретов имеет неверный тип")
        elif version == 1 and path.startswith(SESSIONS_PREFIX):
            name = path.removeprefix(SESSIONS_PREFIX)
            if (
                kind != "telegram_session"
                or not name
                or "/" in name
                or not name.endswith(".session")
            ):
                raise ProfileBackupError("Некорректный Telegram session в manifest")
        else:
            raise ProfileBackupError(f"Неизвестный файл в backup: {path}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest.lower())
            or not isinstance(size, int)
            or size < 0
        ):
            raise ProfileBackupError(f"Некорректный hash/size в manifest: {path}")
        info = members.get(path)
        if info is None or int(info.file_size) != size:
            raise ProfileBackupError(
                f"Файл отсутствует или имеет неверный размер: {path}"
            )
        expected[path] = item
    actual = set(members) - {MANIFEST_NAME}
    if set(expected) != actual:
        extras = sorted(actual - set(expected))
        missing = sorted(set(expected) - actual)
        raise ProfileBackupError(
            f"Состав backup не совпадает с manifest; лишние={extras}, отсутствуют={missing}"
        )
    if DATABASE_MEMBER not in expected:
        raise ProfileBackupError("Backup не содержит обязательную базу")
    if version == 1 and SECRETS_MEMBER not in expected:
        raise ProfileBackupError("Legacy backup не содержит обязательный файл секретов")

    actual_secrets = SECRETS_MEMBER in expected
    actual_sessions = any(
        item.get("kind") == "telegram_session" for item in expected.values()
    )
    if bool(manifest.get("contains_secrets")) != actual_secrets:
        raise ProfileBackupError("Флаг secrets не совпадает с содержимым backup")
    if bool(manifest.get("contains_sessions")) != actual_sessions:
        raise ProfileBackupError("Флаг sessions не совпадает с содержимым backup")
    if version >= 2 and (actual_secrets or actual_sessions):
        raise ProfileBackupError("Backup v2/v3 не может содержать секреты или sessions")
    if version >= 3:
        if manifest.get("database_encrypted") is not True:
            raise ProfileBackupError("Backup v3 должен содержать зашифрованную базу")
        if manifest.get("key_binding") != "current_os_profile":
            raise ProfileBackupError("Backup v3 имеет неизвестную привязку ключа")
    return expected


def _extract_validated(
    archive_path: Path, destination: Path, *, migrate_database: bool,
    key_storage_dir: Path,
) -> ProfileBackupInfo:
    archive_path = Path(archive_path).expanduser().resolve()
    _regular_file(
        archive_path, label="архив резервной копии", max_bytes=MAX_ARCHIVE_BYTES
    )
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with ZipFile(archive_path, "r") as zf:
            members = _validate_members(zf)
            manifest = _load_manifest(zf, members)
            expected = _validate_manifest_files(manifest, members)
            for member, metadata in expected.items():
                target = destination / PurePosixPath(member)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with (
                    zf.open(members[member], "r") as source,
                    target.open("wb") as output,
                ):
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > int(metadata["size"]):
                            raise ProfileBackupError(
                                f"Размер файла изменился при извлечении: {member}"
                            )
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if (
                    size != int(metadata["size"])
                    or digest.hexdigest() != str(metadata["sha256"]).lower()
                ):
                    raise ProfileBackupError(f"SHA-256 не совпадает: {member}")
                if not harden_private_file(target):
                    raise ProfileBackupError(
                        f"Не удалось ограничить права извлечённого файла: {member}"
                    )
    except BadZipFile as exc:
        raise ProfileBackupError(f"ZIP повреждён: {exc}") from exc

    database = destination / DATABASE_MEMBER
    encrypted_database = int(manifest["format_version"]) >= 3
    schema_version = _verify_sqlite(
        database, foreign_keys=True, encrypted=encrypted_database,
        key_storage_dir=key_storage_dir,
    )
    declared_version = int(manifest.get("schema_version", -1))
    if schema_version != declared_version:
        raise ProfileBackupError(
            "Версия SQLite не совпадает с manifest: "
            f"{schema_version} != {declared_version}"
        )
    if schema_version > Database.SCHEMA_VERSION:
        raise ProfileBackupError(
            f"Backup использует schema v{schema_version}, а эта версия LansetSpBot "
            f"поддерживает только v{Database.SCHEMA_VERSION}"
        )

    secrets_path = destination / SECRETS_MEMBER
    if secrets_path.exists():
        try:
            secret_payload = json.loads(secrets_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileBackupError(f"Файл секретов повреждён: {exc}") from exc
        SecretStore.validate_snapshot(secret_payload)

    for member, metadata in expected.items():
        if metadata["kind"] == "telegram_session":
            _verify_sqlite(
                destination / PurePosixPath(member),
                foreign_keys=False, encrypted=False,
            )

    if migrate_database and (
        not encrypted_database or schema_version < Database.SCHEMA_VERSION
    ):
        migrated = Database(
            database, busy_timeout_ms=1_000, key_storage_dir=key_storage_dir
        )
        try:
            pass
        finally:
            migrated.close_thread_connection()
        schema_version = _verify_sqlite(
            database, foreign_keys=True, encrypted=True,
            key_storage_dir=key_storage_dir,
        )
        if schema_version != Database.SCHEMA_VERSION:
            raise ProfileBackupError(
                "Migration staging-базы не довела schema до текущей версии"
            )

    if migrate_database:
        _quiesce_restored_operations(database, key_storage_dir=key_storage_dir)
        schema_version = _verify_sqlite(
            database, foreign_keys=True, encrypted=True,
            key_storage_dir=key_storage_dir,
        )

    return ProfileBackupInfo(
        path=archive_path,
        schema_version=schema_version,
        created_at=str(manifest.get("created_at") or ""),
        file_count=len(expected),
        contains_sessions=bool(manifest.get("contains_sessions")),
        app_version=str(manifest.get("app_version") or ""),
    )


def inspect_profile_backup(archive_path: Path) -> ProfileBackupInfo:
    """Fully validate a backup, including a migration on an isolated copy."""

    with tempfile.TemporaryDirectory(prefix="marlen-profile-inspect-") as temp_name:
        return _extract_validated(
            Path(archive_path), Path(temp_name) / "extracted", migrate_database=True,
            key_storage_dir=APP_PATHS.root,
        )


def _journal_path(paths: AppPaths) -> Path:
    return paths.root.parent / f".{paths.root.name}.profile-restore-transaction.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    temporary.write_bytes(encoded)
    if not harden_private_file(temporary):
        temporary.unlink(missing_ok=True)
        raise ProfileRestoreError("Не удалось защитить журнал восстановления")
    os.replace(temporary, path)
    if not harden_private_file(path):
        raise ProfileRestoreError("Не удалось защитить журнал восстановления")
    _fsync_directory(path.parent)


def _safe_child(path: Path, parent: Path, prefix: str) -> Path:
    resolved = path.resolve()
    parent = parent.resolve()
    if resolved.parent != parent or not resolved.name.startswith(prefix):
        raise ProfileRestoreError(f"Небезопасный путь restore-транзакции: {resolved}")
    return resolved


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        path.unlink()
        return
    shutil.rmtree(path)


def _build_staged_profile(
    extracted: Path, staged_root: Path, *, active_root: Path
) -> ProfileBackupInfo:
    info = _extract_validated(
        extracted, staged_root / ".validated", migrate_database=True,
        key_storage_dir=active_root,
    )
    validated = staged_root / ".validated" / "profile"
    final = staged_root / "profile"
    final.mkdir(parents=True, exist_ok=False)
    for directory in ("logs", "sessions", "backups"):
        ensure_private_directory(final / directory)
    # Windows keeps the DPAPI-wrapped master key in the profile root. Copy the
    # wrapped value (never the plaintext key) so the staged SQLCipher database
    # remains readable after the atomic root swap.
    wrapped_name = OSBoundMasterKeyProvider.WINDOWS_KEY_FILENAME
    wrapped_source = Path(active_root) / wrapped_name
    if wrapped_source.is_file() and not wrapped_source.is_symlink():
        shutil.copy2(wrapped_source, final / wrapped_name)
        if not harden_private_file(final / wrapped_name):
            raise ProfileRestoreError("Не удалось защитить обёрнутый ключ staged-профиля")
    shutil.move(str(validated / "marlen.db"), str(final / "marlen.db"))
    # Backup archives are not encrypted/authenticated. Never activate archived
    # credentials or Telegram auth keys, including those found in legacy v1
    # archives. The user must reconnect accounts and re-enter credentials.
    SecretStore(final / ".secrets.json").replace_snapshot({})
    _remove_tree(staged_root / ".validated")
    for candidate in [
        final / "marlen.db",
        final / ".secrets.json",
    ]:
        if not harden_private_file(candidate):
            raise ProfileRestoreError(
                f"Не удалось ограничить права staged-профиля: {candidate}"
            )
    return info


def _preserve_previous_database(
    rollback_root: Path, active_root: Path
) -> Path | None:
    previous = rollback_root / "marlen.db"
    if not previous.is_file():
        return None
    destination = active_root / "backups" / f"pre-restore-marlen-{_utc_stamp()}.db"
    # The rollback root still contains its DPAPI wrapper on Windows, so derive
    # the old key there and create the snapshot with that same key.
    key = derive_database_key(rollback_root)
    source_connection = connect_encrypted_database(previous, timeout=30.0, key=key)
    destination_connection = connect_encrypted_database(
        destination, timeout=30.0, key=key, validate=False
    )
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    if not harden_private_file(destination):
        destination.unlink(missing_ok=True)
        raise ProfileRestoreError("Не удалось защитить резерв предыдущей базы")
    verify_encrypted_database(destination, key=key, foreign_keys=True)
    return destination


def _verify_active_profile(paths: AppPaths, secret_path: Path) -> None:
    database = Database(paths.database, busy_timeout_ms=1_000)
    try:
        version = _verify_sqlite(
            paths.database, foreign_keys=True, encrypted=True,
            key_storage_dir=paths.root,
        )
        if version != Database.SCHEMA_VERSION:
            raise ProfileRestoreError(
                f"Активированная schema v{version}, ожидалась v{Database.SCHEMA_VERSION}"
            )
    finally:
        database.close_thread_connection()
    store = SecretStore(secret_path)
    store.export_snapshot()
    for session in paths.sessions.glob("*.session"):
        _verify_sqlite(session, foreign_keys=False, encrypted=False)


def restore_profile_backup(
    *, archive_path: Path, paths: AppPaths, secret_path: Path
) -> ProfileRestoreResult:
    """Validate, migrate and atomically activate a profile archive."""

    root = paths.root.resolve()
    parent = root.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{_utc_stamp()}"
    staging_container = _safe_child(
        parent / f".{root.name}.restore-staging-{token}",
        parent,
        f".{root.name}.restore-staging-",
    )
    rollback_root = _safe_child(
        parent / f".{root.name}.restore-rollback-{token}",
        parent,
        f".{root.name}.restore-rollback-",
    )
    failed_root = _safe_child(
        parent / f".{root.name}.restore-failed-{token}",
        parent,
        f".{root.name}.restore-failed-",
    )
    journal = _journal_path(paths)
    old_moved = False
    new_active = False
    try:
        staging_container.mkdir(parents=False, exist_ok=False)
        info = _build_staged_profile(
            Path(archive_path), staging_container, active_root=root
        )
        staged_profile = staging_container / "profile"
        _write_json_atomic(
            journal,
            {
                "version": RESTORE_JOURNAL_VERSION,
                "state": "prepared",
                "root": str(root),
                "staging": str(staging_container),
                "rollback": str(rollback_root),
                "failed": str(failed_root),
            },
        )
        if root.exists():
            os.replace(root, rollback_root)
            old_moved = True
        _write_json_atomic(
            journal,
            {
                "version": RESTORE_JOURNAL_VERSION,
                "state": "old_moved",
                "root": str(root),
                "staging": str(staging_container),
                "rollback": str(rollback_root),
                "failed": str(failed_root),
            },
        )
        os.replace(staged_profile, root)
        new_active = True
        _write_json_atomic(
            journal,
            {
                "version": RESTORE_JOURNAL_VERSION,
                "state": "new_active",
                "root": str(root),
                "staging": str(staging_container),
                "rollback": str(rollback_root),
                "failed": str(failed_root),
            },
        )
        _verify_active_profile(paths, secret_path)
        previous_database = (
            _preserve_previous_database(rollback_root, root) if old_moved else None
        )
        _write_json_atomic(
            journal,
            {
                "version": RESTORE_JOURNAL_VERSION,
                "state": "verified",
                "root": str(root),
                "staging": str(staging_container),
                "rollback": str(rollback_root),
                "failed": str(failed_root),
            },
        )
        _remove_tree(rollback_root)
        _remove_tree(staging_container)
        journal.unlink(missing_ok=True)
        _fsync_directory(parent)
        return ProfileRestoreResult(
            schema_version=info.schema_version,
            file_count=info.file_count,
            contained_sessions=info.contains_sessions,
            previous_database_backup=previous_database,
        )
    except Exception as exc:
        restored = True
        try:
            if new_active and root.exists():
                if failed_root.exists():
                    _remove_tree(failed_root)
                os.replace(root, failed_root)
            if old_moved and rollback_root.exists():
                os.replace(rollback_root, root)
            elif not old_moved and not root.exists():
                root.mkdir(parents=True, exist_ok=True)
            _remove_tree(failed_root)
            _remove_tree(staging_container)
            journal.unlink(missing_ok=True)
            _fsync_directory(parent)
        except Exception as rollback_exc:
            restored = False
            raise ProfileRestoreError(
                "Restore завершился ошибкой, и исходный профиль не удалось "
                f"вернуть полностью: {rollback_exc}",
                profile_restored=False,
            ) from exc
        if isinstance(exc, ProfileRestoreError):
            raise ProfileRestoreError(str(exc), profile_restored=restored) from exc
        if isinstance(exc, ProfileBackupError):
            raise ProfileRestoreError(str(exc), profile_restored=restored) from exc
        raise ProfileRestoreError(
            f"Не удалось восстановить профиль: {type(exc).__name__}: {exc}",
            profile_restored=restored,
        ) from exc


def recover_incomplete_profile_restore(paths: AppPaths) -> bool:
    """Rollback an interrupted restore before any runtime owner opens SQLite."""

    journal = _journal_path(paths)
    if not journal.is_file() or journal.is_symlink():
        return False
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or int(payload.get("version", -1)) != RESTORE_JOURNAL_VERSION
        ):
            raise ProfileRestoreError("Повреждён журнал восстановления профиля")
        parent = paths.root.resolve().parent
        root = Path(str(payload["root"])).resolve()
        if root != paths.root.resolve():
            raise ProfileRestoreError(
                "Журнал восстановления относится к другому профилю"
            )
        staging = _safe_child(
            Path(str(payload["staging"])), parent, f".{root.name}.restore-staging-"
        )
        rollback = _safe_child(
            Path(str(payload["rollback"])), parent, f".{root.name}.restore-rollback-"
        )
        failed = _safe_child(
            Path(str(payload["failed"])), parent, f".{root.name}.restore-failed-"
        )
        state = str(payload.get("state") or "")
        if state == "verified":
            _remove_tree(rollback)
            _remove_tree(staging)
            _remove_tree(failed)
            journal.unlink(missing_ok=True)
            return False
        if rollback.exists():
            if root.exists():
                _remove_tree(failed)
                os.replace(root, failed)
            os.replace(rollback, root)
            _remove_tree(failed)
        _remove_tree(staging)
        journal.unlink(missing_ok=True)
        _fsync_directory(parent)
        return state in {"old_moved", "new_active"}
    except Exception as exc:
        raise ProfileRestoreError(
            f"Не удалось безопасно завершить прерванный restore: {exc}",
            profile_restored=False,
        ) from exc
