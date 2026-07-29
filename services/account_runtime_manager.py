from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.exceptions import NonRetryableTelegramError
from core.redaction import sanitize_exception
from services.account_context import AccountContainerView

log = logging.getLogger(__name__)


@dataclass
class TelegramAccountRuntime:
    account_id: int
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]
    cleanup: Callable[[], Any] | None
    lock: asyncio.Lock


class TelegramAccountRuntimeManager:
    """Lazily own one isolated handler/client graph for each Telegram account."""

    MAX_ACCOUNTS = 5

    def __init__(
        self,
        container,
        *,
        worker_database,
        create_worker_handlers,
        TelegramService,
        ImportService,
        LinkedChatService,
        CommentService,
    ) -> None:
        self.container = container
        self.worker_database = worker_database
        self.create_worker_handlers = create_worker_handlers
        self.factories = {
            "TelegramService": TelegramService,
            "ImportService": ImportService,
            "LinkedChatService": LinkedChatService,
            "CommentService": CommentService,
        }
        self._runtimes: dict[int, TelegramAccountRuntime] = {}
        self._creation_locks: dict[int, asyncio.Lock] = {}
        self._closed = False

    @staticmethod
    def task_account_id(task: dict[str, Any]) -> int:
        payload = task.get("payload") or {}
        payload_account = 0
        if isinstance(payload, dict):
            try:
                payload_account = int(payload.get("account_id") or 0)
            except (TypeError, ValueError, OverflowError):
                payload_account = 0
        try:
            column_account = int(task.get("account_id") or 0)
        except (TypeError, ValueError, OverflowError):
            column_account = 0
        if column_account > 0 and payload_account > 0 and column_account != payload_account:
            raise NonRetryableTelegramError(
                "Task account column does not match payload account",
                code="account_state_mismatch",
                details={
                    "task_account_id": column_account,
                    "payload_account_id": payload_account,
                },
            )
        return column_account or payload_account

    async def _create_runtime(self, account_id: int) -> TelegramAccountRuntime:
        account = self.worker_database.get_telegram_account(account_id)
        if not account:
            raise NonRetryableTelegramError(
                "Telegram account does not exist",
                code="account_missing",
                details={"account_id": account_id},
            )
        if not bool(account.get("authorized")):
            raise NonRetryableTelegramError(
                "Telegram account requires authorization",
                code="authorization_required",
                details={"account_id": account_id},
            )
        context = AccountContainerView(
            self.container,
            account_id=account_id,
            worker_database=self.worker_database,
        )
        created = self.create_worker_handlers(context, **self.factories)
        if asyncio.iscoroutine(created):
            created = await created
        if isinstance(created, tuple):
            handlers, cleanup = created
        else:
            handlers, cleanup = created, None
        if not isinstance(handlers, dict):
            raise TypeError("Account handler factory returned an invalid handler map")
        runtime = TelegramAccountRuntime(
            account_id=account_id,
            handlers=handlers,
            cleanup=cleanup,
            lock=asyncio.Lock(),
        )
        self.worker_database.set_account_runtime_state(account_id, "connected")
        log.info("Created isolated Telegram runtime for account %s", account_id)
        return runtime

    async def get_runtime(self, account_id: int) -> TelegramAccountRuntime:
        owner = int(account_id)
        if owner <= 0:
            raise NonRetryableTelegramError(
                "Telegram task has no positive account id",
                code="invalid_payload",
            )
        if self._closed:
            raise NonRetryableTelegramError(
                "Telegram runtime manager is shutting down",
                code="shutdown_before_dispatch",
            )
        existing = self._runtimes.get(owner)
        if existing is not None:
            return existing
        lock = self._creation_locks.setdefault(owner, asyncio.Lock())
        async with lock:
            existing = self._runtimes.get(owner)
            if existing is not None:
                return existing
            if len(self._runtimes) >= self.MAX_ACCOUNTS:
                raise NonRetryableTelegramError(
                    "Telegram runtime limit reached",
                    code="account_limit_reached",
                )
            runtime = await self._create_runtime(owner)
            self._runtimes[owner] = runtime
            return runtime

    async def dispatch(self, name: str, task: dict[str, Any]) -> Any:
        account_id = self.task_account_id(task)
        if account_id <= 0:
            raise NonRetryableTelegramError(
                f"{name} requires a positive account_id",
                code="invalid_payload",
            )
        cancellation_reader = getattr(
            self.container.queue_worker, "is_scope_cancelled", None
        )
        if callable(cancellation_reader) and cancellation_reader(
            "account", account_id
        ):
            raise NonRetryableTelegramError(
                "Работа аккаунта остановлена",
                code="account_stopped",
            )
        account = self.worker_database.get_telegram_account(account_id)
        if not account:
            raise NonRetryableTelegramError(
                "Telegram account does not exist", code="account_missing"
            )
        state = str(account.get("runtime_state") or "")
        stopped_value = account.get("stopped")
        is_stopped = (
            stopped_value is True
            or (
                isinstance(stopped_value, int)
                and not isinstance(stopped_value, bool)
                and stopped_value == 1
            )
        )
        if is_stopped or state in {"stopping", "stopped"}:
            raise NonRetryableTelegramError(
                "Работа аккаунта остановлена", code="account_stopped"
            )
        if state == "restricted":
            raise NonRetryableTelegramError(
                "Telegram ограничил активность аккаунта",
                code="account_restricted",
            )
        restriction_reader = getattr(
            self.worker_database, "get_account_restriction", None
        )
        if callable(restriction_reader):
            restriction = restriction_reader(account_id=account_id)
            active_value = (
                restriction.get("active")
                if isinstance(restriction, dict)
                else False
            )
            restriction_active = (
                active_value is True
                or (
                    isinstance(active_value, int)
                    and not isinstance(active_value, bool)
                    and active_value == 1
                )
            )
            if restriction_active:
                self.worker_database.set_account_runtime_state(
                    account_id,
                    "restricted",
                    error=str((restriction or {}).get("message") or ""),
                )
                raise NonRetryableTelegramError(
                    "Telegram ограничил активность аккаунта",
                    code="account_restricted",
                )
        runtime = await self.get_runtime(account_id)
        handler = runtime.handlers.get(name)
        if handler is None:
            raise NonRetryableTelegramError(
                f"Handler is unavailable for account: {name}",
                code="handler_missing",
            )
        async with runtime.lock:
            self.worker_database.set_account_runtime_state(account_id, "running")
            try:
                return await handler(task)
            finally:
                latest = self.worker_database.get_telegram_account(account_id) or {}
                if not bool(latest.get("stopped")) and str(
                    latest.get("runtime_state") or ""
                ) not in {"stopping", "restricted", "error"}:
                    self.worker_database.set_account_runtime_state(
                        account_id, "connected"
                    )

    async def stop_runtime(self, account_id: int) -> dict[str, Any]:
        owner = int(account_id)
        runtime = self._runtimes.pop(owner, None)
        if runtime is None:
            return {"account_id": owner, "disconnected": False}
        async with runtime.lock:
            if runtime.cleanup is not None:
                result = runtime.cleanup()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=15.0)
        log.info("Stopped isolated Telegram runtime for account %s", owner)
        return {"account_id": owner, "disconnected": True}

    async def check_runtime(self, account_id: int) -> dict[str, Any]:
        owner = int(account_id)
        runtime = await self.get_runtime(owner)
        handler = runtime.handlers.get("telegram_health")
        if handler is None:
            raise NonRetryableTelegramError(
                "Telegram health handler is unavailable",
                code="handler_missing",
            )
        async with runtime.lock:
            result = await handler(
                {
                    "id": 0,
                    "account_id": owner,
                    "type": "telegram_health",
                    "payload": {"account_id": owner},
                }
            )
        return dict(result or {})

    async def close(self) -> None:
        self._closed = True
        errors: list[str] = []
        for account_id in list(self._runtimes):
            try:
                await self.stop_runtime(account_id)
            except Exception as exc:
                errors.append(
                    f"{account_id}: {sanitize_exception(exc)}"
                )
                log.exception(
                    "Could not stop Telegram runtime for account %s", account_id
                )
        self._runtimes.clear()
        if errors:
            raise RuntimeError("; ".join(errors))


