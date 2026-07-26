from __future__ import annotations


class MarlenError(Exception):
    """Base exception for Marlen application errors."""


class TelegramOperationError(MarlenError):
    """Retryable Telegram operation failure by default."""

    retry = True
    code = "telegram_operation_error"


class NonRetryableTelegramError(TelegramOperationError):
    """Telegram error for which repeating the same queued task is pointless."""

    retry = False

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})
        self.rpc_error = str(self.details.get("rpc_error") or "")


class DeferredTelegramError(TelegramOperationError):
    """Telegram asked the application to retry later without occupying the worker.

    ``retry_after`` is intentionally bounded by the caller before this exception is
    created. QueueWorker persists the delay in SQLite and immediately continues with
    other due tasks.
    """

    retry = True
    code = "telegram_deferred"

    def __init__(self, message: str, *, code: str, retry_after: int):
        super().__init__(message)
        self.code = str(code)
        self.retry_after = max(1, int(retry_after))


class TaskPausedError(MarlenError):
    """A resumable local task reached a safe user-requested pause point."""

    def __init__(self, message: str = "Task paused by user"):
        super().__init__(message)
