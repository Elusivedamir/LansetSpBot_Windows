"""Durable reconciliation between the Telethon session and SQLite identity."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

ACCOUNT_STATE_FILENAME = ".account-state-pending.json"
ACCOUNT_STATE_KEYS = frozenset(
    {
        "telegram.account_id",
        "telegram.account_name",
        "telegram.account_username",
        "telegram.authorized",
    }
)


class AccountStateError(RuntimeError):
    """Raised when a durable account-state transition cannot be completed."""


def pending_account_state_path(database_path: str | Path) -> Path:
    return (
        Path(database_path).expanduser().resolve(strict=False).parent
        / ACCOUNT_STATE_FILENAME
    )


def has_pending_account_state(database_path: str | Path) -> bool:
    return pending_account_state_path(database_path).is_file()


def _normalized(values: dict[str, Any]) -> dict[str, str]:
    if not isinstance(values, dict):
        raise AccountStateError("Account state must be an object")
    unknown = set(values) - ACCOUNT_STATE_KEYS
    if unknown:
        raise AccountStateError(f"Unsupported account-state keys: {sorted(unknown)}")
    result = {key: "" if value is None else str(value) for key, value in values.items()}
    missing = ACCOUNT_STATE_KEYS - set(result)
    if missing:
        raise AccountStateError(f"Incomplete account state: {sorted(missing)}")
    return result


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_pending(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fd = -1
    tmp: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        tmp = Path(name)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        _fsync_parent(path)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def persist_account_state(
    database_path: str | Path, values: dict[str, Any]
) -> dict[str, str]:
    """Persist an identity transition with an idempotent crash-recovery journal.

    The journal is written before SQLite. If SQLite or its commit fails, the
    intended state remains available for startup reconciliation and queue work is
    blocked until it is applied.
    """

    normalized = _normalized(values)
    path = pending_account_state_path(database_path)
    _write_pending(path, normalized)
    try:
        from storage.database import Database

        database = Database(database_path)
        try:
            database.set_settings(normalized)
        finally:
            database.close_thread_connection()
    except Exception as exc:
        raise AccountStateError(
            "Состояние Telegram-сессии сохранено в журнал восстановления, "
            f"но SQLite пока недоступна: {exc}"
        ) from exc
    path.unlink(missing_ok=True)
    _fsync_parent(path)
    return normalized


def reconcile_pending_account_state(database: Any) -> bool:
    """Apply a crash-interrupted account transition before schedulers start."""

    path = pending_account_state_path(database.path)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = _normalized(payload)
        database.set_settings(values)
        path.unlink(missing_ok=True)
        _fsync_parent(path)
        return True
    except Exception as exc:
        raise AccountStateError(
            f"Не удалось восстановить состояние Telegram-аккаунта из {path}: {exc}"
        ) from exc
