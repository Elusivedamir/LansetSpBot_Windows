from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

from core.local_security import ensure_private_directory, harden_private_file


log = logging.getLogger(__name__)


LEGACY_ACCOUNT_SECRET_KEYS = (
    "telegram.api_hash",
    "telegram.phone",
    "telegram.proxy_username",
    "telegram.proxy_password",
    "openai.api_key",
)

ACCOUNT_LIFECYCLE_JOURNAL_PREFIX = "internal.account_lifecycle."
LIFECYCLE_OPERATIONS = frozenset(
    {"register", "reauthorize", "disconnect", "delete"}
)
LIFECYCLE_PHASES = frozenset(
    {"prepared", "session_moved", "session_swapped", "session_staged", "committed"}
)
HIDDEN_SESSION_BASE_RE = re.compile(
    r"^\.(?:swap|delete)_account_[1-9][0-9]*_[a-f0-9]{24}$"
)
SWAP_SESSION_RE = re.compile(
    r"^\.swap_(account_[1-9][0-9]*)_[a-f0-9]{24}\.session$"
)
DELETE_SESSION_RE = re.compile(
    r"^\.delete_(account_[1-9][0-9]*)_[a-f0-9]{24}\.session$"
)
PENDING_SESSION_FILE_RE = re.compile(
    r"^(pending_[a-f0-9]{16,64})\.session$"
)
ACCOUNT_SESSION_FILE_RE = re.compile(r"^(account_[1-9][0-9]*)\.session$")


def lifecycle_journal_key(account_id: int) -> str:
    owner = int(account_id)
    if owner <= 0:
        raise ValueError("Account lifecycle journal requires a positive account id")
    return f"{ACCOUNT_LIFECYCLE_JOURNAL_PREFIX}{owner}"


def write_account_lifecycle_journal(
    secret_store,
    *,
    account_id: int,
    operation: str,
    phase: str = "prepared",
    **payload: Any,
) -> dict[str, Any]:
    owner = int(account_id)
    operation = str(operation or "").strip().lower()
    phase = str(phase or "").strip().lower()
    if operation not in LIFECYCLE_OPERATIONS:
        raise ValueError(f"Unsupported account lifecycle operation: {operation!r}")
    if phase not in LIFECYCLE_PHASES:
        raise ValueError(f"Unsupported account lifecycle phase: {phase!r}")
    document: dict[str, Any] = {
        "version": 1,
        "account_id": owner,
        "operation": operation,
        "phase": phase,
    }
    document.update(payload)
    secret_store.set(
        lifecycle_journal_key(owner),
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return document


def update_account_lifecycle_journal(
    secret_store,
    journal: dict[str, Any],
    *,
    phase: str,
    **updates: Any,
) -> dict[str, Any]:
    document = dict(journal)
    document.update(updates)
    return write_account_lifecycle_journal(
        secret_store,
        account_id=int(document["account_id"]),
        operation=str(document["operation"]),
        phase=phase,
        **{
            key: value
            for key, value in document.items()
            if key not in {"version", "account_id", "operation", "phase"}
        },
    )


def clear_account_lifecycle_journal(secret_store, account_id: int) -> None:
    secret_store.delete(lifecycle_journal_key(account_id))


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
    swap_name: str | None = None,
) -> Iterator[str]:
    """Atomically swap a verified pending session for an existing account session."""

    pending = session_base(Path(session_dir), pending_session_name)
    destination_name = f"account_{int(telegram_account_id)}"
    destination = session_base(Path(session_dir), destination_name)
    pending_file = pending.with_suffix(".session")
    if not pending_file.exists():
        raise RuntimeError("Authorized temporary Telegram session is missing")

    chosen_swap = str(
        swap_name or f".swap_{destination.name}_{secrets.token_hex(12)}"
    )
    if (
        not HIDDEN_SESSION_BASE_RE.fullmatch(chosen_swap)
        or not chosen_swap.startswith(f".swap_{destination.name}_")
    ):
        raise ValueError("Unsafe Telegram session swap name")
    swap = destination.parent / chosen_swap
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
    tombstone_name: str | None = None,
) -> Iterator[dict[str, object]]:
    """Hide local authorization material until the database deletion commits."""

    source = session_base(Path(session_dir), session_name)
    chosen_tombstone = str(
        tombstone_name or f".delete_{source.name}_{secrets.token_hex(12)}"
    )
    if (
        not HIDDEN_SESSION_BASE_RE.fullmatch(chosen_tombstone)
        or not chosen_tombstone.startswith(f".delete_{source.name}_")
    ):
        raise ValueError("Unsafe Telegram session tombstone name")
    tombstone = source.parent / chosen_tombstone
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


