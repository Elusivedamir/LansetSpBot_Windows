"""Safe, rollback-capable removal of Marlen's local user state.

Only explicitly owned application artifacts are removed. The helper never
recursively deletes an arbitrary ``MARLEN_DATA_DIR`` root, because that path can
be overridden by the environment and could otherwise point at a user directory.
"""

from __future__ import annotations

import json
import os
import stat
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.account_state import pending_account_state_path
from core.crypto_vault import OSBoundMasterKeyProvider, VaultError
from core.local_security import harden_private_file
from core.paths import AppPaths
from storage.sqlcipher_driver import migration_artifacts


FACTORY_RESET_JOURNAL_NAME = ".factory-reset-transaction.json"
FACTORY_RESET_JOURNAL_VERSION = 1
FACTORY_RESET_JOURNAL_MAX_BYTES = 64 * 1024
_DIRECTORY_FSYNC = os.fsync


class FactoryResetError(RuntimeError):
    """Raised when local artifacts could not be removed or restored safely."""

    def __init__(self, message: str, *, profile_restored: bool = True) -> None:
        super().__init__(message)
        self.profile_restored = bool(profile_restored)


def _owned_database_path(database_path: Path, root: Path) -> Path:
    """Return the resolved DB path only when it is contained by Marlen's root."""

    resolved_root = Path(root).expanduser().resolve(strict=False)
    resolved_database = Path(database_path).expanduser().resolve(strict=False)
    try:
        relative = resolved_database.relative_to(resolved_root)
    except ValueError as exc:
        raise FactoryResetError(
            "Заводской сброс отменён: SQLite-файл расположен вне каталога "
            f"данных приложения ({resolved_database}). Внешние базы никогда не удаляются."
        ) from exc
    if not relative.parts or resolved_database == resolved_root:
        raise FactoryResetError(
            "Заводской сброс отменён: путь SQLite не указывает на файл внутри "
            "каталога данных приложения."
        )
    return resolved_database


def _owned_artifact_path(path: Path, root: Path, *, label: str) -> Path:
    """Validate a known artifact path lexically without following its symlink."""

    expanded_root = Path(root).expanduser().absolute()
    expanded_path = Path(path).expanduser().absolute()
    try:
        relative = expanded_path.relative_to(expanded_root)
    except ValueError as exc:
        raise FactoryResetError(
            f"Заводской сброс отменён: {label} расположен вне каталога данных приложения "
            f"({expanded_path})."
        ) from exc
    if not relative.parts or expanded_path == expanded_root:
        raise FactoryResetError(f"Заводской сброс отменён: некорректный путь {label}.")
    return expanded_path


@dataclass(frozen=True)
class FactoryResetResult:
    removed_files: int
    removed_directories: int


@dataclass(frozen=True)
class _SnapshotEntry:
    original: Path
    archive_name: str


@dataclass(frozen=True)
class _ManagedResetTargets:
    root: Path
    file_targets: tuple[Path, ...]
    owned_directories: tuple[Path, ...]

    @property
    def all_targets(self) -> tuple[Path, ...]:
        return self.file_targets + self.owned_directories


def _managed_reset_targets(
    *, database_path: Path, paths: AppPaths, secret_path: Path | None
) -> _ManagedResetTargets:
    root = Path(paths.root)
    owned_database = _owned_database_path(Path(database_path), root)
    local_secret_path = _owned_artifact_path(
        Path(secret_path or (root / ".secrets.json")),
        root,
        label="локальное хранилище секретов",
    )
    owned_directories = tuple(
        _owned_artifact_path(Path(directory), root, label=f"каталог {name}")
        for name, directory in (
            ("sessions", paths.sessions),
            ("backups", paths.backups),
            ("logs", paths.logs),
        )
    )
    migration_journal, migration_temp, migration_rollback = migration_artifacts(
        owned_database
    )
    file_targets = tuple(
        sorted(
            {
                owned_database,
                Path(f"{owned_database}-wal"),
                Path(f"{owned_database}-shm"),
                Path(f"{owned_database}-journal"),
                migration_journal,
                migration_temp,
                migration_rollback,
                local_secret_path,
                _owned_artifact_path(
                    root / OSBoundMasterKeyProvider.WINDOWS_KEY_FILENAME,
                    root,
                    label="обёрнутый мастер-ключ шифрования",
                ),
                pending_account_state_path(owned_database),
            },
            key=lambda item: str(item),
        )
    )
    return _ManagedResetTargets(
        root=root,
        file_targets=file_targets,
        owned_directories=owned_directories,
    )


