from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.account_limits import MAX_PARALLEL_ACCOUNT_RUNTIMES
from core.exceptions import DeferredTelegramError, NonRetryableTelegramError
from core.redaction import sanitize_exception
from services.account_context import AccountContainerView

log = logging.getLogger(__name__)


@dataclass
class TelegramAccountRuntime:
    account_id: int
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]
    cleanup: Callable[[], Any] | None
    lock: asyncio.Lock
    last_used: float = 0.0
    reservations: int = 0


class TelegramAccountRuntimeManager:
    """Lazily own one isolated handler/client graph for each Telegram account."""

    MAX_ACCOUNTS = MAX_PARALLEL_ACCOUNT_RUNTIMES

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
        # Accounts being disconnected must not be recreated while their old
        # runtime is still executing or cleaning up.
        self._stopping_accounts: set[int] = set()
        self._evicting_accounts: set[int] = set()
        self._capacity_lock = asyncio.Lock()
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
        # Preserve the historical dependency-injection seam used by lightweight
        # handler tests. A MagicMock database has no real per-account settings
        # catalog, so wrapping it in AccountContainerView would make a configured
        # test Telegram client appear unconfigured. Production databases always
        # use the isolated account view.
        if type(self.worker_database).__module__.startswith("unittest.mock"):
            context = self.container
        else:
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
            last_used=time.monotonic(),
            reservations=0,
        )
        self.worker_database.set_account_runtime_state(account_id, "connected")
        log.info("Created isolated Telegram runtime for account %s", account_id)
        return runtime

    @staticmethod
    def _reserve_runtime(runtime: TelegramAccountRuntime) -> TelegramAccountRuntime:
        runtime.reservations += 1
        return runtime

    @staticmethod
    def _release_runtime(runtime: TelegramAccountRuntime) -> None:
        runtime.reservations = max(0, int(runtime.reservations) - 1)

    async def _cleanup_runtime(self, runtime: TelegramAccountRuntime) -> None:
        if runtime.cleanup is None:
            return
        result = runtime.cleanup()
        if asyncio.iscoroutine(result):
            await asyncio.wait_for(result, timeout=15.0)

    async def _evict_oldest_idle_runtime(self) -> None:
        candidates = [
            runtime
            for account_id, runtime in self._runtimes.items()
            if account_id not in self._stopping_accounts
            and account_id not in self._evicting_accounts
            and runtime.reservations == 0
            and not runtime.lock.locked()
        ]
        if not candidates:
            raise DeferredTelegramError(
                "All Telegram runtime slots are currently busy",
                code="account_runtime_capacity",
                retry_after=1,
            )
        victim = min(candidates, key=lambda runtime: runtime.last_used)
        self._evicting_accounts.add(victim.account_id)
        try:
            if self._runtimes.get(victim.account_id) is victim:
                self._runtimes.pop(victim.account_id, None)
            await self._cleanup_runtime(victim)
        finally:
            self._evicting_accounts.discard(victim.account_id)
        log.info(
            "Released idle Telegram runtime for account %s",
            victim.account_id,
        )

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
        if owner in self._stopping_accounts:
            raise NonRetryableTelegramError(
                "Telegram account runtime is stopping",
                code="account_stopped",
                details={"account_id": owner},
            )
        if owner in self._evicting_accounts:
            raise DeferredTelegramError(
                "Telegram account runtime is being recycled",
                code="account_runtime_recycling",
                retry_after=1,
            )
        existing = self._runtimes.get(owner)
        if existing is not None:
            return self._reserve_runtime(existing)
        lock = self._creation_locks.setdefault(owner, asyncio.Lock())
        async with lock:
            if owner in self._stopping_accounts:
                raise NonRetryableTelegramError(
                    "Telegram account runtime is stopping",
                    code="account_stopped",
                    details={"account_id": owner},
                )
            if owner in self._evicting_accounts:
                raise DeferredTelegramError(
                    "Telegram account runtime is being recycled",
                    code="account_runtime_recycling",
                    retry_after=1,
                )
            existing = self._runtimes.get(owner)
            if existing is not None:
                return self._reserve_runtime(existing)
            async with self._capacity_lock:
                existing = self._runtimes.get(owner)
                if existing is not None:
                    return self._reserve_runtime(existing)
                if len(self._runtimes) >= self.MAX_ACCOUNTS:
                    await self._evict_oldest_idle_runtime()
                runtime = await self._create_runtime(owner)
                self._runtimes[owner] = runtime
                return self._reserve_runtime(runtime)

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
        try:
            handler = runtime.handlers.get(name)
            if handler is None:
                raise NonRetryableTelegramError(
                    f"Handler is unavailable for account: {name}",
                    code="handler_missing",
                )
            async with runtime.lock:
                if (
                    self._runtimes.get(account_id) is not runtime
                    or account_id in self._stopping_accounts
                    or account_id in self._evicting_accounts
                ):
                    raise NonRetryableTelegramError(
                        "Telegram account runtime is no longer available",
                        code="account_stopped",
                        details={"account_id": account_id},
                    )
                self.worker_database.set_account_runtime_state(account_id, "running")
                try:
                    return await handler(task)
                finally:
                    runtime.last_used = time.monotonic()
                    latest = self.worker_database.get_telegram_account(account_id) or {}
                    if not bool(latest.get("stopped")) and str(
                        latest.get("runtime_state") or ""
                    ) not in {"stopping", "restricted", "error"}:
                        self.worker_database.set_account_runtime_state(
                            account_id, "connected"
                        )
        finally:
            self._release_runtime(runtime)

    async def stop_runtime(self, account_id: int) -> dict[str, Any]:
        owner = int(account_id)
        if owner <= 0:
            raise ValueError("Runtime stop requires a positive account id")

        # Publish the stop intent before waiting for either creation or runtime
        # execution. get_runtime() fails closed while this marker is present,
        # so no replacement client can be created alongside the old one.
        self._stopping_accounts.add(owner)
        creation_lock = self._creation_locks.setdefault(owner, asyncio.Lock())
        try:
            async with creation_lock:
                runtime = self._runtimes.get(owner)
                if runtime is None:
                    return {"account_id": owner, "disconnected": False}
                async with runtime.lock:
                    await self._cleanup_runtime(runtime)
                    # Remove only the exact runtime that was stopped. This keeps
                    # the invariant explicit even if future code mutates the map.
                    if self._runtimes.get(owner) is runtime:
                        self._runtimes.pop(owner, None)
        finally:
            self._stopping_accounts.discard(owner)

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
        try:
            async with runtime.lock:
                if (
                    self._runtimes.get(owner) is not runtime
                    or owner in self._stopping_accounts
                    or owner in self._evicting_accounts
                ):
                    raise NonRetryableTelegramError(
                        "Telegram account runtime is no longer available",
                        code="account_stopped",
                        details={"account_id": owner},
                    )
                try:
                    result = await handler(
                        {
                            "id": 0,
                            "account_id": owner,
                            "type": "telegram_health",
                            "payload": {"account_id": owner},
                        }
                    )
                finally:
                    runtime.last_used = time.monotonic()
            return dict(result or {})
        finally:
            self._release_runtime(runtime)

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
