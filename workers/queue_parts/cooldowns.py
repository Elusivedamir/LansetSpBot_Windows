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

class QueueWorkerCooldownMixin:
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
    def _account_cooldown_blocks(self, context: TaskExecutionContext) -> bool:
        return bool(
            context.task_type in self.ACCOUNT_RPC_TASK_TYPES
            and context.account_id > 0
            and self._postpone_for_account_rpc_cooldown(
                task_id=context.task_id,
                task_type=context.task_type,
                account_id=context.account_id,
            )
        )

    def _account_safety_blocks(self, context: TaskExecutionContext) -> bool:
        if context.task_type not in self.ACCOUNT_RPC_TASK_TYPES or context.account_id <= 0:
            return False
        reserver = getattr(self.get_db(), "reserve_account_safety_task", None)
        if not callable(reserver):
            return False
        decision = dict(reserver(account_id=context.account_id, task_id=context.task_id, task_type=context.task_type) or {})
        action = str(decision.get("action") or "allow")
        if action in {"allow", "block"}:
            return False
        if action != "postpone":
            raise RuntimeError(f"Unknown account safety task action: {action}")
        wait = max(1, int(decision.get("wait_seconds") or 1))
        changed = self.get_db().postpone_running_task_for_account_cooldown(
            context.task_id, retry_at=utc_now() + timedelta(seconds=wait),
            code=str(decision.get("reason_code") or "account_safety_pacing"),
        )
        if not changed:
            raise RuntimeError(f"Could not postpone task {context.task_id} for adaptive safety")
        return True
