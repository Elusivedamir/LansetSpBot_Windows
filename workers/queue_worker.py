from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import Future
import json
import logging
import math
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from PySide6.QtCore import QThread, Signal

from core.boot_clock import current_boot_identity, steady_time
from core.campaign_schedule import from_db_time, utc_now
from core.account_restriction import (
    RESTRICTION_CODES,
    activate_account_restriction,
)
from core.exceptions import (
    DeferredTelegramError,
    NonRetryableTelegramError,
    TaskPausedError,
)
from core.performance import log_if_slow
from core.redaction import sanitize_exception, sanitize_text
from storage.database import Database
from storage.db_common import DatabaseError
from workers.flood_wait_guard import install_account_flood_wait

log = logging.getLogger(__name__)
TaskHandler = Callable[[dict], Awaitable[Any]]
HandlerFactory = Callable[[], Any]


class _ScopeDispatchBarrier:
    """Serialize campaign cancellation with the exact mutating RPC boundary."""

    def __init__(
        self,
        worker: "QueueWorker",
        scopes: tuple[tuple[str, int], ...],
        pre_dispatch_check: Callable[[], bool | None] | None = None,
    ):
        self._worker = worker
        self._scopes = scopes
        self._pre_dispatch_check = pre_dispatch_check

    @contextmanager
    def dispatch(self, _request=None):
        worker = self._worker
        now = time.monotonic()
        with worker._scope_lock:
            worker._prune_cancelled_scopes_locked(now)
            cancelled = next(
                (scope for scope in self._scopes if scope in worker._cancelled_scopes),
                None,
            )
            if cancelled is not None or worker.isInterruptionRequested():
                raise DeferredTelegramError(
                    "Operation stopped before Telegram request dispatch",
                    code="shutdown_before_dispatch",
                    retry_after=1,
                )
            if self._pre_dispatch_check is not None:
                allowed = self._pre_dispatch_check()
                if allowed is False:
                    raise DeferredTelegramError(
                        "Operation blocked by current local safety state",
                        code="local_ban_before_dispatch",
                        retry_after=1,
                    )
            # PacedTelegramClient keeps this context until the Telethon coroutine
            # has been scheduled at the real MTProto boundary. A concurrent Stop
            # therefore linearizes either entirely before or entirely after it.
            yield


