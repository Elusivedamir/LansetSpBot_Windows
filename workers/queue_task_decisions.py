"""Pure queue-task ownership and retry decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable

TaskHandler = Callable[[dict], Awaitable[Any]]


class CancellationPersistence(StrEnum):
    REQUEUE = "requeue"
    UNCERTAIN_FAILURE = "uncertain_failure"


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    task_id: int
    task_type: str
    handler: TaskHandler
    payload: dict[str, Any]
    column_account_id: int
    payload_account_id: int
    account_id: int


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    column_account_id: int
    payload_account_id: int
    account_id: int
    mismatch: bool


def parse_account_identity(task: dict[str, Any]) -> AccountIdentity:
    payload = task.get("payload") or {}
    try:
        column_account_id = int(task.get("account_id") or 0)
        payload_value = payload.get("account_id") if isinstance(payload, dict) else 0
        payload_account_id = int(payload_value or 0)
    except (TypeError, ValueError, OverflowError):
        column_account_id = 0
        payload_account_id = 0
    return AccountIdentity(
        column_account_id=column_account_id,
        payload_account_id=payload_account_id,
        account_id=column_account_id or payload_account_id,
        mismatch=(
            column_account_id > 0
            and payload_account_id > 0
            and column_account_id != payload_account_id
        ),
    )


def cancellation_persistence(
    task_type: str, idempotent_task_types: frozenset[str]
) -> CancellationPersistence:
    if str(task_type) in idempotent_task_types:
        return CancellationPersistence.REQUEUE
    return CancellationPersistence.UNCERTAIN_FAILURE


def unexpected_retry_allowed(
    *, task_type: str, retry_count: int, max_retries: int,
    idempotent_task_types: frozenset[str],
) -> bool:
    return (
        str(task_type) in idempotent_task_types
        and max(0, int(retry_count)) < max(0, int(max_retries))
    )