def _journal_path(root: Path) -> Path:
    return Path(root) / FACTORY_RESET_JOURNAL_NAME


def _fsync_directory(directory: Path) -> None:
    """Durably persist directory entry changes where the OS supports it."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    descriptor = os.open(str(directory), flags)
    try:
        _DIRECTORY_FSYNC(descriptor)
    finally:
        os.close(descriptor)


def _write_reset_journal(
    *,
    root: Path,
    state: str,
    snapshot_path: Path,
    entries: tuple[_SnapshotEntry, ...],
) -> Path:
    """Atomically persist the destructive reset phase before any deletion."""

    if state not in {"prepared", "profile_rebuilt"}:
        raise FactoryResetError(f"Неизвестная фаза Factory Reset: {state}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    destination = _journal_path(root)
    payload = {
        "version": FACTORY_RESET_JOURNAL_VERSION,
        "state": state,
        "snapshot": snapshot_path.name,
        "entries": [
            {
                "path": entry.original.relative_to(root).as_posix(),
                "archive_name": entry.archive_name,
            }
            for entry in entries
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > FACTORY_RESET_JOURNAL_MAX_BYTES:
        raise FactoryResetError("Журнал Factory Reset неожиданно велик")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(root)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if not harden_private_file(temporary):
            raise PermissionError(
                f"Не удалось защитить журнал Factory Reset: {temporary}"
            )
        os.replace(temporary, destination)
        if not harden_private_file(destination):
            raise PermissionError(
                f"Не удалось защитить журнал Factory Reset: {destination}"
            )
        _fsync_directory(root)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _remove_reset_journal(root: Path) -> None:
    path = _journal_path(root)
    path.unlink(missing_ok=True)
    _fsync_directory(Path(root))


def _read_reset_journal(
    *, root: Path, allowed_targets: tuple[Path, ...]
) -> tuple[str, Path, tuple[_SnapshotEntry, ...]] | None:
    path = _journal_path(root)
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:
        raise FactoryResetError(
            f"Не удалось проверить журнал незавершённого Factory Reset: {exc}",
            profile_restored=False,
        ) from exc
    if not exists:
        return None
    if path.is_symlink() or not path.is_file():
        raise FactoryResetError(
            "Обнаружен небезопасный журнал Factory Reset", profile_restored=False
        )
    info = path.stat()
    if info.st_nlink != 1:
        raise FactoryResetError(
            "Обнаружен hardlink-журнал Factory Reset", profile_restored=False
        )
    if info.st_size > FACTORY_RESET_JOURNAL_MAX_BYTES:
        raise FactoryResetError(
            "Журнал Factory Reset повреждён или слишком велик", profile_restored=False
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FactoryResetError(
            f"Не удалось прочитать журнал Factory Reset: {exc}",
            profile_restored=False,
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != FACTORY_RESET_JOURNAL_VERSION
    ):
        raise FactoryResetError(
            "Неизвестная версия журнала Factory Reset", profile_restored=False
        )
    state = str(payload.get("state") or "")
    if state not in {"prepared", "profile_rebuilt"}:
        raise FactoryResetError(
            "Некорректная фаза журнала Factory Reset", profile_restored=False
        )
    snapshot_name = str(payload.get("snapshot") or "")
    if (
        not snapshot_name
        or Path(snapshot_name).name != snapshot_name
        or not snapshot_name.startswith(".factory-reset-rollback-")
        or not snapshot_name.endswith(".tar")
    ):
        raise FactoryResetError(
            "Некорректный rollback-снимок в журнале Factory Reset",
            profile_restored=False,
        )
    snapshot_path = Path(root) / snapshot_name

    allowed = {
        target.relative_to(root).as_posix(): target for target in allowed_targets
    }
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise FactoryResetError(
            "В журнале Factory Reset отсутствует карта восстановления",
            profile_restored=False,
        )
    entries: list[_SnapshotEntry] = []
    seen_paths: set[str] = set()
    seen_archives: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise FactoryResetError(
                "Повреждена карта восстановления Factory Reset",
                profile_restored=False,
            )
        relative = str(raw.get("path") or "")
        archive_name = str(raw.get("archive_name") or "")
        expected_archive = f"artifact-{index}"
        if (
            relative not in allowed
            or relative in seen_paths
            or archive_name != expected_archive
            or archive_name in seen_archives
        ):
            raise FactoryResetError(
                "Журнал Factory Reset содержит недопустимый путь",
                profile_restored=False,
            )
        seen_paths.add(relative)
        seen_archives.add(archive_name)
        entries.append(_SnapshotEntry(allowed[relative], archive_name))
    return state, snapshot_path, tuple(entries)


def _cleanup_reset_workspaces(targets: _ManagedResetTargets) -> list[str]:
    """Remove only Marlen-created reset workspaces left by a hard crash."""

    errors: list[str] = []
    prefixes = [
        ".factory-reset-restore-",
        f".{FACTORY_RESET_JOURNAL_NAME}.",
        *(
            f".{directory.name}.factory-reset-"
            for directory in targets.owned_directories
        ),
    ]
    try:
        candidates = tuple(targets.root.iterdir())
    except FileNotFoundError:
        return errors
    except Exception as exc:  # noqa: BLE001
        return [f"просмотр временных каталогов Factory Reset: {exc}"]

    for candidate in candidates:
        if not any(candidate.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            if candidate.is_dir() and not _is_link_like(candidate):
                _clear_directory(candidate)
            else:
                _unlink(candidate)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"очистка временного объекта {candidate}: {type(exc).__name__}: {exc}"
            )
    return errors


def _delete_os_bound_master_key(root: Path) -> None:
    try:
        OSBoundMasterKeyProvider(Path(root)).delete()
    except VaultError as exc:
        raise FactoryResetError(
            f"Не удалось удалить мастер-ключ локального шифрования: {exc}",
            profile_restored=False,
        ) from exc


def recover_incomplete_factory_reset(
    *, database_path: Path, paths: AppPaths, secret_path: Path | None = None
) -> bool:
    """Recover a hard-crashed reset before any database is opened or created.

    ``prepared`` means destructive work may have started and the old profile must
    be restored. ``profile_rebuilt`` is written only after the new empty profile
    passed schema and integrity validation; in that phase startup only removes
    the residual private snapshot/journal.
    """

    targets = _managed_reset_targets(
        database_path=database_path, paths=paths, secret_path=secret_path
    )
    journal = _read_reset_journal(
        root=targets.root, allowed_targets=targets.all_targets
    )
    if journal is None:
        return False
    state, snapshot_path, entries = journal
    workspace_errors = _cleanup_reset_workspaces(targets)
    if workspace_errors:
        raise FactoryResetError(
            "Не удалось очистить временные данные незавершённого Factory Reset: "
            + "; ".join(workspace_errors),
            profile_restored=False,
        )

    if state == "profile_rebuilt":
        _delete_os_bound_master_key(targets.root)
        if _exists(snapshot_path):
            if (
                snapshot_path.is_symlink()
                or not snapshot_path.is_file()
                or snapshot_path.stat().st_nlink != 1
            ):
                raise FactoryResetError(
                    "Небезопасный rollback-снимок после Factory Reset",
                    profile_restored=False,
                )
            snapshot_path.unlink()
            _fsync_directory(targets.root)
        _remove_reset_journal(targets.root)
        return False

    if (
        not _exists(snapshot_path)
        or snapshot_path.is_symlink()
        or not snapshot_path.is_file()
    ):
        raise FactoryResetError(
            "Factory Reset был прерван после начала удаления, но безопасный "
            "rollback-снимок отсутствует. Автоматический запуск заблокирован.",
            profile_restored=False,
        )

    cleanup_errors = _cleanup_managed_targets(
        file_targets=targets.file_targets,
        owned_directories=targets.owned_directories,
    )
    if cleanup_errors:
        raise FactoryResetError(
            "Не удалось очистить частичный профиль перед восстановлением: "
            + "; ".join(cleanup_errors),
            profile_restored=False,
        )
    restore_errors = _restore_rollback_snapshot(
        root=targets.root, snapshot_path=snapshot_path, entries=entries
    )
    if restore_errors:
        raise FactoryResetError(
            "Автоматическое восстановление незавершённого Factory Reset "
            "выполнено не полностью: " + "; ".join(restore_errors),
            profile_restored=False,
        )
    workspace_errors = _cleanup_reset_workspaces(targets)
    if workspace_errors:
        raise FactoryResetError(
            "Профиль восстановлен, но временные данные Factory Reset удалить не удалось: "
            + "; ".join(workspace_errors),
            profile_restored=False,
        )
    snapshot_path.unlink()
    _fsync_directory(targets.root)
    _remove_reset_journal(targets.root)
    return True


def _exists(path: Path) -> bool:
    return path.exists() or _is_link_like(path)


def _is_link_like(path: Path) -> bool:
    """Return True for symlinks and Windows directory junctions."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _remove_link_like(path: Path) -> None:
    """Remove one link/reparse-point entry without traversing its target."""

    if path.is_symlink():
        path.unlink()
        return
    # Windows junctions are directory entries and must be removed with rmdir.
    if _is_link_like(path):
        path.rmdir()
        return
    raise FactoryResetError(f"Ожидалась ссылка или junction: {path}")