def _hidden_session_base(root: Path, name: object) -> Path:
    clean = str(name or "").strip()
    if not HIDDEN_SESSION_BASE_RE.fullmatch(clean):
        raise RuntimeError(f"Unsafe hidden Telegram session name: {clean!r}")
    candidate = (root / clean).resolve()
    if candidate.parent != root:
        raise RuntimeError("Hidden Telegram session path escapes its directory")
    return candidate


def _restore_account_row(database, account_id: int, payload: dict[str, Any]) -> None:
    old = dict(payload.get("old_account") or {})
    if not old:
        return
    with database.get_connection() as conn:
        cursor = conn.execute(
            """UPDATE telegram_accounts
               SET session_name=?, display_name=?, username=?, phone_masked=?,
                   authorized=?, runtime_state=?, stopped=?, last_error=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE telegram_account_id=?""",
            (
                str(old.get("session_name") or f"account_{account_id}"),
                str(old.get("display_name") or "Telegram Account"),
                str(old.get("username") or "") or None,
                str(old.get("phone_masked") or ""),
                1 if old.get("authorized") else 0,
                str(old.get("runtime_state") or "stopped"),
                1 if old.get("stopped") else 0,
                str(old.get("last_error") or "") or None,
                int(account_id),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Could not restore Telegram account row")


def _restore_account_snapshot(database, secret_store, payload: dict[str, Any]) -> None:
    from services.account_context import account_secret_key

    owner = int(payload["account_id"])
    _restore_account_row(database, owner, payload)
    database.replace_account_settings(owner, dict(payload.get("old_public") or {}))
    for key, value in dict(payload.get("old_secrets") or {}).items():
        secret_store.set(account_secret_key(owner, str(key)), value)
    selected = int(payload.get("selected_before") or 0)
    if selected > 0 and database.get_telegram_account(selected):
        database.select_telegram_account(selected)


def _delete_account_secrets(secret_store, owner: int, keys: list[str]) -> None:
    from services.account_context import account_secret_key

    for key in keys:
        secret_store.delete(account_secret_key(owner, str(key)))


def recover_account_lifecycle(
    database,
    secret_store,
    session_dir: Path,
) -> dict[str, object]:
    """Recover cross-resource account operations from encrypted SecretStore."""

    root = Path(session_dir).expanduser().resolve()
    ensure_private_directory(root)
    snapshot = secret_store.export_snapshot()
    journals = sorted(
        (key, value)
        for key, value in snapshot.items()
        if str(key).startswith(ACCOUNT_LIFECYCLE_JOURNAL_PREFIX)
    )
    recovered = 0
    for key, raw in journals:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unreadable account lifecycle journal: {key}") from exc
        if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
            raise RuntimeError(f"Unsupported account lifecycle journal: {key}")
        owner = int(payload.get("account_id") or 0)
        operation = str(payload.get("operation") or "")
        phase = str(payload.get("phase") or "")
        if key != lifecycle_journal_key(owner):
            raise RuntimeError(f"Account lifecycle journal owner mismatch: {key}")
        if operation not in LIFECYCLE_OPERATIONS or phase not in LIFECYCLE_PHASES:
            raise RuntimeError(f"Invalid account lifecycle journal state: {key}")

        pending_name = str(payload.get("pending_session_name") or "")
        final_name = str(payload.get("final_session_name") or f"account_{owner}")
        final_base = session_base(root, final_name)
        pending_base = session_base(root, pending_name) if pending_name else None
        account = database.get_telegram_account(owner)

        if operation == "register":
            if (
                phase == "committed"
                and account is not None
                and final_base.with_suffix(".session").exists()
            ):
                clear_account_lifecycle_journal(secret_store, owner)
                recovered += 1
                continue
            if account is not None and not database.rollback_new_telegram_account(
                owner, expected_session_name=final_name
            ):
                raise RuntimeError(
                    "Interrupted registration owns durable account data; "
                    "automatic rollback was refused"
                )
            _delete_account_secrets(
                secret_store,
                owner,
                [str(item) for item in payload.get("secret_keys") or []],
            )
            _purge_session_family(final_base)
            if pending_base is not None:
                _purge_session_family(pending_base)

        elif operation == "reauthorize":
            swap = _hidden_session_base(root, payload.get("swap_name"))
            if phase == "committed":
                _purge_session_family(swap)
                if pending_base is not None:
                    _purge_session_family(pending_base)
            else:
                if swap.with_suffix(".session").exists():
                    if final_base.with_suffix(".session").exists():
                        _purge_session_family(final_base)
                    _move_session_family(swap, final_base)
                if pending_base is not None:
                    _purge_session_family(pending_base)
                if account is None:
                    raise RuntimeError(
                        "Cannot restore interrupted reauthorization: account is missing"
                    )
                _restore_account_snapshot(database, secret_store, payload)

        elif operation == "disconnect":
            tombstone = _hidden_session_base(root, payload.get("tombstone_name"))
            # If the database already says authorization is revoked, the
            # tombstoned session must not be resurrected after a crash.
            if (
                phase == "committed"
                or account is None
                or not bool(account.get("authorized"))
            ):
                _purge_session_family(tombstone)
                _purge_session_family(final_base)
            else:
                if (
                    not final_base.with_suffix(".session").exists()
                    and tombstone.with_suffix(".session").exists()
                ):
                    _move_session_family(tombstone, final_base)
                _restore_account_snapshot(database, secret_store, payload)

        elif operation == "delete":
            tombstone = _hidden_session_base(root, payload.get("tombstone_name"))
            if phase == "committed" or account is None:
                _purge_session_family(tombstone)
                _purge_session_family(final_base)
                _delete_account_secrets(
                    secret_store,
                    owner,
                    [str(item) for item in payload.get("secret_keys") or []],
                )
            else:
                if not final_base.with_suffix(".session").exists() and tombstone.with_suffix(
                    ".session"
                ).exists():
                    _move_session_family(tombstone, final_base)
                _restore_account_snapshot(database, secret_store, payload)

        clear_account_lifecycle_journal(secret_store, owner)
        recovered += 1
    return {"recovered": recovered, "directory": str(root)}


def recover_session_residues(database, session_dir: Path) -> dict[str, object]:
    """Remove or restore hidden session families after lifecycle recovery."""

    root = Path(session_dir).expanduser().resolve()
    ensure_private_directory(root)
    accounts = list(database.list_telegram_accounts())
    live_names = {
        validate_session_name(account.get("session_name") or "")
        for account in accounts
        if bool(account.get("authorized"))
    }
    restored = 0
    purged = 0

    for session_file in sorted(root.glob(".delete_account_*.session")):
        match = DELETE_SESSION_RE.fullmatch(session_file.name)
        if not match:
            continue
        live_name = match.group(1)
        hidden = session_file.with_suffix("")
        live = session_base(root, live_name)
        if live_name in live_names and not live.with_suffix(".session").exists():
            _move_session_family(hidden, live)
            restored += 1
        else:
            _purge_session_family(hidden)
            purged += 1

    for session_file in sorted(root.glob(".swap_account_*.session")):
        match = SWAP_SESSION_RE.fullmatch(session_file.name)
        if not match:
            continue
        live_name = match.group(1)
        hidden = session_file.with_suffix("")
        live = session_base(root, live_name)
        if live_name in live_names and not live.with_suffix(".session").exists():
            _move_session_family(hidden, live)
            restored += 1
        else:
            _purge_session_family(hidden)
            purged += 1

    for session_file in sorted(root.glob("pending_*.session")):
        match = PENDING_SESSION_FILE_RE.fullmatch(session_file.name)
        if match:
            _purge_session_family(session_base(root, match.group(1)))
            purged += 1

    for session_file in sorted(root.glob("account_*.session")):
        match = ACCOUNT_SESSION_FILE_RE.fullmatch(session_file.name)
        if match and match.group(1) not in live_names:
            _purge_session_family(session_base(root, match.group(1)))
            purged += 1

    return {
        "restored": restored,
        "purged": purged,
        "directory": str(root),
    }
