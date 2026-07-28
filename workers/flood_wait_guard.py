from __future__ import annotations

from typing import Any


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

    writer = getattr(worker_db, "set_account_rpc_cooldown", None)
    if not callable(writer):
        raise RuntimeError("Database cannot persist account RPC cooldowns")

    cooldown = dict(
        writer(
            account_id=owner,
            retry_at=retry_at,
            code=str(code or "flood_wait_deferred"),
            source_task_id=int(source_task_id),
            wait_seconds=wait,
        )
        or {}
    )
    if callable(remember):
        remember(owner, wait, str(cooldown.get("next_allowed_at") or ""))
    return cooldown