def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
    """Compare the durable identity and file type of one filesystem object."""

    return (
        int(before.st_dev) == int(after.st_dev)
        and int(before.st_ino) == int(after.st_ino)
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
    )


def _unlink(path: Path) -> bool:
    if not _exists(path):
        return False
    if _is_link_like(path):
        _remove_link_like(path)
        return True
    if path.is_dir():
        raise IsADirectoryError(path)
    path.unlink()
    return True


def _clear_directory(directory: Path) -> tuple[int, int]:
    """Delete one managed directory without following a swapped path.

    Validation and recursive deletion must not be separated by a path lookup.
    The managed directory entry is therefore moved atomically into a private,
    same-filesystem quarantine directory first.  If an attacker or racing
    process replaces the source entry between ``lstat`` and ``rename``, the
    moved object's identity no longer matches and the reset fails closed before
    any traversal.  A symlink/junction is removed as an entry only.
    """

    if not _exists(directory):
        return 0, 0
    if _is_link_like(directory):
        _remove_link_like(directory)
        return 1, 0
    if not directory.is_dir():
        directory.unlink()
        return 1, 0

    expected = directory.lstat()
    quarantine_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{directory.name}.factory-reset-",
            dir=str(directory.parent),
        )
    )
    quarantined = quarantine_parent / "artifact"
    try:
        directory.rename(quarantined)
        actual = quarantined.lstat()
        if not _same_identity(expected, actual):
            raise FactoryResetError(
                "Заводской сброс остановлен: управляемый каталог был заменён "
                f"между проверкой и удалением ({directory})."
            )

        if _is_link_like(quarantined):
            _remove_link_like(quarantined)
            return 1, 0
        if not stat.S_ISDIR(actual.st_mode):
            quarantined.unlink()
            return 1, 0

        files = 0
        directories = 1  # the managed directory itself
        for child in list(quarantined.iterdir()):
            if child.is_dir() and not _is_link_like(child):
                directories += 1
            else:
                files += 1
        shutil.rmtree(quarantined)
        return files, directories
    except Exception:
        # If the verified object was merely quarantined and deletion failed,
        # put it back so the outer rollback can restore a coherent profile.
        if _exists(quarantined) and not _exists(directory):
            try:
                quarantined.rename(directory)
            except Exception:
                pass
        raise
    finally:
        try:
            quarantine_parent.rmdir()
        except OSError:
            # A residual entry is safer than crossing the ownership boundary.
            pass