class QueueWorker(QThread):
    """Qt-native queue worker with thread-owned DB and async services."""

    IDEMPOTENT_TASK_TYPES = frozenset(
        {"noop", "sync_channels", "sync_saved_dialogs", "link_channels", "import"}
    )
    DIRECT_ACCOUNT_BOUND_TASK_TYPES = frozenset(
        {
            "sync_channels",
            "sync_saved_dialogs",
            "link_channels",
            "auto_comment",
            "direct_message",
            "comment",
        }
    )
    ACCOUNT_RPC_TASK_TYPES = frozenset(
        {
            "sync_channels",
            "sync_saved_dialogs",
            "link_channels",
            "auto_comment",
            "auto_comment_slot",
            "direct_message",
            "comment",
            "join_saved_slot",
        }
    )

    stats_changed = Signal(dict)
    task_completed = Signal(int)
    task_failed = Signal(int, str)
    worker_error = Signal(str)
    lifecycle_changed = Signal(str)

    STATE_STOPPED = "stopped"
    STATE_STARTING = "starting"
    STATE_ACTIVE = "active"
    STATE_DRAINING = "draining"
    STATE_CLEANUP = "cleanup"

    @staticmethod
    def _format_wait_duration(seconds: int) -> str:
        value = max(0, int(seconds))
        hours, remainder = divmod(value, 3600)
        minutes, secs = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours} ч")
        if minutes:
            parts.append(f"{minutes} мин")
        if secs or not parts:
            parts.append(f"{secs} сек")
        return " ".join(parts)

    def _persist_link_activity(
        self,
        level: str,
        message: str,
        *,
        account_id: int | None = None,
    ) -> None:
        """Best-effort mirror of link-task lifecycle into the live journal."""

        try:
            self.get_db().insert_log(
                str(level or "INFO").upper(),
                f"[Связки] {' '.join(str(message or '').split())}",
                account_id=account_id,
            )
        except Exception:
            log.exception("Could not persist queue link activity event")

    def __init__(
        self,
        handler_factory: HandlerFactory,
        max_retries: int = 3,
        database_path: str | Path | None = None,
        parent=None,
        *,
        persistent_idle: bool = False,
    ) -> None:
        super().__init__(parent)
        self.handler_factory = handler_factory
        self.max_retries = max_retries
        self.database_path = Path(database_path) if database_path is not None else None
        self.persistent_idle = bool(persistent_idle)
        self.paused = False
        self.heartbeat: Optional[float] = None
        self.processed_count = 0
        self.failed_count = 0
        self.retry_count = 0
        self._db: Optional[Database] = None
        self._handlers: Dict[str, TaskHandler] = {}
        self._cleanup = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._utility_lock = threading.RLock()
        self._pending_utility_jobs: list[tuple[str, dict[str, Any], Future[Any]]] = []
        self._active_utility_jobs: dict[Future[Any], Future[Any]] = {}
        self._state_lock = threading.RLock()
        self._lifecycle_state = self.STATE_STOPPED
        self._task_wakeup = threading.Event()
        self._scope_lock = threading.RLock()
        self._cancelled_scopes: dict[tuple[str, int], float] = {}
        self._cancelled_scope_retention_seconds = 24 * 60 * 60
        self._startup_started_at: float | None = None
        self._active_task_lock = threading.RLock()
        self._active_tasks: dict[int, tuple[str, int]] = {}
        self.max_parallel_accounts = 5
        self._account_cooldown_lock = threading.RLock()
        # account_id -> (monotonic deadline, persisted UTC deadline key).
        # The monotonic deadline prevents a forward wall-clock correction from
        # shortening an already observed Telegram FloodWait inside this process.
        self._account_cooldown_deadlines: dict[int, tuple[float, str]] = {}

    @property
    def running(self) -> bool:
        return self.isRunning() and not self.isInterruptionRequested()

    @property
    def lifecycle_state(self) -> str:
        with self._state_lock:
            return self._lifecycle_state

    @property
    def has_active_task(self) -> bool:
        with self._active_task_lock:
            return bool(self._active_tasks)

    @property
    def active_task(self) -> tuple[int | None, str | None]:
        with self._active_task_lock:
            if not self._active_tasks:
                return None, None
            task_id, (task_type, _account_id) = next(
                iter(self._active_tasks.items())
            )
            return task_id, task_type

    @property
    def active_account_ids(self) -> set[int]:
        with self._active_task_lock:
            return {
                account_id
                for _task_type, account_id in self._active_tasks.values()
                if account_id > 0
            }

    def _set_active_task(
        self, task: dict | None, *, finished_task_id: int | None = None
    ) -> None:
        with self._active_task_lock:
            if finished_task_id is not None:
                self._active_tasks.pop(int(finished_task_id), None)
                return
            if task is None:
                self._active_tasks.clear()
                return
            task_id = int(task.get("id") or 0)
            payload = task.get("payload") or {}
            try:
                account_id = int(
                    task.get("account_id")
                    or (
                        payload.get("account_id")
                        if isinstance(payload, dict)
                        else 0
                    )
                    or 0
                )
            except (TypeError, ValueError, OverflowError):
                account_id = 0
            if task_id > 0:
                self._active_tasks[task_id] = (
                    str(task.get("type") or ""),
                    account_id,
                )

    def _remember_account_rpc_cooldown(
        self, account_id: int, remaining_seconds: float, persisted_key: str
    ) -> int:
        owner = max(0, int(account_id or 0))
        remaining = max(0.0, float(remaining_seconds or 0.0))
        if owner <= 0 or remaining <= 0:
            return 0
        now = steady_time()
        deadline = now + remaining
        key = str(persisted_key or "")
        with self._account_cooldown_lock:
            current = self._account_cooldown_deadlines.get(owner)
            if current is not None:
                deadline = max(deadline, current[0])
                if not key:
                    key = current[1]
            self._account_cooldown_deadlines[owner] = (deadline, key)
        return max(1, int(math.ceil(deadline - now)))

    def remember_account_rpc_cooldown(
        self, account_id: int, remaining_seconds: float, persisted_key: str = ""
    ) -> int:
        """Install a process-local embargo before any fallible SQLite write."""

        return self._remember_account_rpc_cooldown(
            account_id, remaining_seconds, persisted_key
        )

    @staticmethod
    def _positive_float(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return parsed if math.isfinite(parsed) and parsed > 0 else 0.0

    def _account_rpc_cooldown_remaining(
        self, account_id: int, cooldown: dict[str, Any] | None
    ) -> int:
        """Recover and enforce a cooldown using boot-stable monotonic metadata."""

        owner = max(0, int(account_id or 0))
        if owner <= 0:
            return 0
        now = steady_time()
        with self._account_cooldown_lock:
            current = self._account_cooldown_deadlines.get(owner)
            if current is not None and current[0] <= now:
                self._account_cooldown_deadlines.pop(owner, None)
                current = None
            local_deadline = current[0] if current is not None else 0.0

        data = dict(cooldown or {})
        persisted_key = str(data.get("next_allowed_at") or "")
        if not persisted_key:
            remaining = local_deadline - now
            return max(1, int(math.ceil(remaining))) if remaining > 0 else 0

        boot_id = current_boot_identity()
        row_boot_id = str(data.get("boot_id") or "")
        row_deadline = self._positive_float(data.get("steady_deadline"))
        try:
            fallback_wait = max(1, int(data.get("fallback_wait_seconds") or 0))
        except (TypeError, ValueError, OverflowError):
            fallback_wait = 1
        try:
            wall_remaining = max(0, int(data.get("remaining_seconds") or 0))
        except (TypeError, ValueError, OverflowError):
            wall_remaining = 0
        fallback_wait = max(fallback_wait, wall_remaining, 1)

        if row_boot_id != boot_id or row_deadline <= 0:
            # A new OS boot, an old v25 row or a changed Windows wall clock has
            # no trustworthy elapsed-time anchor. Start the full recorded server
            # wait again and persist the new anchor so another process restart on
            # this boot cannot reset or bypass it.
            candidate = now + float(fallback_wait)
            reanchor = getattr(self.get_db(), "reanchor_account_rpc_cooldown", None)
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
                    return self._account_rpc_cooldown_remaining(owner, anchored)
                row_boot_id = str(anchored.get("boot_id") or boot_id)
                row_deadline = (
                    self._positive_float(anchored.get("steady_deadline")) or candidate
                )
            else:
                row_boot_id = boot_id
                row_deadline = candidate

        if row_boot_id == boot_id and row_deadline <= now:
            clearer = getattr(self.get_db(), "clear_elapsed_account_rpc_cooldown", None)
            if callable(clearer):
                clearer(
                    account_id=owner,
                    expected_next_allowed_at=persisted_key,
                    boot_id=boot_id,
                    observed_steady_time=now,
                )
            with self._account_cooldown_lock:
                current = self._account_cooldown_deadlines.get(owner)
                if current is None or current[0] <= now:
                    self._account_cooldown_deadlines.pop(owner, None)
                    return 0
                remaining = current[0] - now
            return max(1, int(math.ceil(remaining)))

        with self._account_cooldown_lock:
            current = self._account_cooldown_deadlines.get(owner)
            deadline = row_deadline
            if current is not None:
                deadline = max(deadline, current[0])
            self._account_cooldown_deadlines[owner] = (deadline, persisted_key)
            remaining = deadline - now
        return max(1, int(math.ceil(remaining))) if remaining > 0 else 0

    def _postpone_for_account_rpc_cooldown(
        self, *, task_id: int, task_type: str, account_id: int
    ) -> bool:
        try:
            cooldown = self.get_db().get_account_rpc_cooldown(account_id=account_id)
        except Exception:
            # A known FloodWait must remain enforced while SQLite is temporarily
            # unreadable. The process-local deadline was installed first.
            log.exception(
                "Could not read persisted account FloodWait; using local embargo"
            )
            cooldown = {}
        remaining = self._account_rpc_cooldown_remaining(account_id, cooldown)
        if remaining <= 0:
            return False
        # Use the current wall clock only to make SQLite's not_before future.
        # The amount of waiting is fixed by the monotonic deadline above.
        retry_at = utc_now() + timedelta(seconds=remaining)
        changed = self.get_db().postpone_running_task_for_account_cooldown(
            task_id,
            retry_at=retry_at,
            code=(
                "account_flood_wait: все Telegram RPC аккаунта отложены "
                f"ещё на {remaining} сек"
            ),
        )
        if not changed:
            raise RuntimeError(
                f"Could not postpone task {task_id} for account cooldown"
            )
        if task_type == "link_channels":
            self._persist_link_activity(
                "WARNING",
                "Общий FloodWait аккаунта ещё действует. Связки не делают "
                f"Telegram-запросы и продолжатся через "
                f"{self._format_wait_duration(remaining)}.",
                account_id=account_id,
            )
        return True

    def _set_lifecycle_state(self, state: str) -> None:
        with self._state_lock:
            self._lifecycle_state = state
        self.lifecycle_changed.emit(state)

    def notify_task_available(self) -> None:
        """Wake an idle worker or make a draining worker re-check the queue."""
        self._task_wakeup.set()

    def _consume_task_wakeup(self) -> bool:
        if not self._task_wakeup.is_set():
            return False
        self._task_wakeup.clear()
        return True


    def submit_utility(self, name: str, payload: dict[str, Any] | None = None) -> Future[Any]:
        """Run a non-persistent utility handler on the worker-owned event loop.

        The returned Future is safe to poll from Qt. Network I/O remains on the
        same background asyncio loop as Telegram and never blocks the GUI thread.
        """

        public_future: Future[Any] = Future()
        job = (str(name), dict(payload or {}), public_future)
        with self._utility_lock:
            loop = self._event_loop
            if loop is None or not loop.is_running() or not self._handlers:
                self._pending_utility_jobs.append(job)
            else:
                self._schedule_utility_job(loop, job)
        self.notify_task_available()
        return public_future

    def _schedule_utility_job(
        self,
        loop: asyncio.AbstractEventLoop,
        job: tuple[str, dict[str, Any], Future[Any]],
    ) -> None:
        name, payload, public_future = job
        if public_future.cancelled():
            return

        async def invoke() -> Any:
            handler = self._handlers.get(name)
            if handler is None:
                raise RuntimeError(f"Utility handler is unavailable: {name}")
            return await handler({"id": 0, "type": name, "payload": payload})

        internal = asyncio.run_coroutine_threadsafe(invoke(), loop)
        with self._utility_lock:
            self._active_utility_jobs[public_future] = internal

        def propagate_public_cancellation(done_public: Future[Any]) -> None:
            if not done_public.cancelled():
                return
            with self._utility_lock:
                running = self._active_utility_jobs.get(done_public)
            if running is not None and not running.done():
                running.cancel()

        def copy_result(done: Future[Any]) -> None:
            with self._utility_lock:
                self._active_utility_jobs.pop(public_future, None)
            if public_future.done():
                return
            try:
                public_future.set_result(done.result())
            except BaseException as exc:
                if not public_future.done():
                    public_future.set_exception(exc)

        public_future.add_done_callback(propagate_public_cancellation)
        internal.add_done_callback(copy_result)

    def _flush_pending_utility_jobs(self) -> None:
        loop = self._event_loop
        if loop is None or not loop.is_running():
            return
        with self._utility_lock:
            pending = self._pending_utility_jobs
            self._pending_utility_jobs = []
            for job in pending:
                self._schedule_utility_job(loop, job)

    def _fail_pending_utility_jobs(self, message: str) -> None:
        with self._utility_lock:
            pending = self._pending_utility_jobs
            self._pending_utility_jobs = []
            active = list(self._active_utility_jobs.items())
            self._active_utility_jobs.clear()
        for _name, _payload, future in pending:
            if not future.done():
                future.set_exception(RuntimeError(message))
        for public_future, internal_future in active:
            if not internal_future.done():
                internal_future.cancel()
            if not public_future.done():
                public_future.set_exception(RuntimeError(message))

    def get_db(self) -> Database:
        if self._db is None:
            raise RuntimeError("Database is available only inside QueueWorker.run()")
        return self._db

    def run(self) -> None:
        self._startup_started_at = time.monotonic()
        self.paused = False
        self._task_wakeup.clear()
        self._set_lifecycle_state(self.STATE_STARTING)
        try:
            # The connection owner is created in this QThread and never shared.
            # Database startup belongs inside the guard so migration/disk errors
            # are reported to the GUI instead of escaping QThread.run silently.
            self._db = Database(self.database_path, bootstrap=False)
            asyncio.run(self._run_lifecycle())
        except Exception as exc:
            safe_error = sanitize_text(str(exc))
            log.exception("Critical queue worker error: %s", safe_error)
            self.worker_error.emit(safe_error)
        finally:
            self._event_loop = None
            self._fail_pending_utility_jobs("Фоновый обработчик остановлен")
            self._set_active_task(None)
            self._handlers = {}
            self._cleanup = None
            if self._db is not None:
                try:
                    self._db.close_thread_connection()
                except Exception:
                    log.exception("Could not close worker SQLite connection")
            self._db = None
            self._set_lifecycle_state(self.STATE_STOPPED)
            self.stats_changed.emit(self.get_stats(running_override=False))
            log.info("Queue worker stopped")

    async def _run_lifecycle(self) -> None:
        self._event_loop = asyncio.get_running_loop()
        created = self.handler_factory()
        if inspect.isawaitable(created):
            created = await created
        if isinstance(created, tuple):
            self._handlers, self._cleanup = created
        else:
            self._handlers = created
        if not isinstance(self._handlers, dict):
            raise TypeError(
                "handler_factory must return a handlers dict or (handlers, cleanup)"
            )
        self._flush_pending_utility_jobs()
        if self._startup_started_at is not None:
            log_if_slow(
                log,
                "queue_worker_startup",
                self._startup_started_at,
                threshold_seconds=1.0,
                handlers=len(self._handlers),
            )
        try:
            await self._run_async()
        finally:
            self._set_lifecycle_state(self.STATE_CLEANUP)
            if self._cleanup is not None:
                result = self._cleanup()
                if inspect.isawaitable(result):
                    try:
                        await asyncio.wait_for(result, timeout=15.0)
                    except asyncio.TimeoutError:
                        log.error("Queue worker cleanup timed out")

    @staticmethod
    def _normalize_scope(
        scope_type: str, scope_id: int, account_id: int | None = None
    ) -> tuple[str, int]:
        name = str(scope_type or "").strip()
        numeric_id = int(scope_id)
        if not name:
            raise ValueError("Cancellation scope requires a name")
        if name == "channel":
            if numeric_id == 0:
                raise ValueError(
                    "Channel cancellation scope requires a non-zero peer id"
                )
            if account_id is not None:
                owner_account_id = int(account_id)
                if owner_account_id <= 0:
                    raise ValueError(
                        "Channel cancellation scope requires a positive account id"
                    )
                name = f"channel:{owner_account_id}"
        elif numeric_id <= 0:
            raise ValueError("Cancellation scope requires a name and positive id")
        return name, numeric_id

    @classmethod
    def _normalize_scope_entry(cls, scope) -> tuple[str, int]:
        values = tuple(scope)
        if len(values) == 2:
            return cls._normalize_scope(values[0], values[1])
        if len(values) == 3:
            return cls._normalize_scope(values[0], values[1], values[2])
        raise ValueError("Cancellation scope must contain two or three values")

    def _prune_cancelled_scopes_locked(self, now: float) -> None:
        cutoff = now - self._cancelled_scope_retention_seconds
        expired = [
            scope
            for scope, cancelled_at in self._cancelled_scopes.items()
            if cancelled_at < cutoff
        ]
        for scope in expired:
            self._cancelled_scopes.pop(scope, None)

    def request_scope_cancellation(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> None:
        """Cancel one campaign without interrupting unrelated queue tasks."""
        scope = self._normalize_scope(scope_type, scope_id, account_id)
        now = time.monotonic()
        with self._scope_lock:
            self._prune_cancelled_scopes_locked(now)
            self._cancelled_scopes[scope] = now

    def cancel_scopes_and_run(self, scopes, mutation):
        """Atomically publish cancellation and perform its durable DB mutation.

        Mutating Telegram calls use the same lock at their exact dispatch boundary.
        Consequently a Stop/delete operation can never commit in SQLite and then
        lose a race to a previously claimed task that has not dispatched yet.
        """

        normalized = tuple(
            dict.fromkeys(self._normalize_scope_entry(scope) for scope in scopes)
        )
        if not normalized:
            return mutation()
        now = time.monotonic()
        with self._scope_lock:
            self._prune_cancelled_scopes_locked(now)
            newly_cancelled = [
                scope for scope in normalized if scope not in self._cancelled_scopes
            ]
            for scope in normalized:
                self._cancelled_scopes[scope] = now
            try:
                return mutation()
            except BaseException:
                for scope in newly_cancelled:
                    self._cancelled_scopes.pop(scope, None)
                raise

    def create_scope_dispatch_barrier(self, *scopes, pre_dispatch_check=None):
        normalized = tuple(
            dict.fromkeys(self._normalize_scope_entry(scope) for scope in scopes)
        )
        return _ScopeDispatchBarrier(
            self,
            normalized,
            pre_dispatch_check=pre_dispatch_check,
        )

    def clear_scope_cancellation(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> None:
        scope = self._normalize_scope(scope_type, scope_id, account_id)
        with self._scope_lock:
            self._cancelled_scopes.pop(scope, None)

    def is_scope_cancelled(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> bool:
        scope = self._normalize_scope(scope_type, scope_id, account_id)
        now = time.monotonic()
        with self._scope_lock:
            self._prune_cancelled_scopes_locked(now)
            return scope in self._cancelled_scopes

    async def safe_sleep(
        self,
        seconds: float,
        step: float = 0.5,
        *,
        cancel_scope: tuple[str, int] | None = None,
    ) -> bool:
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if self.isInterruptionRequested():
                return False
            if cancel_scope is not None and self.is_scope_cancelled(*cancel_scope):
                return False
            delay = min(step, remaining)
            await asyncio.sleep(delay)
            remaining -= delay
        if self.isInterruptionRequested():
            return False
        return cancel_scope is None or not self.is_scope_cancelled(*cancel_scope)

    def pause(self) -> None:
        self.paused = True
        self.notify_task_available()

    def resume(self) -> None:
        self.paused = False
        self.notify_task_available()

    async def _wait_for_task_available(self, timeout: float | None) -> bool:
        """Sleep without polling SQLite until a task, deadline or shutdown arrives.

        ``threading.Event.wait`` runs in asyncio's executor so the QThread-owned
        event loop remains responsive.  The event is deliberately cleared only
        *after* the wait; clearing it before blocking would lose a task inserted
        between the empty-queue read and this method.
        """

        if self.isInterruptionRequested():
            return False
        normalized_timeout = None
        if timeout is not None:
            normalized_timeout = max(0.01, float(timeout))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._task_wakeup.wait,
            normalized_timeout,
        )
        self._task_wakeup.clear()
        return not self.isInterruptionRequested()

    async def _run_async(self) -> None:
        log.info("Queue worker started in isolated multiaccount mode")
        self._set_lifecycle_state(self.STATE_ACTIVE)
        active: dict[asyncio.Task, int] = {}
        idle_since: float | None = None
        consecutive_processing_failures = 0
        consecutive_claim_failures = 0

        async def reap(done_tasks) -> bool:
            nonlocal consecutive_processing_failures

            for future in done_tasks:
                active.pop(future, None)
                try:
                    future.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    consecutive_processing_failures += 1
                    safe_error = sanitize_exception(exc)
                    log.exception(
                        "Concurrent account task failed: %s", safe_error
                    )
                    self.worker_error.emit(safe_error)
                else:
                    consecutive_processing_failures = 0

            return consecutive_processing_failures >= 5

        try:
            while not self.isInterruptionRequested():
                self.heartbeat = time.time()
                done = {future for future in active if future.done()}
                if done:
                    if await reap(done):
                        log.error(
                            "Queue worker stopped after five consecutive "
                            "processing failures"
                        )
                        break
                    self.stats_changed.emit(self.get_stats())

                claimed_any = False
                fatal_claim_error: DatabaseError | None = None
                if not self.paused:
                    while len(active) < self.max_parallel_accounts:
                        excluded = set(active.values())
                        try:
                            task = self.get_db().claim_next_pending_task(excluded)
                        except DatabaseError as exc:
                            consecutive_claim_failures += 1
                            log.warning(
                                "Task claim failed (%s/5): %s",
                                consecutive_claim_failures,
                                sanitize_exception(exc),
                            )
                            if consecutive_claim_failures >= 5:
                                fatal_claim_error = exc
                                break
                            if not await self.safe_sleep(0.05):
                                break
                            continue
                        if task is None:
                            consecutive_claim_failures = 0
                            break
                        consecutive_claim_failures = 0
                        consecutive_processing_failures = 0
                        payload = task.get("payload") or {}
                        try:
                            account_id = int(
                                task.get("account_id")
                                or (
                                    payload.get("account_id")
                                    if isinstance(payload, dict)
                                    else 0
                                )
                                or 0
                            )
                        except (TypeError, ValueError, OverflowError):
                            account_id = 0
                        future = asyncio.create_task(self._process_task(task))
                        active[future] = account_id
                        claimed_any = True
                        idle_since = None

                if fatal_claim_error is not None:
                    self._set_lifecycle_state(self.STATE_DRAINING)
                    safe_error = sanitize_exception(fatal_claim_error)
                    log.error(
                        "Queue worker stopped after five consecutive task claim failures: %s",
                        safe_error,
                    )
                    self.worker_error.emit(safe_error)
                    raise fatal_claim_error

                if claimed_any:
                    continue

                if active:
                    next_due = self.get_db().seconds_until_next_pending_task()
                    timeout = 0.5
                    if next_due is not None:
                        timeout = min(timeout, max(0.01, float(next_due)))
                    done, _pending = await asyncio.wait(
                        set(active),
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if done:
                        if await reap(done):
                            log.error(
                                "Queue worker stopped after five consecutive "
                                "processing failures"
                            )
                            break
                    continue

                if self.paused:
                    if not await self._wait_for_task_available(None):
                        break
                    continue

                if self.persistent_idle:
                    next_due = self.get_db().seconds_until_next_pending_task()
                    if not await self._wait_for_task_available(next_due):
                        break
                    continue

                if self._consume_task_wakeup():
                    idle_since = None
                    continue
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= 1.5:
                    self._set_lifecycle_state(self.STATE_DRAINING)
                    final_task = self.get_db().claim_next_pending_task()
                    if final_task is None:
                        break
                    self._set_lifecycle_state(self.STATE_ACTIVE)
                    future = asyncio.create_task(self._process_task(final_task))
                    payload = final_task.get("payload") or {}
                    active[future] = int(
                        final_task.get("account_id")
                        or (
                            payload.get("account_id")
                            if isinstance(payload, dict)
                            else 0
                        )
                        or 0
                    )
                    idle_since = None
                    continue
                wait_for = max(
                    0.01, 1.5 - (time.monotonic() - float(idle_since))
                )
                if not await self._wait_for_task_available(wait_for):
                    break
        finally:
            if active:
                for future in active:
                    if not future.done():
                        future.cancel()
                await asyncio.gather(*active, return_exceptions=True)

    async def _process_task(self, task: dict) -> None:
        started = time.monotonic()
        self._set_active_task(task)
        try:
            await self._process_task_impl(task)
        finally:
            self._set_active_task(
                None, finished_task_id=int(task.get("id") or 0)
            )
            log_if_slow(
                log,
                "queue_task",
                started,
                threshold_seconds=5.0,
                task_id=task.get("id"),
                task_type=task.get("type"),
            )

    async def _process_task_impl(self, task: dict) -> None:
        task_id = int(task["id"])
        task_type = str(task["type"])
        handler = self._handlers.get(task_type)
        if handler is None:
            message = f"handler_missing: No handler for task type: {task_type}"
            self.get_db().set_failed(task_id, message, retry=False)
            self.failed_count += 1
            self.task_failed.emit(task_id, message)
            return

        if self.isInterruptionRequested():
            self.get_db().requeue_task(task_id, "Worker interrupted before execution")
            return

        task_payload = task.get("payload") or {}
        try:
            column_account_id = int(task.get("account_id") or 0)
            payload_account_value = (
                task_payload.get("account_id")
                if isinstance(task_payload, dict)
                else 0
            )
            payload_account_id = int(payload_account_value or 0)
        except (TypeError, ValueError, OverflowError):
            column_account_id = 0
            payload_account_id = 0
        if (
            column_account_id > 0
            and payload_account_id > 0
            and column_account_id != payload_account_id
        ):
            message = (
                "account_state_mismatch: task account column does not match "
                f"payload (column={column_account_id}, "
                f"payload={payload_account_id})"
            )
            self.get_db().set_failed(task_id, message, retry=False)
            self.failed_count += 1
            self.task_failed.emit(task_id, message)
            return

        # claim_next_pending_task() normalizes the durable account_id column.
        # Prefer it when a legacy payload does not contain account_id.
        task_account_id = column_account_id or payload_account_id
        if task_type in self.ACCOUNT_RPC_TASK_TYPES and task_account_id > 0:
            current_account_id = 0
            try:
                current_account_id = int(
                    self.get_db().get_setting("telegram.account_id", 0) or 0
                )
            except Exception:
                current_account_id = task_account_id
            if current_account_id > 0 and current_account_id != task_account_id:
                message = (
                    "account_state_mismatch: selected Telegram account changed "
                    f"after task creation (task={task_account_id}, "
                    f"current={current_account_id})"
                )
                self.get_db().set_failed(task_id, message, retry=False)
                campaign_id = int(task_payload.get("campaign_id") or 0)
                if campaign_id > 0:
                    reason = "Кампания приостановлена: Telegram-сессия не совпадает с локальным аккаунтом"
                    slot_id = int(task_payload.get("slot_id") or 0)
                    if task_type == "auto_comment_slot":
                        self.get_db().pause_comment_campaign(campaign_id, reason=reason)
                        if slot_id > 0:
                            self.get_db().defer_comment_slot(
                                slot_id, scheduled_at=utc_now(), result=reason
                            )
                    elif task_type == "join_saved_slot":
                        self.get_db().pause_join_campaign(campaign_id, reason=reason)
                        if slot_id > 0:
                            self.get_db().defer_join_slot(slot_id, utc_now(), reason)
                self.failed_count += 1
                self.task_failed.emit(task_id, message)
                return
        if (
            task_type in self.ACCOUNT_RPC_TASK_TYPES
            and task_account_id > 0
            and self._postpone_for_account_rpc_cooldown(
                task_id=task_id,
                task_type=task_type,
                account_id=task_account_id,
            )
        ):
            return

        link_pause_requested = bool(
            task_type == "link_channels"
            and (
                (
                    isinstance(task_payload, dict)
                    and task_payload.get("_link_pause_requested")
                )
                or self.is_scope_cancelled("task", task_id)
            )
        )
        if link_pause_requested:
            changed = self.get_db().pause_running_link_task(
                task_id,
                "Остановлено после завершения FloodWait/задержки; прогресс сохранён",
            )
            if not changed:
                current = self.get_db().get_task(task_id) or {}
                if str(current.get("status") or "") != "paused":
                    raise RuntimeError(f"Could not pause due link task {task_id}")
            self._persist_link_activity(
                "INFO",
                "FloodWait и защитная задержка завершены. Связки остановлены; "
                "продолжение возможно только по кнопке «Продолжить связки».",
                account_id=task_account_id,
            )
            return

        # A FloodWait may be activated while this task is waiting behind other
        # local work. Re-read it at the final handler boundary so a stale claim
        # cannot cross into Telegram after the embargo was installed.
        if (
            task_type in self.ACCOUNT_RPC_TASK_TYPES
            and task_account_id > 0
            and self._postpone_for_account_rpc_cooldown(
                task_id=task_id,
                task_type=task_type,
                account_id=task_account_id,
            )
        ):
            return

        try:
            await handler(task)
        except asyncio.CancelledError:
            if task_type in self.IDEMPOTENT_TASK_TYPES:
                self.get_db().requeue_task(
                    task_id, "Worker cancelled before completion"
                )
            else:
                self.get_db().set_failed(
                    task_id,
                    "Execution interrupted with uncertain external result; review before retry",
                    retry=False,
                )
            raise
        except TaskPausedError as exc:
            changed = self.get_db().pause_running_link_task(task_id, str(exc))
            if not changed:
                current = self.get_db().get_task(task_id) or {}
                if str(current.get("status") or "") != "paused":
                    raise RuntimeError(f"Could not pause link task {task_id}")
            if task_type == "link_channels":
                stored = self.get_db().get_task(task_id) or task
                progress = max(0, min(100, int(stored.get("progress") or 0)))
                self._persist_link_activity(
                    "INFO",
                    f"Остановлено пользователем. Прогресс сохранён на {progress}%. "
                    "Повторный запуск продолжит с сохранённой позиции.",
                    account_id=task_account_id,
                )
            return
        except DeferredTelegramError as exc:
            message = sanitize_text(f"{getattr(exc, 'code', 'deferred')}: {exc}")
            wait_seconds = max(1, int(exc.retry_after))
            retry_at = utc_now() + timedelta(seconds=wait_seconds)
            code = str(getattr(exc, "code", "deferred") or "deferred")
            if code == "flood_wait_deferred" and task_account_id > 0:
                try:
                    cooldown = install_account_flood_wait(
                        queue_worker=self,
                        worker_db=self.get_db(),
                        account_id=task_account_id,
                        retry_at=retry_at,
                        code=code,
                        source_task_id=task_id,
                        wait_seconds=wait_seconds,
                    )
                except Exception:
                    # Continue trying to defer the current task. Even if every
                    # SQLite write fails, the process-local embargo remains active
                    # and prevents another Telegram RPC for this account.
                    log.exception(
                        "Could not persist account FloodWait; "
                        "local embargo remains active"
                    )
                else:
                    persisted_retry_at = from_db_time(
                        cooldown.get("next_allowed_at")
                    )
                    if persisted_retry_at is not None and persisted_retry_at > retry_at:
                        retry_at = persisted_retry_at
            defer_result = self.get_db().defer_task(
                task_id, retry_at=retry_at, error=message
            )
            if defer_result == "exhausted":
                diagnostics = self.get_db().get_task_defer_diagnostics(task_id)
                defer_count = self._safe_non_negative_int(
                    diagnostics.get("defer_count"), 0
                )
                elapsed = self._safe_non_negative_int(
                    diagnostics.get("elapsed_since_first_defer_seconds"), 0
                )
                reason = str(getattr(exc, "code", "telegram_deferred"))
                exhausted_message = (
                    "defer_limit_exceeded: Задача остановлена: превышен лимит "
                    f"отложенных попыток. task_id={task_id}. "
                    f"Последняя причина: {reason}. "
                    f"Telegram потребовал ожидание: {int(exc.retry_after)} сек. "
                    f"Количество отложенных попыток: {defer_count}. "
                    f"Время с первого отложения: {elapsed} сек. "
                    "Автоматический повтор отключён; требуется проверка."
                )
                self.failed_count += 1
                self.task_failed.emit(task_id, exhausted_message)
                log.error(exhausted_message)
                if task_type == "link_channels":
                    self._persist_link_activity(
                        "ERROR",
                        "Автоматическое продолжение остановлено: превышен лимит "
                        "отложенных попыток. Проверьте аккаунт и запустите связки вручную.",
                        account_id=task_account_id,
                    )
                return
            if defer_result != "deferred":
                raise RuntimeError(f"Could not defer task {task_id}")
            self.retry_count += 1
            if task_type == "link_channels":
                stored = self.get_db().get_task(task_id) or task
                progress = max(0, min(100, int(stored.get("progress") or 0)))
                stored_payload = stored.get("payload") or {}
                if isinstance(stored_payload, str):
                    try:
                        stored_payload = json.loads(stored_payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        stored_payload = {}
                stop_after_wait = bool(
                    isinstance(stored_payload, dict)
                    and stored_payload.get("_link_pause_requested")
                ) or self.is_scope_cancelled("task", task_id)
                if stop_after_wait:
                    message = (
                        "Telegram установил FloodWait. Стоп принят: задача дождётся "
                        f"всех {self._format_wait_duration(int(exc.retry_after))}, "
                        f"сохранит прогресс {progress}% и перейдёт в паузу без нового RPC."
                    )
                else:
                    message = (
                        "Telegram установил FloodWait. Связки сохранены на "
                        f"{progress}%. Канал, вызвавший FloodWait, пропущен; "
                        "работа продолжится со следующего объекта через "
                        f"{self._format_wait_duration(int(exc.retry_after))}."
                    )
                self._persist_link_activity(
                    "WARNING",
                    message,
                    account_id=task_account_id,
                )
            log.info(
                "Task %s deferred for %ss without blocking the worker",
                task_id,
                exc.retry_after,
            )
            return
        except NonRetryableTelegramError as exc:
            code = str(getattr(exc, "code", "non_retryable") or "non_retryable")
            message = sanitize_text(f"{code}: {exc}")
            if code in RESTRICTION_CODES:
                task_payload = task.get("payload") or {}
                restriction_account_id: Any = (
                    task_payload.get("account_id")
                    if isinstance(task_payload, dict)
                    else None
                )
                state = activate_account_restriction(
                    self.get_db(),
                    code=code,
                    message=str(exc),
                    details=dict(getattr(exc, "details", {}) or {}),
                    account_id=restriction_account_id,
                )
                if task_account_id > 0:
                    state_setter = getattr(
                        self.get_db(), "set_account_runtime_state", None
                    )
                    if callable(state_setter):
                        state_setter(
                            task_account_id,
                            "restricted",
                            error=message,
                        )
                comment_id = state.get("comment_campaign_id")
                if comment_id:
                    self.request_scope_cancellation("comment_campaign", int(comment_id))
                join_id = state.get("join_campaign_id")
                if join_id:
                    self.request_scope_cancellation("join_campaign", int(join_id))
            self.get_db().set_failed(task_id, message, retry=False)
            self.failed_count += 1
            self.task_failed.emit(task_id, message)
            return
        except Exception as exc:
            retry_count = self._safe_non_negative_int(task.get("retry_count"), 0)
            max_retries = self._safe_non_negative_int(
                task.get("max_retries"), self.max_retries
            )
            message = sanitize_exception(exc)
            retry = (
                task_type in self.IDEMPOTENT_TASK_TYPES and retry_count < max_retries
            )
            self.get_db().set_failed(task_id, message, retry=retry)
            self.retry_count += int(retry)
            self.failed_count += int(not retry)
            self.task_failed.emit(task_id, message)
            return

        # The handler returned successfully, so an external side effect may already
        # exist. Never put this task back into pending if the completion write fails.
        try:
            changed = self.get_db().set_done(task_id)
        except Exception as exc:
            message = (
                "completion_state_uncertain: handler succeeded but SQLite could not "
                f"record completion: {sanitize_exception(exc)}"
            )
            log.critical("Task %s: %s", task_id, message)
            try:
                self.get_db().set_failed(task_id, message, retry=False)
            except Exception:
                log.exception(
                    "Could not mark uncertain task %s for manual review", task_id
                )
            self.failed_count += 1
            self.task_failed.emit(task_id, message)
            return

        if not changed:
            current = self.get_db().get_task(task_id) or {}
            if str(current.get("status") or "") == "completed":
                # Side-effect handlers may atomically commit their domain result
                # together with the task completion marker.
                self.processed_count += 1
                self.task_completed.emit(task_id)
                return
            log.warning("Task %s finished but was no longer in running state", task_id)
            return
        self.processed_count += 1
        self.task_completed.emit(task_id)

    @staticmethod
    def _safe_non_negative_int(value, default: int) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return max(0, int(default))

    def request_shutdown(self) -> None:
        """Linearize cooperative shutdown with mutating RPC dispatch barriers."""
        with self._scope_lock:
            self.requestInterruption()
        self.notify_task_available()

    def stop(self, wait_ms: int = 45_000) -> bool:
        self.request_shutdown()
        if QThread.currentThread() is self:
            return True
        return bool(self.wait(wait_ms))

    def get_stats(self, *, running_override: bool | None = None) -> dict:
        running = self.running if running_override is None else running_override
        return {
            "running": running,
            "paused": self.paused,
            "processed": self.processed_count,
            "failed": self.failed_count,
            "retried": self.retry_count,
            "heartbeat": self.heartbeat,
        }
