from __future__ import annotations

from datetime import datetime
from typing import cast
import weakref

import shiboken6
from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.campaign_schedule import from_db_time, utc_now
from core.countdown import countdown_label, seconds_until
from gui.background import BackgroundCall


class ActivityPanel(QFrame):
    """Account-scoped live journal reconstructed from persistent state."""

    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.setObjectName("activityPanel")
        self.setMinimumHeight(145)

        self._account_id: int | None = None
        self._campaign_id: int | None = None
        self._campaign_status = ""
        self._last_history_id = 0
        self._last_log_id = 0
        self._last_next_at = ""
        self._last_missed_count = 0
        self._channel_names: dict[int, str] = {}
        self._collapsed = False
        self._last_scheduler_error = ""
        self._restriction_active = False
        self._join_campaign_id: int | None = None
        self._join_attempted = -1
        self._link_task_id: int | None = None
        self._link_task_status = ""
        self._link_task_not_before = ""
        self._link_wait_bucket: int | None = None
        self._refresh_job: BackgroundCall | None = None
        self._refresh_pending = False
        self._account_generation = 0
        self._countdown_at = None
        self._countdown_key = ""
        self._countdown_prefix = "Следующая проверка"
        self._countdown_include_deadline = True
        self._countdown_due_refresh_requested = False
        self._countdown_fallback = "Следующая проверка: —"

        self.title_label = QLabel("ЖИВОЙ ЖУРНАЛ")
        self.title_label.setObjectName("activityTitle")
        self.state_label = QLabel("Ожидание кампании")
        self.state_label.setObjectName("activityBadge")
        self.next_label = QLabel("Следующая проверка: —")
        self.next_label.setMinimumWidth(290)
        self.next_label.setObjectName("activityNext")

        self.collapse_button = QPushButton("Свернуть")
        self.collapse_button.setObjectName("tinyButton")
        self.collapse_button.clicked.connect(self._toggle_collapsed)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self.title_label)
        header.addWidget(self.state_label)
        header.addStretch(1)
        header.addWidget(self.next_label)
        header.addWidget(self.collapse_button)

        self.feed = QPlainTextEdit()
        self.feed.setObjectName("activityLog")
        self.feed.setReadOnly(True)
        self.feed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.feed.setMaximumBlockCount(500)
        self.feed.setPlaceholderText("Здесь появятся действия LansetSpBot…")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 14)
        layout.setSpacing(9)
        layout.addLayout(header)
        layout.addWidget(self.feed, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(3_000)
        self.timer.timeout.connect(self.request_refresh)
        self.timer.start()

        self.countdown_timer = QTimer(self)
        self.countdown_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.countdown_timer.setInterval(1_000)
        self.countdown_timer.timeout.connect(self._update_countdown_label)
        self.countdown_timer.start()
        self._append("Журнал готов. Запустите нужную операцию в рабочем разделе.")
        self.request_refresh()

    def _current_account_id(self) -> int:
        getter = getattr(self.adapter, "get_current_account_id", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(getter() or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    def handle_account_changed(self) -> None:
        self._account_generation += 1
        self._refresh_pending = self._refresh_job is not None
        self._reset_for_account(self._current_account_id())
        if self._refresh_job is None:
            self.request_refresh()

    def set_compact(self, compact: bool) -> None:
        self.title_label.setVisible(not compact)
        self.next_label.setVisible(not compact)
        self.collapse_button.setText(
            ("▲" if self._collapsed else "▼")
            if compact
            else ("Развернуть" if self._collapsed else "Свернуть")
        )
        self.collapse_button.setToolTip(
            "Развернуть журнал" if self._collapsed else "Свернуть журнал"
        )

    def _toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self.feed.setVisible(not self._collapsed)
        compact = not self.title_label.isVisible()
        self.collapse_button.setText(
            ("▲" if self._collapsed else "▼")
            if compact
            else ("Развернуть" if self._collapsed else "Свернуть")
        )
        self.setMinimumHeight(54 if self._collapsed else 145)
        self.setMaximumHeight(64 if self._collapsed else 16_777_215)

    @staticmethod
    def _clock() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _append(self, text: str) -> None:
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        self.feed.appendPlainText(f"[{self._clock()}]  {clean}")
        cursor = self.feed.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.feed.setTextCursor(cursor)
        self.feed.ensureCursorVisible()

    def _reset_for_account(self, account_id: int) -> None:
        was_initialized = self._account_id is not None
        self._account_id = max(0, int(account_id or 0))
        self.feed.clear()
        self._campaign_id = None
        self._campaign_status = ""
        self._last_history_id = 0
        self._last_log_id = 0
        self._last_next_at = ""
        self._last_missed_count = 0
        self._channel_names = {}
        self._last_scheduler_error = ""
        self._restriction_active = False
        self._join_campaign_id = None
        self._join_attempted = -1
        self._link_task_id = None
        self._link_task_status = ""
        self._link_task_not_before = ""
        self._link_wait_bucket = None
        self.state_label.setText(
            "Ожидание кампании" if self._account_id > 0 else "Аккаунт не подключён"
        )
        self._clear_countdown("Следующая проверка: —")
        if self._account_id > 0:
            prefix = "Аккаунт переключён. " if was_initialized else "Журнал загружен. "
            self._append(prefix + "Показаны только действия выбранного аккаунта.")
        else:
            self._append("Telegram-аккаунт не подключён.")

    @staticmethod
    def _duration_text(seconds: int) -> str:
        seconds = max(0, int(seconds))
        if seconds < 10:
            return "сейчас"
        if seconds < 60:
            return f"через {seconds} сек"
        minutes = round(seconds / 60)
        if minutes < 60:
            return f"через {minutes} мин"
        hours, minutes = divmod(minutes, 60)
        return f"через {hours} ч {minutes} мин" if minutes else f"через {hours} ч"

    def _next_description(self, value) -> tuple[str, str]:
        parsed = from_db_time(value)
        if parsed is None:
            return "Следующая проверка: —", ""
        seconds = round((parsed - utc_now()).total_seconds())
        relative = self._duration_text(seconds)
        local_time = parsed.astimezone().strftime("%H:%M")
        return f"Следующая проверка {relative} · {local_time}", relative

    def _set_countdown(
        self,
        value,
        *,
        prefix: str = "Следующая проверка",
        include_deadline: bool = True,
        fallback: str | None = None,
    ) -> None:
        parsed = from_db_time(value)
        key = parsed.isoformat() if parsed is not None else ""
        if key != self._countdown_key or prefix != self._countdown_prefix:
            self._countdown_due_refresh_requested = False
        self._countdown_at = parsed
        self._countdown_key = key
        self._countdown_prefix = str(prefix)
        self._countdown_include_deadline = bool(include_deadline)
        self._countdown_fallback = fallback or f"{prefix}: —"
        self._update_countdown_label()

    def _clear_countdown(self, text: str = "Следующая проверка: —") -> None:
        self._countdown_at = None
        self._countdown_key = ""
        self._countdown_due_refresh_requested = False
        self._countdown_fallback = str(text or "Следующая проверка: —")
        self.next_label.setText(self._countdown_fallback)

    def _update_countdown_label(self) -> None:
        target = self._countdown_at
        if target is None:
            self.next_label.setText(self._countdown_fallback)
            return
        self.next_label.setText(
            countdown_label(
                self._countdown_prefix,
                target,
                include_deadline=self._countdown_include_deadline,
                include_date=False,
            )
        )
        if (
            seconds_until(target) == 0
            and self._refresh_job is None
            and not self._countdown_due_refresh_requested
        ):
            self._countdown_due_refresh_requested = True
            self.request_refresh()

    def _history_message(self, row: dict) -> str:
        channel_id = row.get("channel_id")
        channel = "канал"
        if channel_id is not None:
            try:
                channel = self._channel_names.get(int(channel_id), str(channel_id))
            except (TypeError, ValueError):
                channel = str(channel_id)
        status = str(row.get("status") or "").strip()
        post_id = row.get("post_id")
        if status == "Отправлено":
            suffix = f" под постом #{post_id}" if post_id is not None else ""
            return f"Оставлен комментарий в чате обсуждения «{channel}»{suffix}."
        return f"«{channel}» — {status}." if status else f"Проверка «{channel}» завершена."

    def _refresh_persistent_logs(self, rows: list[dict]) -> None:
        for row in sorted(rows, key=lambda item: int(item.get("id") or 0)):
            row_id = int(row.get("id") or 0)
            if row_id <= self._last_log_id:
                continue
            level = str(row.get("level") or "INFO").upper()
            message = str(row.get("message") or "").strip()
            if message:
                self._append(f"[{level}] {message}")
            self._last_log_id = max(self._last_log_id, row_id)

    @staticmethod
    def _countdown_text(seconds: int) -> str:
        value = max(0, int(seconds))
        hours, remainder = divmod(value, 3600)
        minutes, secs = divmod(remainder, 60)
        return (
            f"{hours:02d}:{minutes:02d}:{secs:02d}"
            if hours
            else f"{minutes:02d}:{secs:02d}"
        )

    @staticmethod
    def _link_wait_type(task: dict) -> str:
        payload = task.get("payload") or {}
        checkpoint = payload.get("_link_checkpoint") if isinstance(payload, dict) else {}
        if isinstance(checkpoint, dict):
            return str(checkpoint.get("wait_type") or "")
        return ""

    def _apply_link_task(self, raw_task: object, *, restricted: bool) -> bool:
        if not isinstance(raw_task, dict):
            return False
        task_id = int(raw_task.get("id") or 0)
        if task_id <= 0:
            return False
        status = str(raw_task.get("status") or "")
        progress = max(0, min(100, int(raw_task.get("progress") or 0)))
        not_before = str(raw_task.get("not_before") or "")
        wait_type = self._link_wait_type(raw_task)
        previous_status = self._link_task_status
        is_new = task_id != self._link_task_id
        if is_new:
            self._link_task_id = task_id
            self._link_wait_bucket = None
            previous_status = ""

        retry_at = from_db_time(not_before)
        remaining = (
            max(0, round((retry_at - utc_now()).total_seconds()))
            if retry_at is not None
            else 0
        )
        waiting = status == "pending" and remaining > 0
        active = status in {"running", "pending"}
        if active and not restricted:
            if wait_type == "local_join_cooldown":
                self.state_label.setText(f"Связки · локальная пауза JOIN · {progress}%")
            elif wait_type == "channel_cooldown":
                self.state_label.setText(f"Связки · пауза между каналами · {progress}%")
            elif waiting or wait_type == "telegram_flood_wait":
                self.state_label.setText(f"Связки · Telegram FloodWait · {progress}%")
            elif status == "running":
                self.state_label.setText(f"Связки · выполняются · {progress}%")
            else:
                self.state_label.setText(f"Связки · ожидание запуска · {progress}%")

        if is_new and status == "pending" and not waiting:
            self._append("[Связки] Задача поставлена в очередь.")
        if waiting:
            prefix = {
                "local_join_cooldown": "Локальная пауза между вступлениями",
                "channel_cooldown": "Пауза между каналами",
            }.get(wait_type, "Telegram FloodWait")
            self._set_countdown(retry_at, prefix="Продолжение", include_deadline=False)
            bucket = ((remaining + 59) // 60) * 60
            if self._link_wait_bucket != bucket:
                self._link_wait_bucket = bucket
                self._append(
                    f"[Связки] {prefix}: осталось {self._countdown_text(remaining)}; "
                    f"прогресс сохранён на {progress}%."
                )
        elif active and status == "running":
            self._clear_countdown("Checkpoint сохраняется после каждого канала")
            if previous_status == "pending":
                self._append("[Связки] Работа продолжена с сохранённой позиции.")
            self._link_wait_bucket = None
        if status in {"failed", "cancelled"} and status != previous_status:
            error = str(raw_task.get("error") or "неизвестная ошибка").strip()
            self._append(f"[Связки] Операция остановлена: {error}.")
        self._link_task_status = status
        self._link_task_not_before = not_before
        return active

    def _load_snapshot(
        self,
        *,
        account_id: int | None = None,
        generation: int | None = None,
    ) -> dict[str, object]:
        owner = self._current_account_id() if account_id is None else max(0, int(account_id))
        owner_generation = self._account_generation if generation is None else int(generation)

        def scoped(name: str, *, default=None):
            getter = getattr(self.adapter, name, None)
            if not callable(getter):
                return default
            try:
                return getter(account_id=owner)
            except TypeError:
                return getter()

        state = scoped("get_comment_campaign_state")
        join_state = scoped("get_join_campaign_state")
        restriction = dict(scoped("get_account_restriction_state", default={}) or {})
        link_task = scoped("get_active_link_task")
        channels: list[dict] = []
        history: list[dict] = []
        if isinstance(state, dict) and owner > 0:
            try:
                channels = list(self.adapter.get_channels(account_id=owner) or [])
            except TypeError:
                channels = list(self.adapter.get_channels() or [])
            try:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=int(state["id"]), limit=110, account_id=owner
                    )
                    or []
                )
            except TypeError:
                history = list(
                    self.adapter.get_comment_history(
                        campaign_id=int(state["id"]), limit=110
                    )
                    or []
                )
        try:
            logs = self.adapter.get_logs(limit=150, account_id=owner)
        except TypeError:
            logs = self.adapter.get_logs(limit=150)
        scheduler_getter = getattr(self.adapter, "get_scheduler_error", None)
        scheduler_error = scheduler_getter() if callable(scheduler_getter) else ""
        return {
            "account_id": owner,
            "generation": owner_generation,
            "logs": list(logs or []),
            "restriction": restriction,
            "scheduler_error": str(scheduler_error or ""),
            "link_task": link_task,
            "state": state,
            "join_state": join_state,
            "channels": channels,
            "history": history,
        }

    def request_refresh(self) -> None:
        if self._refresh_job is not None:
            self._refresh_pending = True
            return
        panel_ref = weakref.ref(self)
        account_id = self._current_account_id()
        generation = self._account_generation

        def live_panel() -> ActivityPanel | None:
            panel = panel_ref()
            return panel if panel is not None and shiboken6.isValid(panel) else None

        def load_snapshot() -> dict[str, object] | None:
            panel = live_panel()
            return (
                panel._load_snapshot(account_id=account_id, generation=generation)
                if panel is not None
                else None
            )

        cleanup = getattr(self.adapter, "close_thread_connection", None)
        job = BackgroundCall(load_snapshot, cleanup=cleanup if callable(cleanup) else None)
        self._refresh_job = job

        def succeeded(snapshot: object) -> None:
            panel = live_panel()
            if panel is not None and isinstance(snapshot, dict):
                panel._apply_snapshot(snapshot)

        def failed(_message: str) -> None:
            panel = live_panel()
            if panel is not None and generation == panel._account_generation:
                panel.state_label.setText("Нет связи с базой")

        def finished() -> None:
            panel = live_panel()
            if panel is None:
                return
            if panel._refresh_job is job:
                panel._refresh_job = None
            if panel._refresh_pending:
                panel._refresh_pending = False
                QTimer.singleShot(0, panel.request_refresh)

        job.signals.succeeded.connect(succeeded)
        job.signals.failed.connect(failed)
        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def refresh(self) -> None:
        try:
            snapshot = self._load_snapshot()
        except Exception:
            self.state_label.setText("Нет связи с базой")
            return
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: dict[str, object]) -> None:
        snapshot_account = max(0, int(cast(int, snapshot.get("account_id")) or 0))
        try:
            snapshot_generation = int(
                cast(int, snapshot.get("generation", self._account_generation))
            )
        except (TypeError, ValueError, OverflowError):
            snapshot_generation = -1
        current = self._current_account_id()
        if current != snapshot_account or snapshot_generation != self._account_generation:
            if self._account_id != current:
                self._reset_for_account(current)
            self._refresh_pending = True
            return
        if self._account_id != snapshot_account:
            self._reset_for_account(snapshot_account)

        logs = snapshot.get("logs")
        self._refresh_persistent_logs(list(logs) if isinstance(logs, list) else [])
        scheduler_error = str(snapshot.get("scheduler_error") or "")
        if scheduler_error and scheduler_error != self._last_scheduler_error:
            self._append(f"Ошибка планировщика: {scheduler_error}. Кампания приостановлена.")
        self._last_scheduler_error = scheduler_error

        restriction = snapshot.get("restriction")
        restriction = restriction if isinstance(restriction, dict) else {}
        restricted = bool(restriction.get("active"))
        if restricted:
            code = str(restriction.get("code") or "telegram_restricted")
            self.state_label.setText(f"RESTRICTED · {code}")
            self._clear_countdown("Отправки остановлены из-за ограничения Telegram")
            if not self._restriction_active:
                self._append(
                    "Telegram ограничил аккаунт. Новые JOIN/SEND остановлены; "
                    "проверьте состояние вручную в официальном приложении Telegram."
                )
        self._restriction_active = restricted

        link_active = self._apply_link_task(snapshot.get("link_task"), restricted=restricted)
        state = snapshot.get("state")
        state = state if isinstance(state, dict) else None
        join_state = snapshot.get("join_state")
        join_state = join_state if isinstance(join_state, dict) else None

        if join_state:
            join_id = int(join_state.get("id") or 0)
            attempted = int(join_state.get("attempted_count") or 0)
            joined = int(join_state.get("joined_count") or 0)
            total = int(join_state.get("total_count") or 0)
            if join_id != self._join_campaign_id or attempted != self._join_attempted:
                self._append(
                    f"Кампания вступлений: обработано {attempted}/{total}, "
                    f"успешно вступили в {joined}."
                )
            self._join_campaign_id = join_id
            self._join_attempted = attempted
            if not restricted and not link_active and not state:
                raw_status = str(join_state.get("status") or "")
                label = {
                    "running": "Вступления",
                    "paused": "Вступления · пауза",
                    "completed": "Вступления · завершены",
                    "stopped": "Вступления · остановлены",
                }.get(raw_status, "Вступления")
                self.state_label.setText(f"{label} · {attempted}/{total} · успешно {joined}")

        if not state:
            if not restricted and not link_active and not join_state:
                self.state_label.setText("Ожидание кампании")
                self._clear_countdown("Следующая проверка: —")
            return

        campaign_id = int(state.get("id") or 0)
        status = str(state.get("status") or "")
        attempted = int(state.get("attempted_count") or 0)
        sent = int(state.get("sent_count") or 0)
        limit = int(state.get("daily_limit") or 40)
        planned = max(1, int(state.get("planned_count") or limit))
        if not restricted and not link_active:
            status_name = {
                "running": "Активна",
                "paused": "Пауза",
                "network_wait": "Ожидание сети",
                "completed": "Завершена",
                "stopped": "Остановлена",
            }.get(status, status or "Кампания")
            self.state_label.setText(
                f"{status_name} · {attempted}/{planned} · отправлено {sent} · темп {limit}/24ч"
            )
            if status == "paused":
                self._clear_countdown("Следующая проверка: после продолжения")
            elif status in {"completed", "stopped"}:
                self._clear_countdown("Следующая проверка: —")
            else:
                self._set_countdown(
                    state.get("next_scheduled_at"),
                    fallback="Следующая проверка: ожидается планирование",
                )

        if campaign_id != self._campaign_id:
            self._campaign_id = campaign_id
            self._campaign_status = status
            self._last_history_id = 0
            channels = snapshot.get("channels")
            rows = list(channels) if isinstance(channels, list) else []
            self._channel_names = {
                int(row["channel_id"]): row.get("title")
                or row.get("username")
                or str(row["channel_id"])
                for row in rows
                if isinstance(row, dict) and row.get("channel_id") is not None
            }
            self._append(
                f"Загружена кампания: статус {status or 'неизвестно'}, "
                f"запланировано {planned}, темп {limit}/24ч."
            )
        elif status != self._campaign_status:
            self._append(f"Статус кампании изменён: {status}.")
            self._campaign_status = status

        history = snapshot.get("history")
        for row in list(history) if isinstance(history, list) else []:
            if not isinstance(row, dict):
                continue
            row_id = int(row.get("id") or 0)
            if row_id <= self._last_history_id:
                continue
            self._append(self._history_message(row))
            self._last_history_id = max(self._last_history_id, row_id)

        missed = int((state.get("schedule_counts") or {}).get("missed", 0))
        if missed > self._last_missed_count:
            self._append(
                f"После простоя пропущено {missed - self._last_missed_count} слотов. "
                "Догоняющей рассылки не будет."
            )
        self._last_missed_count = missed