def _remove_for_restore(path: Path) -> None:
    if not _exists(path):
        return
    if path.is_dir() and not _is_link_like(path):
        shutil.rmtree(path)
    elif _is_link_like(path):
        _remove_link_like(path)
    else:
        path.unlink()


def _restore_artifact(snapshot: Path, destination: Path) -> None:
    """Restore one snapshot entry without following a saved symlink."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.is_symlink():
        _remove_for_restore(destination)
        destination.symlink_to(os.readlink(snapshot))
        return

    if snapshot.is_dir():
        if _exists(destination) and (
            _is_link_like(destination) or not destination.is_dir()
        ):
            _remove_for_restore(destination)
        shutil.copytree(
            snapshot,
            destination,
            symlinks=True,
            copy_function=shutil.copy2,
            dirs_exist_ok=True,
        )
        return

    if _exists(destination) and destination.is_dir() and not _is_link_like(destination):
        shutil.rmtree(destination)
    elif _is_link_like(destination):
        _remove_link_like(destination)
    shutil.copy2(snapshot, destination, follow_symlinks=False)


def _create_rollback_snapshot(
    root: Path, targets: tuple[Path, ...]
) -> tuple[Path | None, tuple[_SnapshotEntry, ...]]:
    """Create one owner-only tar snapshot before the first destructive operation."""

    existing = tuple(target for target in targets if _exists(target))
    if not existing:
        return None, ()

    root.mkdir(parents=True, exist_ok=True)
    descriptor, snapshot_name = tempfile.mkstemp(
        prefix=".factory-reset-rollback-", suffix=".tar", dir=str(root)
    )
    os.close(descriptor)
    snapshot_path = Path(snapshot_name)
    entries = tuple(
        _SnapshotEntry(original=target, archive_name=f"artifact-{index}")
        for index, target in enumerate(existing)
    )
    try:
        with tarfile.open(snapshot_path, mode="w", dereference=False) as archive:
            for entry in entries:
                archive.add(
                    entry.original,
                    arcname=entry.archive_name,
                    recursive=True,
                )
        # On Windows ``os.fsync`` maps to ``_commit`` and requires a
        # descriptor opened for writing.  Reopening the completed tar as
        # read-only caused ``OSError: [Errno 9] Bad file descriptor`` before
        # any reset deletion could begin.
        with snapshot_path.open("r+b") as snapshot_file:
            os.fsync(snapshot_file.fileno())
        if not harden_private_file(snapshot_path):
            raise PermissionError(
                f"Не удалось защитить rollback-снимок: {snapshot_path}"
            )
        _fsync_directory(root)
    except Exception:
        snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path, entries


def _restore_rollback_snapshot(
    *, root: Path, snapshot_path: Path, entries: tuple[_SnapshotEntry, ...]
) -> list[str]:
    """Restore every owned artifact from an intact pre-delete snapshot."""

    errors: list[str] = []
    restore_root = Path(
        tempfile.mkdtemp(prefix=".factory-reset-restore-", dir=str(root))
    )
    try:
        with tarfile.open(snapshot_path, mode="r") as archive:
            archive.extractall(restore_root, filter="data")
        for entry in entries:
            try:
                _restore_artifact(restore_root / entry.archive_name, entry.original)
            except Exception as exc:  # noqa: BLE001 - report every rollback failure
                errors.append(f"восстановление {entry.original}: {exc}")
    except Exception as exc:  # noqa: BLE001 - archive must remain available for diagnosis
        errors.append(f"чтение rollback-снимка {snapshot_path}: {exc}")
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)
    return errors


def _rollback_and_raise(
    *,
    root: Path,
    snapshot_path: Path | None,
    entries: tuple[_SnapshotEntry, ...],
    original_error: str,
    journal_path: Path | None = None,
) -> None:
    rollback_errors: list[str] = []
    if snapshot_path is not None:
        rollback_errors = _restore_rollback_snapshot(
            root=root,
            snapshot_path=snapshot_path,
            entries=entries,
        )
        try:
            snapshot_path.unlink()
        except Exception as exc:  # noqa: BLE001 - residual snapshot must be disclosed
            rollback_errors.append(f"удаление rollback-снимка {snapshot_path}: {exc}")

    if journal_path is not None:
        try:
            journal_path.unlink(missing_ok=True)
            _fsync_directory(root)
        except Exception as exc:  # noqa: BLE001 - residual journal must be disclosed
            rollback_errors.append(
                f"удаление журнала Factory Reset {journal_path}: {exc}"
            )

    if rollback_errors:
        raise FactoryResetError(
            "Сброс прерван, а автоматическое восстановление выполнено не полностью: "
            f"{original_error}; " + "; ".join(rollback_errors),
            profile_restored=False,
        )
    raise FactoryResetError(
        "Сброс выполнен не полностью; удалённые файлы локального профиля "
        "восстановлены из rollback-снимка: " + original_error
    )


def _cleanup_managed_targets(
    *, file_targets: tuple[Path, ...], owned_directories: tuple[Path, ...]
) -> list[str]:
    """Remove partial post-reset artifacts before restoring the snapshot.

    A post-reset initializer may already have recreated SQLite sidecars or one
    of Marlen's managed directories before failing.  Restoring the old profile
    on top of those partial artifacts could leave a mixed old/new state, so the
    complete managed target set is cleared first.  Unknown files in the profile
    root remain untouched.
    """

    errors: list[str] = []
    for target in file_targets:
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                _unlink(target)
        except Exception as exc:  # noqa: BLE001 - disclose every cleanup failure
            errors.append(f"очистка {target}: {type(exc).__name__}: {exc}")
    for directory in owned_directories:
        try:
            _clear_directory(directory)
        except Exception as exc:  # noqa: BLE001 - disclose every cleanup failure
            errors.append(f"очистка {directory}: {type(exc).__name__}: {exc}")
    return errors


def reset_local_state(
    *,
    database_path: Path,
    paths: AppPaths,
    secret_path: Path | None = None,
    post_reset_initializer: Callable[[], None] | None = None,
) -> FactoryResetResult:
    """Remove every persisted local profile artifact owned by Marlen atomically.

    Before the first delete, every existing owned artifact is copied into one
    owner-only local rollback archive. Any failure during deletion, recreation
    of the empty runtime profile, or final archive removal restores the complete
    pre-reset profile before the error is returned. After the rollback-capable
    phase commits, the operation also removes the OS-bound encryption-key record
    from Windows DPAPI storage.
    """

    targets = _managed_reset_targets(
        database_path=database_path, paths=paths, secret_path=secret_path
    )
    root = targets.root
    database_path = _owned_database_path(Path(database_path), root)
    file_targets = targets.file_targets
    owned_directories = targets.owned_directories
    all_targets = targets.all_targets

    # A previous hard crash must be resolved before beginning a new destructive
    # transaction. This is also important for bounded Windows lock retries.
    recover_incomplete_factory_reset(
        database_path=database_path, paths=paths, secret_path=secret_path
    )

    try:
        snapshot_path, snapshot_entries = _create_rollback_snapshot(root, all_targets)
    except Exception as exc:
        raise FactoryResetError(
            "Заводской сброс отменён до удаления данных: не удалось создать "
            f"rollback-снимок: {exc}"
        ) from exc

    journal_path: Path | None = None
    if snapshot_path is not None:
        try:
            journal_path = _write_reset_journal(
                root=root,
                state="prepared",
                snapshot_path=snapshot_path,
                entries=snapshot_entries,
            )
        except Exception as exc:
            snapshot_path.unlink(missing_ok=True)
            raise FactoryResetError(
                "Заводской сброс отменён до удаления данных: не удалось "
                f"зафиксировать durable-журнал: {exc}"
            ) from exc

    removed_files = 0
    removed_directories = 0
    try:
        for target in file_targets:
            removed_files += int(_unlink(target))
        for directory in owned_directories:
            files, directories = _clear_directory(directory)
            removed_files += files
            removed_directories += directories
    except Exception as exc:  # noqa: BLE001 - rollback restores all earlier deletions
        _rollback_and_raise(
            root=root,
            snapshot_path=snapshot_path,
            entries=snapshot_entries,
            original_error=f"удаление локальных данных: {type(exc).__name__}: {exc}",
            journal_path=journal_path,
        )

    if post_reset_initializer is not None:
        try:
            post_reset_initializer()
        except Exception as exc:  # noqa: BLE001 - rollback restores pre-reset profile
            cleanup_errors = _cleanup_managed_targets(
                file_targets=file_targets,
                owned_directories=owned_directories,
            )
            if cleanup_errors or snapshot_path is None:
                details = "; ".join(cleanup_errors) if cleanup_errors else ""
                suffix = f"; {details}" if details else ""
                if snapshot_path is not None:
                    restore_errors = _restore_rollback_snapshot(
                        root=root,
                        snapshot_path=snapshot_path,
                        entries=snapshot_entries,
                    )
                    if restore_errors:
                        suffix += "; " + "; ".join(restore_errors)
                    try:
                        snapshot_path.unlink()
                    except Exception as unlink_exc:  # noqa: BLE001
                        suffix += (
                            f"; удаление rollback-снимка {snapshot_path}: {unlink_exc}"
                        )
                if journal_path is not None:
                    try:
                        journal_path.unlink(missing_ok=True)
                        _fsync_directory(root)
                    except Exception as journal_exc:  # noqa: BLE001
                        suffix += f"; удаление журнала Factory Reset: {journal_exc}"
                raise FactoryResetError(
                    "Сброс удалил локальные данные, но пустой рабочий профиль "
                    "создать не удалось. Приложение нельзя безопасно продолжать: "
                    f"{type(exc).__name__}: {exc}{suffix}",
                    profile_restored=False,
                ) from exc
            _rollback_and_raise(
                root=root,
                snapshot_path=snapshot_path,
                entries=snapshot_entries,
                original_error=(
                    f"создание пустого рабочего профиля: {type(exc).__name__}: {exc}"
                ),
                journal_path=journal_path,
            )

    if snapshot_path is not None:
        try:
            journal_path = _write_reset_journal(
                root=root,
                state="profile_rebuilt",
                snapshot_path=snapshot_path,
                entries=snapshot_entries,
            )
        except Exception as exc:  # noqa: BLE001 - old profile is still recoverable
            _rollback_and_raise(
                root=root,
                snapshot_path=snapshot_path,
                entries=snapshot_entries,
                original_error=(
                    "фиксация завершённой фазы Factory Reset: "
                    f"{type(exc).__name__}: {exc}"
                ),
                journal_path=journal_path,
            )
        try:
            snapshot_path.unlink()
            _fsync_directory(root)
        except Exception as exc:  # noqa: BLE001 - intact archive permits full rollback
            try:
                journal_path = _write_reset_journal(
                    root=root,
                    state="prepared",
                    snapshot_path=snapshot_path,
                    entries=snapshot_entries,
                )
            except Exception as journal_exc:
                raise FactoryResetError(
                    "Новый профиль создан, но rollback-снимок и durable-журнал "
                    "не удалось безопасно завершить: "
                    f"{type(exc).__name__}: {exc}; journal: {journal_exc}",
                    profile_restored=False,
                ) from exc
            _rollback_and_raise(
                root=root,
                snapshot_path=snapshot_path,
                entries=snapshot_entries,
                original_error=(
                    f"удаление временного rollback-снимка: {type(exc).__name__}: {exc}"
                ),
                journal_path=journal_path,
            )
        try:
            _remove_reset_journal(root)
        except Exception as exc:
            # The journal is already in ``profile_rebuilt`` state and the old
            # snapshot is gone. A following startup will retry only journal
            # cleanup and will never recreate or overwrite user data.
            raise FactoryResetError(
                "Заводской сброс завершён, но не удалось удалить durable-журнал: "
                f"{type(exc).__name__}: {exc}",
                profile_restored=False,
            ) from exc

    # Rotate the OS-bound encryption key only after the rollback snapshot and
    # durable journal are gone. At this point the old encrypted profile cannot
    # be revived. The validation database created above used the old key, so it
    # must be recreated once more under the fresh DPAPI master key.
    try:
        _delete_os_bound_master_key(root)
        for candidate in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
            Path(f"{database_path}-journal"),
            *migration_artifacts(database_path),
        ):
            removed_files += int(_unlink(candidate))
        if post_reset_initializer is not None:
            post_reset_initializer()
    except Exception as exc:
        raise FactoryResetError(
            "Заводской сброс удалил старый профиль, но не смог создать новую "
            "зашифрованную базу после ротации ключа: "
            f"{type(exc).__name__}: {exc}",
            profile_restored=False,
        ) from exc

    # The single-instance lock is intentionally preserved until the caller has
    # completed the reset. Releasing or deleting it here would let a second
    # process start against a profile that is only partially removed.
    try:
        root.rmdir()
        removed_directories += 1
    except OSError:
        # Unknown files are intentionally preserved. Never recursively remove
        # an environment-overridden root directory.
        pass

    return FactoryResetResult(
        removed_files=removed_files,
        removed_directories=removed_directories,
    )
