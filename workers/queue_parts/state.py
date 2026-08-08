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
from core.account_limits import MAX_CONCURRENT_TELEGRAM_ACCOUNT_TASKS
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
from workers.queue_task_decisions import (
    CancellationPersistence,
    TaskExecutionContext,
    cancellation_persistence,
    parse_account_identity,
    unexpected_retry_allowed,
)
log = logging.getLogger(__name__)
TaskHandler = Callable[[dict], Awaitable[Any]]
HandlerFactory = Callable[[], Any]

class QueueWorkerStateMixin:
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
