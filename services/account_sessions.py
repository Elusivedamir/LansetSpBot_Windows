from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from core.local_security import ensure_private_directory, harden_private_file


log = logging.getLogger(__name__)


LEGACY_ACCOUNT_SECRET_KEYS = (
    "telegram.api_hash",
    "telegram.phone",
    "telegram.proxy_username",
    "telegram.proxy_password",
    "telegram.proxy_secret",
    "openai.api_key",
)


def migrate_legacy_account_secrets(
    database,
    secret_store,
    *,
    keys=None,
    secret_lock=None,
) -> dict[str, object]:
    """Move every legacy SQLite/unnamespaced secret into protected account storage."""

    selected = int(database.get_selected_account_id() or 0)
    selected_exists = bool(
        selected > 0 and database.get_telegram_account(selected)
    )
    selected_owner = selected if selected_exists else 0
    key_list = tuple(sorted(str(key) for key in (keys or LEGACY_ACCOUNT_SECRET_KEYS)))
    getter = getattr(type(secret_store), "get_strict_optional", None)
    lock = secret_lock if secret_lock is not None else nullcontext()
    migrated = 0
    deleted = 0

    def strict_get(key: str):
        if callable(getter):
            return secret_store.get_strict_optional(key)
        return secret_store.get(key, "") or None

    def migrate_value(
        target: str,
        legacy: object,
        *,
        delete_source,
        label: str,
    ) -> None:
        nonlocal migrated, deleted
        if legacy in (None, ""):
            return
        current = strict_get(target)
        if current is None:
            secret_store.set(target, str(legacy))
            verified = strict_get(target)
            if verified != str(legacy):
                raise RuntimeError(
                    f"Could not verify protected secret migration for {label}"
                )
            migrated += 1
        delete_source()
        deleted += 1

    # The global SQLite compatibility mirror is the newest legacy source for the
    # selected account, so migrate it before older account_settings copies.
    for key in key_list:
        with lock:
            legacy = database.get_setting(key, "")
            target = f"account.{selected_owner}.{key}" if selected_owner > 0 else key
            migrate_value(
                target,
                legacy,
                delete_source=lambda key=key: database.delete_setting(key),
                label=f"settings:{key}",
            )

    # Older releases stored the same values under unnamespaced SecretStore keys.
    if selected_owner > 0:
        for key in key_list:
            with lock:
                legacy = strict_get(key)
                migrate_value(
                    f"account.{selected_owner}.{key}",
                    legacy,
                    delete_source=lambda key=key: secret_store.delete(key),
                    label=f"secret-store:{key}",
                )

    accounts = list(database.list_telegram_accounts())
    for account in accounts:
        owner = int(account.get("telegram_account_id") or 0)
        if owner <= 0:
            continue
        for key in key_list:
            with lock:
                legacy = database.get_account_setting(owner, key, "")
                migrate_value(
                    f"account.{owner}.{key}",
                    legacy,
                    delete_source=lambda owner=owner, key=key: (
                        database.delete_account_setting(owner, key)
                    ),
                    label=f"account_settings:{owner}:{key}",
                )

    remaining_global = [
        key for key in key_list if database.get_setting(key, "") not in (None, "")
    ]
    remaining_accounts = [
        (int(account.get("telegram_account_id") or 0), key)
        for account in accounts
        for key in key_list
        if int(account.get("telegram_account_id") or 0) > 0
        and database.get_account_setting(
            int(account.get("telegram_account_id") or 0), key, ""
        )
        not in (None, "")
    ]
    if remaining_global or remaining_accounts:
        raise RuntimeError(
            "Legacy SQLite secrets remain after protected migration"
        )
    return {
        "migrated": migrated,
        "deleted": deleted,
        "account_id": selected_owner,
        "accounts_checked": len(accounts),
    }


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


def _purge_session_family(base: Path) -> None:
    session_file = base.with_suffix(".session")
    for suffix in ("-wal", "-shm", "-journal"):
        _unlink_with_retry(Path(f"{session_file}{suffix}"))
    _unlink_with_retry(session_file)


SESSION_FAMILY_SUFFIXES = ("", "-wal", "-shm", "-journal")
MOVE_JOURNAL_RE = re.compile(r"^\.session_move_[a-f0-9]{24}\.json$")
MOVE_BASE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,180}$")


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_move_journal(source_base: Path, destination_base: Path) -> Path:
    source = Path(source_base).resolve()
    destination = Path(destination_base).resolve()
    if source.parent != destination.parent:
        raise RuntimeError("Telegram session move must stay in one directory")
    if not MOVE_BASE_RE.fullmatch(source.name) or not MOVE_BASE_RE.fullmatch(
        destination.name
    ):
        raise RuntimeError("Unsafe Telegram session move journal name")
    journal = source.parent / f".session_move_{secrets.token_hex(12)}.json"
    payload = {
        "version": 1,
        "source": source.name,
        "destination": destination.name,
    }
    with journal.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if not harden_private_file(journal):
        journal.unlink(missing_ok=True)
        raise RuntimeError("Could not protect Telegram session move journal")
    _fsync_directory(journal.parent)
    return journal