def create_multiaccount_handlers(
    container,
    *,
    create_worker_handlers,
    TelegramService,
    ImportService,
    LinkedChatService,
    CommentService,
):
    worker_db = container.queue_worker.get_db()
    manager = TelegramAccountRuntimeManager(
        container,
        worker_database=worker_db,
        create_worker_handlers=create_worker_handlers,
        TelegramService=TelegramService,
        ImportService=ImportService,
        LinkedChatService=LinkedChatService,
        CommentService=CommentService,
    )

    names = {
        "sync_channels",
        "link_channels",
        "auto_comment",
        "auto_comment_slot",
        "direct_message",
        "comment",
        "sync_saved_dialogs",
        "join_saved_slot",
        "openai_test",
    }

    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}

    for handler_name in names:
        async def routed(
            task: dict[str, Any],
            _name: str = handler_name,
        ) -> Any:
            return await manager.dispatch(_name, task)

        handlers[handler_name] = routed

    async def noop(_task: dict[str, Any]) -> None:
        await asyncio.sleep(0)

    async def import_data(task: dict[str, Any]) -> Any:
        # File imports are deliberately not cross-account Telegram operations.
        # Keep the legacy importer but execute through the selected target account.
        return await manager.dispatch("import", task)

    async def stop_account_runtime(task: dict[str, Any]) -> dict[str, Any]:
        payload = task.get("payload") or {}
        account_id = int(
            task.get("account_id")
            or (payload.get("account_id") if isinstance(payload, dict) else 0)
            or 0
        )
        return await manager.stop_runtime(account_id)

    async def check_account_runtime(task: dict[str, Any]) -> dict[str, Any]:
        payload = task.get("payload") or {}
        account_id = int(
            task.get("account_id")
            or (payload.get("account_id") if isinstance(payload, dict) else 0)
            or 0
        )
        return await manager.check_runtime(account_id)

    handlers["noop"] = noop
    handlers["import"] = import_data
    handlers["stop_account_runtime"] = stop_account_runtime
    handlers["check_account_runtime"] = check_account_runtime
    return handlers, manager.close
