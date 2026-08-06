from __future__ import annotations

import math
from typing import Any

from core.boot_clock import current_boot_identity, steady_time


def _positive_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def persist_account_flood_wait(
    *,
    worker_db: Any,
    account_id: int,
    retry_at: Any,
    code: str,
    wait_seconds: int,
    source_task_id: int | None = None,
) -> dict[str, Any]:
    """Persist one account-wide FloodWait without requiring a queue worker."""

    owner = max(0, int(account_id or 0))
    wait = max(1, int(wait_seconds or 0))
    if owner <= 0:
        raise ValueError("Account FloodWait requires a positive account_id")

    writer = getattr(worker_db, "set_account_rpc_cooldown", None)
    if not callable(writer):
        raise RuntimeError("Database cannot persist account RPC cooldowns")
    return dict(
        writer(
            account_id=owner,
            retry_at=retry_at,
            code=str(code or "flood_wait_deferred"),
            source_task_id=(
                int(source_task_id) if source_task_id is not None else None
            ),
            wait_seconds=wait,
        )
        or {}
    )


def persisted_account_flood_wait_remaining(
    *, worker_db: Any, account_id: int
) -> int:
    """Return a boot-safe remaining persisted FloodWait for a fresh process.

    A standalone runner has no QueueWorker process-local deadline. It therefore
    reuses the database's boot identity, monotonic deadline and conservative
    fallback wait so a reboot or wall-clock change cannot bypass Telegram's wait.
    """

    owner = max(0, int(account_id or 0))
    if owner <= 0:
        raise ValueError("Account FloodWait requires a positive account_id")
    reader = getattr(worker_db, "get_account_rpc_cooldown", None)
    if not callable(reader):
        raise RuntimeError("Database cannot read account RPC cooldowns")
    data = dict(reader(account_id=owner) or {})
    persisted_key = str(data.get("next_allowed_at") or "")
    if not persisted_key:
        return 0

    now = steady_time()
    boot_id = current_boot_identity()
    row_boot_id = str(data.get("boot_id") or "")
    row_deadline = _positive_float(data.get("steady_deadline"))
    try:
        stored_fallback = max(0, int(data.get("fallback_wait_seconds") or 0))
    except (TypeError, ValueError, OverflowError):
        stored_fallback = 0
    try:
        wall_remaining = max(0, int(data.get("remaining_seconds") or 0))
    except (TypeError, ValueError, OverflowError):
        wall_remaining = 0
    # fallback_wait_seconds is the authoritative boot-change budget written
    # alongside the cooldown. SQLite's wall-clock projection intentionally
    # rounds upward and may report one extra second, so use it only to recover
    # legacy rows that do not yet have a persisted fallback.
    fallback_wait = stored_fallback if stored_fallback > 0 else max(wall_remaining, 1)

    if row_boot_id != boot_id or row_deadline <= 0:
        candidate = now + float(fallback_wait)
        reanchor = getattr(worker_db, "reanchor_account_rpc_cooldown", None)
        if callable(reanchor):
            anchored = dict(
                reanchor(
                    account_id=owner,
                    expected_next_allowed_at=persisted_key,
                    boot_id=boot_id,
                    steady_deadline=candidate,
                    fallback_wait_seconds=fallback_wait,
                )
                or {}
            )
            anchored_key = str(anchored.get("next_allowed_at") or "")
            if anchored_key and anchored_key != persisted_key:
                return persisted_account_flood_wait_remaining(
                    worker_db=worker_db, account_id=owner
                )
            row_boot_id = str(anchored.get("boot_id") or boot_id)
            row_deadline = _positive_float(anchored.get("steady_deadline")) or candidate
        else:
            row_boot_id = boot_id
            row_deadline = candidate

    if row_boot_id == boot_id and row_deadline <= now:
        clearer = getattr(worker_db, "clear_elapsed_account_rpc_cooldown", None)
        if callable(clearer):
            clearer(
                account_id=owner,
                expected_next_allowed_at=persisted_key,
                boot_id=boot_id,
                observed_steady_time=now,
            )
        return 0

    remaining = row_deadline - now
    return max(1, int(math.ceil(remaining))) if remaining > 0 else 0


def install_account_flood_wait(
    *,
    queue_worker: Any,
    worker_db: Any,
    account_id: int,
    retry_at: Any,
    code: str,
    source_task_id: int,
    wait_seconds: int,
) -> dict[str, Any]:
    """Install a local embargo before persisting the account-wide FloodWait.

    SQLite can be temporarily unavailable exactly when Telegram asks the account
    to stop. The monotonic process-local deadline is therefore installed first.
    A failed database write is propagated to the caller, but it cannot reopen the
    Telegram RPC boundary for another task in the same process.
    """

    owner = max(0, int(account_id or 0))
    wait = max(1, int(wait_seconds or 0))
    if owner <= 0:
        raise ValueError("Account FloodWait requires a positive account_id")

    remember = getattr(queue_worker, "remember_account_rpc_cooldown", None)
    if not callable(remember):
        remember = getattr(queue_worker, "_remember_account_rpc_cooldown", None)
    if callable(remember):
        remember(owner, wait, "")

    cooldown = persist_account_flood_wait(
        worker_db=worker_db,
        account_id=owner,
        retry_at=retry_at,
        code=code,
        source_task_id=source_task_id,
        wait_seconds=wait,
    )
    if callable(remember):
        remember(owner, wait, str(cooldown.get("next_allowed_at") or ""))
    return cooldown
