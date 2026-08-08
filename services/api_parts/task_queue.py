from __future__ import annotations

from typing import TYPE_CHECKING

import json
import logging
import math
import threading
from typing import Any, cast

from PySide6.QtCore import QTimer, Slot

from core.account_state import has_pending_account_state
from services.audience_parser import validate_audience_task_payload

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from core.mixin_host import MixinHost as _MixinHost
else:

    class _MixinHost:
        pass


class TaskQueueAPIMixin(_MixinHost):
    _last_worker_error: str | None

    @staticmethod
    def _decode_task(task: dict[str, Any]) -> dict[str, Any]:
        result = dict(task)
        payload = result.get("payload")
        if isinstance(payload, str):
            try:
                result["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                result["payload"] = {}
                result["payload_error"] = "invalid_json"
        return result

    def get_channels(self, account_id: int | None = None) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.database.get_channels(account_id=account_id),
        )

    def delete_channels(self, channel_ids) -> dict[str, Any]:
        normalized = sorted({int(value) for value in channel_ids if int(value) != 0})
        if not normalized:
            raise ValueError("Выберите хотя бы один канал или группу")
        account_id = int(self.database.get_setting("telegram.account_id", 0) or 0)
        if account_id <= 0:
            raise ValueError("Сначала авторизуйте Telegram-аккаунт")
        scopes = tuple(("channel", channel_id, account_id) for channel_id in normalized)
        result = self._cancel_scopes_and_mutate(
            scopes,
            lambda: self.database.delete_channels_transactional(
                normalized, account_id=account_id
            ),
        )
        for task_id in result.get("cancelled_task_ids", []):
            self._request_scope_cancellation("task", int(task_id))
        return cast(dict[str, Any], result)

    def get_commenting_channels(self) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.database.get_channels_for_commenting(self.max_channels_per_run),
        )

    def get_max_channels_per_run(self) -> int:
        return int(self.max_channels_per_run)

    def get_comment_daily_limit(
        self, account_id: int | None = None
    ) -> int:
        """Return the locally persisted GUI limit for one explicit account."""
        owner = int(account_id or self.get_current_account_id() or 0)
        database = self.database.for_account(owner) if owner > 0 else self.database
        raw = database.get_setting(
            self.COMMENT_DAILY_LIMIT_SETTING, self.max_channels_per_run
        )
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            value = self.max_channels_per_run
        return max(0, min(1000, value))

    def set_comment_daily_limit(
        self,
        value: int,
        *,
        account_id: int | None = None,
    ) -> int:
        """Persist the next-campaign limit for one explicit account."""
        try:
            normalized = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "Количество комментариев должно быть целым числом"
            ) from exc
        normalized = max(0, min(1000, normalized))
        owner = int(account_id or self.get_current_account_id() or 0)
        if owner <= 0:
            raise ValueError("Сначала выберите Telegram-аккаунт")
        database = self.database.for_account(owner)
        if database.get_active_comment_campaign(account_id=owner):
            raise ValueError(
                "Лимит нельзя менять во время активной кампании. "
                "Остановите её и задайте значение перед новым запуском"
            )
        database.set_setting(self.COMMENT_DAILY_LIMIT_SETTING, normalized)
        return normalized

    def create_task(
        self, task_type: str, payload: dict[str, Any], max_retries: int = 3
    ) -> dict[str, Any]:
        if (
            not isinstance(task_type, str)
            or task_type.strip() not in self.ALLOWED_TASK_TYPES
        ):
            raise ValueError(f"Unsupported task type: {task_type!r}")
        task_type = task_type.strip()
        if not isinstance(payload, dict):
            raise ValueError("Task payload must be an object")
        payload = dict(payload)
        if task_type == "comment":
            if payload.get("post_id") is None or payload.get("channel_id") is None:
                raise ValueError("comment requires post_id and channel_id")
            if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                raise ValueError("comment requires non-empty text")
        elif task_type == "auto_comment":
            comments = payload.get("comments")
            if not isinstance(comments, list) or not any(
                isinstance(item, str) and item.strip() for item in comments
            ):
                raise ValueError("auto_comment requires at least one non-empty comment")
            for key in ("delay_min", "delay_max"):
                if key in payload:
                    try:
                        value = float(payload[key])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError(f"{key} must be a finite number") from exc
                    if not math.isfinite(value):
                        raise ValueError(f"{key} must be a finite number")
                    if value < 0 or value > 3600:
                        raise ValueError(f"{key} is outside the allowed range")
                    payload[key] = value
        elif task_type in {"auto_comment_slot", "join_saved_slot"}:
            if payload.get("campaign_id") is None or payload.get("slot_id") is None:
                raise ValueError(f"{task_type} requires campaign_id and slot_id")
        elif task_type == "import":
            files = payload.get("files")
            single = payload.get("kind") is not None and payload.get("path") is not None
            if not (isinstance(files, dict) and files) and not single:
                raise ValueError("import requires non-empty files object or kind/path")
        elif task_type == "parse_audience":
            payload = validate_audience_task_payload(payload)
        if task_type in self.ACCOUNT_BOUND_TASK_TYPES:
            try:
                requested = int(payload.get("account_id") or 0)
                selected = int(self.get_current_account_id() or 0)
                account_id = requested or selected
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Некорректный Telegram-аккаунт") from exc
            if account_id <= 0:
                raise ValueError("Сначала авторизуйте Telegram-аккаунт")
            if not self.database.account_accepts_new_work(account_id):
                raise ValueError(
                    "Работа выбранного аккаунта остановлена или ограничена"
                )
            payload["account_id"] = account_id
        effective_retries = (
            0 if task_type in self.NON_IDEMPOTENT_TASK_TYPES else max_retries
        )
        if task_type == "link_channels":
            row, created = self.database.create_or_get_link_task(
                account_id=int(payload["account_id"]),
                payload=payload,
                max_retries=effective_retries,
            )
            result = self._decode_task(dict(row))
            result["reused"] = not bool(created)
            return result
        task_id = self.database.insert_task(task_type, payload, effective_retries)
        return {
            "id": task_id,
            "type": task_type,
            "status": "pending",
            "progress": 0,
            "payload": payload,
        }

    def get_tasks(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return [
            self._decode_task(task)
            for task in self.database.get_tasks(status=status, limit=limit)
        ]

    def cancel_task(self, task_id: int) -> bool:
        task = self.database.get_task(task_id)
        if task and str(task.get("type") or "") in {
            "auto_comment_slot",
            "join_saved_slot",
        }:
            log.warning(
                "Generic cancellation rejected for campaign task %s; use campaign pause or stop",
                task_id,
            )
            return False
        if task and str(task.get("type") or "") == "parse_audience":
            # Останавливаем только этот парсинг; другие аккаунты продолжают работу.

            def cancel_parser_task() -> bool:
                current = self.database.get_task(task_id) or {}
                status = str(current.get("status") or "")
                if status in {"running", "processing"}:
                    worker = self.queue_worker
                    is_running = getattr(worker, "isRunning", None)
                    if worker is not None and (
                        not callable(is_running) or bool(is_running())
                    ):
                        # The task-local scope stops the read-only handler at its
                        # next safe boundary. The handler persists cancellation
                        # only after it has removed its temporary output.
                        return True
                    return bool(
                        self.database.cancel_running_audience_task(
                            task_id, "Остановлено пользователем"
                        )
                    )
                return bool(self.database.cancel_task(task_id))

            return bool(
                self._cancel_scopes_and_mutate(
                    (("task", int(task_id)),),
                    cancel_parser_task,
                )
            )
        return bool(self.database.cancel_task(task_id))

    def get_active_link_task(
        self, account_id: int | None = None
    ) -> dict[str, Any] | None:
        row = self.database.get_active_link_task(account_id=account_id)
        return self._decode_task(row) if row else None

    def count_unchecked_link_targets(self, account_id: int | None = None) -> int:
        return int(self.database.count_unchecked_link_targets(account_id=account_id))

    def pause_link_task(self, task_id: int) -> bool:
        task = self.database.get_task(task_id)
        if not task or str(task.get("type") or "") != "link_channels":
            return False
        status = str(task.get("status") or "")
        if status in {"pending", "running", "processing"}:
            worker = self.queue_worker
            if status in {"running", "processing"} and (
                worker is None or not worker.isRunning()
            ):
                return bool(self.database.pause_running_link_task(task_id))
            result = str(self.database.request_link_task_pause(task_id) or "")
            if result in {"waiting", "requested"}:
                self._request_scope_cancellation("task", int(task_id))
            return result in {"waiting", "requested", "paused"}
        return status == "paused"

    def resume_link_task(self, task_id: int) -> bool:
        changed = bool(self.database.resume_link_task(task_id))
        if changed:
            self._clear_scope_cancellation("task", int(task_id))
        return changed

    def get_templates(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.database.get_templates())

    def get_logs(
        self,
        level: str | None = None,
        limit: int = 100,
        account_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self.database.get_logs(
                level=level,
                limit=limit,
                account_id=account_id,
            ),
        )

    def _queue_start_blocker_locked(self) -> str | None:
        if self.queue_worker is None:
            return "worker_missing"
        if self._shutdown_requested:
            return "shutdown_in_progress"
        if self._secret_migration_required.is_set():
            return "local_secret_migration"
        if has_pending_account_state(self.database.path):
            return "account_transition_pending"
        # Authorization and Telegram restrictions are account-scoped.
        # They must not stop the shared worker from serving other accounts.
        return None

    def get_queue_unavailable_reason(self) -> str | None:
        """Return a stable machine-readable reason why the queue cannot start."""
        with self._queue_lock:
            return self._queue_start_blocker_locked()

    def start_queue(self) -> bool:
        if self._shutdown_requested:
            return False
        worker = self.queue_worker
        with self._queue_lock:
            blocker = self._queue_start_blocker_locked()
            if blocker is not None:
                log.warning("Queue start blocked: %s", blocker)
                return False
            if worker is None:
                # _queue_start_blocker_locked() already reports a missing worker,
                # so reaching this point means the two checks disagree.
                log.error("Queue start reached an inconsistent worker state")
                return False
            self._last_worker_error = None
            if worker.isRunning():
                # A task may be inserted while the worker is leaving its idle
                # loop or disconnecting Telegram. Wake it immediately and also
                # remember to restart after cleanup if it is already too late.
                self._restart_requested = True
                notify = getattr(worker, "notify_task_available", None)
                if callable(notify):
                    notify()
                return True
            self._restart_requested = False
            worker.start()
        return True

    @Slot(str)
    def _on_worker_error(self, message: str) -> None:
        """Remember the exact terminal queue error for unclaimed GUI tasks."""

        detail = str(message or "unknown queue worker error").strip()
        with self._queue_lock:
            self._last_worker_error = detail
        log.error("Queue worker reported an error: %s", detail)

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self.queue_worker
        if worker is None:
            return
        with self._queue_lock:
            interrupted = worker.isInterruptionRequested()
            should_restart = (
                self._restart_requested
                and not self._shutdown_requested
                and not interrupted
            )
            self._restart_requested = False
        if should_restart:
            # Run on the GUI event loop after QThread has fully released its
            # previous native thread resources.
            QTimer.singleShot(0, self._restart_if_pending)

    @Slot()
    def _restart_if_pending(self) -> None:
        worker = self.queue_worker
        if worker is None:
            return
        with self._queue_lock:
            if self._shutdown_requested or worker.isRunning():
                return
            if not self.database.has_due_pending_tasks():
                return
            worker.start()

    def is_queue_running(self) -> bool:
        """Return whether unfinished queue work blocks an account change.

        This deliberately does not mirror ``QThread.isRunning()``: in production
        the worker stays alive while idle to retain the Telegram connection.
        """

        worker = self.queue_worker
        active = bool(worker is not None and getattr(worker, "has_active_task", False))
        return active or bool(self.database.has_account_change_blocking_tasks())

    def prepare_account_change(self, timeout_ms: int = 45_000) -> bool:
        """Disconnect the idle persistent worker before auth touches its session.

        The caller must first verify that no campaigns or unfinished tasks own the
        current account. Stopping the idle worker closes Telethon and its SQLite
        session cleanly, preventing concurrent access by ``TelegramAuthWorker``.
        """

        if self.is_queue_running():
            return False
        worker = self.queue_worker
        if worker is None or not worker.isRunning():
            return True
        with self._queue_lock:
            self._restart_requested = False
        stopped = bool(worker.stop(max(1, int(timeout_ms))))
        if not stopped:
            log.error("Idle queue worker did not stop before account change")
        return stopped

    def stop_queue(self) -> bool:
        worker = self.queue_worker
        if worker is None or not worker.isRunning():
            return False
        with self._queue_lock:
            self._restart_requested = False
        request_shutdown = getattr(type(worker), "request_shutdown", None)
        if callable(request_shutdown):
            request_shutdown(worker)
        else:  # pragma: no cover - compatibility worker doubles
            worker.requestInterruption()
            notify = getattr(worker, "notify_task_available", None)
            if callable(notify):
                notify()
        return True

    def _cancel_scopes_and_mutate(self, scopes, mutation):
        """Linearize durable stop/pause state with Telegram dispatch barriers.

        QueueWorker owns the same lock used immediately before mutating MTProto
        calls.  Holding it while cancellation scopes are published and SQLite is
        changed prevents a task that was claimed earlier from crossing the
        dispatch boundary after the campaign has become paused/stopped.
        """
        normalized = tuple(tuple(scope) for scope in scopes)
        worker = self.queue_worker
        callback = getattr(type(worker), "cancel_scopes_and_run", None)
        if worker is not None and callable(callback):
            return callback(worker, normalized, mutation)

        # Compatibility fallback for tests or reduced worker implementations.
        # Persist first, then publish cancellation just as the pre-barrier code did.
        result = mutation()
        if result:
            for scope in normalized:
                self._request_scope_cancellation(*scope)
        return result

    def _request_scope_cancellation(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> None:
        worker = self.queue_worker
        callback = getattr(worker, "request_scope_cancellation", None)
        if callable(callback):
            if account_id is None:
                callback(str(scope_type), int(scope_id))
            else:
                callback(str(scope_type), int(scope_id), int(account_id))

    def _clear_scope_cancellation(
        self, scope_type: str, scope_id: int, account_id: int | None = None
    ) -> None:
        worker = self.queue_worker
        callback = getattr(worker, "clear_scope_cancellation", None)
        if callable(callback):
            if account_id is None:
                callback(str(scope_type), int(scope_id))
            else:
                callback(str(scope_type), int(scope_id), int(account_id))

    def _request_campaign_cancellation(self, campaign_id: int) -> None:
        self._request_scope_cancellation("comment_campaign", campaign_id)

    def _clear_campaign_cancellation(self, campaign_id: int) -> None:
        self._clear_scope_cancellation("comment_campaign", campaign_id)

    def prepare_shutdown(self) -> None:
        """Disable automatic restarts and cooperatively stop the worker."""
        self._campaign_timer.stop()
        self._delivery_recovery_timer.stop()
        self._maintenance_timer.stop()
        worker = self.queue_worker
        with self._queue_lock:
            self._shutdown_requested = True
            self._restart_requested = False
        if worker is not None and worker.isRunning():
            request_shutdown = getattr(type(worker), "request_shutdown", None)
            if callable(request_shutdown):
                request_shutdown(worker)
            else:  # pragma: no cover - compatibility worker doubles
                worker.requestInterruption()
                notify = getattr(worker, "notify_task_available", None)
                if callable(notify):
                    notify()

    def prepare_factory_reset(self) -> dict[str, bool]:
        """Stop persistent campaigns and enter the coordinated shutdown state."""
        comment_stopped = bool(self.stop_comment_campaign())
        join_stopped = bool(self.stop_join_campaign())
        self.prepare_shutdown()
        log.info(
            "Factory reset shutdown prepared: comment_campaign_stopped=%s "
            "join_campaign_stopped=%s",
            comment_stopped,
            join_stopped,
        )
        return {
            "comment_campaign_stopped": comment_stopped,
            "join_campaign_stopped": join_stopped,
        }

    def is_secret_migration_running(self) -> bool:
        return bool(self._secret_migration_thread.is_alive())

    def wait_for_secret_migration(self, timeout_ms: int) -> bool:
        """Wait for the local-file migration thread before closing/deleting data."""
        thread = self._secret_migration_thread
        if not thread.is_alive():
            return True
        if threading.current_thread() is thread:
            return False
        thread.join(max(0, int(timeout_ms)) / 1000.0)
        return not thread.is_alive()

    def cancel_shutdown(self) -> None:
        """Restore schedulers after a graceful-shutdown attempt was aborted."""
        with self._queue_lock:
            self._shutdown_requested = False
            self._restart_requested = False
        if not self._campaign_timer.isActive():
            self._campaign_timer.start()
        if not self._delivery_recovery_timer.isActive():
            self._delivery_recovery_timer.start()
        if not self._maintenance_timer.isActive():
            self._maintenance_timer.start()
        QTimer.singleShot(0, self._campaign_tick)
        QTimer.singleShot(0, self._reconcile_stale_deliveries)
        QTimer.singleShot(0, self._run_daily_maintenance)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        task = self.database.get_task(task_id)
        worker = self.queue_worker
        with self._queue_lock:
            worker_error = self._last_worker_error
        if (
            task
            and str(task.get("status") or "") == "pending"
            and worker_error
            and (worker is None or not worker.isRunning())
        ):
            message = f"queue_worker_failed: {worker_error}"
            if self.database.fail_due_pending_task(task_id, message):
                log.error(
                    "Pending task %s failed because the queue worker stopped: %s",
                    task_id,
                    worker_error,
                )
                task = self.database.get_task(task_id)
        return self._decode_task(task) if task else None

    def close_thread_connection(self) -> None:
        """Release SQLite state owned by the current worker thread."""
        self.database.close_thread_connection()