def _remove_move_journal(journal: Path) -> None:
    _unlink_with_retry(Path(journal))
    _fsync_directory(Path(journal).parent)


def _session_artifact(base: Path, suffix: str) -> Path:
    session_file = Path(base).with_suffix(".session")
    return session_file if not suffix else Path(f"{session_file}{suffix}")


def _recover_move_journal(journal: Path) -> None:
    if not harden_private_file(journal):
        raise RuntimeError(
            f"Could not protect Telegram session move journal: {journal.name}"
        )
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unreadable Telegram session move journal: {journal.name}"
        ) from exc
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        raise RuntimeError(
            f"Unsupported Telegram session move journal: {journal.name}"
        )
    source_name = str(payload.get("source") or "")
    destination_name = str(payload.get("destination") or "")
    if not MOVE_BASE_RE.fullmatch(source_name) or not MOVE_BASE_RE.fullmatch(
        destination_name
    ):
        raise RuntimeError(
            f"Unsafe Telegram session move journal: {journal.name}"
        )
    root = journal.parent.resolve()
    source = (root / source_name).resolve()
    destination = (root / destination_name).resolve()
    if source.parent != root or destination.parent != root:
        raise RuntimeError(
            f"Telegram session recovery escapes its directory: {journal.name}"
        )

    source_main = _session_artifact(source, "")
    destination_main = _session_artifact(destination, "")
    source_exists = source_main.exists()
    destination_exists = destination_main.exists()
    if source_exists and destination_exists:
        raise RuntimeError(
            "Conflicting Telegram session files require manual recovery: "
            f"{source_main.name}, {destination_main.name}"
        )

    if destination_exists:
        for suffix in SESSION_FAMILY_SUFFIXES[1:]:
            current = _session_artifact(source, suffix)
            target = _session_artifact(destination, suffix)
            if current.exists() and target.exists():
                raise RuntimeError(
                    "Conflicting Telegram session sidecars require manual recovery: "
                    f"{current.name}, {target.name}"
                )
            if current.exists():
                _replace_with_retry(current, target)
        from services.telegram_service import TelegramService

        TelegramService._secure_session_file(destination_main)
    elif source_exists:
        for suffix in SESSION_FAMILY_SUFFIXES[1:]:
            current = _session_artifact(destination, suffix)
            target = _session_artifact(source, suffix)
            if current.exists() and target.exists():
                raise RuntimeError(
                    "Conflicting Telegram session sidecars require manual recovery: "
                    f"{current.name}, {target.name}"
                )
            if current.exists():
                _replace_with_retry(current, target)
        from services.telegram_service import TelegramService

        TelegramService._secure_session_file(source_main)
    else:
        residues = [
            _session_artifact(base, suffix)
            for base in (source, destination)
            for suffix in SESSION_FAMILY_SUFFIXES[1:]
            if _session_artifact(base, suffix).exists()
        ]
        detail = (
            ": " + ", ".join(path.name for path in residues)
            if residues
            else ""
        )
        raise RuntimeError(
            "Telegram session move journal refers to two missing main databases"
            + detail
        )

    _remove_move_journal(journal)


def recover_interrupted_session_moves(session_dir: Path) -> dict[str, object]:
    """Complete or roll back interrupted session-family renames before Telethon starts."""

    root = Path(session_dir).expanduser().resolve()
    ensure_private_directory(root)
    recovered = 0
    for journal in sorted(root.glob(".session_move_*.json")):
        if not MOVE_JOURNAL_RE.fullmatch(journal.name):
            continue
        _recover_move_journal(journal)
        recovered += 1
    return {"recovered": recovered, "directory": str(root)}


