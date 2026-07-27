from __future__ import annotations

import os
import re
import time
from pathlib import Path

from core.local_security import ensure_private_directory


LEGACY_ACCOUNT_SECRET_KEYS = (
    "telegram.api_hash",
    "telegram.phone",
    "telegram.proxy_username",
    "telegram.proxy_password",
    "telegram.proxy_secret",
    "openai.api_key",
)


def migrate_legacy_account_secrets(database, secret_store) -> dict[str, object]:
    """Move former single-account secrets into the first account namespace."""

    owner = int(database.get_selected_account_id() or 0)
    if owner <= 0 or not database.get_telegram_account(owner):
        return {"migrated": 0, "reason": "no_selected_account"}
    getter = getattr(type(secret_store), "get_strict_optional", None)
    migrated = 0
    for key in LEGACY_ACCOUNT_SECRET_KEYS:
        target = f"account.{owner}.{key}"
        current = (
            secret_store.get_strict_optional(target)
            if callable(getter)
            else secret_store.get(target, "") or None
        )
        legacy = (
            secret_store.get_strict_optional(key)
            if callable(getter)
            else secret_store.get(key, "") or None
        )
        if current is None and legacy not in (None, ""):
            secret_store.set(target, str(legacy))
            verified = (
                secret_store.get_strict_optional(target)
                if callable(getter)
                else secret_store.get(target, "") or None
            )
            if verified != str(legacy):
                raise RuntimeError(f"Could not verify migrated secret {key}")
            migrated += 1
        if legacy not in (None, ""):
            secret_store.delete(key)
    return {"migrated": migrated, "account_id": owner}


SESSION_NAME_RE = re.compile(
    r"^(?:main|account_[1-9][0-9]*|pending_[a-f0-9]{16,64})$"
)


def validate_session_name(value: object) -> str:
    name = str(value or "").strip()
    if not SESSION_NAME_RE.fullmatch(name):
        raise ValueError("Unsafe Telegram session name")
    return name


def session_base(session_dir: Path, session_name: object) -> Path:
    root = Path(session_dir).expanduser().resolve()
    ensure_private_directory(root)
    name = validate_session_name(session_name)
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError("Telegram session path escapes the sessions directory")
    return candidate


def _replace_with_retry(source: Path, destination: Path) -> None:
    attempts = 10 if os.name == "nt" else 2
    for attempt in range(attempts):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.08 * (attempt + 1))


def _unlink_with_retry(path: Path) -> None:
    attempts = 10 if os.name == "nt" else 2
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.08 * (attempt + 1))


def _move_session_family(source_base: Path, destination_base: Path) -> None:
    source_file = source_base.with_suffix(".session")
    destination_file = destination_base.with_suffix(".session")
    if not source_file.exists():
        return
    if destination_file.exists():
        raise RuntimeError(
            f"Target Telegram session already exists: {destination_file.name}"
        )
    _replace_with_retry(source_file, destination_file)
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            source_sidecar = Path(f"{source_file}{suffix}")
            destination_sidecar = Path(f"{destination_file}{suffix}")
            if source_sidecar.exists():
                _replace_with_retry(source_sidecar, destination_sidecar)
                moved.append((destination_sidecar, source_sidecar))
        from services.telegram_service import TelegramService

        TelegramService._secure_session_file(destination_file)
    except Exception:
        for current, original in reversed(moved):
            if current.exists() and not original.exists():
                _replace_with_retry(current, original)
        if destination_file.exists() and not source_file.exists():
            _replace_with_retry(destination_file, source_file)
        raise


def migrate_legacy_main_session(database, session_dir: Path) -> dict[str, object]:
    """Rename one legacy main.session after v31 registered its owner.

    No copy or backup is made. A failed rename leaves the original path intact.
    """

    selected = int(database.get_selected_account_id() or 0)
    if selected <= 0:
        return {"migrated": False, "reason": "no_selected_account"}
    account = database.get_telegram_account(selected)
    if not account:
        return {"migrated": False, "reason": "account_missing"}
    current_name = validate_session_name(account.get("session_name") or "main")
    if current_name != "main":
        return {"migrated": False, "reason": "already_migrated"}

    source = session_base(Path(session_dir), "main")
    destination_name = f"account_{selected}"
    destination = session_base(Path(session_dir), destination_name)
    if not source.with_suffix(".session").exists():
        if destination.with_suffix(".session").exists():
            database.update_account_session_name(selected, destination_name)
            return {"migrated": True, "reason": "database_reconciled"}
        return {"migrated": False, "reason": "legacy_session_missing"}

    _move_session_family(source, destination)
    try:
        database.update_account_session_name(selected, destination_name)
    except Exception:
        _move_session_family(destination, source)
        raise
    return {
        "migrated": True,
        "account_id": selected,
        "session_name": destination_name,
    }


def finalize_pending_session(
    database,
    session_dir: Path,
    *,
    pending_session_name: str,
    telegram_account_id: int,
) -> str:
    pending = session_base(Path(session_dir), pending_session_name)
    destination_name = f"account_{int(telegram_account_id)}"
    destination = session_base(Path(session_dir), destination_name)
    if pending == destination:
        return destination_name
    _move_session_family(pending, destination)
    return destination_name


def rollback_finalized_session(
    session_dir: Path,
    *,
    pending_session_name: str,
    telegram_account_id: int,
) -> None:
    pending = session_base(Path(session_dir), pending_session_name)
    destination = session_base(
        Path(session_dir), f"account_{int(telegram_account_id)}"
    )
    if destination.with_suffix(".session").exists() and not pending.with_suffix(
        ".session"
    ).exists():
        _move_session_family(destination, pending)


def discard_pending_session(session_dir: Path, pending_session_name: str) -> None:
    """Remove only a validated temporary authorization session and sidecars."""

    pending = session_base(Path(session_dir), pending_session_name)
    session_file = pending.with_suffix(".session")
    for suffix in ("-wal", "-shm", "-journal"):
        _unlink_with_retry(Path(f"{session_file}{suffix}"))
    _unlink_with_retry(session_file)