def _move_session_family(source_base: Path, destination_base: Path) -> None:
    source_file = source_base.with_suffix(".session")
    destination_file = destination_base.with_suffix(".session")
    if not source_file.exists():
        return
    if destination_file.exists():
        raise RuntimeError(
            f"Target Telegram session already exists: {destination_file.name}"
        )
    journal = _write_move_journal(source_base, destination_base)
    moved: list[tuple[Path, Path]] = []
    try:
        _replace_with_retry(source_file, destination_file)
        for suffix in ("-wal", "-shm", "-journal"):
            source_sidecar = Path(f"{source_file}{suffix}")
            destination_sidecar = Path(f"{destination_file}{suffix}")
            if source_sidecar.exists():
                _replace_with_retry(source_sidecar, destination_sidecar)
                moved.append((destination_sidecar, source_sidecar))
        from services.telegram_service import TelegramService

        TelegramService._secure_session_file(destination_file)
    except BaseException:
        rollback_complete = True
        try:
            for current, original in reversed(moved):
                if current.exists() and not original.exists():
                    _replace_with_retry(current, original)
            if destination_file.exists() and not source_file.exists():
                _replace_with_retry(destination_file, source_file)
        except Exception:
            rollback_complete = False
            log.exception(
                "Telegram session move rollback was interrupted; journal retained: %s",
                journal,
            )
        if rollback_complete:
            try:
                _remove_move_journal(journal)
            except Exception:
                log.exception(
                    "Could not remove rolled-back Telegram session journal %s",
                    journal,
                )
        raise
    else:
        try:
            _remove_move_journal(journal)
        except Exception:
            # The family move already completed and is authoritative. Keep the
            # journal for idempotent startup cleanup instead of reporting a false
            # operation failure to the account/database layer.
            log.exception(
                "Telegram session move completed; cleanup journal retained: %s",
                journal,
            )


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
    if not pending.with_suffix(".session").exists():
        raise RuntimeError("Authorized temporary Telegram session is missing")
    _move_session_family(pending, destination)
    if not destination.with_suffix(".session").exists():
        raise RuntimeError("Authorized Telegram session was not finalized")
    return destination_name


@contextmanager
def replace_pending_session(
    session_dir: Path,
    *,
    pending_session_name: str,
    telegram_account_id: int,
) -> Iterator[str]:
    """Atomically swap a verified pending session for an existing account session."""

    pending = session_base(Path(session_dir), pending_session_name)
    destination_name = f"account_{int(telegram_account_id)}"
    destination = session_base(Path(session_dir), destination_name)
    pending_file = pending.with_suffix(".session")
    if not pending_file.exists():
        raise RuntimeError("Authorized temporary Telegram session is missing")

    swap = destination.parent / (
        f".swap_{destination.name}_{secrets.token_hex(12)}"
    )
    had_previous = destination.with_suffix(".session").exists()
    if had_previous:
        _move_session_family(destination, swap)
    try:
        _move_session_family(pending, destination)
        if not destination.with_suffix(".session").exists():
            raise RuntimeError("Authorized Telegram session replacement failed")
        yield destination_name
    except BaseException:
        if destination.with_suffix(".session").exists() and not pending_file.exists():
            _move_session_family(destination, pending)
        else:
            _purge_session_family(destination)
        if had_previous and swap.with_suffix(".session").exists():
            _move_session_family(swap, destination)
        raise
    else:
        try:
            _purge_session_family(swap)
        except Exception:
            # The new session and database state are already authoritative.
            # Do not report the reauthorization as failed and roll unrelated
            # secrets back merely because an obsolete hidden file is locked.
            log.exception(
                "Could not purge revoked previous Telegram session %s", swap
            )


@contextmanager
def stage_account_session_removal(
    session_dir: Path,
    *,
    session_name: str,
) -> Iterator[dict[str, object]]:
    """Hide local authorization material until the database deletion commits."""

    source = session_base(Path(session_dir), session_name)
    tombstone = source.parent / (
        f".delete_{source.name}_{secrets.token_hex(12)}"
    )
    cleanup: dict[str, object] = {"removed": False, "warning": ""}
    moved = source.with_suffix(".session").exists()
    if moved:
        _move_session_family(source, tombstone)
    try:
        yield cleanup
    except BaseException:
        if moved and tombstone.with_suffix(".session").exists():
            _move_session_family(tombstone, source)
        raise
    else:
        try:
            _purge_session_family(tombstone)
            session_file = source.with_suffix(".session")
            for pattern in (
                f"{session_file.name}.corrupt.*",
                f".{session_file.name}.restore.*.tmp",
            ):
                for artifact in session_file.parent.glob(pattern):
                    if artifact.is_file():
                        _unlink_with_retry(artifact)
            cleanup["removed"] = True
        except Exception as exc:
            # The account row is already deleted. Treat the random hidden file as
            # revoked residue and surface a warning without restoring secrets for
            # an account that no longer exists.
            cleanup["warning"] = str(exc)
            log.exception(
                "Could not purge revoked Telegram session residue %s",
                tombstone,
            )


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
